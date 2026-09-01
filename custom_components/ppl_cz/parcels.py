"""Canonical parcel shape, status mapping and list helpers.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. That keeps the carrier-specific
mapping apart from the coordinator (which is nearly identical everywhere) and
makes the mapping trivially unit-testable without spinning up HA.

Every field this module reads off a raw PPL CZ shipment is documented as
``payload: reconstructed`` in the private mechanics doc — no account with a
real shipment was available at build time — so every lookup here is guarded
rather than trusted, and the one-shot warnings below exist specifically to
get a real payload's shape reported once a user hits one.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DIRECTION_INCOMING,
    DIRECTION_OUTGOING,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Where users report a status/shape we do not map yet.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-ppl-cz/issues/new"
    "?template=unrecognised_status.yml"
)

# The app's own coarse enum (ShipmentEventDto) — closed, all 9 values known
# (status_vocab: complete), shared between a shipment's lastShipmentEvent and
# each history event's own `event` field.
#
# PICKUP_POINT is the pending "awaiting pickup" state on a still-open
# shipment — do not conflate it with the granular Delivered.Parcelshop /
# DeliveredToPickupPoint event codes, which describe a mid-history hop on an
# already-terminal DELIVERED shipment (dropped at a locker, then collected).
_STATUS_MAP: dict[str, ParcelStatus] = {
    "ORDER": ParcelStatus.REGISTERED,
    "IN_TRANSPORT": ParcelStatus.IN_TRANSIT,
    "DELIVERING": ParcelStatus.OUT_FOR_DELIVERY,
    "PICKUP_POINT": ParcelStatus.AT_PICKUP_POINT,
    "DELIVERED": ParcelStatus.DELIVERED,
    "RETURNING": ParcelStatus.RETURNING,
    "BACK_TO_SENDER": ParcelStatus.RETURNING,
    "DELETED": ParcelStatus.PROBLEM,
    "UNKNOWN": ParcelStatus.UNKNOWN,
}

# Status codes we have already warned about, so each unmapped one is logged
# only once per HA session instead of on every poll.
_unmapped_statuses_logged: set[str] = set()

# One-shot pre-1.0 shape warnings (CONVENTIONS.md: unconfirmed shapes warn
# once, on structure only, never on values).
_items_shape_logged = False
_events_shape_logged = False
_delivery_info_shape_logged = False
_delivery_point_type_logged = False
_unknown_direction_logged: set[str] = set()


def _warn_unmapped_status(code: str) -> None:
    """Log an unmapped carrier status once, with a copy-paste issue link."""
    if code in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(code)
    _LOGGER.warning(
        "Unrecognised PPL CZ status — help us map it. Open an issue "
        "and paste this line: %s\n  status=%s → reported as 'unknown'",
        NEW_ISSUE_URL,
        code,
    )


def map_parcel_status(code: str | None) -> ParcelStatus:
    """Map a carrier status code to a canonical :class:`ParcelStatus`.

    ``None`` (a not-yet-scanned parcel) reports ``unknown`` silently; an
    unrecognised code reports ``unknown`` with a one-shot warning.
    """
    if not code:
        return ParcelStatus.UNKNOWN
    mapped = _STATUS_MAP.get(code)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(code)
    return ParcelStatus.UNKNOWN


def map_event_status(code: str | None) -> ParcelStatus | None:
    """Map a history entry's coarse ``event`` code to a canonical status.

    Unmapped codes keep ``status: null`` on the history entry (rather than
    ``unknown``, so a consumer can tell "no mapping" from "mapped to
    unknown") and warn once, reusing the parcel-status one-shot set.
    """
    if not code:
        return None
    mapped = _STATUS_MAP.get(code)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(code)
    return None


def _describe_shape(value: Any, prefix: str = "") -> list[str]:
    """Return ``path: type`` lines for a payload — structure only, no values.

    Lists are described from their first element (a representative sample,
    not every item) so the line count stays small on a long shipment list.
    """
    lines: list[str] = []
    if isinstance(value, dict):
        for key in sorted(value):
            path = f"{prefix}.{key}" if prefix else str(key)
            lines.extend(_describe_shape(value[key], path))
    elif isinstance(value, list):
        if value:
            lines.extend(_describe_shape(value[0], f"{prefix}[]"))
        else:
            lines.append(f"{prefix}: list (empty)")
    else:
        lines.append(f"{prefix}: {type(value).__name__}")
    return lines


def note_items_shape(items: list[dict]) -> None:
    """One-shot: report the shipment list item's shape the first time it's non-empty.

    The envelope (``metadata.pagination`` / ``items``) is already confirmed
    live; it's the *item* fields inside it that are still
    ``payload: reconstructed``.
    """
    global _items_shape_logged
    if _items_shape_logged or not items:
        return
    _items_shape_logged = True
    lines = _describe_shape(items[0])
    _LOGGER.warning(
        "PPL CZ returned a populated shipment list for the first time — "
        "its item shape was reconstructed from static analysis, not "
        "confirmed live. Please help us verify it (structure only, no "
        "values): %s\n  %s",
        NEW_ISSUE_URL,
        "; ".join(lines),
    )


def note_events_shape(events: list[dict]) -> None:
    """One-shot: report an event-history item's shape the first time it's non-empty."""
    global _events_shape_logged
    if _events_shape_logged or not events:
        return
    _events_shape_logged = True
    lines = _describe_shape(events[0])
    _LOGGER.warning(
        "PPL CZ returned a populated event history for the first time — "
        "this shape was reconstructed, not confirmed live. Please help us "
        "verify it (structure only, no values): %s\n  %s",
        NEW_ISSUE_URL,
        "; ".join(lines),
    )


def note_delivery_info_shape(payload: dict) -> None:
    """One-shot: report a populated ``deliveryInfo`` response's shape.

    This endpoint (``GET .../shipments/{id}/deliveryInfo``) was never called
    live before this probe — its existence came from static analysis only,
    and whether it actually carries a usable ETA/delivery window is an open
    question this warning exists to close. See PPL CZ's CLAUDE.md "No ETA,
    ever" note for why the integration otherwise has nothing to put in
    `planned_from`/`planned_to`.
    """
    global _delivery_info_shape_logged
    if _delivery_info_shape_logged or not payload:
        return
    _delivery_info_shape_logged = True
    lines = _describe_shape(payload)
    _LOGGER.warning(
        "PPL CZ returned a populated deliveryInfo response for the first "
        "time — this endpoint has never been seen live before and may carry "
        "a real delivery ETA/window. Please help us verify it (structure "
        "only, no values): %s\n  %s",
        NEW_ISSUE_URL,
        "; ".join(lines),
    )


def _note_delivery_point_type(point_type: str) -> None:
    """One-shot: report a toDeliveryPoint.type value the first time one is named.

    The enum's members were never enumerated during the teardown.
    """
    global _delivery_point_type_logged
    if _delivery_point_type_logged:
        return
    _delivery_point_type_logged = True
    _LOGGER.warning(
        "PPL CZ named a delivery-point type for the first time (%s) — this "
        "enum's members were never confirmed. Please help us verify it: %s",
        point_type,
        NEW_ISSUE_URL,
    )


def _warn_unknown_direction(identifier: Any) -> None:
    """One-shot per shipment: neither `discriminator` nor a subtype field matched."""
    key = str(identifier or "")
    if key in _unknown_direction_logged:
        return
    _unknown_direction_logged.add(key)
    _LOGGER.warning(
        "Could not tell whether a PPL CZ shipment is incoming or outgoing "
        "(id=%s) — defaulting to incoming. Please help us confirm the "
        "discriminator/field shape: %s",
        key,
        NEW_ISSUE_URL,
    )


def shipment_direction(raw: dict) -> str:
    """Return ``"incoming"`` or ``"outgoing"`` for one shipment list item.

    ``discriminator`` is checked first (case-insensitive substring match, its
    exact string values are unconfirmed); when it's missing or unrecognised,
    direction falls back to which subtype-only field is present (``sender``
    -> incoming, ``recipient`` -> outgoing). A shipment matching neither logs
    a one-shot warning and defaults to incoming rather than being silently
    dropped from every list.
    """
    discriminator = str(raw.get("discriminator") or "").lower()
    if "outgoing" in discriminator:
        return DIRECTION_OUTGOING
    if "incoming" in discriminator:
        return DIRECTION_INCOMING
    if "recipient" in raw:
        return DIRECTION_OUTGOING
    if "sender" in raw:
        return DIRECTION_INCOMING
    _warn_unknown_direction(raw.get("number") or raw.get("id"))
    return DIRECTION_INCOMING


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def to_iso_timestamp(value: Any) -> str | None:
    """Return an ISO 8601 string for an API timestamp field.

    Numbers are treated as **epoch milliseconds** — the common case for the
    consumer APIs in this suite. Strings pass through untouched; their
    consumers are guarded by :func:`parse_iso`.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return str(value)


def format_dimensions(
    length: float | None, width: float | None, height: float | None
) -> dict[str, Any] | None:
    """Return the canonical ``dimensions`` dict, or ``None`` when incomplete.

    Unused by :func:`normalize_parcel` below — PPL CZ's DTOs carry no
    dimensions at all — kept as suite-wide machinery for parity with every
    other carrier module.
    """
    if length is None or width is None or height is None:
        return None
    return {
        "length": length,
        "width": width,
        "height": height,
        "text": f"{int(length)} x {int(width)} x {int(height)} cm",
    }


def build_history(
    events: list | None, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from a shipment's ``/events`` response.

    Each entry is ``{timestamp, status, raw_status}``. ``status`` maps the
    coarse, required ``event`` field (the same 9-value enum as
    ``lastShipmentEvent``); ``raw_status`` prefers the granular ``code`` field
    (unconfirmed to be present — ``code: string?``), then the human
    ``message``, then the coarse ``event`` code as a last resort. Sorted
    oldest -> newest and capped to the most recent ``max_events``.
    """
    parseable: list[tuple[datetime, dict]] = []
    unparseable: list[dict] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        timestamp = to_iso_timestamp(event.get("createdAt"))
        if not timestamp:
            continue
        event_code = event.get("event")
        entry = {
            "timestamp": timestamp,
            "status": map_event_status(event_code),
            "raw_status": event.get("code") or event.get("message") or event_code,
        }
        parsed = parse_iso(timestamp)
        if parsed is None:
            unparseable.append(entry)
        else:
            parseable.append((parsed, entry))
    parseable.sort(key=lambda item: item[0])
    ordered = [entry for _, entry in parseable] + unparseable
    return ordered[-max_events:]


def tracking_url(tracking_code: str | None) -> str | None:
    """Construct the consumer tracking deep-link for a parcel."""
    if not tracking_code:
        return None
    return TRACKING_URL.format(tracking_code=tracking_code)


# Only these base-DTO fields are carried under `raw` — a deliberate, narrower
# set than "the whole payload": address/delivery-point blocks stay out of a
# plain sensor attribute (they're still redacted in diagnostics too, but this
# keeps them off the entity state altogether, not just off the public issue
# paste).
_RAW_FIELDS = (
    "ownership",
    "cod",
    "phaseText",
    "discriminator",
    "codPaidStatus",
    "isWaitingForSync",
)


def normalize_parcel(raw: dict, *, history: list[dict] | None = None) -> dict:
    """Return a carrier-agnostic parcel dict with curated fields under ``raw``.

    The **keys of the returned dict are the contract**: every carrier in the
    suite returns exactly these, in this order. Set a key to ``None`` when
    PPL CZ does not expose it — never omit it.

    ``history`` is the already-built canonical event list (see
    :func:`build_history`), fetched by the coordinator only when the opt-in
    history option is on — a per-parcel ``GET .../events`` call, not part of
    the list response. Two things key off it that would otherwise be
    unavailable:

    * ``delivered_at`` — the list response carries no delivered timestamp at
      all, only the coarse ``lastShipmentEvent`` flag; the actual instant
      lives on the matching event's ``createdAt``. Without history fetched,
      ``delivered_at`` stays ``None`` even for a delivered parcel — the same
      "can't supply it without a paid extra call" gap as weight/dimensions.
    * ``raw_status`` — prefers the latest event's own text (more specific)
      over the bare coarse code, when available.

    Direction (incoming vs outgoing) is not itself a canonical field — it
    only decides which of ``sender``/``receiver`` gets populated, mirroring
    how the coordinator splits the two lists.
    """
    tracking_code = raw.get("number")
    status_code = raw.get("lastShipmentEvent")
    status = map_parcel_status(status_code)
    delivered = status is ParcelStatus.DELIVERED

    raw_status = status_code
    delivered_at = None
    if history:
        latest = history[-1]
        raw_status = latest.get("raw_status") or raw_status
        delivered_event = next(
            (
                entry
                for entry in reversed(history)
                if entry.get("status") is ParcelStatus.DELIVERED
            ),
            None,
        )
        if delivered_event is not None:
            delivered_at = delivered_event.get("timestamp")
    if not delivered:
        delivered_at = None

    direction = shipment_direction(raw)
    sender = raw.get("sender") if direction == DIRECTION_INCOMING else None
    receiver = raw.get("recipient") if direction == DIRECTION_OUTGOING else None

    delivery_point = raw.get("toDeliveryPoint")
    pickup_point = None
    if isinstance(delivery_point, dict):
        point_type = delivery_point.get("type")
        if point_type:
            _note_delivery_point_type(point_type)
        pickup_point = delivery_point.get("name") or None

    raw_extra = {key: raw[key] for key in _RAW_FIELDS if key in raw}

    return {
        "carrier": "PPL CZ",
        "barcode": tracking_code,
        "sender": sender or None,
        "receiver": receiver or None,
        "status": status,
        "raw_status": raw_status,
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": None,
        "planned_to": None,
        "pickup": status is ParcelStatus.AT_PICKUP_POINT,
        "pickup_point": pickup_point,
        "url": tracking_url(tracking_code),
        "weight": None,
        "dimensions": None,
        "history": history if history is not None else None,
        "raw": raw_extra,
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(
        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
    )
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
