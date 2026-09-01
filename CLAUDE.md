# Working in this repository

Home Assistant custom integration for **PPL CZ** parcel tracking (the mojePPL
account app's REST backend). Distributed via HACS; not part of HA core. One
carrier in the [ha-parcel-integrations](https://github.com/ha-parcel-integrations)
suite, **generated from ha-carrier-template** — everything outside
*Carrier-specific notes* is suite-wide; when in doubt check the template or a
sibling repo. Account-based (passwordless e-mail + PIN login), no manual
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
| consider "fixing" a lint/pattern the skill flags (poll interval, inline client, sync requests) | *Deliberate skill divergences* — likely intentional, don't re-flag |
| commit, bump, tag, release, or write release notes; add a feature without a test | *Workflow / Commits / Versioning / Testing* |

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
exchange, the `dhl-api-key` static header, the shipment list/events endpoints
and the status vocabulary. Not duplicated here; this section is
integration-level decisions only.

**Login is three network calls behind two user-facing steps.**
`async_step_user` requests a PIN (`POST registrations`); `async_step_code`
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

**`raw` is a curated subset, not the whole payload.** `parcels._RAW_FIELDS`
exposes only `ownership`, `cod`, `phaseText`, `discriminator`,
`codPaidStatus`, `isWaitingForSync` — per the build plan's own mapping
table. `toAddress` / `toDeliveryPoint` are deliberately left out of the
per-parcel sensor's `raw` attribute (not just redacted in diagnostics): a
full delivery address has no reason to sit in a plain entity attribute
that shows up in the HA UI and logbook.

**No ETA, ever.** PPL CZ's DTOs carry no `planned_from`/`planned_to` source
at all (confirmed absent from the mechanics doc's field list, not just
usually empty) — so, like vinted-go, this integration ships **no calendar
platform and no `next_delivery` sensor** rather than a permanently-inert
one. `const.py`'s `PLATFORMS` and `CAPABILITIES` both reflect this; keep
them in agreement if that ever changes.

**Pre-1.0 one-shot warnings** (`parcels.py`, all structure-only — no
values): first populated shipment-list item shape, first populated
event-history item shape, an unmapped `lastShipmentEvent`/event `code`, a
`toDeliveryPoint.type` value seen for the first time (the enum's members
were never enumerated in the teardown), and a shipment whose direction
couldn't be determined. Every one of these fires because the item payload
is still `payload: reconstructed` — the test account used for the live
capture had zero shipments. **The gate item that stays open post-build**:
replace the reconstruction in `tracking.md` with a real body once a real
account/parcel is available, and correct anything these warnings surface
along the way.

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

**Multi-device is not gated.** The mechanics doc originally flagged whether
an HA login could log out the phone app; the maintainer ruled it not
applicable to this build (2026-08-22) before the repo was generated. No
warning, no config-flow caveat for it — nothing in this repo treats that as
an open question.

**Do not build:** the website tracking-by-number surface
(`ppl.cz/vyhledat-zasilku` → `api.dhl.com/ecs/ppl/webapi/TrackAndTrace`) — a
per-request Google reCAPTCHA v3 token generated client-side, not solvable
without a real browser; anything under `/api/v1/me/profiles`,
`/cod_payments`, `/ratings`, `/recipient_availability`, `/recipient_phone`,
`/title`, `/archive` — user-profile edits, payments and ratings, none of it
a parcel field.

## Options and reloads — account-based model

The options flow is one sectioned form (`data_entry_flow.section`).
**Account-based**, so it calls `async_schedule_reload` on submit and
registers **no** update listener (combining a listener with a
reload-on-update flow is deprecated, an error in HA 2026.12+).

The user-tunable poll interval is a deliberate HACS divergence (see
CONVENTIONS.md). `CONF_REFRESH_INTERVAL` = 15/30/60/120/240 min, default 30,
plus `"auto"` (dynamic, status-driven polling — see below). New config
entries default to `"auto"`; an entry created before this option existed
keeps its numeric value untouched.

**Dynamic polling (Phase 1 of `carrier-research/dynamic-polling.md`,
account-based model, Section 2.2)** — `"auto"` is one more selectable
`CONF_REFRESH_INTERVAL` value, not a replacement for the numeric options.
When selected, the coordinator recomputes `update_interval` at the end of
every `_async_update_data`: a 15 min hot tier the moment any active
incoming *or* outgoing parcel is `out_for_delivery` (starting 1h before
`planned_from`, or immediately if missing), a 45 min mid tier otherwise —
which never stops, since the account call is the only way to discover a new
shipment that appears without going through this integration — and a
00:00–06:00 local-time quiet window with anchor polls at each end, plus a
small deterministic per-`entry_id` stagger. `problem`/`returning` stay in
the mid tier, not hot. **PPL CZ's DTOs carry no ETA at all** (see the "No
ETA, ever" note above), so `planned_from` is always `None` — every
`out_for_delivery` parcel takes the "no `planned_from`" branch straight to
the hot tier; the 1h-lookahead branch is architecturally unreachable from
real PPL CZ data, the same situation ha-quickpac/ha-sameday/ha-sunyou hit on
their own conversions. Surfaced in diagnostics under `"polling"`
(`current_tier_minutes`, `update_interval_seconds`). Do not build a Phase 2
(making `auto` unconditional / dropping the dropdown) without a separate
maintainer decision — that is explicitly out of scope for this rollout.

## Module layout

| File | Carrier-specific? |
|---|---|
| `api.py` (login/refresh/shipments/events, error types, token refresh) | **yes** |
| `const.py` (domain, endpoints, `ParcelStatus`, option keys) | partly (endpoints) |
| `parcels.py` (status map, `normalize_parcel`, direction split, history, sort, filters — pure, no I/O) | partly (`_STATUS_MAP`, `normalize_parcel`, `shipment_direction`) |
| `coordinator.py` (fetch, split, history fan-out cache, event firing) | mostly not |
| `config_flow.py` (2-step e-mail+PIN login, reauth, options) | partly |
| `__init__.py` (client setup, token persistence, first refresh) | mostly not |
| `sensor.py` / `button.py` / `device_trigger.py` | no |
| `diagnostics.py` | partly (`TO_REDACT`) |

No `services.py` and no `calendar.py` — the account auto-imports parcels and
there is no ETA to put on a calendar. `parcels.py` is deliberately free of
I/O and HA objects so the per-carrier part stays unit-testable without Home
Assistant. Config: `ConfigEntry.runtime_data` (typed, no `hass.data`),
`PARALLEL_UPDATES = 0`, coordinator takes `config_entry=entry`. The
coordinator maps `PPLCZAuthError` → `ConfigEntryAuthFailed` and
`PPLCZApiError` → `UpdateFailed`; `aiohttp.ClientError` is not caught around
the whole update (the coordinator wraps that), but per-parcel events calls
are best-effort inside `api.py`/`coordinator._history_for`. Entities:
`has_entity_name` + `translation_key`, `icons.json`, translated units,
`_attr_attribution`, `_unrecorded_attributes` on anything with a parcel list
or `raw`. Over-redact diagnostics — they get pasted into public issues.

## Running tests

```
python -m pytest tests/ --cov=custom_components.ppl_cz
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing. A code change updates the README + this file + `docs/` in the same
commit; the API reference lives in this carrier's own directory in the private
`carrier-research/<slug>/api/`, never in this repo.
