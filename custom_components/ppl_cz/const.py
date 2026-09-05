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


# No Platform.CALENDAR: the mobile-app inbox never carries an ETA (no
# planned_from/planned_to source in the DTOs), so a calendar entity would
# always be empty. Same call vinted-go made for the same reason.
PLATFORMS = [Platform.BUTTON, Platform.SENSOR]

# Every optional key the parcel contract defines. CAPABILITIES below must be a
# subset of this — it exists so a typo in CAPABILITIES fails a test instead of
# silently dropping a carrier off a table on the docs site.
KNOWN_CAPABILITIES = frozenset(
    {"weight", "dimensions", "delivery_window", "pickup_point", "url", "history"}
)

# PPL CZ's list DTOs (ShipmentResponseBaseDto + Incoming/OutgoingShipmentResponseDto)
# carry no weight, dimensions or ETA field at all — those stay None in
# normalize_parcel() unconditionally, not just "usually empty".
CAPABILITIES = frozenset({"pickup_point", "url", "history"})

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

# Human-facing deep link surfaced on each parcel's `url` field. Not fetched by
# this integration — the page itself needs a per-request reCAPTCHA v3 token
# generated client-side, so it is a "view online" link only.
TRACKING_URL = "https://www.ppl.cz/vyhledat-zasilku?shipmentId={tracking_code}"

# Direction of one shipment list item, derived in parcels.py from the
# (unconfirmed) `discriminator` field / subtype-only fields. Maps to the
# suite's incoming / outgoing.
DIRECTION_INCOMING = "incoming"
DIRECTION_OUTGOING = "outgoing"

# --- Config entry data --------------------------------------------------------
CONF_EMAIL = "email"
CONF_ACCESS_TOKEN = "access_token"
# ISO timestamp computed at token-fetch time (now + expires_in) rather than the
# raw expires_in itself — expires_in alone is useless across a restart without
# an anchor.
CONF_TOKEN_EXPIRES_AT = "token_expires_at"

# --- Options -----------------------------------------------------------------
# Delivered-parcels retention: keep delivered parcels visible for the last N
# days, or keep only the N most recent — identical across the suite.
CONF_DELIVERED_FILTER_TYPE = "delivered_filter_type"
CONF_DELIVERED_FILTER_AMOUNT = "delivered_filter_amount"
DEFAULT_DELIVERED_FILTER_TYPE = "days"
DEFAULT_DELIVERED_FILTER_AMOUNT = 7

# Refresh interval (minutes) controls how often the coordinator polls the
# carrier. No rate limiting was observed in the (static + one live login)
# analysis, but polling itself was never exercised live — treat 30 min as a
# starting assumption, not a settled fact.
#
# Deliberate divergence from the HA Core rule that polling intervals are not
# user-configurable: that rule targets core integrations, and in a HACS parcel
# tracker a tunable cadence is a wanted feature.
CONF_REFRESH_INTERVAL = "refresh_interval"
REFRESH_INTERVAL_AUTO = "auto"
REFRESH_INTERVAL_OPTIONS = (15, 30, 60, 120, 240)
DEFAULT_REFRESH_INTERVAL = 30  # minutes — default for entries that predate "auto"
# New config entries default to "auto" (dynamic-polling rollout, Phase 1); an
# existing entry keeps whatever it already has, numeric or "auto".
DEFAULT_NEW_REFRESH_INTERVAL = REFRESH_INTERVAL_AUTO

# Dynamic, status-driven polling — selected via "auto" above. PPL CZ's
# list DTOs carry no ETA at all
# (see CAPABILITIES above), so the "1h before planned_from" lookahead never
# has a value to compare against — an out_for_delivery parcel always jumps
# straight to the hot tier here, the same "planned_from always None" shape
# ha-quickpac/ha-sameday/ha-sunyou hit on their own conversions.
#
# Quiet window: no polling between these local hours except the two anchors
# below, for overnight / end-of-day catch-up.
QUIET_WINDOW_START_HOUR = 0
QUIET_WINDOW_END_HOUR = 6

# Cadence while polling is active (minutes). Hot = at least one active
# incoming or outgoing parcel is out_for_delivery within HOT_LOOKAHEAD_HOURS
# of its planned_from (or has no planned_from at all, always true here); mid
# = anything else still in flight, or nothing tracked at all. This is an
# account-based coordinator (Section 2.2), so it never
# fully stops — the mid-tier poll is also how a new shipment gets
# discovered, since a single account call is the only way to see one that
# appeared without going through this integration.
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
