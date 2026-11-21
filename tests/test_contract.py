"""The parcel contract's checks, run over every normalised sample."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.ppl_cz.account.parcels import (
    build_history as account_build_history,
    normalize_parcel as normalize_account,
)
from custom_components.ppl_cz.const import ParcelStatus
from custom_components.ppl_cz.parcel_contract import planned_window
from custom_components.ppl_cz.tracking.parcels import (
    normalize_parcel as normalize_tracking,
    parse_iso,
)

from .payloads import (
    TRACKING_CODE,
    delivery_point,
    events_for_delivered,
    incoming_shipment,
    outgoing_shipment,
    tracking_access_point,
    tracking_event,
    tracking_events_for_delivered,
    tracking_shipment,
)

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]


def _tracking(**kwargs) -> dict:
    return normalize_tracking(
        tracking_shipment(**kwargs), tracking_code=TRACKING_CODE, include_history=True
    )


def _collected_tracking() -> dict:
    return _tracking(
        phase="Delivered",
        access_point=tracking_access_point(name="Locker 1"),
        events=tracking_events_for_delivered(),
    )


def _collected_account() -> dict:
    history = account_build_history(events_for_delivered())
    return normalize_account(
        incoming_shipment(
            status="DELIVERED", to_delivery_point=delivery_point(name="Locker 2")
        ),
        history=history,
    )


SAMPLES = {
    "tracking_active": lambda: _tracking(phase="ShipmentInTransport"),
    "tracking_pickup": lambda: _tracking(
        phase="Delivered.Parcelshop", access_point=tracking_access_point()
    ),
    "tracking_collected": _collected_tracking,
    "tracking_returning": lambda: _tracking(phase="BackToSender"),
    "tracking_problem": lambda: _tracking(phase="NotDelivered"),
    "tracking_moment": lambda: _tracking(
        phase="OutForDelivery", expected_delivery_date="2099-05-03T13:00:00+02:00"
    ),
    "tracking_date": lambda: _tracking(
        phase="OutForDelivery", expected_delivery_date="2099-05-03"
    ),
    "tracking_bare": lambda: normalize_tracking({}, tracking_code=TRACKING_CODE),
    "account_active": lambda: normalize_account(incoming_shipment()),
    "account_pickup": lambda: normalize_account(
        incoming_shipment(
            status="PICKUP_POINT", to_delivery_point=delivery_point(name="Locker 2")
        )
    ),
    "account_collected": _collected_account,
    "account_outgoing": lambda: normalize_account(outgoing_shipment()),
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return SAMPLES[request.param]()


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_planned_window_is_ordered_and_anchored(parcel):
    if parcel["planned_to"] is not None:
        assert parcel["planned_from"] is not None
        assert parse_iso(parcel["planned_to"]) > parse_iso(parcel["planned_from"])


def test_planned_is_empty_when_the_parcel_is_done_or_waiting(parcel):
    if parcel["status"] in (
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
    ):
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None


def test_pickup_is_a_bool_and_the_point_needs_it(parcel):
    assert isinstance(parcel["pickup"], bool)
    if not parcel["pickup"]:
        assert parcel["pickup_point"] is None


def test_a_collected_tracking_parcel_stays_pickup():
    parcel = _collected_tracking()
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Locker 1"


def test_a_collected_tracking_parcel_stays_pickup_from_its_events_alone():
    parcel = normalize_tracking(
        tracking_shipment(
            phase="Delivered",
            events=[
                tracking_event("Delivered.Parcelshop", "2026-05-02T09:00:00Z"),
                tracking_event("Delivered", "2026-05-02T09:05:00Z"),
            ],
        ),
        tracking_code=TRACKING_CODE,
    )
    assert parcel["history"] is None
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] is None


def test_a_home_delivery_is_not_pickup():
    parcel = _tracking(
        phase="Delivered",
        events=[
            *tracking_events_for_delivered()[:3],
            tracking_event("Delivered", "2026-05-02T09:05:00Z"),
        ],
    )
    assert parcel["delivered"] is True
    assert parcel["pickup"] is False


def test_a_collected_account_parcel_stays_pickup():
    parcel = _collected_account()
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Locker 2"


def test_an_account_pickup_parcel_without_a_point_flag_uses_its_status():
    parcel = normalize_account(incoming_shipment(status="PICKUP_POINT"))
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] is None


def test_a_returning_parcel_is_not_delivered():
    parcel = _tracking(phase="BackToSender")
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None


def test_a_moment_ends_at_the_end_of_that_day():
    parcel = _tracking(
        phase="OutForDelivery", expected_delivery_date="2099-05-03T13:00:00+02:00"
    )
    end = datetime.fromisoformat(parcel["planned_to"])
    assert (end.hour, end.minute, end.second, end.microsecond) == (23, 59, 59, 0)
    assert end.date() == parse_iso(parcel["planned_from"]).date()


def test_a_utc_moment_ends_on_the_czech_day():
    parcel = _tracking(
        phase="OutForDelivery", expected_delivery_date="2099-05-03T22:30:00Z"
    )
    end = datetime.fromisoformat(parcel["planned_to"])
    assert end.isoformat() == "2099-05-04T23:59:59+02:00"


def test_a_date_covers_the_whole_day():
    parcel = _tracking(phase="OutForDelivery", expected_delivery_date="2099-05-03")
    start = datetime.fromisoformat(parcel["planned_from"])
    end = datetime.fromisoformat(parcel["planned_to"])
    assert (start.hour, start.minute, start.second) == (0, 0, 0)
    assert (end.hour, end.minute, end.second, end.microsecond) == (23, 59, 59, 0)
    assert start.date() == end.date()
    assert start.utcoffset() == end.utcoffset()


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (
            "2099-05-03T10:00:00+02:00",
            "2099-05-03T12:00:00+02:00",
            ("2099-05-03T10:00:00+02:00", "2099-05-03T12:00:00+02:00"),
        ),
        (
            "2099-05-03T12:00:00+02:00",
            "2099-05-03T10:00:00+02:00",
            ("2099-05-03T10:00:00+02:00", "2099-05-03T12:00:00+02:00"),
        ),
        (
            "2099-05-03",
            "2099-05-05",
            ("2099-05-03T00:00:00+02:00", "2099-05-05T23:59:59+02:00"),
        ),
        (
            "2099-05-03T10:00:00+02:00",
            "2099-05-03T10:00:00+02:00",
            ("2099-05-03T10:00:00+02:00", "2099-05-03T23:59:59+02:00"),
        ),
        (
            None,
            "2099-05-03",
            ("2099-05-03T00:00:00+02:00", "2099-05-03T23:59:59+02:00"),
        ),
        ("2099-05-03T23:59:59+02:00", None, ("2099-05-03T23:59:59+02:00", None)),
        ("not-a-date", None, (None, None)),
        (None, None, (None, None)),
    ],
)
def test_planned_window_shapes(start, end, expected):
    assert planned_window(start, end, zone=ZoneInfo("Europe/Prague")) == expected
