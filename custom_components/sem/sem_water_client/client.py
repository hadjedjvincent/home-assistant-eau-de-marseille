"""Implement the portal protocol without importing Home Assistant.

The public bootstrap key identifies the portal's web application; it is not a
customer credential. Passwords, session tokens and response bodies are never
included in logs or exception messages.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import csv
from datetime import datetime, timedelta
from io import StringIO
import math
from typing import Any, TypedDict
import uuid
from zoneinfo import ZoneInfo

import aiohttp

from .exceptions import CannotConnect, InvalidAuth

PARIS = ZoneInfo("Europe/Paris")


class Reading(TypedDict):
    """A supplier reading, in liters, retaining its original local date."""

    date: str
    index: float
    consumption: float | None
    type: str


class SEM:
    """Use an injected session, or own one when used outside Home Assistant.

    Call login before querying the portal and logout in a finally block. Clients
    must serialize their operations: portal sessions have one conversation ID.
    An injected ClientSession is never closed or modified by this client.
    """

    BASE_URL = "https://espaceclients.eaudemarseille-metropole.fr"
    _ACCESS_KEY = "XX_ma3pD-2017-SEMM-PRD!"
    HEADERS = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "fr,fr-FR;q=0.8,en-US;q=0.5,en;q=0.3",
        "Content-Type": "application/json;charset=utf-8",
    }

    def __init__(
        self, email: str, password: str, session: aiohttp.ClientSession | None = None
    ) -> None:
        self.email = email
        self.password = password
        self.session = session
        self.token: str | None = None
        self.auth_token: str | None = None
        self.conversation_id = f"JS-WEB-Netscape-{uuid.uuid4()}"
        self._owns_session = False

    @asynccontextmanager
    async def _request(
        self, method: str, path: str, *, request_timeout: int = 45, **kwargs: Any
    ) -> AsyncIterator[aiohttp.ClientResponse]:
        if self.session is None:
            self.session = aiohttp.ClientSession()
            self._owns_session = True
        try:
            async with self.session.request(
                method,
                f"{self.BASE_URL}{path}",
                timeout=aiohttp.ClientTimeout(total=request_timeout),
                **kwargs,
            ) as response:
                if response.status in (401, 403):
                    raise InvalidAuth("The portal rejected authentication")
                if response.status != 200:
                    raise CannotConnect(f"The portal returned HTTP {response.status}")
                yield response
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnect("Unable to communicate with the SEM portal") from err

    @staticmethod
    async def _json(response: aiohttp.ClientResponse) -> dict[str, Any]:
        try:
            data = await response.json()
        except (ValueError, aiohttp.ContentTypeError) as err:
            raise CannotConnect("The portal returned invalid JSON") from err
        if not isinstance(data, dict):
            raise CannotConnect("The portal returned an unexpected JSON structure")
        return data

    def _headers(self) -> dict[str, str]:
        return self.HEADERS | {
            "Token": self.auth_token or self.token or self._ACCESS_KEY,
            "ConversationId": self.conversation_id,
            "Origin": self.BASE_URL,
            "Referer": f"{self.BASE_URL}/",
        }

    async def login(self) -> None:
        """Authenticate without leaking a previous session's credentials."""
        self.token = self.auth_token = None
        async with self._request("GET", "/"):
            pass
        async with self._request(
            "POST",
            "/webapi/Acces/generateToken",
            headers=self._headers(),
            json={
                "ConversationId": self.conversation_id,
                "ClientId": "SOMEI-SEMM-PRD",
                "AccessKey": self._ACCESS_KEY,
            },
        ) as response:
            token = (await self._json(response)).get("token")
            if not isinstance(token, str) or not token:
                raise CannotConnect("The portal did not provide a session token")
            self.token = token
        async with self._request(
            "POST",
            "/webapi/Utilisateur/authentification",
            headers=self._headers(),
            json={"identifiant": self.email, "motDePasse": self.password},
        ) as response:
            token = (await self._json(response)).get("tokenAuthentique")
            if not isinstance(token, str) or not token:
                raise InvalidAuth("The portal rejected authentication")
            self.auth_token = token

    async def logout(self) -> None:
        """Release the portal session; cleanup failures must not mask the result."""
        try:
            if self.session is not None and self.auth_token:
                try:
                    async with self._request(
                        "GET",
                        "/webapi/Acces/releaseMe",
                        headers=self._headers(),
                        request_timeout=15,
                    ):
                        pass
                except (CannotConnect, InvalidAuth):
                    pass
        finally:
            self.auth_token = self.token = None
            await self.close()

    async def close(self) -> None:
        """Close only a session created by this client."""
        if self._owns_session and self.session is not None:
            await self.session.close()
            self.session = None
            self._owns_session = False

    async def test_credentials(self) -> None:
        """Verify credentials and always release the session."""
        try:
            await self.login()
        finally:
            await self.logout()

    async def get_contracts(self) -> list[dict[str, Any]]:
        """Return contracts belonging to the authenticated customer."""
        async with self._request(
            "GET",
            "/webapi/Abonnement/contrats",
            headers=self._headers(),
            params={
                "userWebId": "",
                "recherche": "",
                "tri": "NumeroContrat",
                "triDecroissant": "false",
                "indexPage": "0",
                "nbElements": "500",
            },
        ) as response:
            contracts = (await self._json(response)).get("resultats")
        if not isinstance(contracts, list):
            raise CannotConnect("The portal returned an invalid contract list")
        for contract in contracts:
            try:
                valid = (
                    isinstance(contract, dict) and int(contract["numeroContrat"]) > 0
                )
            except (KeyError, TypeError, ValueError):
                valid = False
            if not valid:
                raise CannotConnect("The portal returned an invalid contract")
        return contracts

    @staticmethod
    def parse_csv_from_text(csv_text: str) -> list[dict[str, str]]:
        """Reject malformed exports instead of importing an incomplete batch."""
        if not csv_text.strip():
            return []
        reader = csv.DictReader(StringIO(csv_text), delimiter=";")
        if reader.fieldnames:
            reader.fieldnames = [
                field.strip().lstrip("\ufeff").strip() for field in reader.fieldnames
            ]
        rows = []
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("Unexpected column count in the SEM export")
            clean = {key.strip(): str(value).strip() for key, value in row.items()}
            if any(clean.values()):
                rows.append(clean)
        return rows

    async def get_historical_data(
        self, contract_id: int, days: int = 730
    ) -> list[Reading]:
        """Fetch date-stamped indices; an absent index is unknown, never zero."""
        if not 1 <= days <= 730 or contract_id <= 0:
            raise ValueError("Invalid contract or date range")
        end = datetime.now(PARIS).replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=days)
        async with self._request(
            "POST",
            f"/webapi/Export/Csv/{contract_id}/null/null",
            headers=self._headers(),
            request_timeout=60,
            json={
                "typeExport": "Conso",
                "nomFichierExport": "Export_Tableau_Des_Consommations",
                "titreExport": f"Export des consommations - {end.isoformat()}",
                "dateDebut": start.strftime("%Y-%m-%d"),
                "dateFin": end.strftime("%Y-%m-%d"),
                "granulariteConsommation": "JOURNEE",
            },
        ) as response:
            rows = self.parse_csv_from_text(await response.text())
        if not rows:
            return []
        required = {"Date de relève", "Index (m3)", "Consommation (m3)", "Type"}
        if not required.issubset(rows[0]):
            raise ValueError("Missing columns in the SEM export")

        def liters(value: str, *, index: bool = False) -> float:
            number = float("".join(value.split()).replace(",", ".")) * 1000
            if not math.isfinite(number) or (index and number < 0):
                raise ValueError("Invalid SEM volume")
            return round(number, 6)

        data: list[Reading] = []
        for row in rows:
            try:
                day = datetime.strptime(row["Date de relève"], "%d/%m/%Y").date()
                if not row["Index (m3)"]:
                    continue
                data.append(
                    {
                        "date": day.isoformat(),
                        "index": liters(row["Index (m3)"], index=True),
                        "consumption": liters(row["Consommation (m3)"])
                        if row["Consommation (m3)"]
                        else None,
                        "type": row["Type"],
                    }
                )
            except (KeyError, ValueError) as err:
                raise ValueError("Invalid reading in the SEM export") from err
        return sorted(data, key=lambda item: item["date"])
