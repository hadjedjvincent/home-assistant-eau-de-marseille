"""Exercise SEM against a real HA Recorder in a disposable SQLite database.

Run with Python 3.14 and a supported Home Assistant version installed. No SEM credentials
or production database are used. The JSON report and SQLite file are retained
in the directory printed at the end for independent inspection.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import importlib
import json
from pathlib import Path
import sys
import tempfile
from types import MappingProxyType
from unittest.mock import patch

from homeassistant import config_entries, loader
from homeassistant.components.recorder import get_instance
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    device_registry,
    entity_registry,
    frame,
    recorder as recorder_helper,
)
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

# Import HA first: from 2026.9 it provides the supported voluptuous alias.
import voluptuous as vol  # isort: skip

INTEGRATION_PATH = Path(__file__).resolve().parents[1] / "custom_components/sem"


async def main():
    root = Path(tempfile.mkdtemp(prefix="sem-recorder-validation-"))
    components = root / "custom_components"
    components.mkdir()
    (components / "sem").symlink_to(INTEGRATION_PATH)  # noqa: ASYNC240
    sys.path.insert(0, str(root))
    hass = HomeAssistant(str(root))
    loader.async_setup(hass)
    frame.async_setup(hass)
    recorder_helper.async_initialize_recorder(hass)
    hass.config_entries = config_entries.ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    # Older HA initializes this registry in async_load; newer releases split
    # allocation from loading. This harness supports both startup sequences.
    if setup_registry := getattr(device_registry, "async_setup", None):
        setup_registry(hass)
    await device_registry.async_load(hass)
    await entity_registry.async_load(hass)
    await hass.config.async_set_time_zone("Europe/Paris")
    report = {"home_assistant": HA_VERSION, "checks": []}
    try:
        assert await async_setup_component(
            hass, "recorder", {"recorder": {"auto_purge": False, "auto_repack": False}}
        )
        # Load SEM before HA starts, as happens after a real restart. Recorder's
        # write queue cannot run until HA has finished loading its integrations.
        integration = importlib.import_module("custom_components.sem")
        startup_day = (dt_util.now().date() - timedelta(days=2)).isoformat()

        class StartupPortal:
            async def login(self):
                pass

            async def logout(self):
                pass

            async def get_historical_data(self, contract_id, days=730):
                return [{"date": startup_day, "index": 1500000}]

        startup_entry = config_entries.ConfigEntry(
            domain="sem",
            title="SEM cold startup",
            version=1,
            minor_version=1,
            source="user",
            unique_id="567890",
            options={},
            data={
                "contract_id": 567890,
                "username": "test@example.invalid",
                "password": "test",
            },
            discovery_keys=MappingProxyType({}),
            subentries_data=None,
        )
        with (
            patch.object(integration, "SEM", return_value=StartupPortal()),
            patch.object(
                integration.aiohttp_client, "async_get_clientsession", return_value=None
            ),
        ):
            async with asyncio.timeout(10):
                await hass.config_entries.async_add(startup_entry)
        assert startup_entry.state is config_entries.ConfigEntryState.LOADED
        pending = await hass.services.async_call(
            "sem",
            "verify_statistics",
            {"contract_id": 567890},
            blocking=True,
            return_response=True,
        )
        assert pending["status"] == "pending_or_mismatch"
        async with asyncio.timeout(30):
            await hass.async_start()
            await hass.async_block_till_done()
        completed = await hass.services.async_call(
            "sem",
            "verify_statistics",
            {"contract_id": 567890},
            blocking=True,
            return_response=True,
        )
        assert completed["status"] == "verified"
        assert completed["recorded_points"] == 1
        assert await hass.config_entries.async_unload(startup_entry.entry_id)
        report["checks"].append(
            "cold startup completes before Recorder writes; deferred verification succeeds after HA starts"
        )
        stats = importlib.import_module("custom_components.sem.statistics_manager")
        recorder = get_instance(hass)
        today = dt_util.now().date()
        manager = stats.SEMStatistics(hass, 123456)
        rows = [
            {
                "date": (today - timedelta(days=days)).isoformat(),
                "index": index,
                "consumption": consumption,
                "type": "Automatic reading",
            }
            for days, index, consumption in [
                (900, 1000000, None),
                (731, 1000100, None),
                (4, 1000300, 200),
                (3, 1000400, 100),
                (2, 1000600, 200),
            ]
        ]
        await manager.async_update(rows)
        imported = await stats.async_read_points(hass, manager.statistic_id)
        assert len(imported) == 5
        assert imported[-1]["state"] == 1000600
        assert imported == sorted(imported, key=lambda item: item["start"])
        assert all(p["sum"] == p["state"] - 1000000 for p in imported)
        ledger = await manager.store.async_load()
        assert set(ledger) == {"points", "sum_origin", "verified"}
        assert ledger["sum_origin"] == 1000000
        report["checks"].append(
            "fresh journal has only runtime fields; initial Energy consumption is zero"
        )
        metadata = await stats.async_read_metadata(hass, manager.statistic_id)
        assert metadata["name"] == "SEM water consumption 123456 (dated readings)"
        assert metadata["statistic_id"] == "sem:water_consumption_123456"
        # Change only the display metadata through Recorder's public API, then
        # refresh identical readings. No new supplier point should be necessary
        # to synchronize the name, and every stored value must remain identical.
        stats.async_add_external_statistics(
            hass, {**metadata, "name": "Outdated statistic name"}, []
        )
        async with asyncio.timeout(10):
            while True:
                seeded_metadata = await stats.async_read_metadata(
                    hass, manager.statistic_id
                )
                if seeded_metadata["name"] == "Outdated statistic name":
                    break
                await asyncio.sleep(0.2)
        await manager.async_update(rows[-3:])
        assert await stats.async_read_points(hass, manager.statistic_id) == imported
        assert await stats.async_read_metadata(hass, manager.statistic_id) == metadata
        report["metadata"] = metadata
        report["checks"].append(
            "English statistic name synchronizes without new readings or changed values"
        )
        report["checks"].append("repeated import is idempotent")

        # Reload the durable ledger as a new coordinator would after restart.
        manager = stats.SEMStatistics(hass, 123456)
        corrected = [{**rows[-1], "index": 1000650, "consumption": 250}]
        await manager.async_update(corrected)
        after = await stats.async_read_points(hass, manager.statistic_id)
        assert after[-1]["state"] == 1000650 and len(after) == 5
        report["checks"].append(
            "same-date correction and ledger reload preserve older points"
        )

        try:
            await manager.async_update([{**rows[-1], "index": 1}])
        except ValueError:
            pass
        else:
            raise AssertionError("decreasing meter index was accepted")
        assert await stats.async_read_points(hass, manager.statistic_id) == after
        report["checks"].append("inconsistent readings cannot change either series")
        # Only the SEM transport is substituted; setup, entities and Recorder
        # below are the actual Home Assistant implementation.
        integration = importlib.import_module("custom_components.sem")

        class Portal:
            def __init__(self):
                self.active = 0
                self.max_active = 0

            async def login(self):
                self.active += 1
                self.max_active = max(self.max_active, self.active)

            async def logout(self):
                self.active -= 1

            async def get_historical_data(self, contract_id, days=730):
                return [
                    *rows[:-1],
                    *corrected,
                    {**rows[-1], "date": today.isoformat(), "index": 9999999},
                ]

        portal = Portal()
        entry = config_entries.ConfigEntry(
            domain="sem",
            title="SEM validation",
            version=1,
            minor_version=1,
            source="user",
            unique_id="123456",
            options={"import_history": True},
            data={
                "contract_id": 123456,
                "username": "test@example.invalid",
                "password": "test",
            },
            discovery_keys=MappingProxyType({}),
            subentries_data=None,
        )
        with (
            patch.object(integration, "SEM", return_value=portal),
            patch.object(
                integration.aiohttp_client, "async_get_clientsession", return_value=None
            ),
        ):
            await hass.config_entries.async_add(entry)
        await hass.async_block_till_done()
        assert entry.state is config_entries.ConfigEntryState.LOADED
        assert set(hass.services.async_services()["sem"]) == {
            "verify_statistics",
            "import_history",
        }
        source = entity_registry.async_get(hass).async_get_entity_id(
            "sensor", "sem", "sem_water_index_123456"
        )
        assert source is not None
        assert hass.states.get(source).attributes["state_class"] == "total"
        assert hass.states.get(source).state == "1000650.0"
        assert (
            hass.states.get(source).attributes["energy_statistic_id"]
            == manager.statistic_id
        )
        try:
            await hass.services.async_call(
                "sem",
                "import_history",
                {"contract_id": 123456, "unexpected_option": True},
                blocking=True,
            )
        except vol.Invalid:
            pass
        else:
            raise AssertionError("import_history accepted an unknown parameter")
        await asyncio.gather(
            hass.services.async_call(
                "sem",
                "import_history",
                {"days": 90, "contract_id": 123456},
                blocking=True,
            ),
            hass.services.async_call(
                "sem",
                "import_history",
                {"days": 30, "contract_id": 123456},
                blocking=True,
            ),
            entry.runtime_data.async_refresh(),
        )
        assert portal.max_active == 1 and portal.active == 0
        status = await hass.services.async_call(
            "sem",
            "verify_statistics",
            {"contract_id": 123456},
            blocking=True,
            return_response=True,
        )
        assert status["status"] == "verified"
        assert await stats.async_read_points(hass, manager.statistic_id) == after
        report["checks"].append(
            "only English actions are registered; unknown import parameters are rejected"
        )
        report["statistics_status"] = status
        report["checks"].append(
            "real entity and service setup; today's provisional row excluded; concurrent imports serialized"
        )
        # Exercise the manual action through HA, including error handling that
        # normally belongs to DataUpdateCoordinator's scheduled refresh path.
        coordinator = entry.runtime_data
        with patch.object(
            portal,
            "login",
            side_effect=importlib.import_module(
                "custom_components.sem.sem_water_client"
            ).InvalidAuth,
        ):
            try:
                await hass.services.async_call(
                    "sem", "import_history", {"contract_id": 123456}, blocking=True
                )
            except HomeAssistantError:
                pass
            else:
                raise AssertionError("invalid credentials were accepted")
        await hass.async_block_till_done()
        assert not coordinator.last_update_success
        reauth = [
            flow
            for flow in hass.config_entries.flow.async_progress()
            if flow["handler"] == "sem" and flow["context"]["source"] == "reauth"
        ]
        assert len(reauth) == 1
        hass.config_entries.flow.async_abort(reauth[0]["flow_id"])
        # The fake login failed before opening a session; restore its counter.
        portal.active = 0
        await coordinator.async_import_history(90)
        assert coordinator.last_update_success
        report["checks"].append(
            "manual authentication failure starts reauthentication and recovery restores availability"
        )

        # Unloading must cancel both the download and imports queued behind it.
        # Otherwise an old coordinator can overwrite a freshly reloaded journal.
        download_started = asyncio.Event()
        unblock_download = asyncio.Event()

        async def blocked_download(*args, **kwargs):
            download_started.set()
            await unblock_download.wait()
            raise AssertionError("download continued after entry unload")

        with patch.object(portal, "get_historical_data", side_effect=blocked_download):
            active_import = asyncio.create_task(coordinator.async_import_history(90))
            await asyncio.wait_for(download_started.wait(), timeout=5)
            queued_import = asyncio.create_task(coordinator.async_import_history(30))
            await asyncio.sleep(0)
            assert await hass.config_entries.async_unload(entry.entry_id)
            tasks = (active_import, queued_import)
            done, pending_tasks = await asyncio.wait(tasks, timeout=1)
            if pending_tasks:
                for task in pending_tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise AssertionError("manual import survived config entry unload")
            assert all(task.cancelled() for task in done)
        assert portal.active == 0
        assert await stats.async_read_points(hass, manager.statistic_id) == after
        report["checks"].append(
            "unload cancels active and queued manual imports without changing Recorder"
        )
        try:
            await hass.services.async_call(
                "sem", "import_history", {"contract_id": 123456}, blocking=True
            )
        except HomeAssistantError as err:
            assert err.translation_key == "entry_not_loaded"
        else:
            raise AssertionError("import_history accepted an unloaded entry")
        report["checks"].append("history import refuses unloaded entries")
        report["checks"].append("config entry unload succeeds")
        # Simulate interruption after the import committed, before the durable
        # completion marker. Recovery uses the real pending journal on disk.
        interrupted = stats.SEMStatistics(hass, 234567)
        real_save = interrupted.store.async_save

        async def interrupted_save(data):
            if data["verified"]:
                raise OSError("Simulated interruption before completion marker")
            await real_save(data)

        with patch.object(
            interrupted.store, "async_save", side_effect=interrupted_save
        ):
            try:
                await interrupted.async_update(corrected)
            except OSError:
                pass
            else:
                raise AssertionError("interruption was not simulated")
        recovered = stats.SEMStatistics(hass, 234567)
        await recovered.async_update(corrected)
        recovered_rows = await stats.async_read_points(hass, recovered.statistic_id)
        assert len(recovered_rows) == 1 and recovered_rows[0]["state"] == 1000650
        report["checks"].append(
            "interrupted completion recovers from durable pending ledger"
        )

        dst = stats.SEMStatistics(hass, 345678)
        dates = [
            "2025-03-29",
            "2025-03-30",
            "2025-03-31",
            "2025-10-25",
            "2025-10-26",
            "2025-10-27",
        ]
        await dst.async_update(
            [{"date": day, "index": (i + 1) * 100} for i, day in enumerate(dates)]
        )
        dst_rows = await stats.async_read_points(hass, dst.statistic_id)
        assert dst_rows[2]["start"] - dst_rows[1]["start"] == 23 * 3600
        assert dst_rows[5]["start"] - dst_rows[4]["start"] == 25 * 3600
        report["checks"].append(
            "Paris 23-hour and 25-hour days retain correct UTC timestamps"
        )
        await manager.store.async_remove()
        try:
            await stats.SEMStatistics(hass, 123456).async_update(corrected)
        except ValueError as err:
            assert "without a journal" in str(err)
        else:
            raise AssertionError(
                "existing statistics were accepted without their journal"
            )
        assert await stats.async_read_points(hass, manager.statistic_id) == after
        await manager.store.async_save(manager._ledger)
        report["checks"].append(
            "missing journal prevents changes to existing statistics"
        )
        # Recorder's Energy-facing change API uses zero before the first point.
        # Verify consumption itself, not only consistency of stored index values.
        changes = await recorder.async_add_executor_job(
            stats.statistics_during_period,
            hass,
            stats.EPOCH,
            None,
            {manager.statistic_id},
            "day",
            {"volume": "L"},
            {"change"},
        )
        assert changes[manager.statistic_id][0]["change"] == 0
        report["checks"].append(
            "first meter index is a baseline, never an absolute-index consumption"
        )
        known = stats.SEMStatistics(hass, 678901)
        initial = [
            {
                "date": (today - timedelta(days=4)).isoformat(),
                "index": 1000000,
                "consumption": 200,
            },
            {
                "date": (today - timedelta(days=3)).isoformat(),
                "index": 1000100,
                "consumption": 100,
            },
        ]
        await known.async_update(initial)
        known_rows = await stats.async_read_points(hass, known.statistic_id)
        assert [p["sum"] for p in known_rows] == [200, 300]
        known = stats.SEMStatistics(hass, 678901)
        await known.async_update(initial[-1:])
        assert await stats.async_read_points(hass, known.statistic_id) == known_rows
        # A correction to the first index without daily consumption must keep
        # the established origin; otherwise previously known water disappears.
        await known.async_update(
            [{**initial[0], "index": 1000010, "consumption": None}]
        )
        corrected_first = await stats.async_read_points(hass, known.statistic_id)
        assert [p["sum"] for p in corrected_first] == [210, 300]
        await known.async_update(initial)
        report["checks"].append(
            "first-index correction without consumption preserves the established origin"
        )
        await known.async_update(
            [
                {
                    "date": (today - timedelta(days=5)).isoformat(),
                    "index": 999800,
                    "consumption": 150,
                }
            ]
        )
        changes = await recorder.async_add_executor_job(
            stats.statistics_during_period,
            hass,
            stats.EPOCH,
            None,
            {known.statistic_id},
            "day",
            {"volume": "L"},
            {"change"},
        )
        assert [p["change"] for p in changes[known.statistic_id]] == [150, 200, 100]
        report["checks"].append(
            "first supplier consumption and later backfill preserve correct Energy deltas after reload"
        )
        report["external"] = after
        report["status"] = "passed"
    finally:
        # Unload even when an assertion fails, so the diagnostic run closes
        # platform tasks and sessions before Recorder and the event loop stop.
        for loaded_entry in hass.config_entries.async_entries("sem"):
            if loaded_entry.state is config_entries.ConfigEntryState.LOADED:
                await hass.config_entries.async_unload(loaded_entry.entry_id)
        await hass.async_stop()
        (root / "report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        print(f"Validation artifacts: {root}")


if __name__ == "__main__":
    if len(sys.argv) != 1:
        raise SystemExit("Usage: python scripts/verify_recorder.py")
    asyncio.run(main())
