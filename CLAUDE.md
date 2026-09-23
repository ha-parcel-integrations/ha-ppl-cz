# Working in this repository

Home Assistant custom integration for **PPL CZ** parcel tracking. Distributed
via HACS; not part of HA core. One carrier in the
[ha-parcel-integrations](https://github.com/ha-parcel-integrations) suite,
**generated from ha-carrier-template** — everything outside
*Carrier-specific notes* is suite-wide; when in doubt check the template or a
sibling repo. **Two sources**, picked at setup: **account** (the mojePPL app's
REST backend, passwordless e-mail + PIN login) and **tracking** (the public
website's tracking-by-number backend, no credential at all). No manual
services. No DTO layer.

## Shared conventions — fetch when relevant

Suite-wide rules live in
[`.github/CONVENTIONS.md`](https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md)
and are **not** repeated here. Don't fetch it every session — fetch it **before**
you act in one of these areas:

| Before you … | Fetch `CONVENTIONS.md` § |
|---|---|
| touch entities, sensors, config/options flow, coordinator, diagnostics, translations | *Home Assistant developer docs* (its table points on to the canonical HA page — don't rely on memory) |
| add/rename a parcel field, a `ParcelStatus`, or a bus event; change the sort/first-refresh; touch unmapped-status logging | *Parcel contract* — exact key set, units, sort, events + suppression; `test_parcels.py::test_normalize_publishes_exactly_the_canonical_keys` guards the key set |
| change which optional field this carrier populates vs. always returns `None` | Update `const.py`'s `CAPABILITIES` in the same commit — it feeds the comparison table on the docs site, so a field that starts (or stops) coming back non-null and isn't reflected there is a wrong claim on the website, not just a stale comment |
| ship anything while below 1.0.0 (unconfirmed data) | *Pre-1.0 releases* — one-shot WARNINGs for every guessed shape/code |
| consider "fixing" a lint/pattern the skill flags (inline client, sync requests) | *Deliberate skill divergences* — likely intentional, don't re-flag |
| commit, bump, tag, release, or write release notes; add a feature without a test | *Workflow / Commits / Versioning / Testing* |

**Structure, options flow, dynamic polling and module layout are suite-wide**
and identical in every carrier — the authoritative spec is
[`ha-carrier-template/scaffold/CLAUDE.md`](https://github.com/ha-parcel-integrations/ha-carrier-template/blob/main/scaffold/CLAUDE.md).
Where this repo diverges from it, that is recorded below under
*Divergences from the scaffold*.

**Polling cadence is not configurable — don't add the option back.** Both
coordinators recompute `update_interval` at the end of every
`_async_update_data` (quiet window 00:00–06:00 with two anchors, hot 15 min
/ mid 45 min, plus a per-install stagger). The **account** coordinator
follows Section 2.2 (never a full stop, since the mid-tier poll is also how
a new shipment on the account gets discovered); the **tracking** coordinator
follows Section 2.1 instead and may suspend entirely — see *Divergences from
the scaffold*. The `refresh_interval` dropdown (Phase 1, 0.10.0) is gone; a
stale stored value is never read. The account options flow has two sections
left, `delivered` and `history`; the tracking options flow is a menu instead
— see the two-source note below.

**Suite-wide tripwires, kept inline on purpose:**
- **First refresh in `__init__.py`, before `async_forward_entry_setups`** — from
  a forwarded platform HA can't catch `ConfigEntryNotReady` and half-sets-up the
  entry. Runtime-only; tests don't catch a regression.
- **Setup stale-entity sweep is scoped to `domain == "sensor"` and skips
  `non_parcel_unique_ids`** — else it deletes the refresh button / the
  summary+diagnostic sensors. Add a new non-parcel sensor's unique_id to the set.
- **Per-parcel sensors are removed by the summary sensor** via
  `entity_registry.async_remove` (self-removal races and leaves ghosts).

## Carrier-specific notes

**API mechanics live in `carrier-research/ppl-cz/api/` (private research
repo)** — the three-call email+PIN login, the Azure AD B2C ROPC token
exchange, both `dhl-api-key` static headers, the shipment list/events
endpoints, the website tracking-by-number endpoint and both status
vocabularies. Not duplicated here; this section is integration-level
decisions only.

**Two sources, `account/` and `tracking/` subpackages (2026-09-23).**
Mirrors bpost's split exactly: each source owns its client, coordinator and
normaliser under `custom_components/ppl_cz/<source>/`; `api.py`,
`coordinator.py` and `parcels.py` at package root are re-export shims kept so
pre-split imports and automations still resolve (`api.py`/`coordinator.py`
use bpost's `globals().update(vars(_account))` trick, not a named re-export,
since the account modules have a wide public surface — including private
helpers older tests reach into). `session.py` stays at the root: both
sources hit `api.dhl.com` and need the cookie-free jar, so it isn't an
account concern.
- **`entry.data[CONF_SOURCE]` picks the source** (`account` / `tracking`),
  chosen from a menu at setup. **A missing key defaults to `SOURCE_ACCOUNT`**
  — the inverse of bpost's default, because this repo started as an
  account-only integration. Every `entry.data.get(CONF_SOURCE, …)` in this
  repo takes `SOURCE_ACCOUNT` as its fallback; getting it backwards would
  silently convert every existing user's account hub into an empty tracking
  hub on upgrade.
- **`CAPABILITIES`/`PLATFORMS` are per-source.** `PLATFORMS` is a
  `CONF_SOURCE`-keyed dict (`__init__.py` always calls
  `async_forward_entry_setups`/`async_unload_platforms` with
  `PLATFORMS[source]`, never a union) — account gets `[BUTTON, SENSOR]`,
  tracking gets `[BUTTON, CALENDAR, SENSOR]` since only it has a real ETA.
  `CAPABILITIES` itself stays a **`CAPABILITIES_BY_VARIANT`** dict, not
  `CONF_SOURCE`-keyed — the docs site's generator
  (`ha-parcel-integrations.github.io/scripts/generate.py`) regex-parses that
  exact constant name (see bpost's own precedent), keyed by human-readable
  variant labels (`"Account"`/`"Tracking"`) rather than the internal
  `SOURCE_*` values. `CAPABILITIES` is a flat alias to the `"Account"`
  variant for any consumer still expecting one set.
- **The tracking hub is a singleton; account hubs stay multi-entry.**
  `async_set_unique_id(f"{DOMAIN}_{SOURCE_TRACKING}")` +
  `_abort_if_unique_id_configured()` in `config_flow.py` — the same effect
  `single_config_entry` has for `ha-sunyou`/`ha-quickpac`/`ha-sameday`,
  applied to one source instead of the whole integration. Do **not** add
  `single_config_entry` to `manifest.json` — multi-account is deliberate.
- **Reauth is account-only and unreachable from a tracking entry** — the
  tracking coordinator has no credential to reject, so it never raises
  `ConfigEntryAuthFailed`.
- **Tracking-source direction (incoming/outgoing) is declared by the user,
  never inferred.** The website payload carries no `discriminator` and no
  sender/recipient split, and with no account there is no identity to
  compare a party against — a parcel the user sent and one they are
  receiving look identical on the wire. Copied `ha-packeta`'s solution for
  the same account-less problem: `CONF_DIRECTION` on each `CONF_PARCELS`
  entry (`DIRECTION_INCOMING`/`DIRECTION_OUTGOING`,
  `DEFAULT_DIRECTION = DIRECTION_INCOMING` so a pre-existing entry needs no
  migration), one shared list, two options-flow steps
  (`incoming_parcels`/`outgoing_parcels`) that each replace only their own
  direction's entries — `tracking/parcels.tracked_direction()` and
  `config_flow._async_step_parcel_list()`. Do not reuse
  `account/parcels.shipment_direction()` — it reads account-only fields.
  The tracking coordinator wires into the *same* outgoing sensors/events the
  account source already has (`outgoing_parcels`, `outgoing_delivered_parcels`,
  the two outgoing device triggers) rather than inventing parallel ones.
- **The website `dhl-api-key` is a second, distinct shared secret** from the
  mobile app's, with its own 2026-09-23 accepted-risk ruling — see the
  transport rules below. Never let either key value appear outside
  `const.py`.

**Login is three network calls behind two user-facing steps.**
`async_step_account` requests a PIN (`POST registrations`); `async_step_code`
confirms it (`PUT registrations/{id}` → a one-time Azure password) and
immediately exchanges that password for a bearer token pair (Azure B2C ROPC,
`grant_type=password`). Reauth (`async_step_reauth` → `async_step_reauth_confirm`)
re-runs the same PIN request against the stored e-mail, then falls into
`async_step_code` again — it is not a separate flow.

**Session model: re-mint from a stored password, never refresh (2026-08-23).**
PPL's B2C tenant hard-revokes the whole refresh-token lineage ~60 minutes
after the original `grant_type=password` login, regardless of how many
successful refreshes happened in between — confirmed live twice (see
[issue #1](https://github.com/ha-parcel-integrations/ha-ppl-cz/issues/1)) and
root-caused via a third APK teardown pass: the mojePPL app itself never sends
`grant_type=refresh_token` at all. It stores the PIN-exchange Azure password
and simply re-runs the password grant whenever a request 401s. This
integration mirrors that instead of fighting it: `config_flow.py` persists
`CONF_PASSWORD` (the same one-time password from `async_confirm_pin`) in the
config entry alongside `CONF_EMAIL`, and `api.py`'s `_async_remint` re-runs
`async_exchange_password(email, password)` on demand — there is no refresh
call anywhere in this client. An entry from before 0.9.2 has no stored
password, so `__init__.py` sends it through reauth once rather than
crash-loop. **`diagnostics.py` redacts `password`** — it is a real, reusable
credential, not a token, and must never appear in a diagnostics dump pasted
into a public issue.

**A `200` with a transient (non-JSON) body gets a silent retry, not a
forced reauth (2026-08-23, widened 2026-08-24).** Observed live on the local
dev instance: the token endpoint
and the shipments GET both occasionally hand back a syntactically valid
`200` whose body isn't the JSON contract, rather than a real rejection. Two
confirmed shapes: a zero-byte body (a stale pooled `aiohttp` connection
surviving past a long idle gap — the host waking from sleep is the one case
caught so far), and an HTML page stamped with Azure's own
`CorrelationId`/`DataCenter` comments — B2C's own server-side exception page
(`GLOBALEX.Detail`: *"AADB2C: We are unable to sign you in. Please contact
the administrator to adjust the number of authentication steps."*), not a
network-level artifact. That second shape has a known cause and a real fix —
see the cookie-jar note below; the retry here is only a backstop.
`_json()` raises `_EmptyResponseError` /
`_UpstreamErrorPageError` (both `_TransientBodyError`) for these two specific
shapes only — not any unparseable body; malformed-but-JSON-shaped content
still surfaces immediately as a real API-contract problem. Distinct from the
`AADB2C90129` revocation this integration already handles — that one comes
back as JSON with a proper error code and correctly still raises
`PPLCZAuthError`.

**Never put this integration on HA's shared aiohttp session — Azure B2C
cookies poison it (2026-08-24, root cause of the above).** The HTML-error-page
shape turned out not to be an Azure outage at all. Once it starts, **every**
re-mint fails until Home Assistant is restarted — and a restart is precisely
what clears the shared cookie jar, since the access token, password and email
all survive it in the config entry and so can't be the trigger. The failure
also follows the client to a *different* Azure data centre (`AM3` on one
incident, `DB3` on the next, both failing), so it travels in the request, not
on one unhealthy backend. B2C stamps `x-ms-cpim-*` cookies onto every ROPC
exchange — among them the one tracking in-flight journey transactions — and
accumulated across a long-lived shared jar they eventually break the very
journey step the error message names.

`session.py`'s `async_create_ppl_session` therefore hands out a dedicated
session with an `aiohttp.DummyCookieJar`; `__init__.py` closes it on unload,
the config flow lets HA's shutdown do it (`auto_cleanup=True`) since its
sessions are short-lived. **No call in this integration needs a cookie**, so
never swap this back to `async_get_clientsession` — `test_setup_uses_a_cookie_free_session`
guards it. The one-retry handling above stays as a cheap backstop for a
genuinely transient body, but it was never able to fix this: a retry inside
the same poll reuses the same jar and fails identically.

**Token storage: an absolute expiry timestamp, not the raw `expires_in`.**
`api.py._store_tokens` computes `token_expires_at = now + expires_in` at
fetch time and persists that (plus `access_token`) in the config entry —
`expires_in` alone is useless across a restart without an anchor, and it
arrives as a JSON string (`_parse_expires_in` guards a non-parseable value).
`_async_ensure_fresh_token` re-mints **proactively**, 120s before that
timestamp, so a poll doesn't race an about-to-expire token; a 401 still gets
one reactive retry as a backstop. **Only a failed re-mint** (the stored
password itself was rejected) raises `PPLCZAuthError` → `ConfigEntryAuthFailed`
— a plain access-token expiry is invisible to the user, handled entirely
inside the client.

**Re-mint is serialised behind `PPLCZApiClient._remint_lock`.** Two callers
racing a stale token — the scheduled poll and a manually-pressed refresh
button are the two paths that can genuinely overlap, since HA's coordinator
does not mutually exclude its own interval timer against
`async_request_refresh` — would otherwise both re-mint independently:
harmless (a fresh password grant always succeeds on its own), but a wasted
round-trip. Both the proactive path (`_async_ensure_fresh_token`) and the
reactive 401 retry (`_async_refresh_if_current`) acquire the lock and
re-check the access token afterwards, so a caller that lost the race skips
its own re-mint. The lock itself predates the session-model change (fixed
2026-08-22 for the same race under the old refresh-token model); it carries
over unchanged.

**The static `dhl-api-key` header is shipped deliberately.** It is
hardcoded identically in every mojePPL install — normally the exact
shared/extracted-secret class this suite's standing ruling refuses (bpost,
the three UK carriers) — but the maintainer reviewed this one specifically
and ruled it an accepted risk (2026-08-22, recorded in the research doc's
Verdict). Do not treat this as a precedent for a future carrier; get a fresh
ruling each time. It has never been tested whether the API would 401 without
it — moot, since the ruling makes the question academic for this build.

**One list call returns both directions — split by `shipment_direction()`,
not two endpoints.** `GET /api/v2/me/shipments?shipment_type=ALL` answers for
incoming *and* outgoing shipments in one response (unlike DHL NL/DPD's
separate "sent" endpoint). `parcels.shipment_direction()` reads the
(unconfirmed) `discriminator` field first, falling back to which
subtype-only field is present (`sender` → incoming, `recipient` →
outgoing); a shipment matching neither logs a one-shot warning and defaults
to incoming rather than silently dropping it from every list. The
coordinator mirrors vinted-go's shape for this (one coordinator, `data`/
`delivered`/`outgoing`/`delivered_outgoing`), not DHL NL's two-coordinator
split — PPL CZ has no second endpoint to justify one.

**`delivered_at` is populated only when the history option is on.** Unlike
every other field, PPL CZ's list DTO carries no delivered timestamp at all —
the only place a delivered instant exists is the matching event's
`createdAt`, which means a `GET .../events` call. Rather than pay that
fan-out cost unconditionally just for one timestamp, `delivered_at` stays
`None` on a delivered parcel until `CONF_INCLUDE_HISTORY` is on — the same
"can't supply it without a paid extra call" trade-off as `weight`/
`dimensions`. This was folded back into `tracking.md` as a correction (the
original mapping table didn't flag the option dependency).

**History fan-out is cached on the shipment's coarse status**, mirroring DHL
NL's track-trace cache: `coordinator._history_for` refetches
`GET .../events` only when `lastShipmentEvent` changed since the last poll,
not every refresh. A failed events call is best-effort — it keeps whatever
was cached (or `None`) rather than failing the whole poll or blanking the
attribute; a genuine `PPLCZAuthError` still propagates (that's a real
"log in again" signal, not a per-parcel hiccup).

**`raw` is a curated subset, not the whole payload.** `account/parcels._RAW_FIELDS`
exposes only `ownership`, `cod`, `phaseText`, `discriminator`,
`codPaidStatus`, `isWaitingForSync` — per the build plan's own mapping
table. `toAddress` / `toDeliveryPoint` are deliberately left out of the
per-parcel sensor's `raw` attribute (not just redacted in diagnostics): a
full delivery address has no reason to sit in a plain entity attribute
that shows up in the HA UI and logbook. The tracking source curates its own
`raw` the same way (`tracking/parcels._RAW_FIELDS`) — `addresses` and
`accessPoint`'s own contents stay out for the same reason.

**No ETA, ever — account source only.** PPL CZ's account DTOs carry no
`planned_from`/`planned_to` source at all (confirmed absent from the
mechanics doc's field list, not just usually empty) — so, like vinted-go,
the account source ships **no calendar platform and no `next_delivery`
sensor** rather than a permanently-inert one. This does **not** carry over
to the tracking source, which has a real `expectedDeliveryDate` and does get
a calendar entity — see the two-source note above. `const.py`'s `PLATFORMS`
and `CAPABILITIES_BY_VARIANT` both reflect the split; keep them in agreement
if either source's field support ever changes.

**Pre-1.0 one-shot warnings** (`account/parcels.py`, all structure-only — no
values): first populated shipment-list item shape, first populated
event-history item shape, an unmapped `lastShipmentEvent`/event `code`, a
`toDeliveryPoint.type` value seen for the first time (the enum's members
were never enumerated in the teardown), and a shipment whose direction
couldn't be determined. **The incoming item shape is now confirmed** — a
real user's diagnostics (issue #1, 2026-08-22) supplied a populated
`items[]` with two delivered incoming shipments, full 7-event histories and
no unrecognised code — but **the outgoing item shape stays open**: no real
outgoing shipment has been seen, so `toAddress`, `toDeliveryPoint` and a
populated `cod` on that side are still reconstructed, and the warnings above
stay armed for it. The tracking source needs none of this net — its payload
was confirmed live on real parcels before it shipped, so
`tracking/parcels.py` has no first-sighting warnings, only the one-shot
unmapped-status warning every source in the suite carries.

**`deliveryInfo` research probe (2026-09-01) — logging only, never a data
source.** `GET .../shipments/{id}/deliveryInfo` (per
`carrier-research/ppl-cz/api/tracking.md`) was found by static analysis and
never called live before this — its response shape, and even whether it
answers at all, is completely unknown. Since the maintainer can't probe it
by hand, `coordinator._probe_delivery_info` calls it once per shipment id
(cached in `self._delivery_info_probed`, forever — not once per poll) for
every *active* (not-yet-delivered) shipment, and `parcels.note_delivery_info_shape`
fires the same one-shot structure-only `WARNING` pattern as
`note_items_shape`/`note_events_shape` the first time a real call returns a
populated body. Failures (404, error, unexpected shape) are silent — a 404
here is itself the answer to an open question, not something to alarm the
user about. **The result is never wired into `planned_from`/`planned_to`** —
until a real response shape is confirmed there is nothing to map; see "No
ETA, ever" above. If a real shape does come back, that is a build decision
for a future session, not something to infer from the warning alone.

**One credential per account — an app login and this integration evict each
other (2026-09-22, reverses the 2026-08-22 ruling).** The mechanics doc
originally flagged whether an HA login could log out the phone app; that was
ruled not applicable before the repo was generated, and
[issue #4](https://github.com/ha-parcel-integrations/ha-ppl-cz/issues/4)
proved it wrong. A fourth APK teardown pass settled it: the ROPC grant's
`username` is the plain e-mail address, never the device, so the account has
exactly **one** Azure credential; and the app mints a fresh one for that same
identity on every login (its confirm-step DTO is literally named
`CreatedUserResponseDto`), generating throwaway random UUIDs for `deviceId`
and `registrationSessionId` each time exactly as `config_flow.py` does. So a
mojePPL app login rotates the password stored here out from under us, and a
setup/reauth here does the same to the phone. **This is not fixable** — the
PIN arrives by e-mail, so reconnecting is inherently manual. It is disclosed
instead, in the `user` (source menu), `account` and `reauth_confirm` step
descriptions and the README. **This is also the whole reason the tracking
source exists** — it gives users a way to sidestep the eviction entirely,
without fixing it (see the two-source note above and
[issue #4](https://github.com/ha-parcel-integrations/ha-ppl-cz/issues/4)).
Don't re-add a "multi-device is fine" claim anywhere.

Note this is a *different* failure from the ~60-minute lineage revocation
above: rotation kills the credential the instant someone logs in elsewhere,
with no timeout involved. Issue #1's live test only ever proved that two
sessions can *poll* concurrently, which is still true — it never re-logged-in
on the app, so it never exercised this.

**Only `access_denied`/`invalid_grant` on a 400 mean reauth.** The token
endpoint's other 400s are Azure failing, not the credential being rejected,
and reauth would send the user after a PIN that fixes nothing — so
`AZURE_CREDENTIAL_REJECTED_ERRORS` gates which ones become `PPLCZAuthError`;
everything else (including a 400 whose body won't parse) is a plain
`PPLCZApiError` and gets retried. The app draws the same line, logging out on
`access_denied` and on nothing else. 401/403 stay unconditional auth errors.
The rejection log line is a **WARNING, not DEBUG** — it ends in a reauth
prompt either way, and Azure's `error`/`error_description` are the only thing
that tells a rotated password apart from a revoked one in an issue report.
Neither field carries user PII.

**Account do-not-build list:** anything under `/api/v1/me/profiles`,
`/cod_payments`, `/ratings`, `/recipient_availability`, `/recipient_phone`,
`/title`, `/archive` — user-profile edits, payments and ratings, none of it
a parcel field.

**Website tracking transport rules (`tracking/api.py`), all load-bearing:**
- **The empty JSON body (`{}`) is required on every POST** — omitting it is
  a `415 UnsupportedMediaType`, not a `400`. `async_get_parcel` always sends
  `json={}`, never omits the argument.
- **No reCAPTCHA token, cookie, `Origin` or `Referer` is sent, and none is
  needed.** The 2026-08-22 "reCAPTCHA-walled" verdict was wrong — reversed
  2026-09-23 by re-probing with plain `curl`: the bundled React app does
  mint a token, but the server never checks it. Do not add browser
  fingerprinting or a recaptcha solver here; a real `401`/`403` means the
  key was rejected, not that a captcha is needed.
- **Not found is a `400`, matched on `detail`, never on the status alone.**
  `{"title":"BadRequest","status":400,"detail":"service.TrackAndTrace.ShipmentNotFound"}`
  raises `PPLCZTrackingNotFound`; any other `400` (or a `400` whose body
  won't parse) raises the plain `PPLCZTrackingApiError` — a real malformed
  request or a rejected key must never be read as "parcel not found".
- **No `PPLCZAuthError` path exists on this source.** There is no
  credential, so nothing can expire; a rejected `TRACKING_DHL_API_KEY` is a
  compatibility failure (`PPLCZTrackingApiError`) that needs a new release,
  never a user-facing reauth prompt.

## Divergences from the scaffold

Everything not listed here follows the scaffold exactly.

*Options and reloads* — **account source**: `async_schedule_reload` on
submit with **no** update listener (unchanged by the source split). The
interval half of this divergence is gone: `CONF_REFRESH_INTERVAL` (15/30/60/
120/240 min plus `"auto"`, Phase 1, 0.10.0) was removed in the Phase 2
convergence (maintainer decision 2026-09-12, every remaining Phase-1 carrier
goes unconditional), so this carrier now matches the scaffold's no-interval
model — see the tripwire above; don't add the option back. **Tracking
source**: the scaffold's own update-listener + live-refresh model, like
bpost's tracking source and Packeta — combining a reload flow with a
listener is deprecated, which is exactly why the two sources diverge from
each other here.

*Dynamic polling* — **account source: PPL CZ's DTOs carry no ETA at all**
(see "No ETA, ever" above), so `planned_from` is always `None`: every
`out_for_delivery` parcel takes the "no `planned_from`" branch straight to
the hot tier, and the 1h-lookahead branch is architecturally unreachable
from real account data — the same situation `ha-quickpac`/`ha-sameday`/
`ha-sunyou` hit. Its tests therefore exercise that branch with hand-built
dicts / a patched tier helper rather than an invented ETA payload. **The
tracking source has a real `expectedDeliveryDate`**, so its 1h-lookahead
branch is reachable from real data — its tests use a real ISO timestamp
instead. Both coordinators surface the tier in diagnostics under
`"polling"` (`current_tier_minutes`, `update_interval_seconds`), recomputed
at the end of every `_async_update_data`. One further split: the account
coordinator never fully suspends (a single call is the only way to
discover a new shipment); the tracking coordinator may suspend entirely
when nothing is tracked or everything tracked is delivered, mirroring
bpost's/Packeta's barcode-based model.

## Running tests

```
python -m pytest tests/ --cov=custom_components.ppl_cz
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing. A code change updates the README + this file + `docs/` in the same
commit; the API reference lives in this carrier's own directory in the private
`carrier-research/<slug>/api/`, never in this repo.
