"""Constants for the PPL CZ parcel tracker integration."""
from enum import StrEnum

from homeassistant.const import Platform

DOMAIN = "ppl_cz"


class ParcelStatus(StrEnum):
    """Carrier-agnostic parcel status.

    **Do not extend or rename these members.** Every integration in the parcel
    suite publishes exactly this vocabulary on the ``status`` field of each
    normalised parcel, so cross-carrier automations and the aggregator can
    target ``status: out_for_delivery`` regardless of carrier. Listed in
    roughly the order a parcel moves through.
    """

    REGISTERED = "registered"               # Sender announced the parcel; not handed over yet
    IN_TRANSIT = "in_transit"               # In the carrier's network
    OUT_FOR_DELIVERY = "out_for_delivery"   # On a delivery vehicle today
    AT_PICKUP_POINT = "at_pickup_point"     # Ready to collect at a pickup location
    DELIVERED = "delivered"                 # Handed over
    RETURNING = "returning"                 # Failed delivery, going back to sender
    PROBLEM = "problem"                     # Carrier reports an exception/issue
    UNKNOWN = "unknown"                     # Raw status we have not mapped yet


# Two independently configured sources, chosen at setup (config_flow.py's
# menu) and stored in entry.data[CONF_SOURCE]. A pre-0.12.0 entry predates
# this key entirely — it was an account entry before a second source
# existed, so a missing CONF_SOURCE must always default to SOURCE_ACCOUNT.
# Getting this backwards silently converts every existing user's account hub
# into an empty tracking hub on upgrade; every entry.data.get(CONF_SOURCE, …)
# in this repo takes SOURCE_ACCOUNT as its fallback.
CONF_SOURCE = "source"
SOURCE_ACCOUNT = "account"
SOURCE_TRACKING = "tracking"

# CAPABILITIES and PLATFORMS are per-source, not a union with None's: a
# capability the chosen source cannot deliver is a wrong claim on the
# docs-site comparison table, and an account hub must never get a
# permanently-empty calendar entity. async_forward_entry_setups/
# async_unload_platforms must always use PLATFORMS[source].
#
# Account: the mobile-app inbox never carries an ETA (no planned_from/
# planned_to source in the DTOs), so no calendar entity — the same call
# vinted-go made for the same reason. Tracking: expectedDeliveryDate is real,
# so it gets Platform.CALENDAR.
PLATFORMS = {
    SOURCE_ACCOUNT: [Platform.BUTTON, Platform.SENSOR],
    SOURCE_TRACKING: [Platform.BUTTON, Platform.CALENDAR, Platform.SENSOR],
}

# Every optional key the parcel contract defines. Every value in CAPABILITIES
# below must be a subset of this — it exists so a typo fails a test instead
# of silently dropping a carrier off a table on the docs site.
KNOWN_CAPABILITIES = frozenset(
    {"weight", "dimensions", "delivery_window", "pickup_point", "url", "history"}
)

# Named CAPABILITIES_BY_VARIANT, not a CONF_SOURCE-keyed dict, because the
# docs site's generator (ha-parcel-integrations.github.io/scripts/generate.py)
# regex-parses this exact constant name for a multi-backend carrier — see
# bpost's own const.py for the precedent this follows. Variant labels are
# human-readable strings (matching bpost's "Tracking"/"Account"), not the
# internal SOURCE_* values. CAPABILITIES stays as a flat alias to the
# account variant, for any docs-site consumer still expecting a single set.
#
# Account: PPL CZ's list DTOs (ShipmentResponseBaseDto +
# Incoming/OutgoingShipmentResponseDto) carry no weight, dimensions or ETA
# field at all — those stay None in normalize_parcel() unconditionally, not
# just "usually empty". Tracking: the website payload adds weight (kg,
# already metric) and a real expectedDeliveryDate; still no dimensions.
CAPABILITIES_BY_VARIANT = {
    "Account": frozenset({"pickup_point", "url", "history"}),
    "Tracking": frozenset({"weight", "delivery_window", "pickup_point", "url", "history"}),
}
CAPABILITIES = CAPABILITIES_BY_VARIANT["Account"]

# --- mojePPL account API (api.dhl.com/ecs/ppl/mobapp) ------------------------
#
# Passwordless email+PIN login, three calls:
#   1. POST registrations       {email, deviceId, registrationSessionId} -> 204
#   2. PUT  registrations/{id}  {pin, deviceId} -> {password}  (a one-time
#      Azure AD B2C password minted for this login)
#   3. Azure B2C ROPC token exchange (grant_type=password) -> access token
# Then GET /api/v2/me/shipments?shipment_type=ALL (Bearer + dhl-api-key) returns
# both incoming and outgoing shipments in one call — no separate "sent" endpoint.
#
# Step 3 is also how a stale access token gets renewed — re-run the same
# password grant with the stored PIN-exchange password, the same way the app
# itself does. There is no refresh-token step: PPL's B2C tenant hard-revokes
# the whole token lineage ~1h after the original login regardless of
# intervening refreshes, and the app never sends grant_type=refresh_token at
# all.
#
# The password is the account's single credential, not this device's: the ROPC
# grant's username is the plain e-mail address, and step 2 mints a fresh
# password for that one identity every time anyone runs the login. So a login
# in the mobile app rotates the password stored here out from under us (and a
# login here does the same to the app) — see CLAUDE.md, "One credential per
# account".
API_BASE = "https://api.dhl.com/ecs/ppl/mobapp"
REGISTRATIONS_URL = f"{API_BASE}/api/v1/registrations"
REGISTRATION_CONFIRM_URL = f"{API_BASE}/api/v1/registrations/{{registration_session_id}}"
SHIPMENTS_URL = f"{API_BASE}/api/v2/me/shipments?shipment_type=ALL"
SHIPMENT_EVENTS_URL = f"{API_BASE}/api/v1/me/shipments/{{shipment_id}}/events"
SHIPMENT_DELIVERY_INFO_URL = f"{API_BASE}/api/v1/me/shipments/{{shipment_id}}/deliveryInfo"

# Every /mobapp call also carries this static, shared key — identical across
# every install of the mojePPL app, not a per-user or per-device credential.
# Shipping an extracted shared secret is normally refused by this suite's
# standing ruling (the reason bpost and the three UK carriers were passed on);
# the maintainer reviewed this one specifically and ruled it an accepted risk
# for PPL CZ (2026-08-22). Do not treat this as a precedent for a future
# carrier — get a fresh ruling each time.
DHL_API_KEY_HEADER = "dhl-api-key"
DHL_API_KEY = "G83gXcEfTws2hTUbWEreWFor5SdOj5QR"

# Azure AD B2C ROPC token endpoint. Unrelated to api.dhl.com — no dhl-api-key
# header, this host is pure Azure.
AZURE_TOKEN_URL = (
    "https://PPLCZMobIdentity.b2clogin.com/PPLCZMobIdentity.onmicrosoft.com/"
    "B2C_1A_ROPC_AUTH/oauth2/v2.0/token"
)
AZURE_CLIENT_ID = "e8286178-1efe-4e0a-8cb8-98f126391a3c"
AZURE_SCOPE = f"openid {AZURE_CLIENT_ID} offline_access"

# The only two `error` values on a 400 that mean "this credential is dead" and
# so justify dragging the user through reauth. Any other 400 is something
# else going wrong at Azure's end and gets retried instead — the mojePPL app
# draws the same line, logging out on access_denied and on nothing else.
AZURE_CREDENTIAL_REJECTED_ERRORS = frozenset({"access_denied", "invalid_grant"})

# Human-facing deep link surfaced on each parcel's `url` field. Not fetched by
# this integration — the page itself needs a per-request reCAPTCHA v3 token
# generated client-side, so it is a "view online" link only.
TRACKING_URL = "https://www.ppl.cz/vyhledat-zasilku?shipmentId={tracking_code}"

# Direction of one shipment list item. Account source: derived in
# account/parcels.py from the (unconfirmed) `discriminator` field /
# subtype-only fields. Tracking source: declared by the user per barcode
# (CONF_DIRECTION below) — the website payload has no discriminator and no
# sender/recipient split, and with no account there is no identity to
# compare a party against, so it cannot be inferred the way the account
# source infers it.
DIRECTION_INCOMING = "incoming"
DIRECTION_OUTGOING = "outgoing"

# Tracking hub only: the direction the user declared for one tracked
# barcode, stored alongside CONF_BARCODE in each CONF_PARCELS entry. An
# entry written before this key existed carries none, so it must default to
# incoming rather than forcing an options migration for a single field.
CONF_DIRECTION = "direction"
DEFAULT_DIRECTION = DIRECTION_INCOMING

# --- website tracking-by-number source (api.dhl.com/ecs/ppl/webapi) --------
#
# POST TrackAndTrace/<shipmentId> with an empty JSON body ({} — required, or
# the endpoint answers 415 UnsupportedMediaType, not a 400) and a static
# dhl-api-key header, no reCAPTCHA/cookie/Origin/Referer needed. Not-found is
# a 400 with detail == TRACKING_NOT_FOUND_DETAIL — match on that field, never
# on the status alone, since a real 400 (malformed request, key rejected) is
# otherwise indistinguishable. Confirmed live 2026-09-23 on two real parcels.
TRACKING_API_URL = "https://api.dhl.com/ecs/ppl/webapi/TrackAndTrace/{tracking_code}"
TRACKING_NOT_FOUND_DETAIL = "service.TrackAndTrace.ShipmentNotFound"

# This key is a *second* extracted shared secret, distinct from DHL_API_KEY
# above — a public web bundle, not the mobile app, so its rotation risk
# differs (no app release to signal a redeploy). Transport compatibility
# material, never a user credential: do not expose it in UI, diagnostics or
# log messages. A rejected key is a compatibility failure needing a new
# release, never a user-facing reauth — this route has no credential at all,
# so nothing can expire. Its own accepted-risk ruling, separate from
# DHL_API_KEY's (maintainer, 2026-09-23) — do not treat either as a
# precedent for a future carrier.
TRACKING_DHL_API_KEY = "7HH634Q79Zpge4xEGeFAHXAnUMRxv0XQ"

# --- Config entry data --------------------------------------------------------
CONF_EMAIL = "email"
CONF_ACCESS_TOKEN = "access_token"
# ISO timestamp computed at token-fetch time (now + expires_in) rather than the
# raw expires_in itself — expires_in alone is useless across a restart without
# an anchor.
CONF_TOKEN_EXPIRES_AT = "token_expires_at"

# Tracking hub only: the user-entered barcodes, stored in the config entry
# options as a list of {barcode} dicts — mirrors bpost's tracking source.
CONF_PARCELS = "parcels"
CONF_BARCODE = "barcode"

# --- Options -----------------------------------------------------------------
# Delivered-parcels retention: keep delivered parcels visible for the last N
# days, or keep only the N most recent — identical across the suite.
CONF_DELIVERED_FILTER_TYPE = "delivered_filter_type"
CONF_DELIVERED_FILTER_AMOUNT = "delivered_filter_amount"
DEFAULT_DELIVERED_FILTER_TYPE = "days"
DEFAULT_DELIVERED_FILTER_AMOUNT = 7

# Dynamic, status-driven polling — unconditional, no user-facing interval
# option, shared by both sources' coordinators. No rate limiting was observed
# on either surface (static + one live account login; four hand-run tracking
# calls), but neither was ever exercised under real polling load — treat
# both as unmeasured, not confirmed safe. The account DTOs carry no ETA at
# all (see CAPABILITIES above), so the "1h before planned_from" lookahead
# never has a value to compare against there — an out_for_delivery parcel
# always jumps straight to the hot tier, the same "planned_from always None"
# shape ha-quickpac/ha-sameday/ha-sunyou hit on their own conversions. The
# tracking source has a real expectedDeliveryDate, so its lookahead can fire.
#
# Quiet window: no polling between these local hours except the two anchors
# below, for overnight / end-of-day catch-up.
QUIET_WINDOW_START_HOUR = 0
QUIET_WINDOW_END_HOUR = 6

# Cadence while polling is active (minutes). Hot = at least one active
# parcel is out_for_delivery within HOT_LOOKAHEAD_HOURS of its planned_from
# (or has no planned_from at all); mid = anything else still in flight, or
# nothing tracked at all. The account coordinator never fully stops — the
# mid-tier poll is also how a new shipment gets discovered, since a single
# account call is the only way to see one that appeared without going
# through this integration. The tracking coordinator has no such discovery
# concern (the user adds barcodes explicitly) and may suspend entirely, like
# bpost's/dragonfly's barcode-based model.
HOT_INTERVAL_MINUTES = 15
MID_INTERVAL_MINUTES = 45
HOT_LOOKAHEAD_HOURS = 1

# Small, stable per-install offset added to every computed interval so
# different installs don't all hit an anchor or tier boundary at the same
# second. Deterministic (hash of the config entry id), not random.
STAGGER_MINUTES = 7

# Per-parcel status history is opt-in and off by default, identical across the
# suite. Keep it off by default: it is a large attribute, and PPL CZ needs a
# second call per parcel (GET .../events) to populate it.
CONF_INCLUDE_HISTORY = "include_history"
DEFAULT_INCLUDE_HISTORY = False

# Cap each parcel's history to the most recent N events so the attribute stays
# well under HA's ~16 KB state-attribute limit.
HISTORY_MAX_EVENTS = 20
