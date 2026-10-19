"""Import delayed meter readings into a dedicated external statistic.

The entity's Recorder statistics describe when HA received an index. They must
never be mixed with this series, whose timestamps describe SEM reading dates.
"""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from functools import partial
import math
from typing import TypedDict

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    StatisticsRow,
    async_add_external_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .sem_water_client import Reading

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class IndexPoint(TypedDict):
    """One retained meter index at an exact UTC timestamp."""

    start: float
    state: float


class StatisticsLedger(TypedDict):
    """Readings and common sum origin required to resume normal imports."""

    points: list[IndexPoint]
    sum_origin: float
    verified: bool


async def async_read_points(
    hass: HomeAssistant, statistic_id: str
) -> list[StatisticsRow]:
    """Read all long-term values in liters using Recorder's read-only API."""
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        EPOCH,
        None,
        {statistic_id},
        "hour",
        {"volume": "L"},
        {"state", "sum", "last_reset"},
    )
    return result.get(statistic_id, [])


async def async_read_metadata(
    hass: HomeAssistant, statistic_id: str
) -> StatisticMetaData | None:
    """Read metadata without accessing Recorder's ORM or internal caches."""
    result = await get_instance(hass).async_add_executor_job(
        partial(get_metadata, hass, statistic_ids={statistic_id})
    )
    return result[statistic_id][1] if statistic_id in result else None


def validate_points(points: Sequence[IndexPoint]) -> None:
    """Require finite, ordered readings from a nondecreasing physical meter."""
    previous_start = previous_index = None
    for point in points:
        start, index = point["start"], point["state"]
        if not math.isfinite(start) or start % 3600:
            raise ValueError("A SEM reading is not dated at the start of an hour")
        if not math.isfinite(index) or index < 0:
            raise ValueError("A SEM meter index is negative or not finite")
        if previous_start is not None and start <= previous_start:
            raise ValueError("SEM reading dates are not strictly increasing")
        if previous_index is not None and index < previous_index:
            date = datetime.fromtimestamp(start, UTC).isoformat()
            raise ValueError(
                f"Decreasing SEM index at {date}: {previous_index:g} to {index:g} L; "
                "check the readings or a possible meter replacement"
            )
        previous_start, previous_index = start, index


class SEMStatistics:
    """Keep a durable, replayable ledger; confirm imports by reading them back.

    Persisting the pending ledger before queueing the import allows an interrupted
    import to be replayed after restart. Upserts keep the same timestamps, so a
    retry cannot duplicate consumption. Only this integration writes the target.
    """

    def __init__(self, hass: HomeAssistant, contract_id: int) -> None:
        """Open the permanent journal for this contract without changing it."""
        self.hass = hass
        self.contract_id = contract_id
        self.statistic_id = f"{DOMAIN}:water_consumption_{contract_id}"
        self.store = Store[StatisticsLedger](
            hass, 1, f"{DOMAIN}.statistics.{contract_id}"
        )
        self._ledger: StatisticsLedger | None = None

    @property
    def latest_point(self) -> IndexPoint:
        """Return the latest retained reading after a successful update."""
        if self._ledger is None:
            raise RuntimeError("No SEM readings have been imported yet")
        return self._ledger["points"][-1]

    async def async_update(self, readings: Sequence[Reading]) -> None:
        """Merge confirmed readings and verify Recorder before reporting success.

        The caller serializes updates for the contract, including manual imports.
        Existing points missing from an API response are deliberately retained.
        """
        paris = dt_util.get_time_zone("Europe/Paris")
        today = dt_util.now(paris).date()
        incoming: dict[float, float] = {}
        consumptions: dict[float, float | None] = {}
        for row in readings:
            day = datetime.fromisoformat(row["date"]).date()
            # Today's row may still be provisional. Never synthesize a zero for it.
            if day >= today:
                continue
            start = datetime.combine(day, datetime.min.time(), tzinfo=paris).timestamp()
            index = float(row["index"])
            if start in incoming and incoming[start] != index:
                raise ValueError(f"Conflicting SEM indices for {day}")
            consumption = row.get("consumption")
            if consumption is not None and not math.isfinite(consumption):
                raise ValueError("A SEM consumption value is not finite")
            incoming[start] = index
            consumptions[start] = consumption
        if not incoming:
            raise ValueError("No confirmed SEM readings before today")

        if self._ledger is None:
            self._ledger = await self.store.async_load()
        ledger = self._ledger
        if ledger is None:
            if await async_read_metadata(self.hass, self.statistic_id):
                # Do not guess a baseline when persistent storage was lost.
                raise ValueError(
                    "Existing SEM statistics without a journal; restore .storage"
                )
            ledger = {"points": [], "sum_origin": 0.0, "verified": False}

        merged = {p["start"]: p["state"] for p in ledger["points"]}
        merged.update(incoming)
        points: list[IndexPoint] = [
            {"start": start, "state": index} for start, index in sorted(merged.items())
        ]
        validate_points(points)
        # Energy computes the first change against zero. An absolute meter index
        # as the first sum would therefore create a fictitious consumption spike.
        # Keep a durable origin, rebase when backfill changes the oldest point, and
        # use the supplier's first daily consumption only when it is available.
        first = points[0]
        # A correction at the same date must retain the implicit preceding
        # index (the origin), unless SEM supplies a new daily consumption.
        # Otherwise an omitted consumption would erase water already counted.
        first_date_changed = (
            not ledger["points"] or ledger["points"][0]["start"] != first["start"]
        )
        origin = ledger["sum_origin"]
        first_consumption = consumptions.get(first["start"])
        if first_date_changed or first_consumption is not None:
            origin = first["state"] - (first_consumption or 0.0)
        if not math.isfinite(origin) or any(
            not math.isfinite(p["state"] - origin) for p in points
        ):
            raise ValueError("A SEM cumulative volume is not finite")
        expected = {p["start"]: (p["state"], p["state"] - origin) for p in points}
        metadata_before = await async_read_metadata(self.hass, self.statistic_id)
        if metadata_before and (
            metadata_before["unit_of_measurement"] != "L"
            or metadata_before["source"] != DOMAIN
        ):
            raise ValueError("The SEM statistics metadata has changed")
        recorded = await async_read_points(self.hass, self.statistic_id)
        actual = {p["start"]: (p["state"], p["sum"]) for p in recorded}
        if actual.keys() - expected.keys():
            raise ValueError(
                "SEM statistics contain points missing from the journal; check the backup"
            )
        changed = [p for p in points if actual.get(p["start"]) != expected[p["start"]]]
        pending: StatisticsLedger = {
            **ledger,
            "points": points,
            "verified": False,
            "sum_origin": origin,
        }
        await self.store.async_save(pending)
        self._ledger = pending

        metadata: StatisticMetaData = {
            "statistic_id": self.statistic_id,
            "source": DOMAIN,
            "name": f"SEM water consumption {self.contract_id} (dated readings)",
            "mean_type": StatisticMeanType.NONE,
            "has_sum": True,
            "unit_class": "volume",
            "unit_of_measurement": "L",
        }
        # Recorder also updates display metadata when the import has no points.
        # Synchronize the name without waiting for a new supplier reading or
        # replaying unchanged history. The statistic ID remains the same.
        if (
            not changed
            and metadata_before
            and metadata_before["name"] != metadata["name"]
        ):
            async_add_external_statistics(self.hass, metadata, [])
        # Rebase sums consistently without changing indices or timestamps.
        # Supplier corrections and older readings can change the common origin.
        # Updating the same dated rows preserves interval consumption without deletion.
        # Do not rewrite years of unchanged data on every six-hour refresh.
        for offset in range(0, len(changed), 1000):
            async_add_external_statistics(
                self.hass,
                metadata,
                [
                    {
                        "start": datetime.fromtimestamp(p["start"], UTC),
                        "state": p["state"],
                        "sum": p["state"] - origin,
                    }
                    for p in changed[offset : offset + 1000]
                ],
            )
        # Recorder processes its write queue only after HA has started. Waiting
        # here during config-entry setup would prevent that startup from finishing.
        # Keep the durable journal pending; setup registers a post-start refresh.
        if self.hass.state is not CoreState.running:
            return

        # Confirm only our own committed rows. Waiting for the entire Recorder
        # queue would include unrelated work and could block without a deadline.
        try:
            async with asyncio.timeout(30):
                while True:
                    recorded = await async_read_points(self.hass, self.statistic_id)
                    actual = {p["start"]: (p["state"], p["sum"]) for p in recorded}
                    if actual == expected:
                        recorded_metadata = await async_read_metadata(
                            self.hass, self.statistic_id
                        )
                        if (
                            recorded_metadata
                            and recorded_metadata["name"] == metadata["name"]
                        ):
                            break
                    await asyncio.sleep(0.2)
        except TimeoutError as err:
            raise ValueError(
                "Recorder did not confirm the SEM import; the journal will be retried"
            ) from err
        complete: StatisticsLedger = {**pending, "verified": True}
        await self.store.async_save(complete)
        self._ledger = complete


async def async_statistics_report(hass: HomeAssistant, contract_id: int) -> dict:
    """Compare Recorder with the permanent journal without changing either.

    Read storage afresh so this report also works while a config entry is retrying
    setup or a pending import is being confirmed.
    """
    statistic_id = f"{DOMAIN}:water_consumption_{contract_id}"
    ledger = await Store[StatisticsLedger](
        hass, 1, f"{DOMAIN}.statistics.{contract_id}"
    ).async_load()
    if ledger is None:
        return {"status": "not_initialized", "statistic_id": statistic_id}
    recorded = await async_read_points(hass, statistic_id)
    origin = ledger["sum_origin"]
    expected = {p["start"]: (p["state"], p["state"] - origin) for p in ledger["points"]}
    actual = {p["start"]: (p["state"], p["sum"]) for p in recorded}
    return {
        "status": "verified"
        if ledger["verified"] and actual == expected
        else "pending_or_mismatch",
        "statistic_id": statistic_id,
        "recorded_points": len(recorded),
        "sum_origin_liters": origin,
        "first_record": dt_util.utc_from_timestamp(
            ledger["points"][0]["start"]
        ).isoformat(),
        "last_record": dt_util.utc_from_timestamp(
            ledger["points"][-1]["start"]
        ).isoformat(),
    }
