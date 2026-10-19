"""Exercise complete flows, including recovery and entity identity constraints."""

from unittest.mock import patch

from aiohttp import ClientError
import pytest

from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType

INPUT = {"username": "test@example.invalid", "password": "password"}


async def start(hass):
    result = await hass.config_entries.flow.async_init(
        "sem", context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


async def submit(hass, result, data=None):
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], data or INPUT
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [True, False])
async def test_success_and_duplicate(hass, portal, manual):
    result = await start(hass)
    data = {**INPUT, "contract_id": 123456} if manual else INPUT
    result = await submit(hass, result, data)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.unique_id == "123456"
    assert entry.data == {**INPUT, "contract_id": 123456}
    assert entry.options == {"import_history": True}
    await hass.async_block_till_done()
    repeated = await start(hass)
    repeated = await submit(hass, repeated, data)
    assert repeated["type"] is FlowResultType.ABORT
    assert repeated["reason"] == "already_configured"


@pytest.mark.asyncio
async def test_choose_contract(hass, portal):
    portal.get_contracts.return_value = [
        {"numeroContrat": "123456", "adresseLivraison": {"ville": "Marseille"}},
        {"numeroContrat": "654321", "adresseLivraison": None},
    ]
    result = await submit(hass, await start(hass), {**INPUT, "import_history": False})
    assert result["step_id"] == "choose_contract"
    result = await submit(hass, result, {"contract": "654321"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "654321"
    assert result["result"].options["import_history"] is False


@pytest.mark.asyncio
async def test_no_contracts(hass, portal):
    portal.get_contracts.return_value = []
    result = await submit(hass, await start(hass))
    assert result["reason"] == "no_contracts_found"


@pytest.mark.asyncio
async def test_manual_contract_requires_account_access(hass, portal):
    result = await submit(hass, await start(hass), {**INPUT, "contract_id": 999999})
    assert result["errors"] == {"base": "contract_not_found"}
    result = await submit(hass, result, INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,error",
    [
        ("auth", "invalid_auth"),
        ("connect", "cannot_connect"),
        ("client", "cannot_connect"),
        ("timeout", "cannot_connect"),
        ("unexpected", "unknown"),
    ],
)
async def test_login_error_recovers(hass, portal, failure, error):
    from custom_components.sem.sem_water_client import CannotConnect, InvalidAuth

    exception = {
        "auth": InvalidAuth(),
        "connect": CannotConnect(),
        "client": ClientError(),
        "timeout": TimeoutError(),
        "unexpected": RuntimeError("test failure"),
    }[failure]
    portal.login.side_effect = exception
    result = await submit(hass, await start(hass))
    assert result["errors"] == {"base": error}
    portal.login.side_effect = None
    result = await submit(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "123456"
    assert portal.logout.await_count >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,error",
    [
        ("auth", "invalid_auth"),
        ("connect", "cannot_connect"),
        ("unexpected", "unknown"),
    ],
)
async def test_contract_error_recovers(hass, portal, failure, error):
    from custom_components.sem.sem_water_client import CannotConnect, InvalidAuth

    portal.get_contracts.side_effect = {
        "auth": InvalidAuth(),
        "connect": CannotConnect(),
        "unexpected": RuntimeError("test failure"),
    }[failure]
    result = await submit(hass, await start(hass))
    assert result["errors"] == {"base": error}
    portal.get_contracts.side_effect = None
    result = await submit(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,error",
    [
        (None, None),
        ("auth", "invalid_auth"),
        ("connect", "cannot_connect"),
        ("unexpected", "unknown"),
        ("wrong_account", "contract_not_found"),
    ],
)
async def test_reauth_preserves_contract(hass, portal, failure, error):
    created = await submit(hass, await start(hass))
    entry = created["result"]
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_init(
        "sem",
        context={
            "source": SOURCE_REAUTH,
            "entry_id": entry.entry_id,
            "unique_id": entry.unique_id,
        },
        data=entry.data,
    )
    assert result["step_id"] == "reauth_confirm"
    from custom_components.sem.sem_water_client import CannotConnect, InvalidAuth

    if failure == "wrong_account":
        portal.get_contracts.return_value = [{"numeroContrat": "654321"}]
    elif failure:
        exception = {
            "auth": InvalidAuth(),
            "connect": CannotConnect(),
            "unexpected": RuntimeError("test failure"),
        }[failure]
        portal.login.side_effect = exception
    with patch.object(hass.config_entries, "async_reload", return_value=True):
        credentials = {"username": "new@example.invalid", "password": "new"}
        result = await submit(hass, result, credentials)
        if error:
            assert result["errors"] == {"base": error}
            assert entry.data["username"] == INPUT["username"]
            portal.login.side_effect = None
            portal.get_contracts.return_value = [{"numeroContrat": "123456"}]
            result = await submit(hass, result, credentials)
        assert result["reason"] == "reauth_successful"
        assert entry.unique_id == "123456"
        assert entry.data == {**credentials, "contract_id": 123456}
        assert entry.options == {"import_history": True}
