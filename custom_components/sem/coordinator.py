"""Serialize SEM downloads and imports, including manual refreshes."""

import asyncio
from datetime import timedelta
import logging
from typing import TypedDict

from aiohttp import ClientError

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ServiceValidationError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import CONF_CONTRACT_ID, CONF_IMPORT_HISTORY, DOMAIN
from .sem_water_client import SEM, CannotConnect, InvalidAuth
from .statistics_manager import SEMStatistics

_LOGGER = logging.getLogger(__name__)


class MeterData(TypedDict):
    """Latest confirmed meter index and its supplier date."""

    date: str
    index: float


class SEMDataUpdateCoordinator(DataUpdateCoordinator[MeterData]):
    """Fetch confirmed readings every six hours without shifting their dates."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, sem: SEM) -> None:
        """Create one client and serialized update path for the contract."""
        super().__init__(
            hass,
            _LOGGER,
            name="SEM Water Consumption",
            config_entry=entry,
            update_interval=timedelta(hours=6),
        )
        self.sem = sem
        self.entry = entry
        self.contract_id = entry.data[CONF_CONTRACT_ID]
        self.statistics = SEMStatistics(hass, self.contract_id)
        self._import_lock = asyncio.Lock()
        self._initial_import_done = False

    async def _async_fetch(self, days: int) -> MeterData:
        async with self._import_lock:
            try:
                await self.sem.login()
                readings = await self.sem.get_historical_data(
                    self.contract_id, days=days
                )
                today = (
                    dt_util.now(dt_util.get_time_zone("Europe/Paris"))
                    .date()
                    .isoformat()
                )
                readings = [row for row in readings if row["date"] < today]
                await self.statistics.async_update(readings)
                self._initial_import_done = True
                # A shorter manual import or an incomplete API response must not
                # move the displayed index backwards: use the retained ledger.
                latest = self.statistics.latest_point
                date = (
                    dt_util.utc_from_timestamp(latest["start"])
                    .astimezone(dt_util.get_time_zone("Europe/Paris"))
                    .date()
                    .isoformat()
                )
                return {"date": date, "index": latest["state"]}
            except InvalidAuth as err:
                raise ConfigEntryAuthFailed(
                    translation_domain=DOMAIN, translation_key="invalid_auth"
                ) from err
            except (CannotConnect, ClientError, TimeoutError, ValueError) as err:
                raise UpdateFailed(str(err)) from err
            finally:
                await self.sem.logout()

    async def _async_update_data(self) -> MeterData:
        days = 90
        if not self._initial_import_done and self.entry.options.get(
            CONF_IMPORT_HISTORY, True
        ):
            days = 730
        return await self._async_fetch(days)

    async def async_import_history(self, days: int) -> None:
        """Run a manual import within the lifetime of this config entry."""
        if self.entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="entry_not_loaded"
            )
        # The service caller belongs to HA, not this entry. Register the actual
        # import with the entry so unload cancels active and queued downloads
        # before a new coordinator can open the same persistent journal.
        await self.entry.async_create_background_task(
            self.hass,
            self._async_import_and_publish(days),
            f"SEM history import {self.contract_id}",
        )

    async def _async_import_and_publish(self, days: int) -> None:
        """Give manual requests the same error handling as scheduled refreshes."""
        try:
            data = await self._async_fetch(days)
        except ConfigEntryAuthFailed as err:
            self.async_set_update_error(err)
            # This integration always provides a reauthentication flow. Use
            # the stable entry API available throughout the supported range.
            self.entry.async_start_reauth(self.hass)
            raise
        except UpdateFailed as err:
            self.async_set_update_error(err)
            raise
        self.async_set_updated_data(data)


type SEMConfigEntry = ConfigEntry[SEMDataUpdateCoordinator]
