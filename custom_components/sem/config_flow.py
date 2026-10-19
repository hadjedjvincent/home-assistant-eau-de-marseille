"""Configure one SEM water contract through the Home Assistant UI."""

import logging
from typing import Any

from aiohttp import ClientError
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client, config_validation as cv, selector

from .const import CONF_CONTRACT_ID, CONF_IMPORT_HISTORY, DOMAIN
from .sem_water_client import SEM, CannotConnect, InvalidAuth

_LOGGER = logging.getLogger(__name__)


def credentials_schema(username: str | None = None) -> vol.Schema:
    """Use a password selector so the UI does not expose typed credentials."""
    username_key = (
        vol.Required(CONF_USERNAME, default=username)
        if username
        else vol.Required(CONF_USERNAME)
    )
    return vol.Schema(
        {
            username_key: selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.EMAIL)
            ),
            vol.Required(CONF_PASSWORD): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
        }
    )


STEP_USER_DATA_SCHEMA = credentials_schema().extend(
    {
        vol.Optional(CONF_CONTRACT_ID): cv.positive_int,
        vol.Optional(CONF_IMPORT_HISTORY, default=True): cv.boolean,
    }
)


async def validate_input(
    hass: HomeAssistant, data: dict[str, Any]
) -> list[dict[str, Any]]:
    """Verify both authentication and access to the account's contracts."""
    client = SEM(
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
        aiohttp_client.async_get_clientsession(hass),
    )
    try:
        await client.login()
        return await client.get_contracts()
    finally:
        await client.logout()


class SEMConfigFlow(ConfigFlow, domain=DOMAIN):
    """Keep contract identity stable when credentials are renewed."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the in-progress account and contract selection."""
        self.data: dict[str, Any] = {}
        self._contracts: list[dict[str, Any]] = []

    async def _async_create_contract_entry(
        self, contract_id: int, import_history: bool
    ) -> ConfigFlowResult:
        """Deduplicate by contract, not email, before creating an entry."""
        await self.async_set_unique_id(str(contract_id))
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"SEM {contract_id}",
            data={
                CONF_USERNAME: self.data[CONF_USERNAME],
                CONF_PASSWORD: self.data[CONF_PASSWORD],
                CONF_CONTRACT_ID: contract_id,
            },
            options={CONF_IMPORT_HISTORY: import_history},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Authenticate once, then select a contract belonging to that account."""
        errors = {}
        if user_input is not None:
            self.data = user_input
            try:
                self._contracts = await validate_input(self.hass, user_input)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect, ClientError, TimeoutError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception while configuring SEM")
                errors["base"] = "unknown"
            else:
                if contract_id := user_input.get(CONF_CONTRACT_ID):
                    if any(
                        int(c["numeroContrat"]) == contract_id for c in self._contracts
                    ):
                        return await self._async_create_contract_entry(
                            contract_id, user_input.get(CONF_IMPORT_HISTORY, True)
                        )
                    errors["base"] = "contract_not_found"
                else:
                    return await self.async_step_choose_contract()
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_choose_contract(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select a contract; the flow manager validates choices against the form."""
        if user_input is not None:
            return await self._async_create_contract_entry(
                int(user_input["contract"]), self.data.get(CONF_IMPORT_HISTORY, True)
            )
        if not self._contracts:
            return self.async_abort(reason="no_contracts_found")
        if len(self._contracts) == 1:
            return await self._async_create_contract_entry(
                int(self._contracts[0]["numeroContrat"]),
                self.data.get(CONF_IMPORT_HISTORY, True),
            )
        options: dict[str, str] = {}
        for contract in self._contracts:
            number = str(contract["numeroContrat"])
            address = contract.get("adresseLivraison")
            city = address.get("ville") if isinstance(address, dict) else None
            options[number] = f"{number} - {city}" if city else number
        return self.async_show_form(
            step_id="choose_contract",
            data_schema=vol.Schema({vol.Required("contract"): vol.In(options)}),
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start credential renewal for an existing contract."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reject a different account unless it can access this exact contract."""
        entry = self._get_reauth_entry()
        errors = {}
        if user_input is not None:
            try:
                contracts = await validate_input(self.hass, user_input)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect, ClientError, TimeoutError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception while renewing SEM credentials")
                errors["base"] = "unknown"
            else:
                if any(
                    int(c["numeroContrat"]) == entry.data[CONF_CONTRACT_ID]
                    for c in contracts
                ):
                    return self.async_update_reload_and_abort(
                        entry, data_updates=user_input
                    )
                errors["base"] = "contract_not_found"
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=credentials_schema(entry.data[CONF_USERNAME]),
            errors=errors,
        )
