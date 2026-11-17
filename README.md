<h3 align="center">Lyse Charge for Home Assistant</h3>

<p align="center">
Monitor and control your <b>Lyse Lading</b> EV chargers from <a href="https://www.home-assistant.io/">Home Assistant</a>, through the same cloud API the Lyse Lading app uses.
</p>

<p align="center">
<a href="https://hacs.xyz/"><img src="https://img.shields.io/badge/HACS-Custom-41BDF5.svg" alt="HACS Custom"></a>
<a href="https://github.com/alexbrasetvik/ha-lyse-charge/actions/workflows/validate.yml"><img src="https://github.com/alexbrasetvik/ha-lyse-charge/actions/workflows/validate.yml/badge.svg" alt="Validate"></a>
<a href="LICENSE.txt"><img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License: MIT"></a>
</p>

> **Unofficial.** This is an independent community integration, not affiliated with or endorsed by Lyse. Lyse publishes no API documentation, so this integration's API calls were worked out from the Lyse Lading iOS app (v1.5.23). Lyse can change it at any time, as it did when it moved the API to a new host in November 2026. Use at your own risk.

## Features

For each charger you add:

- **Charging switch**, per socket: on starts a charging session, off stops it.
- **Charger status** and **socket status**.
- **Power** (kW), **session energy** (kWh) and **session cost** of the active session.
- **Tariff**: the current energy price, including VAT, per kWh.

Live values arrive over the app's websocket subscription as they change. The integration also polls charger details and active sessions every 30 seconds.

## Installation

1. In HACS, open the menu → **Custom repositories**.
2. Add `https://github.com/alexbrasetvik/ha-lyse-charge` with category **Integration**.
3. Search for **Lyse Charge**, download it, and restart Home Assistant.

Requires Home Assistant 2026.10 or later.

## Setup

1. Go to **Settings → Devices & services → Add integration → Lyse Charge**.
2. Sign in with your Lyse Lading email and password.
3. Pick the chargers to add. The list comes from your charging history, so a charger you have never charged at won't show up yet.

To change chargers later, open the integration and press **Configure**. If the integration has no chargers selected, it raises a repair notice saying so.

The integration stores your password so it can sign in again on its own when its session runs out. If Lyse rejects it, Home Assistant asks you to sign in again.

## Known limitations

- **Chargers with more than one socket**: the integration keeps live values per charger, not per socket, so one socket's live sensors can show the other socket's power and energy.
- **Manufacturer and firmware version** are missing from the device, because the app's API no longer provides them.

## Development

```sh
uv sync
mise run check      # every gate below, without modifying files
mise run format     # format in place
mise run lint       # ruff and strict mypy
mise run test       # tests
mise run hassfest   # checks out HA core at the installed version into .cache/
```

The tests check the GraphQL queries against the operations the official app sends, extracted into `tests/fixtures/lyse_lading_app.graphql`. After an app update, refresh that file and rerun the tests:

```sh
mise run extract-app-graphql /path/to/Runner.app
```
