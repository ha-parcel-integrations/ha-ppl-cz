# PPL CZ Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-ppl-cz.svg)](https://github.com/ha-parcel-integrations/ha-ppl-cz/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

A custom Home Assistant integration for your [PPL CZ](https://www.ppl.cz) (mojePPL) account. Log in with your e-mail and a one-time PIN — no password to store — and it automatically tracks every parcel on your account: the ones you're **receiving** and the ones you **sent**. PPL is one of Czechia's two dominant private carriers, part of DHL Group / DHL eCommerce.

Part of the [ha-parcel-integrations](https://github.com/ha-parcel-integrations) family: it publishes the same canonical parcel format, statuses and events as the other carrier integrations, so it plugs straight into the [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) and cross-carrier automations.

> ### ⚠️ Early release — real parcel data still collecting
>
> Login, the PIN exchange and polling the account inbox are all confirmed
> live, end-to-end, against a real mojePPL account. What is **not** yet
> confirmed is the exact shape of a populated parcel — the account used to
> verify this integration had no shipments on it, so every field is guarded
> defensively and a mismatch reports **`unknown`** (never a wrong status)
> plus a one-shot warning with a ready-made issue link — please
> [report it](https://github.com/ha-parcel-integrations/ha-ppl-cz/issues/new?template=unrecognised_status.yml)
> once you see one, so the mapping can be confirmed or corrected.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Options](#options)
- [Dynamic polling](#dynamic-polling)
- [Removal](#removal)
- [Sensors](#sensors)
- [Parcel status reference](#parcel-status-reference)
- [Events](#events)
- [Examples](#examples)
- [Debugging](#debugging)
- [Troubleshooting](#troubleshooting)
- [Related integrations](#related-integrations)
- [Disclaimer](#disclaimer)
- [Contributing](#contributing)
- [License](#license)

## Features

- **Automatic import** of every parcel on your mojePPL account — no manual tracking numbers
- **Both directions**: parcels you're receiving (incoming) and parcels you sent (outgoing), each with its own sensors
- **Passwordless login**: sign in with your e-mail and a one-time PIN — no password stored
- Per-parcel sensor with the canonical status (`registered` / `in_transit` / `at_pickup_point` / `delivered` / …), PPL CZ's own status text, and a tracking deep-link
- Summary sensors: incoming, outgoing, and recently delivered (both directions)
- Events + device triggers for no-code automations (parcel registered / status changed / delivered, incoming and outgoing)
- Opt-in per-parcel status history
- Manual refresh button and a diagnostic last-update sensor

## Requirements

- A mojePPL account (the app's e-mail + PIN login — no password needed)

## Installation

### HACS (recommended)

1. In HACS, choose the three-dot menu → **Custom repositories**.
2. Add `https://github.com/ha-parcel-integrations/ha-ppl-cz` as an **Integration**.
3. Install **PPL CZ** and restart Home Assistant.

### Manual

Copy `custom_components/ppl_cz` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

Add the integration via **Settings → Devices & Services → Add Integration → PPL CZ**, then:

1. **Enter your e-mail address.** PPL CZ sends a 4-digit PIN to that inbox.
2. **Enter the PIN** from that e-mail.

That's it — no password. Your parcels are imported automatically and refreshed on a schedule. The session renews itself silently; you only log in again if Home Assistant asks you to (a rare **reauth** prompt, roughly every two weeks if the integration hasn't polled successfully in that time).

## Options

Open **Configure** on the integration entry:

| Section | Option | Default | Description |
|---|---|---|---|
| Delivered parcels | Filter by / amount | last 7 days | How long delivered parcels stay visible on the delivered sensors. |
| Parcel history | Include status history | off | Adds a `history` attribute per parcel with each status update. Also the only way `delivered_at` gets a real timestamp — PPL CZ's parcel list carries no delivered date on its own. |
| Polling | Refresh every | Automatic | **Automatic**, or a fixed **15 / 30 / 60 / 120 / 240 minutes**. New installs default to Automatic; existing installs keep their current fixed value until changed. Changes take effect immediately, no HA restart needed. See [Dynamic polling](#dynamic-polling) below. |

## Dynamic polling

You can set **Refresh every** to **Automatic** instead of a fixed number of
minutes. Instead of polling PPL CZ at the same rate around the clock, the
integration adjusts its own cadence to what your parcels are actually doing:

- **Quiet hours** — no polling between 00:00–06:00 local time, aside from one
  catch-up check at each end of that window (around midnight and around 6
  AM), so an overnight update is never missed.
- **Hot (every 15 minutes)** — while any tracked incoming or outgoing parcel
  is out for delivery. PPL CZ's API never returns an expected delivery
  window, so this kicks in the moment a parcel goes out for delivery, not an
  hour ahead of a known window like some other carriers in this suite.
- **Normal (every 45 minutes)** otherwise — this is also the minimum cadence,
  since it's the only way to discover a new shipment that appears on the
  account without going through Home Assistant. Delivered parcels never
  affect the cadence — only what's still in transit counts.
- A small, fixed per-install offset is added on top, so not every PPL CZ
  installation out there polls at exactly the same second.

This is opt-in for now, but it's expected to become the default — and
eventually the only — polling behaviour across the parcel-integrations
suite. If you try Automatic, we'd genuinely like to hear how it goes: share
your experience in [this
discussion](https://github.com/orgs/ha-parcel-integrations/discussions/12).

## Removal

Standard HA removal applies: **Settings → Devices & Services → PPL CZ → ⋮ → Delete**.

## Sensors

| Entity | Description |
|---|---|
| `sensor.ppl_cz_<email>_incoming_parcels` | Active parcels you're receiving; full list under the `parcels` attribute |
| `sensor.ppl_cz_<email>_outgoing_parcels` | Active parcels you sent |
| `sensor.ppl_cz_<email>_delivered_parcels` | Recently received parcels (see the retention option) |
| `sensor.ppl_cz_<email>_outgoing_delivered_parcels` | Recently delivered parcels you sent |
| `sensor.ppl_cz_<email>_parcel_<code>` | One per active parcel (either direction); state is the canonical status, attributes carry the full normalised parcel |
| `sensor.ppl_cz_<email>_last_successful_update` | Diagnostic: when PPL CZ was last polled successfully |

A delivered parcel moves from its per-parcel sensor to the matching delivered sensor automatically.

A **`button.ppl_cz_<email>_refresh`** entity triggers an immediate poll outside
the regular interval.

## Parcel status reference

The `status` field is the carrier-agnostic enum shared by the whole integration family:

| Status | Meaning |
|---|---|
| `registered` | Announced to PPL CZ, not yet handed over |
| `in_transit` | In PPL CZ's sorting network |
| `out_for_delivery` | On a delivery vehicle today |
| `at_pickup_point` | Waiting for you at a PPL Parcelshop / Parcelbox (including AlzaBox) |
| `delivered` | Delivered |
| `returning` | Failed delivery, going back to the sender (or already back) |
| `problem` | Cancelled or removed before shipping |
| `unknown` | Not yet scanned, or a status we have not mapped yet |

The carrier's own status text is always available as `raw_status`. PPL CZ exposes no weight, dimensions or expected delivery window, so those fields are always empty.

## Events

The integration fires these on the event bus (also available as device triggers on the PPL CZ device):

| Event | When |
|---|---|
| `ppl_cz_parcel_registered` | A new parcel you're receiving appears |
| `ppl_cz_parcel_status_changed` | A received parcel's status changes (`old_status` / `new_status`), except the final hop to delivered |
| `ppl_cz_parcel_delivered` | A received parcel is delivered |
| `ppl_cz_outgoing_parcel_status_changed` | A sent parcel's status changes |
| `ppl_cz_outgoing_parcel_delivered` | A sent parcel is delivered |

Every payload is the full normalised parcel plus the hub's `device_id`. Events are suppressed on the first refresh after start-up. (There is no delivery-time event — PPL CZ exposes no ETA.)

## Examples

Ready-to-paste automations live in [`examples/`](examples/), including notifying when a parcel is ready for pickup.

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/hki-parcels-card](https://github.com/jonisnet/hki-parcels-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Debugging

```yaml
logger:
  logs:
    custom_components.ppl_cz: debug
```

## Troubleshooting

- **Home Assistant asks me to reconnect PPL CZ** — the stored session could not be renewed (typically because it's been more than two weeks since a successful refresh). Follow the reauth prompt: enter your e-mail and the fresh PIN it e-mails you.
- **A parcel shows `unknown`** — PPL CZ has not scanned it yet, or reports a status we do not map. If a status logs "Unrecognised PPL CZ status", please [open an issue](https://github.com/ha-parcel-integrations/ha-ppl-cz/issues/new) with the logged line so the mapping can be extended.

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://github.com/ha-parcel-integrations) — a family of parcel-carrier integrations that all publish the same canonical parcel format, statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier up into one set of sensors.
- Browse [the organisation](https://github.com/ha-parcel-integrations) for the current list of supported carriers.

## Disclaimer

This integration uses the same account API as the mojePPL app, with your own account. It is not affiliated with, endorsed by, or supported by PPL CZ. Be gentle with the polling interval.

## Contributing

Pull requests and issues are welcome. Please open an issue before submitting a large change.

## License

[MIT](LICENSE)
