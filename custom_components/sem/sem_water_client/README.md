# sem-water-client

An asynchronous, typed Python client for the **Eau de Marseille Métropole**
customer portal. This unofficial client is independent of Home Assistant.
It supports login, contract enumeration and daily CSV meter readings.

```python
import asyncio
from aiohttp import ClientSession
from sem_water_client import SEM


async def main():
    async with ClientSession() as session:
        client = SEM("your-email", "your-password", session)
        try:
            await client.login()
            contracts = await client.get_contracts()
            readings = await client.get_historical_data(
                int(contracts[0]["numeroContrat"]), days=90
            )
            print(readings[-1] if readings else None)
        finally:
            await client.logout()


asyncio.run(main())
```

Each reading exposes `date`, `index`, `consumption`, and `type`. Indices and volumes
are returned in liters. `date` is the supplier's local date,
not the request time. Empty index cells are omitted, never converted to zero.
Malformed rows abort the batch. Daily consumption may be signed when the
supplier corrects a reading. The caller decides whether a date is provisional;
Home Assistant excludes the current Paris day.

`InvalidAuth` identifies rejected credentials or expired sessions.
`CannotConnect` identifies transport/protocol failures. `ValueError` identifies
invalid input or CSV data. Do not log credential objects or raw response bodies.

Requests have timeouts. Calls on a client must be serialized. An injected
`ClientSession` is neither closed nor modified by the client. If no session is
provided, the client owns and closes its session on logout. Always clean up in a
`finally` block, including after failed login.

The library uses the web application's undocumented endpoints; SEM does not
endorse it. Provider changes may require an integration update. Source and issues are available in the
[publication repository](https://github.com/hadjedjvincent/home-assistant-eau-de-marseille).

## Bundled client

Home Assistant loads this client from the integration directory. HACS installs it
with the rest of the integration; no separate package installation is required.
The repository keeps a Python package build for isolated protocol tests and type
checking. Client changes are delivered as part of integration releases.

Code is licensed under MIT. Provider marks and brand assets are excluded
from the Python distribution and retain their owners' rights.
