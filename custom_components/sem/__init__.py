"""Set up the SEM integration and its non-destructive history import action."""

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import CoreState, HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import aiohttp_client, config_validation as cv
from homeassistant.helpers.start import async_at_started

from .const import CONF_CONTRACT_ID, DOMAIN
from .coordinator import SEMConfigEntry, SEMDataUpdateCoordinator
from .sem_water_client import SEM
from .statistics_manager import async_statistics_report

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS = [Platform.SENSOR]

SERVICE_IMPORT_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_CONTRACT_ID): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Optional("days", default=730): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=730)
        ),
    }
)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register import and read-only statistics checks for configured contracts."""

    async def report(call: ServiceCall) -> dict:
        return await async_statistics_report(hass, call.data[CONF_CONTRACT_ID])

    hass.services.async_register(
        DOMAIN,
        "verify_statistics",
        report,
        schema=vol.Schema({vol.Required(CONF_CONTRACT_ID): cv.positive_int}),
        supports_response=SupportsResponse.ONLY,
    )

    async def import_history(call: ServiceCall) -> None:
        entries = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if call.data.get(CONF_CONTRACT_ID) in (None, entry.data[CONF_CONTRACT_ID])
        ]
        if not entries:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_matching_contract"
            )
        for entry in entries:
            coordinator = getattr(entry, "runtime_data", None)
            if entry.state is not ConfigEntryState.LOADED or coordinator is None:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="entry_not_loaded"
                )
            # Use the same lock/client as scheduled refreshes. Exceptions propagate
            # to the action caller instead of falsely reporting a successful import.
            await coordinator.async_import_history(call.data["days"])

    hass.services.async_register(
        DOMAIN, "import_history", import_history, schema=SERVICE_IMPORT_SCHEMA
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SEMConfigEntry) -> bool:
    """Create one API client and one coordinator per contract."""
    sem = SEM(
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        aiohttp_client.async_get_clientsession(hass),
    )
    coordinator = SEMDataUpdateCoordinator(hass, entry, sem)
    # Capture startup state before awaiting I/O: HA may finish starting while
    # this entry loads. async_at_started also handles that race immediately.
    refresh_after_start = hass.state is not CoreState.running
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if refresh_after_start:

        async def confirm_after_start(_hass: HomeAssistant) -> None:
            # The coordinator serializes this refresh with manual imports and
            # handles API failures normally. The persisted journal makes retries
            # safe even if the initial queued import has already committed.
            await coordinator.async_refresh()

        entry.async_on_unload(async_at_started(hass, confirm_after_start))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SEMConfigEntry) -> bool:
    """Remove entities and their coordinator listeners."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
