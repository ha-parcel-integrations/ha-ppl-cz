"""Tests for the tracking-code source's status map and normalizer."""
from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ppl_cz.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_DIRECTION,
    DIRECTION_INCOMING,
    DIRECTION_OUTGOING,
    DOMAIN,
    ParcelStatus,
)
from custom_components.ppl_cz.tracking.parcels import (
    apply_delivered_filter,
    build_history,
    map_event_status,
    map_parcel_status,
    normalize_parcel,
    parse_iso,
    sort_parcels_by_ts,
    to_iso_timestamp,
    tracked_direction,
    tracking_url,
)

from .payloads import (
    TRACKING_CODE,
    tracking_access_point,
    tracking_event,
    tracking_events_for_delivered,
    tracking_shipment,
)

# --- status map --------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("WaitingForShipment", ParcelStatus.REGISTERED),
        ("Active", ParcelStatus.REGISTERED),
        ("PickedUpFromSender", ParcelStatus.IN_TRANSIT),
        ("ShipmentInTransport", ParcelStatus.IN_TRANSIT),
        ("ShipmentInTransport.TakeOverFromSender", ParcelStatus.IN_TRANSIT),
        ("PreparingForDelivery", ParcelStatus.OUT_FOR_DELIVERY),
        ("LoadingForDelivery", ParcelStatus.OUT_FOR_DELIVERY),
        ("OutForDelivery", ParcelStatus.OUT_FOR_DELIVERY),
        ("Delivered", ParcelStatus.DELIVERED),
        ("BackToSender", ParcelStatus.RETURNING),
        ("Canceled", ParcelStatus.PROBLEM),
        ("Rejected", ParcelStatus.PROBLEM),
        ("NotDelivered", ParcelStatus.PROBLEM),
    ],
)
def test_status_map(code, expected):
    assert map_parcel_status(code) == expected


def test_delivered_parcelshop_is_delivered_not_awaiting_pickup():
    """The band-ordering trap the plan names explicitly."""
    assert map_parcel_status("Delivered.Parcelshop") == ParcelStatus.DELIVERED


def test_delivered_to_pickup_point_is_delivered_not_awaiting_pickup():
    assert map_parcel_status("DeliveredToPickupPoint") == ParcelStatus.DELIVERED


def test_delivered_back_to_sender_is_delivered():
    assert map_parcel_status("Delivered.BackToSender") == ParcelStatus.DELIVERED


def test_none_code_is_silently_unknown(caplog):
    assert map_parcel_status(None) == ParcelStatus.UNKNOWN
    assert "Unrecognised" not in caplog.text


def test_unmapped_code_warns_once(caplog):
    assert map_parcel_status("SomeNewCode") == ParcelStatus.UNKNOWN
    assert map_parcel_status("SomeNewCode") == ParcelStatus.UNKNOWN
    assert caplog.text.count("SomeNewCode") == 1


def test_map_event_status_unmapped_is_null_not_unknown():
    assert map_event_status("SomeNewCode") is None


def test_map_event_status_none_is_null():
    assert map_event_status(None) is None


def test_map_event_status_mapped():
    assert map_event_status("Delivered") == ParcelStatus.DELIVERED


# --- build_history -------------------------------------------------------


def test_build_history_sorts_oldest_to_newest():
    events = tracking_events_for_delivered()
    history = build_history(events)
    assert [e["timestamp"] for e in history] == sorted(e["timestamp"] for e in history)
    assert history[-1]["status"] == ParcelStatus.DELIVERED


def test_build_history_skips_events_without_a_timestamp():
    events = [tracking_event("Delivered", None)]
    assert build_history(events) == []


def test_build_history_ignores_non_dict_entries():
    assert build_history(["not-a-dict"]) == []


def test_build_history_raw_status_prefers_code_over_text():
    events = [tracking_event("Delivered", "2026-05-02T09:05:00Z", event_text="Delivered!")]
    history = build_history(events)
    assert history[0]["raw_status"] == "Delivered"


def test_build_history_caps_at_max_events():
    events = [
        tracking_event("ShipmentInTransport", f"2026-05-0{i}T00:00:00Z")
        for i in range(1, 6)
    ]
    history = build_history(events, max_events=2)
    assert len(history) == 2


# --- normalize_parcel ------------------------------------------------------


def test_normalize_parcel_in_transit():
    raw = tracking_shipment(phase="ShipmentInTransport")
    parcel = normalize_parcel(raw, tracking_code=TRACKING_CODE)
    assert parcel["barcode"] == TRACKING_CODE
    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None
    assert parcel["carrier"] == "PPL CZ"


def test_normalize_parcel_delivered_sets_delivered_at_from_last_event_date():
    events = tracking_events_for_delivered()
    raw = tracking_shipment(phase="Delivered", events=events)
    parcel = normalize_parcel(raw, tracking_code=TRACKING_CODE)
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == events[-1]["eventDate"]
    # A delivered parcel never carries a planned_from — it already arrived.
    assert parcel["planned_from"] is None


def test_normalize_parcel_active_parcel_gets_expected_delivery_date():
    raw = tracking_shipment(
        phase="OutForDelivery", expected_delivery_date="2026-05-03T00:00:00Z"
    )
    parcel = normalize_parcel(raw, tracking_code=TRACKING_CODE)
    assert parcel["planned_from"] == "2026-05-03T00:00:00Z"
    assert parcel["planned_to"] is None


def test_normalize_parcel_weight_passes_through_unconverted():
    raw = tracking_shipment(weight=2.5)
    parcel = normalize_parcel(raw, tracking_code=TRACKING_CODE)
    assert parcel["weight"] == 2.5
    assert parcel["dimensions"] is None


def test_normalize_parcel_pickup_point_prefers_parcelshop_name():
    raw = tracking_shipment(access_point=tracking_access_point(name="Locker 1"))
    parcel = normalize_parcel(raw, tracking_code=TRACKING_CODE)
    assert parcel["pickup_point"] == "Locker 1"


def test_normalize_parcel_no_access_point_leaves_pickup_point_none():
    raw = tracking_shipment(access_point=None)
    parcel = normalize_parcel(raw, tracking_code=TRACKING_CODE)
    assert parcel["pickup_point"] is None
    assert parcel["pickup"] is False


def test_normalize_parcel_not_found_placeholder_is_unknown():
    parcel = normalize_parcel({}, tracking_code=TRACKING_CODE)
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["barcode"] == TRACKING_CODE
    assert parcel["url"] is not None


def test_normalize_parcel_history_is_none_when_not_requested():
    raw = tracking_shipment(events=tracking_events_for_delivered())
    parcel = normalize_parcel(raw, tracking_code=TRACKING_CODE, include_history=False)
    assert parcel["history"] is None


def test_normalize_parcel_history_populated_when_requested():
    raw = tracking_shipment(events=tracking_events_for_delivered())
    parcel = normalize_parcel(raw, tracking_code=TRACKING_CODE, include_history=True)
    assert parcel["history"]
    assert len(parcel["history"]) == len(tracking_events_for_delivered())


def test_normalize_parcel_raw_is_curated_not_verbatim():
    raw = tracking_shipment(access_point=tracking_access_point())
    parcel = normalize_parcel(raw, tracking_code=TRACKING_CODE)
    assert "addresses" not in parcel["raw"]
    assert "accessPoint" not in parcel["raw"]
    assert parcel["raw"]["phase"] == raw["phase"]


def test_normalize_parcel_sender_from_addresses():
    raw = tracking_shipment()
    parcel = normalize_parcel(raw, tracking_code=TRACKING_CODE)
    assert parcel["sender"] == "Example Sender"
    assert parcel["receiver"] is None


def test_normalize_parcel_missing_addresses_leaves_sender_none():
    raw = tracking_shipment()
    raw["addresses"] = None
    parcel = normalize_parcel(raw, tracking_code=TRACKING_CODE)
    assert parcel["sender"] is None


# --- parse_iso / to_iso_timestamp / tracking_url ----------------------------


def test_parse_iso_garbage_returns_none():
    assert parse_iso("not-a-timestamp") is None


def test_parse_iso_none_returns_none():
    assert parse_iso(None) is None


def test_parse_iso_naive_value_treated_as_utc():
    parsed = parse_iso("2026-05-02T09:05:00")
    assert parsed.tzinfo is timezone.utc


def test_to_iso_timestamp_epoch_milliseconds():
    assert to_iso_timestamp(0) == datetime(1970, 1, 1, tzinfo=timezone.utc).isoformat()


def test_to_iso_timestamp_overflow_returns_none():
    assert to_iso_timestamp(10**20) is None


def test_to_iso_timestamp_none_returns_none():
    assert to_iso_timestamp(None) is None


def test_tracking_url_none_code_returns_none():
    assert tracking_url(None) is None


def test_build_history_keeps_an_unparseable_timestamp_entry():
    events = [tracking_event("Delivered", "2026-05-02T09:05:00Z")]
    # Force the timestamp to look unparseable to parse_iso() while still
    # passing to_iso_timestamp()'s own (permissive) truthiness check.
    events[0]["eventDate"] = "garbage-but-truthy"
    history = build_history(events)
    assert history[0]["timestamp"] == "garbage-but-truthy"


# --- tracked_direction -----------------------------------------------------


def test_tracked_direction_explicit_incoming():
    assert tracked_direction({CONF_DIRECTION: DIRECTION_INCOMING}) == DIRECTION_INCOMING


def test_tracked_direction_explicit_outgoing():
    assert tracked_direction({CONF_DIRECTION: DIRECTION_OUTGOING}) == DIRECTION_OUTGOING


def test_tracked_direction_missing_key_defaults_to_incoming():
    """An entry written before CONF_DIRECTION existed needs no migration."""
    assert tracked_direction({}) == DIRECTION_INCOMING


# --- sort / delivered filter (shared shape with the account source) --------


def test_sort_parcels_by_ts_missing_timestamp_sorts_last():
    parcels = [{"planned_from": None}, {"planned_from": "2026-01-01T00:00:00Z"}]
    sorted_parcels = sort_parcels_by_ts(parcels, "planned_from")
    assert sorted_parcels[0]["planned_from"] == "2026-01-01T00:00:00Z"
    assert sorted_parcels[1]["planned_from"] is None


def _entry(filter_type: str, amount: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
    )


def test_delivered_filter_by_days():
    now = datetime.now(timezone.utc)
    parcels = [
        {"barcode": "RECENT", "delivered_at": (now - timedelta(days=1)).isoformat()},
        {"barcode": "OLD", "delivered_at": (now - timedelta(days=30)).isoformat()},
    ]
    kept = apply_delivered_filter(parcels, _entry("days", 7))
    assert [p["barcode"] for p in kept] == ["RECENT"]


def test_delivered_filter_by_count():
    parcels = [{"barcode": "A"}, {"barcode": "B"}]
    assert apply_delivered_filter(parcels, _entry("parcels", 1)) == parcels[:1]
