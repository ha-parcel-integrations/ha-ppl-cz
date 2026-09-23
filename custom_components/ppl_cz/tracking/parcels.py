"""Canonical parcel shape and status mapping for the tracking-code source.

Everything here is a **pure function** — no I/O, no Home Assistant objects
beyond the config entry's options. This is a separate vocabulary from the
account source's coarse 9-value enum: the website surface reports its own
granular ``events[].code`` / ``phase`` / ``lastEventCode`` codes, confirmed
live on two real parcels (``payload: confirmed`` for this surface), but the
maintainer-supplied code list is **incomplete, not closed** — an unmapped
code warns once and falls back to ``unknown``, the same as the account
source.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry

from ..const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_DIRECTION,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_DIRECTION,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-ppl-cz/issues/new"
    "?template=unrecognised_status.yml"
)

# The granular code vocabulary, confirmed live 2026-08-22/2026-09-23 on real
# parcels — a superset of the maintainer's original list. Explicitly
# incomplete, not closed: new codes are expected, not a sign of a bug.
#
# Trap: "Delivered.Parcelshop" / "DeliveredToPickupPoint" is NOT the pending
# "awaiting pickup" state. In the one real sample it is a mid-history event
# on an already-delivered shipment (dropped at a locker, then collected) —
# so both map to DELIVERED, never AT_PICKUP_POINT. The current vocabulary
# has no code observed for an *open* shipment sitting at a pickup point; a
# future one belongs here when it surfaces, not guessed at now.
_STATUS_MAP: dict[str, ParcelStatus] = {
    "WaitingForShipment": ParcelStatus.REGISTERED,
    "Active": ParcelStatus.REGISTERED,
    "PickedUpFromSender": ParcelStatus.IN_TRANSIT,
    "ShipmentInTransport": ParcelStatus.IN_TRANSIT,
    "ShipmentInTransport.TakeOverFromSender": ParcelStatus.IN_TRANSIT,
    "PreparingForDelivery": ParcelStatus.OUT_FOR_DELIVERY,
    "LoadingForDelivery": ParcelStatus.OUT_FOR_DELIVERY,
    "OutForDelivery": ParcelStatus.OUT_FOR_DELIVERY,
    "DeliveredToPickupPoint": ParcelStatus.DELIVERED,
    "Delivered": ParcelStatus.DELIVERED,
    "Delivered.Parcelshop": ParcelStatus.DELIVERED,
    "Delivered.BackToSender": ParcelStatus.DELIVERED,
    "BackToSender": ParcelStatus.RETURNING,
    "Canceled": ParcelStatus.PROBLEM,
    "Rejected": ParcelStatus.PROBLEM,
    "NotDelivered": ParcelStatus.PROBLEM,
}

_unmapped_statuses_logged: set[str] = set()


def _warn_unmapped_status(code: str) -> None:
    """Log an unmapped tracking status once, with a copy-paste issue link."""
    if code in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(code)
    _LOGGER.warning(
        "Unrecognised PPL CZ tracking status — help us map it. Open an "
        "issue and paste this line: %s\n  status=%s → reported as 'unknown'",
        NEW_ISSUE_URL,
        code,
    )


def map_parcel_status(code: str | None) -> ParcelStatus:
    """Map a granular tracking code to a canonical :class:`ParcelStatus`.

    ``None`` (nothing known yet, e.g. an untracked-but-added barcode) reports
    ``unknown`` silently; an unrecognised code reports ``unknown`` with a
    one-shot warning.
    """
    if not code:
        return ParcelStatus.UNKNOWN
    mapped = _STATUS_MAP.get(code)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(code)
    return ParcelStatus.UNKNOWN


def map_event_status(code: str | None) -> ParcelStatus | None:
    """Map one history entry's own code, same table as :func:`map_parcel_status`.

    Unmapped codes keep ``status: null`` (rather than ``unknown``, so a
    consumer can tell "no mapping" from "mapped to unknown") and warn once,
    reusing the same one-shot set.
    """
    if not code:
        return None
    mapped = _STATUS_MAP.get(code)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(code)
    return None


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing
    on a mixed set.
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
    """Return an ISO 8601 string for a tracking-payload timestamp field."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return str(value)


def build_history(
    events: list | None, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from the tracking payload's ``events``.

    Each entry is ``{code, eventDate, eventText}`` on the wire. Unlike the
    account source this arrives inline with the main lookup, at no extra
    request cost — still opt-in via ``CONF_INCLUDE_HISTORY``, for consistency
    across the suite (the bpost precedent: keep it off by default even when
    it costs nothing). Sorted oldest -> newest, capped at ``max_events``.
    """
    parseable: list[tuple[datetime, dict]] = []
    unparseable: list[dict] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        timestamp = to_iso_timestamp(event.get("eventDate"))
        if not timestamp:
            continue
        code = event.get("code")
        entry = {
            "timestamp": timestamp,
            "status": map_event_status(code),
            "raw_status": code or event.get("eventText"),
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


def _pickup_point_name(access_point: Any) -> str | None:
    """Return a display name for ``accessPoint``, richer than the account source's.

    ``accessPoint`` also carries ``gps``/``openHours``, which stay out of the
    canonical field (a plain string, like every other suite carrier) and out
    of ``raw`` (an address-adjacent location, not a status).
    """
    if not isinstance(access_point, dict):
        return None
    return access_point.get("parcelshopName") or access_point.get("name") or None


# Curated subset of the tracking payload kept under `raw` — mirrors the
# account source's own curated allowlist rather than passing the payload
# through verbatim. `addresses` (full sender/receiver address blocks) and
# `accessPoint`'s own contents (already summarised into `pickup_point`) stay
# out of a plain sensor attribute.
_RAW_FIELDS = (
    "phase",
    "lastEventCode",
    "lastEventText",
    "cod",
    "packagesInSet",
    "isBackToSender",
    "hierarchy",
)


def normalize_parcel(
    raw: dict, *, tracking_code: str, include_history: bool = False
) -> dict:
    """Return a carrier-agnostic parcel dict for one tracking-by-number result.

    ``tracking_code`` is the barcode the user entered, not read from ``raw``
    — the deep link and the sensor identity need it even for a not-yet-found
    code, for which ``raw`` is an empty dict (every lookup below already
    tolerates that). This does not itself decide incoming vs outgoing — the
    website payload carries no ``discriminator`` and no sender/recipient
    split, so it cannot be inferred the way ``shipment_direction()``
    (account-source only) infers it. The coordinator splits by the
    user-declared direction instead (see :func:`tracked_direction`).
    """
    status_code = raw.get("phase") or raw.get("lastEventCode")
    status = map_parcel_status(status_code)
    delivered = status is ParcelStatus.DELIVERED

    events = raw.get("events")
    history = build_history(events) if include_history else None

    delivered_at = to_iso_timestamp(raw.get("lastEventDate")) if delivered else None
    planned_from = None if delivered else to_iso_timestamp(raw.get("expectedDeliveryDate"))

    addresses = raw.get("addresses")
    sender = None
    if isinstance(addresses, list):
        sender = next(
            (
                a.get("name")
                for a in addresses
                if isinstance(a, dict) and a.get("type") == "SENDER" and a.get("name")
            ),
            None,
        )

    raw_extra = {key: raw[key] for key in _RAW_FIELDS if key in raw}

    return {
        "carrier": "PPL CZ",
        "barcode": tracking_code,
        "sender": sender,
        "receiver": None,
        "status": status,
        "raw_status": status_code,
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": planned_from,
        "planned_to": None,
        "pickup": status is ParcelStatus.AT_PICKUP_POINT,
        "pickup_point": _pickup_point_name(raw.get("accessPoint")),
        "url": tracking_url(tracking_code),
        "weight": raw.get("weight"),
        "dimensions": None,
        "history": history,
        "raw": raw_extra,
    }


def tracked_direction(item: dict) -> str:
    """Return the declared direction of one tracked-parcel options entry.

    Entries stored before ``CONF_DIRECTION`` existed carry no ``direction``
    key, so they default to incoming rather than forcing an options
    migration for a single field.
    """
    return item.get(CONF_DIRECTION) or DEFAULT_DIRECTION


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing
    or unparseable always sort to the end, regardless of ``descending``.
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

    ``parcels`` must already be sorted newest-first. ``days`` keeps
    deliveries from the last N days (an unparseable ``delivered_at`` is kept
    rather than silently dropped); the ``parcels`` type keeps the N most
    recent. Parcels stay *tracked* either way — this only controls what the
    delivered sensor shows.
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
