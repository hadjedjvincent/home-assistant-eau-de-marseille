# Verifying the SEM integration

The integration supports Home Assistant **2026.3.0** and later, using Python **3.14**.
CI verifies **2026.3.0**, **2026.9.0**, and **2026.10.2** in separate environments.
These cover the minimum version, the transition to Probatio, and the current release.
The default development requirements pin 2026.10.2; to test a different supported
version, create a clean environment and install `requirements-tools.txt` together
with `homeassistant==VERSION`. Do not install the default development requirements
in that environment afterward, as they would replace the version being tested.
Scenarios use the actual Home Assistant flow manager, a local HTTP server for the
client protocol, and a real SQLite Recorder for statistics.

```sh
python -m pip install -r requirements-dev.txt
python -m coverage run -m pytest -q --junitxml=validation/pytest-report.xml
python -m coverage report --include='*/config_flow.py' --fail-under=100
python -m coverage json --include='*/config_flow.py' -o validation/config-flow-coverage.json
```

The local HTTP server needs permission to bind to a loopback port. Network error
and recovery scenarios use synthetic responses without SEM credentials.
JUnit and coverage reports provide repeatable evidence. CI retains them alongside
the temporary Recorder database and Python distributions.

## Recorder scenario

Run from the repository in a development environment separate from your installation:

```sh
python3.14 -m venv /tmp/sem-validation
/tmp/sem-validation/bin/python -m pip install -r requirements-dev.txt
/tmp/sem-validation/bin/python scripts/verify_recorder.py
/tmp/sem-validation/bin/python -m pytest -q
/tmp/sem-validation/bin/ruff check .
```

The scenario starts Home Assistant with Recorder, imports synthetic history covering
more than two years, and exercises imports through the actual APIs. It checks:

- Retention of readings outside the SEM request window.
- Rejection of new decreasing indices and inconsistent readings before writing.
- Idempotent imports and corrections for an existing date.
- Display name synchronization without new readings, changed values, or a new statistic ID.
- Journal reload and recovery from an interrupted import.
- Fresh entity setup and config entry unloading.
- Integration loading before Home Assistant starts, followed by confirmation after startup.
- Registration of exactly `verify_statistics` and `import_history`, with unknown import parameters rejected.
- Serialization of manual imports and scheduled refreshes.
- Cancellation of active and queued imports when the config entry unloads.
- Immediate reauthentication after a manual import encounters invalid credentials.
- Preservation of the sum origin when the first index is corrected without a daily consumption.
- Exclusion of provisional readings for the current day.
- UTC timestamps for 23-hour and 25-hour days in Paris.
- Correct Energy consumption for the first point, a known first-day volume, and older backfilled readings.

Only the SEM transport is substituted for setup and import scenarios. They use
no provider credentials, portal requests, or production database.
The script prints the temporary directory containing:

- `report.json`, with the status, checks, and before/after statistics.
- `home-assistant_v2.db`, a SQLite database that can be inspected independently.
- `.storage/sem.statistics.*`, permanent journals for fictional contracts.

The process must exit successfully and the report must contain `status: passed`.
Reports from each CI run are available in its GitHub Actions artifacts.
Home Assistant's custom-integration warning is expected and is not a certification.

The scenario uses synthetic data only and does not accept a private journal or
production database as input. Its artifacts can be reproduced without customer data.

## Compatibility decisions

The minimum supported release includes Python 3.14, the Recorder metadata used by
SEM (`mean_type` and `unit_class`), and local brand images. The schema code uses
Home Assistant's supported `voluptuous` API: older releases provide Voluptuous,
while newer releases alias it to Probatio. No extra validation dependency or
version-specific runtime branch is needed. Reauthentication uses the stable
`ConfigEntry.async_start_reauth` API because SEM always provides that flow.

The standalone harness adapts only its registry initialization to the HA version;
production Home Assistant performs that initialization itself. Python 3.12/3.13
client checks validate the independent HTTP client, not the Home Assistant runtime.
