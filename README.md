# NetzNÖ Smartmeter Integration for Home Assistant

![HACS](https://github.com/TheJoeIaut/hass-NetzNOESmartMeter/actions/workflows/hacs.yml/badge.svg)
![Hassfest](https://github.com/TheJoeIaut/hass-NetzNOESmartMeter/actions/workflows/hassfest.yml/badge.svg)
![Lint](https://github.com/TheJoeIaut/hass-NetzNOESmartMeter/actions/workflows/lint.yml/badge.svg)
![Test](https://github.com/TheJoeIaut/hass-NetzNOESmartMeter/actions/workflows/test.yml/badge.svg)
![Release](https://github.com/TheJoeIaut/hass-NetzNOESmartMeter/actions/workflows/release.yml/badge.svg)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)

## About

This repo contains a custom component for [Home Assistant](https://www.home-assistant.io) for exposing a sensor
providing information about a registered [NetzNÖ Smartmeter](https://www.netz-noe.at/smartmeter).

The integration syncs all consumption data in Home Assistant, which allows a hourly view on consumption data in Home Assistant:
![Screenshot of the energy dashboard, showing hourly consumption measured by a NetzNÖ SmartMeter](/docs/netznoe-energyusage.png)

## Energiegemeinschaft (energy community)

An Energiegemeinschaft produces power locally. That power is consumed first,
and only what it cannot cover is taken from the public grid. The NetzNÖ API
reports this split for every 15 minute interval, and the integration can track
it as two extra sensors per metering point:

| Sensor | Meaning |
| --- | --- |
| `Smartmeter <id>` | Your full consumption. Unchanged, whether or not the option is on. |
| `Smartmeter <id> Eigendeckung` | The share covered by the energy community. |
| `Smartmeter <id> Restnetzbezug` | The remainder drawn from the public grid. |

Eigendeckung and Restnetzbezug always add up to the full consumption, so the
main sensor stays the single source of truth for how much you used.

### The split arrives late

NetzNÖ publishes the split some time after the consumption itself. Until it
appears for a given interval, that interval counts entirely as grid usage, so
**Restnetzbezug shows the same values as the full sensor**. Once the values are
published, the previous day is read again and its statistics are rewritten, at
which point Eigendeckung fills in and Restnetzbezug drops to the grid-only
share. Nothing needs to be done by hand.

Because the split is only published for interval (15 minute) meters, the option
has no effect on meters that report a single value per day.

## Acknowledgments

This integration was created and is maintained by
[TobiKr](https://github.com/TobiKr) at
[TobiKr/hass-NetzNOESmartMeter](https://github.com/TobiKr/hass-NetzNOESmartMeter).
This repository is a fork; all of the original work is theirs.

It in turn builds on the excellent [Wiener Netze Smartmeter](https://github.com/DarwinsBuddy/WienerNetzeSmartmeter)
integration by [DarwinsBuddy](https://github.com/DarwinsBuddy) and contributors,
which served as the foundation for the Netz NÖ adaptation.

### License

This fork includes code originally published without an explicit license. The
modifications and new files added here are licensed under the
[MIT License](LICENSE); see [NOTICE](NOTICE) for the scope. If you are the
original author and prefer a different license or no redistribution, please
open an issue.

## Installation

### Manual

Copy `<project-dir>/custom_components/netznoe` into `<home-assistant-root>/config/custom_components`

### HACS
1. Add this repository as a custom repository in HACS
2. Search for `NetzNÖ Smartmeter` or `netznoe` in HACS
3. Install
4. Restart Home Assistant
5. Configure the integration

## Configure

You can choose between UI configuration or manual (by adding your credentials to `configuration.yaml` and `secrets.yaml` resp.)
After successful configuration you can add sensors to your favourite dashboard, or even to your energy dashboard to track your total consumption.

### UI
1. Navigate to Settings > Devices & Services > Add Integration
2. Search for "NetzNÖ" and add the integration
3. Enter your NetzNÖ SmartMeter Portal credentials and confirm
4. Choose whether to track an Energiegemeinschaft. The box is ticked for you
   when your account already belongs to one; leave it unticked if you only want
   total consumption
5. Adding the SmartMeter can take a couple of minutes as it syncs all existing data

To change the Energiegemeinschaft setting later, open the integration and press
**Configure**. The two extra sensors appear or disappear right away, and the
statistics already collected are kept either way.

### Manual
See [Example configuration files](example/configuration.yaml)

## Changes in this fork

- **Fixed a timestamp offset in the statistics import.** The importer ignored
  the timestamps the API returns and rebuilt them from each reading's position
  in the day, starting at midnight UTC. A day's readings actually begin at
  22:15 UTC on the previous day, so everything landed about two hours late in
  the energy dashboard. It now uses the timestamps the API reports, which are
  UTC despite carrying no offset, and attributes each reading to the hour it
  actually covers, since an API timestamp marks the *end* of its interval.
- **Added Energiegemeinschaft support**, described above.
- **Sync every hour instead of once a day.** The importer refused to query again
  until 24 hours had passed, which left near real time devices such as a
  Wallbox a day behind.
- **Recover from a lost session during long imports.** A full history import can
  outlive its session; every remaining day then failed silently and the run
  finished having written nothing, only to start the same doomed import again an
  hour later. Authentication failures now log back in and retry, and an import
  that could not read everything says so.

## Development

The repository follows the [integration_blueprint](https://github.com/ludeeus/integration_blueprint)
layout. Open it in VS Code and reopen in the dev container, then:

- `scripts/develop` starts a Home Assistant instance on port 8123 with this
  component loaded, configured by [`config/configuration.yaml`](config/configuration.yaml)
- `scripts/lint` formats and lints with ruff
- `pytest` runs the test suite

Note that the API reports its timestamps in UTC without an offset, even though
the portal shows them in Austrian local time; a day runs from 22:15 on the
previous day to 22:00. See [CONTRIBUTING.md](CONTRIBUTING.md) for details.
