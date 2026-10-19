"""Verify the standalone client over real HTTP in a local portal simulator."""

from contextlib import asynccontextmanager
from pathlib import Path
import sys

from aiohttp import ClientSession, web
import pytest
import pytest_asyncio

# Exercise the same source package that is built into the standalone wheel.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "custom_components/sem"))

from sem_water_client import SEM, CannotConnect, InvalidAuth  # noqa: E402

CSV = "Date de relève;Index (m3);Consommation (m3);Type\n12/10/2026;1 511,644;0,302;Télérelève\n"


@pytest_asyncio.fixture
async def portal():
    state = {"fail": None, "status": 500, "csv": CSV, "released": 0, "payload": None}

    async def handler(request):
        path = request.path
        if state["fail"] == path:
            return web.Response(status=state["status"], text="private server detail")
        if path == "/":
            return web.Response(text="portal")
        if path.endswith("generateToken"):
            return web.json_response({"token": "bootstrap"})
        if path.endswith("authentification"):
            assert await request.json() == {
                "identifiant": "user",
                "motDePasse": "secret",
            }
            return web.json_response({"tokenAuthentique": "authenticated"})
        if path.endswith("contrats"):
            return web.json_response({"resultats": [{"numeroContrat": "123456"}]})
        if "/Export/Csv/" in path:
            state["payload"] = await request.json()
            return web.Response(text=state["csv"])
        if path.endswith("releaseMe"):
            state["released"] += 1
            return web.Response(text="released")
        raise AssertionError(path)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", state
    finally:
        await runner.cleanup()


@asynccontextmanager
async def client_at(url):
    async with ClientSession() as session:
        client = SEM("user", "secret", session)
        client.BASE_URL = url
        try:
            yield client, session
        finally:
            await client.logout()
            assert not session.closed


@pytest.mark.asyncio
async def test_full_protocol_and_shared_session(portal):
    url, state = portal
    async with client_at(url) as (client, session):
        await client.login()
        assert await client.get_contracts() == [{"numeroContrat": "123456"}]
        assert await client.get_historical_data(123456) == [
            {
                "date": "2026-10-12",
                "index": 1511644.0,
                "consumption": 302.0,
                "type": "Télérelève",
            }
        ]
        assert state["payload"]["granulariteConsommation"] == "JOURNEE"
    assert state["released"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,status,error",
    [
        ("/", 500, CannotConnect),
        ("/webapi/Acces/generateToken", 500, CannotConnect),
        ("/webapi/Utilisateur/authentification", 401, InvalidAuth),
        ("/webapi/Utilisateur/authentification", 403, InvalidAuth),
        ("/webapi/Utilisateur/authentification", 500, CannotConnect),
    ],
)
async def test_login_failure_is_classified(portal, path, status, error):
    url, state = portal
    state.update(fail=path, status=status)
    async with client_at(url) as (client, _):
        with pytest.raises(error) as raised:
            await client.login()
        assert "secret" not in str(raised.value)
        assert "private server detail" not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,error", [(401, InvalidAuth), (403, InvalidAuth), (500, CannotConnect)]
)
@pytest.mark.parametrize(
    "path,method",
    [
        ("/webapi/Abonnement/contrats", "contracts"),
        ("/webapi/Export/Csv/123456/null/null", "export"),
    ],
)
async def test_authenticated_failure(portal, status, error, path, method):
    url, state = portal
    async with client_at(url) as (client, _):
        await client.login()
        state.update(fail=path, status=status)
        with pytest.raises(error):
            if method == "contracts":
                await client.get_contracts()
            else:
                await client.get_historical_data(123456)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "csv,expected_error",
    [
        ("", False),
        ("wrong;header\nvalue;value\n", True),
        (
            "Date de relève;Index (m3);Consommation (m3);Type\n12/10/2026;;0;Télérelève\n",
            False,
        ),
        (CSV.replace("1 511,644", "nan"), True),
        (CSV.replace("1 511,644", "-1"), True),
        (CSV.replace("12/10/2026", "invalid"), True),
        (CSV + "bad;extra;row;with;column\n", True),
    ],
)
async def test_csv_validation_over_http(portal, csv, expected_error):
    url, state = portal
    state["csv"] = csv
    async with client_at(url) as (client, _):
        await client.login()
        if expected_error:
            with pytest.raises(ValueError):
                await client.get_historical_data(123456)
        else:
            assert await client.get_historical_data(123456) == []


@pytest.mark.asyncio
async def test_signed_consumption_and_missing_consumption(portal):
    url, state = portal
    state["csv"] = CSV.replace("0,302", "-0,085")
    async with client_at(url) as (client, _):
        await client.login()
        assert (await client.get_historical_data(123456))[0]["consumption"] == -85
        state["csv"] = CSV.replace("0,302", "")
        assert (await client.get_historical_data(123456))[0]["consumption"] is None


@pytest.mark.asyncio
async def test_release_failure_still_clears_credentials(portal):
    url, state = portal
    async with client_at(url) as (client, session):
        await client.login()
        state.update(fail="/webapi/Acces/releaseMe", status=500)
        await client.logout()
        assert client.auth_token is None and client.token is None
        assert not session.closed


@pytest.mark.asyncio
async def test_owned_session_is_closed_after_credential_check(portal):
    """Exercise the library outside Home Assistant without an injected session."""
    url, state = portal
    client = SEM("user", "secret")
    client.BASE_URL = url
    await client.test_credentials()
    assert client.session is None
    assert client.auth_token is None and client.token is None
    assert state["released"] == 1
