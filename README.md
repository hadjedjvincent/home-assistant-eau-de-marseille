# Eau de Marseille Métropole for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5?logo=homeassistant&logoColor=white)](https://my.home-assistant.io/redirect/hacs_repository/?owner=hadjedjvincent&repository=home-assistant-eau-de-marseille&category=integration)
[![Validation](https://github.com/hadjedjvincent/home-assistant-eau-de-marseille/actions/workflows/validate.yml/badge.svg?branch=main)](https://github.com/hadjedjvincent/home-assistant-eau-de-marseille/actions/workflows/validate.yml)
[![Latest release](https://img.shields.io/github/v/release/hadjedjvincent/home-assistant-eau-de-marseille)](https://github.com/hadjedjvincent/home-assistant-eau-de-marseille/releases/latest)
[![Home Assistant 2026.3.0 or later](https://img.shields.io/badge/Home_Assistant-%E2%89%A52026.3.0-18BCF2?logo=homeassistant&logoColor=white)](https://www.home-assistant.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

This custom integration retrieves water meter readings from the Eau de Marseille
Métropole customer portal. The provider usually publishes readings 24 to 48 hours
late. Consumption is assigned to the provider's reading dates in the Energy dashboard.

Version **1.0.0** requires Home Assistant **2026.3.0** or later. This is an
independent custom integration, available as a custom repository in HACS.

The validation workflow runs HACS validation, Hassfest, and automated integration
and Recorder scenarios. These checks are not an official Home Assistant certification
or an endorsement by the water provider.

## Requirements

You need an account on the [Eau de Marseille Métropole customer portal](https://espaceclients.eaudemarseille-metropole.fr)
and a contract with daily readings available in that portal. Other providers,
including other customer portals operated by the Eaux de Marseille group, are not supported.

## Installation

### HACS

1. Add [this repository](https://github.com/hadjedjvincent/home-assistant-eau-de-marseille)
   as a custom repository in HACS, using the **Integration** category.
2. Install **Eau de Marseille Métropole**.
3. Restart Home Assistant.

The integration uses the standard `custom_components/sem/` layout.
Brand images are included with the integration.

### Manual installation

Copy `custom_components/sem/` into `/config/custom_components/sem/`, then restart
Home Assistant. The directory includes the client, translations, and brand images.

### Configuration

1. Go to **Settings** > **Devices & services** and add **Eau de Marseille Métropole**.
2. Enter your customer portal email address and password.
3. Enter a contract number, or leave it empty to choose a contract from your account.
4. Enable the initial history import if you want up to 730 days of readings.
   Otherwise, the first request covers 90 days.

The selected contract must belong to the authenticated account. If your password
changes, use the reauthentication flow offered by Home Assistant.

## Energy dashboard

In the water configuration, select the external source named
`SEM water consumption <contract> (dated readings)`. Its statistic ID is
`sem:water_consumption_<contract>`.

The meter index sensor shows the latest reading in liters. Its `last_known_date`
attribute identifies the provider's reading date. Use the dated external source
for Energy: the index sensor's automatic statistics follow receipt time.

## Data updates

- Readings are fetched every six hours, covering the previous 90 days.
- Readings are dated at midnight in the `Europe/Paris` time zone. Today's date and
  future dates are excluded because they can contain provisional readings.
- Missing readings are never replaced by zero.
- A provider correction updates the point for the same date. An incomplete export
  does not remove readings already stored.
- Manual imports and scheduled refreshes are serialized.
- The permanent journal retains readings after they leave the requested date range.
  Interrupted imports can be replayed and checked against Recorder.

When days are missing between readings, their combined consumption appears on the
next available reading. The integration does not estimate daily or hourly use.
New decreasing indices are rejected because a meter replacement cannot be inferred
safely from the export. Check the portal and Home Assistant logs.

## Actions

Action identifiers are in English. Their displayed names and descriptions follow
your Home Assistant language setting.

### Verify statistics

In **Developer tools** > **Actions**, run:

```yaml
action: sem.verify_statistics
data:
  contract_id: 123456
```

Replace the example number with your configured contract. This action is read-only.
The response includes:

- `status: verified` when Recorder matches the validated permanent journal.
- `pending_or_mismatch` while an import is pending or a discrepancy needs review.
- `not_initialized` when the contract has no journal.
- `recorded_points`, `first_record`, and `last_record` to describe the stored period.
- `sum_origin_liters` to identify the common origin used to calculate consumption.

A `verified` response confirms technical consistency with Recorder. It does not
certify the provider's measurements.

### Import readings

```yaml
action: sem.import_history
data:
  contract_id: 123456
  days: 730
```

`days` accepts values from 1 to 730. Omit `contract_id` to refresh every configured
contract; all matching entries must be loaded. The action retains existing history.

## Permanent journal and backups

Keep `.storage/sem.statistics.<contract>`. The integration needs this permanent
journal to preserve readings and verify imports. Back up the full configuration
and Recorder database together.

If external statistics exist but their journal is missing, imports stop. Restore
a consistent backup of Recorder and `.storage` before retrying.

Without a custom `db_url` in the `recorder:` configuration, Home Assistant uses
SQLite at `/config/home-assistant_v2.db`. The integration uses Recorder APIs rather
than database-specific SQL writes. Automated Recorder scenarios use SQLite.
Home Assistant manages normal state and short-term statistics retention.

## First reading and sum origin

Recorded sums use a common origin: `sum = index - sum_origin`. This prevents the
first meter index from being counted as consumption from zero.

If the supplier provides consumption for the first day, that value is included.
Otherwise, the first index is a baseline and earlier consumption remains unknown.
Backfilling older readings adjusts the origin consistently. Meter indices and
reading dates remain unchanged.

The origin is stored in the permanent journal and reported by `sem.verify_statistics`.
It remains stable when a shorter refresh no longer includes the first reading,
or when that index is corrected without an updated daily consumption.

## Startup and troubleshooting

Recorder begins processing queued writes after Home Assistant starts. During setup,
SEM saves its journal and queues imports without waiting for those writes. It
refreshes after startup to confirm the recorded values.

The report can briefly return `pending_or_mismatch` during this process. SEM checks
its own writes for up to 30 seconds. If confirmation fails, the journal remains
available for a later retry.

For connection problems, check that the customer portal is available and your
credentials work there. Allow for the provider's publication delay before treating
a missing recent reading as an error.

## Privacy and limitations

Credentials are stored in the Home Assistant configuration entry. Requests go to
the provider's portal; the integration does not send them to another service.
The portal API is undocumented and may change. This integration does not measure
real-time use, provide billing, or add custom automation triggers or conditions.

Automated network scenarios use a local HTTP server and synthetic responses.
They do not contact SEM or replace validation with actual provider readings.

## Updates and removal

Before updating, create a full Home Assistant backup. Update through HACS or replace
the files in `/config/custom_components/sem/`, then restart Home Assistant. Keep the
permanent journal and the configured Energy source.

To remove the integration, remove its water source from Energy, then delete the
SEM entry under **Settings** > **Devices & services**. Uninstall it through HACS
or remove `/config/custom_components/sem/`, then restart Home Assistant.

Removing the entry stops updates and removes its entities. The integration does
not explicitly erase external statistics or the permanent journal. To remove
stored statistics intentionally, use Home Assistant's statistics management tools
after making a backup. Retention also depends on your Recorder configuration.

## Development

See the [reproducible verification instructions](tests/README.md) for local scenarios
and their retained test artifacts.

## License

The code is released under the [MIT License](LICENSE). Provider names, logos, and
other brand artwork remain the property of their respective owners and are not
covered by this license.
