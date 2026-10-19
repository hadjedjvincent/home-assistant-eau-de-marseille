"""Run configuration flows against Home Assistant rather than framework stubs."""

import importlib
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch

import pytest_asyncio

from homeassistant import config_entries, loader
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    device_registry,
    entity_registry,
    frame,
    recorder as recorder_helper,
)

ROOT = Path(__file__).resolve().parents[3]


@pytest_asyncio.fixture
async def hass(tmp_path):
    """Provide a disposable HA installation with the actual flow manager."""
    components = tmp_path / "custom_components"
    components.mkdir()
    (components / "sem").symlink_to(ROOT / "custom_components/sem")
    sys.path.insert(0, str(tmp_path))
    hass = HomeAssistant(str(tmp_path))
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
    await hass.async_start()
    try:
        yield hass
    finally:
        await hass.async_stop()
        sys.path.remove(str(tmp_path))


@pytest_asyncio.fixture
async def portal(hass):
    """Substitute transport only; keep the config flow and entry manager real."""
    flow = importlib.import_module("custom_components.sem.config_flow")
    integration = importlib.import_module("custom_components.sem")
    client = AsyncMock()
    client.get_contracts.return_value = [{"numeroContrat": "123456"}]
    with (
        patch.object(flow, "SEM", return_value=client),
        patch.object(flow.aiohttp_client, "async_get_clientsession", return_value=None),
        patch.object(integration, "async_setup_entry", return_value=True),
    ):
        yield client
