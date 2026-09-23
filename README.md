# PPL CZ Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-ppl-cz.svg)](https://github.com/ha-parcel-integrations/ha-ppl-cz/releases)
[![Downloads](https://img.shields.io/github/downloads/ha-parcel-integrations/ha-ppl-cz/total.svg)](https://github.com/ha-parcel-integrations/ha-ppl-cz/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

A custom Home Assistant integration for [PPL CZ](https://www.ppl.cz), one of Czechia's two dominant private carriers (part of DHL Group / DHL eCommerce). It offers two independent ways to track parcels, picked when you add the integration:

- **Account** — log in with your e-mail and a one-time PIN (no password to store) and it automatically tracks every parcel on your mojePPL account: the ones you're **receiving** and the ones you **sent**.
- **Tracking codes** — no account at all. Add each parcel's tracking number by hand (and say whether it's incoming or outgoing), and the integration polls PPL's public tracking service for it. Because there's no account involved, nothing gets signed out anywhere — see [Choosing a source](#choosing-a-source).

Part of the [ha-parcel-integrations](https://ha-parcel-integrations.github.io/) family: it publishes the same canonical parcel format, statuses and events as the other carrier integrations, so it plugs straight into the [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) and cross-carrier automations.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Choosing a source](#choosing-a-source)
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

- **Two sources, your choice**: automatic import from your mojePPL account, or manual tracking-code entry with no account at all
- **Both directions**: parcels you're receiving (incoming) and parcels you sent (outgoing), each with its own sensors, on either source
- **Account source**: passwordless login — sign in with your e-mail and a one-time PIN, no password stored
- **Tracking-code source**: nothing to sign in to, so nothing to sign out of — add barcodes as you get them
- Per-parcel sensor with the canonical status (`registered` / `in_transit` / `at_pickup_point` / `delivered` / …), PPL CZ's own status text, and a tracking deep-link
- Summary sensors: incoming, outgoing, and recently delivered (both directions)
- A **deliveries calendar** on the tracking-code source, where a real expected delivery date is available
- Events + device triggers for no-code automations (parcel registered / status changed / delivered, incoming and outgoing)
- Opt-in per-parcel status history
- Manual refresh button and a diagnostic last-update sensor

## Requirements

- **Account source**: a mojePPL account (the app's e-mail + PIN login — no password needed)
- **Tracking-code source**: nothing — just the tracking number(s) printed on your shipping confirmation or delivery notice

## Installation

### HACS (recommended)

1. In HACS, choose the three-dot menu → **Custom repositories**.
2. Add `https://github.com/ha-parcel-integrations/ha-ppl-cz` as an **Integration**.
3. Install **PPL CZ** and restart Home Assistant.

### Manual

Copy `custom_components/ppl_cz` into your `config/custom_components/` folder and restart Home Assistant.

## Choosing a source

Add the integration via **Settings → Devices & Services → Add Integration → PPL CZ**. The first step asks which source to set up:

- **Account** signs in to your mojePPL account and imports parcels automatically. **A mojePPL account can only be signed in one place at a time** — PPL issues a single credential per account and replaces it on every sign-in, so connecting Home Assistant here signs the mobile app out, and signing back in on the app disconnects Home Assistant until you reconnect it. This is how PPL's login works; the integration cannot work around it.
- **Tracking codes** needs no account, so nothing gets signed out anywhere — but you add each parcel's barcode yourself instead of it appearing automatically, and PPL's tracking service can't tell whether a code is one you're receiving or one you sent, so you say which when you add it.

You can set up both at once, or either on its own — an account hub and a tracking-code hub coexist without conflict, and only one tracking-code hub is allowed (multiple mojePPL account hubs are fine, for a household with more than one account).

## Configuration

### Account

1. **Enter your e-mail address.** PPL CZ sends a 4-digit PIN to that inbox.
2. **Enter the PIN** from that e-mail.

That's it — no password. Your parcels are imported automatically and refreshed on a schedule. The session renews itself silently; you only log in again if Home Assistant asks you to (a **reauth** prompt) — see the note under [Choosing a source](#choosing-a-source).

### Tracking codes

Setup itself asks nothing — there's no account to validate. Add your first tracking codes afterwards, from **Configure** on the integration entry (see [Options](#options) below).

## Options

Open **Configure** on the integration entry. What you see depends on the source:

**Account** — a single form:

| Section | Option | Default | Description |
|---|---|---|---|
| Delivered parcels | Filter by / amount | last 7 days | How long delivered parcels stay visible on the delivered sensors. |
| Parcel history | Include status history | off | Adds a `history` attribute per parcel with each status update. Also the only way `delivered_at` gets a real timestamp — PPL CZ's account parcel list carries no delivered date on its own. |

**Tracking codes** — a menu:

| Menu entry | What it does |
|---|---|
| Incoming parcels | Manage the list of tracking codes for parcels you're receiving |
| Outgoing parcels | Manage the list of tracking codes for parcels you sent |
| Settings | The same delivered-parcels retention and history options as the account source |

Filing a code in the wrong list? Re-enter it in the other one — that moves it rather than creating a duplicate.

Changing an account-source option reloads the integration; a tracking-code change (a barcode, or a setting) applies live, with no reload.

## Dynamic polling

Polling isn't a setting here — instead of checking PPL CZ at the same rate
around the clock, the integration adjusts its own cadence to what your parcels
are actually doing:

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

Installs that were still on a fixed interval move over automatically —
nothing to change. This is now the polling behaviour across the
parcel-integrations suite, where the cadence is no longer a setting anywhere.

## Removal

Standard HA removal applies: **Settings → Devices & Services → PPL CZ → ⋮ → Delete**.

## Sensors

Each hub (an account, or the one tracking-code hub) gets its own set, named after the account e-mail or `tracking_codes`:

| Entity | Description |
|---|---|
| `sensor.ppl_cz_<hub>_incoming_parcels` | Active parcels you're receiving; full list under the `parcels` attribute |
| `sensor.ppl_cz_<hub>_outgoing_parcels` | Active parcels you sent |
| `sensor.ppl_cz_<hub>_delivered_parcels` | Recently received parcels (see the retention option) |
| `sensor.ppl_cz_<hub>_outgoing_delivered_parcels` | Recently delivered parcels you sent |
| `sensor.ppl_cz_<hub>_parcel_<code>` | One per active parcel (either direction); state is the canonical status, attributes carry the full normalised parcel |
| `sensor.ppl_cz_<hub>_last_successful_update` | Diagnostic: when PPL CZ was last polled successfully |

A delivered parcel moves from its per-parcel sensor to the matching delivered sensor automatically.

A **`button.ppl_cz_<hub>_refresh`** entity triggers an immediate poll outside
the regular interval.

The tracking-code hub also gets a **`calendar.ppl_cz_tracking_codes_deliveries`** entity — one event per active parcel with a known expected delivery date. The account source has no calendar entity: PPL's account API carries no delivery-date field at all.

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

The carrier's own status text is always available as `raw_status`. Field availability differs by source: the **account** source exposes no weight, dimensions or expected delivery window; the **tracking-code** source adds a real `weight` (kilograms) and an expected delivery date, but still no dimensions. Neither source ever populates `dimensions`.

## Events

The integration fires these on the event bus (also available as device triggers on the PPL CZ device):

| Event | When |
|---|---|
| `ppl_cz_parcel_registered` | A new parcel you're receiving appears |
| `ppl_cz_parcel_status_changed` | A received parcel's status changes (`old_status` / `new_status`), except the final hop to delivered |
| `ppl_cz_parcel_delivered` | A received parcel is delivered |
| `ppl_cz_outgoing_parcel_status_changed` | A sent parcel's status changes |
| `ppl_cz_outgoing_parcel_delivered` | A sent parcel is delivered |

Every payload is the full normalised parcel plus the hub's `device_id`, and both sources fire the same event names. Events are suppressed on the first refresh after start-up. (There is no delivery-time event on either source.)

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

- **Home Assistant asks me to reconnect PPL CZ** — this only happens on an account hub; the tracking-code source has no credential to reject. The stored sign-in was rejected, most often because the account was signed in somewhere else — usually the mojePPL app, which replaces the credential Home Assistant holds (see [Choosing a source](#choosing-a-source)). Follow the reauth prompt: enter your e-mail and the fresh PIN it e-mails you. Doing so signs the app out again. If you'd rather avoid this entirely, add a tracking-codes hub instead — see [Choosing a source](#choosing-a-source).
- **A parcel shows `unknown`** — PPL CZ has not scanned it yet, or reports a status we do not map. If a status logs "Unrecognised PPL CZ status" (either source), please [open an issue](https://github.com/ha-parcel-integrations/ha-ppl-cz/issues/new) with the logged line so the mapping can be extended.
- **An outgoing parcel on the account source looks off** — the account source's *incoming* shipment shape is confirmed against real parcels; the *outgoing* one (parcels you sent) is not yet, since no real outgoing shipment has been seen. Fields are guarded defensively and a mismatch reports `unknown` rather than a wrong status, plus a one-shot warning — please [report it](https://github.com/ha-parcel-integrations/ha-ppl-cz/issues/new?template=unrecognised_status.yml) if you see one, so the shape can be confirmed. The tracking-code source has no such gap — its payload was confirmed live before release.

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://ha-parcel-integrations.github.io/) — a family of parcel-carrier integrations that all publish the same canonical parcel format, statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier up into one set of sensors.
- Browse [the organisation](https://ha-parcel-integrations.github.io/) for the current list of supported carriers.

## Disclaimer

This is an independent, community-built project. It is not affiliated with, endorsed by, sponsored by, or supported by PPL CZ, Home Assistant, or any other third party referenced in this project. Please don't contact PPL CZ for support with this integration.

All third-party trademarks, trade names, product names, logos, and other brand assets are the property of their respective owners. References to them are solely to identify the relevant carrier or service and do not imply affiliation, sponsorship, or endorsement. Nothing in this project grants or implies any licence or right to use third-party brand assets.

This integration may rely on public, unofficial, or undocumented carrier interfaces, accessed with your own account or API key where required. These may change or be withdrawn without notice and may be subject to PPL CZ's terms. Data is sent only to PPL CZ's own services or those of its group; this project operates no servers of its own. You are responsible for ensuring that your use complies with applicable law and those terms. Use is at your own risk; see the [licence](LICENSE) for warranty limitations.

The account source uses the same account API as the mojePPL app, with your own account. The tracking-code source uses the same public tracking API as [ppl.cz](https://www.ppl.cz)'s own website — it needs no account, but does carry a fixed access key extracted from that site rather than one issued to you individually.

## Contributing

Pull requests and issues are welcome. Please open an issue before submitting a large change.

## License

[MIT](LICENSE)
