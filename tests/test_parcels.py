"""Tests for the pure parcel-mapping helpers."""
from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.ppl_cz.parcels as parcels_mod
from custom_components.ppl_cz.const import (
    CAPABILITIES,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DIRECTION_INCOMING,
    DIRECTION_OUTGOING,
    DOMAIN,
    KNOWN_CAPABILITIES,
    ParcelStatus,
)
from custom_components.ppl_cz.parcels import (
    apply_delivered_filter,
    build_history,
    map_event_status,
    map_parcel_status,
    normalize_parcel,
    note_delivery_info_shape,
    note_events_shape,
    note_items_shape,
    parse_iso,
    shipment_direction,
    sort_parcels_by_ts,
    to_iso_timestamp,
)

from .payloads import (
    delivery_point,
    events_for_delivered,
    incoming_shipment,
    outgoing_shipment,
    shipment_event,
)

# --- status mapping ---------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected",
    [
        ("ORDER", ParcelStatus.REGISTERED),
        ("IN_TRANSPORT", ParcelStatus.IN_TRANSIT),
        ("DELIVERING", ParcelStatus.OUT_FOR_DELIVERY),
        ("PICKUP_POINT", ParcelStatus.AT_PICKUP_POINT),
        ("DELIVERED", ParcelStatus.DELIVERED),
        ("RETURNING", ParcelStatus.RETURNING),
        ("BACK_TO_SENDER", ParcelStatus.RETURNING),
        ("DELETED", ParcelStatus.PROBLEM),
        ("UNKNOWN", ParcelStatus.UNKNOWN),
    ],
)
def test_map_parcel_status_known(code, expected):
    assert map_parcel_status(code) == expected


def test_map_parcel_status_missing_and_unmapped():
    assert map_parcel_status(None) == ParcelStatus.UNKNOWN
    assert map_parcel_status("") == ParcelStatus.UNKNOWN
    assert map_parcel_status("TELEPORTED") == ParcelStatus.UNKNOWN


def test_map_event_status_missing_and_unmapped_are_none():
    assert map_event_status(None) is None
    assert map_event_status("SOMETHING_NEW") is None
    assert map_event_status("DELIVERED") == ParcelStatus.DELIVERED


def test_unmapped_status_warns_only_once(caplog):
    assert map_parcel_status("ABDUCTED") == ParcelStatus.UNKNOWN
    assert map_parcel_status("ABDUCTED") == ParcelStatus.UNKNOWN
    assert caplog.text.count("ABDUCTED") == 1
    assert "issues/new" in caplog.text


def test_pickup_point_band_does_not_conflate_with_delivered_parcelshop_code():
    """The PICKUP_POINT trap named in the build plan: a coarse `PICKUP_POINT`
    status (pending pickup) must not be confused with a `Delivered.Parcelshop`
    granular event code on an already-terminal DELIVERED shipment."""
    assert map_parcel_status("PICKUP_POINT") == ParcelStatus.AT_PICKUP_POINT
    assert map_parcel_status("DELIVERED") == ParcelStatus.DELIVERED
    # The granular code lives on an event's `code` field, never on
    # `lastShipmentEvent`/`event` — map_event_status only ever sees the
    # coarse code, so a granular "Delivered.Parcelshop" string would itself
    # be unmapped (and fall to None with a warning), never misread as
    # PICKUP_POINT.
    assert map_event_status("Delivered.Parcelshop") is None


# --- timestamp helpers ------------------------------------------------------


def test_parse_iso_handles_z_naive_and_garbage():
    assert parse_iso("2026-04-29T13:12:42Z").tzinfo is not None
    assert parse_iso("2026-04-29T13:12:42").tzinfo == timezone.utc
    assert parse_iso("not-a-date") is None
    assert parse_iso(None) is None


def test_to_iso_timestamp_passthrough_and_epoch():
    assert to_iso_timestamp("2026-04-29T13:12:42Z") == "2026-04-29T13:12:42Z"
    assert to_iso_timestamp(None) is None
    assert to_iso_timestamp(10**20) is None  # out of range -> None, never raises


# --- build_history ------------------------------------------------------


def test_build_history_orders_oldest_to_newest_and_prefers_code():
    history = build_history(events_for_delivered())
    assert len(history) == 4
    assert history[0]["status"] == ParcelStatus.REGISTERED
    assert history[0]["raw_status"] == "Active"  # granular `code`, not the message
    assert history[-1]["status"] == ParcelStatus.DELIVERED


def test_build_history_handles_missing_and_malformed():
    assert build_history(None) == []
    assert build_history([{"event": "ORDER"}]) == []  # no createdAt
    assert build_history(["not-a-dict"]) == []


def test_build_history_falls_back_to_message_then_event_code():
    history = build_history(
        [shipment_event("IN_TRANSPORT", "2026-04-28T10:00:00Z", message="On the way")]
    )
    assert history[0]["raw_status"] == "On the way"
    history = build_history(
        [shipment_event("IN_TRANSPORT", "2026-04-28T10:00:00Z")]
    )
    assert history[0]["raw_status"] == "IN_TRANSPORT"


# --- shipment_direction -------------------------------------------------


def test_direction_from_discriminator():
    assert shipment_direction({"discriminator": "Incoming"}) == DIRECTION_INCOMING
    assert shipment_direction({"discriminator": "Outgoing"}) == DIRECTION_OUTGOING


def test_direction_falls_back_to_subtype_fields():
    assert shipment_direction({"sender": "Shop"}) == DIRECTION_INCOMING
    assert shipment_direction({"recipient": "Jane"}) == DIRECTION_OUTGOING


def test_direction_unknown_warns_once_and_defaults_incoming(caplog):
    parcels_mod._unknown_direction_logged.clear()
    assert shipment_direction({"number": "X1"}) == DIRECTION_INCOMING
    assert shipment_direction({"number": "X1"}) == DIRECTION_INCOMING
    assert caplog.text.count("X1") == 1
    assert "issues/new" in caplog.text


# --- one-shot shape warnings ----------------------------------------------


def test_note_items_shape_fires_once_on_populated_list(caplog):
    parcels_mod._items_shape_logged = False
    note_items_shape([])  # empty -> no warning
    assert "shipment list" not in caplog.text
    note_items_shape([incoming_shipment()])
    note_items_shape([incoming_shipment()])
    assert caplog.text.count("shipment list") == 1
    assert "number: str" in caplog.text


def test_note_events_shape_fires_once_on_populated_list(caplog):
    parcels_mod._events_shape_logged = False
    note_events_shape([])
    assert "event history" not in caplog.text
    note_events_shape(events_for_delivered())
    note_events_shape(events_for_delivered())
    assert caplog.text.count("event history") == 1


def test_note_delivery_info_shape_fires_once_on_populated_response(caplog):
    parcels_mod._delivery_info_shape_logged = False
    note_delivery_info_shape({})  # empty -> no warning
    assert "deliveryInfo" not in caplog.text
    note_delivery_info_shape({"deliveryWindowFrom": "2026-09-01T09:00:00Z"})
    note_delivery_info_shape({"deliveryWindowFrom": "2026-09-01T09:00:00Z"})
    assert caplog.text.count("deliveryInfo") == 1
    assert "deliveryWindowFrom: str" in caplog.text


def test_delivery_point_type_warns_once(caplog):
    parcels_mod._delivery_point_type_logged = False
    normalize_parcel(
        incoming_shipment(to_delivery_point=delivery_point(point_type="PARCEL_SHOP"))
    )
    normalize_parcel(
        incoming_shipment(to_delivery_point=delivery_point(point_type="LOCKER"))
    )
    assert caplog.text.count("delivery-point type") == 1
    assert "PARCEL_SHOP" in caplog.text


# --- normalize_parcel -------------------------------------------------------

CANONICAL_KEYS = [
    "carrier", "barcode", "sender", "receiver", "status", "raw_status",
    "delivered", "delivered_at", "planned_from", "planned_to", "pickup",
    "pickup_point", "url", "weight", "dimensions", "history", "raw",
]


def test_normalize_publishes_exactly_the_canonical_keys():
    assert list(normalize_parcel(incoming_shipment())) == CANONICAL_KEYS


def test_normalize_incoming_in_transit_parcel():
    parcel = normalize_parcel(incoming_shipment(status="IN_TRANSPORT"))
    assert parcel["carrier"] == "PPL CZ"
    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert parcel["raw_status"] == "IN_TRANSPORT"
    assert parcel["sender"] == "Example Shop s.r.o."
    assert parcel["receiver"] is None
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None
    assert parcel["url"].endswith(incoming_shipment()["number"])


def test_normalize_outgoing_parcel_populates_receiver_only():
    parcel = normalize_parcel(outgoing_shipment())
    assert parcel["sender"] is None
    assert parcel["receiver"] == "Jane Doe"


def test_normalize_pickup_point_and_bool():
    raw = incoming_shipment(
        status="PICKUP_POINT", to_delivery_point=delivery_point(name="AlzaBox Wenceslas")
    )
    parcel = normalize_parcel(raw)
    assert parcel["status"] == ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "AlzaBox Wenceslas"


def test_normalize_delivered_without_history_has_no_delivered_at():
    """PPL CZ's list DTO carries no delivered timestamp at all — only the
    matching event's createdAt has it, so without history fetched the
    timestamp genuinely cannot be known."""
    parcel = normalize_parcel(incoming_shipment(status="DELIVERED"))
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] is None
    assert parcel["history"] is None


def test_normalize_delivered_with_history_finds_the_timestamp():
    history = build_history(events_for_delivered())
    parcel = normalize_parcel(incoming_shipment(status="DELIVERED"), history=history)
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-04-29T13:12:42Z"
    assert parcel["raw_status"] == "Delivered"  # the latest event's own code
    assert len(parcel["history"]) == 4


def test_normalize_active_parcel_ignores_history_delivered_timestamp():
    """Even with history present, a non-delivered parcel's delivered_at stays
    None — history here just supplies the more specific raw_status."""
    history = build_history(
        [shipment_event("IN_TRANSPORT", "2026-04-28T10:00:00Z", code="Active")]
    )
    parcel = normalize_parcel(incoming_shipment(status="IN_TRANSPORT"), history=history)
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None
    assert parcel["raw_status"] == "Active"


def test_normalize_curates_raw_fields():
    raw = incoming_shipment()
    parcel = normalize_parcel(raw)
    assert set(parcel["raw"]) == {
        "ownership", "cod", "phaseText", "discriminator", "codPaidStatus",
    }
    assert "toAddress" not in parcel["raw"]
    assert "toDeliveryPoint" not in parcel["raw"]
    assert "sender" not in parcel["raw"]


def test_normalize_history_none_when_not_fetched():
    parcel = normalize_parcel(incoming_shipment())
    assert parcel["history"] is None


# --- sort / filter -----------------------------------------------------------


def test_sort_parcels_ascending_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "planned_from": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "planned_from": None},
        {"barcode": "c", "planned_from": "2026-05-01T10:00:00Z"},
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["c", "a", "b"]


def _entry(filter_type: str, amount: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
        unique_id="1",
    )


def _delivered_pair() -> list[dict]:
    now = datetime.now(timezone.utc)
    return [
        {"barcode": "RECENT", "delivered_at": (now - timedelta(days=1)).isoformat()},
        {"barcode": "OLD", "delivered_at": (now - timedelta(days=30)).isoformat()},
    ]


def test_delivered_filter_by_days():
    kept = apply_delivered_filter(_delivered_pair(), _entry("days", 7))
    assert [p["barcode"] for p in kept] == ["RECENT"]


def test_delivered_filter_by_count():
    parcels = _delivered_pair()
    assert apply_delivered_filter(parcels, _entry("parcels", 1)) == parcels[:1]


def test_capabilities_are_known_values():
    """A typo here would silently misreport this carrier on the docs site."""
    assert CAPABILITIES <= KNOWN_CAPABILITIES


def test_capabilities_are_pickup_point_url_and_history():
    """The list DTOs carry no weight/dimensions/ETA at all."""
    assert CAPABILITIES == {"pickup_point", "url", "history"}
