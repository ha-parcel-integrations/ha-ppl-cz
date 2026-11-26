"""Tests for the PPL CZ deliveries calendar (tracking source only)."""

from datetime import UTC, date, datetime
from unittest.mock import MagicMock

from custom_components.ppl_cz.calendar import PPLCZDeliveriesCalendar, async_setup_entry


def _entry(entry_id: str = "e1") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


def _coordinator(data: list[dict]) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = data
    return coordinator


def _parcel(
    barcode,
    planned_from=None,
    planned_to=None,
    pickup=False,
    pickup_point=None,
    status="out_for_delivery",
):
    return {
        "barcode": barcode,
        "status": status,
        "planned_from": planned_from,
        "planned_to": planned_to,
        "pickup": pickup,
        "pickup_point": pickup_point,
        "url": "https://www.ppl.cz/vyhledat-zasilku?shipmentId=1",
    }


def _cal(data):
    return PPLCZDeliveriesCalendar(_coordinator(data), _entry())


def test_event_returns_earliest_upcoming():
    cal = _cal(
        [
            _parcel("LATE", planned_from="2099-01-02T10:00:00Z"),
            _parcel("SOON", planned_from="2099-01-01T10:00:00Z"),
        ]
    )
    event = cal.event
    assert event.uid == "SOON"
    assert event.summary == "Parcel SOON"


def test_event_none_when_no_planned():
    assert _cal([_parcel("A")]).event is None


def test_event_none_when_no_parcels_at_all():
    assert _cal([]).event is None


def test_moment_gets_one_hour_default_duration():
    cal = _cal([_parcel("A", planned_from="2099-01-01T10:00:00Z")])
    events = cal._events()
    assert events[0].end == datetime(2099, 1, 1, 11, 0, tzinfo=UTC)


def test_interval_uses_window():
    cal = _cal(
        [
            _parcel(
                "A",
                planned_from="2099-01-01T10:00:00Z",
                planned_to="2099-01-01T12:00:00Z",
            )
        ]
    )
    assert cal._events()[0].end == datetime(2099, 1, 1, 12, 0, tzinfo=UTC)


def test_pickup_sets_location():
    cal = _cal(
        [
            _parcel(
                "A",
                planned_from="2099-01-01T10:00:00Z",
                pickup=True,
                pickup_point="Example Pickup Point",
            )
        ]
    )
    assert cal._events()[0].location == "Example Pickup Point"


def test_no_pickup_leaves_location_none():
    cal = _cal(
        [
            _parcel(
                "A", planned_from="2099-01-01T10:00:00Z", pickup=False, pickup_point="X"
            )
        ]
    )
    assert cal._events()[0].location is None


def test_naive_planned_from_treated_as_utc():
    cal = _cal([_parcel("A", planned_from="2099-01-01T10:00:00")])
    assert cal._events()[0].start == datetime(2099, 1, 1, 10, 0, tzinfo=UTC)


def test_unparseable_planned_from_skipped():
    assert _cal([_parcel("A", planned_from="not-a-date")]).event is None


def test_description_includes_barcode_status_and_url():
    cal = _cal([_parcel("A", planned_from="2099-01-01T10:00:00Z")])
    description = cal._events()[0].description
    assert "Barcode: A" in description
    assert "Status: out_for_delivery" in description
    assert "https://www.ppl.cz" in description


def test_no_barcode_uses_generic_summary():
    cal = _cal([_parcel("", planned_from="2099-01-01T10:00:00Z")])
    events = cal._events()
    assert events[0].summary == "PPL CZ parcel"
    assert events[0].uid is None


async def test_get_events_filters_by_range():
    cal = _cal(
        [
            _parcel("PAST", planned_from="2000-01-01T10:00:00Z"),
            _parcel("FUTURE", planned_from="2099-01-01T10:00:00Z"),
        ]
    )
    start = datetime(2098, 1, 1, tzinfo=UTC)
    end = datetime(2100, 1, 1, tzinfo=UTC)
    events = await cal.async_get_events(MagicMock(), start, end)
    assert {e.uid for e in events} == {"FUTURE"}


async def test_async_setup_entry_adds_one_calendar(hass):
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.runtime_data.coordinator = _coordinator([])
    added = []
    await async_setup_entry(hass, entry, lambda entities: added.extend(entities))
    assert len(added) == 1
    assert isinstance(added[0], PPLCZDeliveriesCalendar)


def test_whole_day_window_is_an_all_day_event():
    cal = _cal(
        [
            _parcel(
                "A",
                planned_from="2099-05-03T00:00:00+02:00",
                planned_to="2099-05-03T23:59:59+02:00",
            )
        ]
    )
    event = cal._events()[0]
    assert event.start == date(2099, 5, 3)
    assert event.end == date(2099, 5, 4)


def test_moment_stretched_to_end_of_day_stays_timed():
    cal = _cal(
        [
            _parcel(
                "A",
                planned_from="2099-05-03T13:00:00+02:00",
                planned_to="2099-05-03T23:59:59+02:00",
            )
        ]
    )
    event = cal._events()[0]
    assert isinstance(event.start, datetime)
    assert event.end.second == 59


def test_event_title_prefers_contents_then_sender_then_barcode():
    with_contents = _parcel("A", planned_from="2099-01-01T10:00:00Z")
    with_contents["contents"] = "Running shoes"
    with_contents["sender"] = "Example Shop"
    assert _cal([with_contents]).event.summary == "Running shoes"
    with_sender = _parcel("B", planned_from="2099-01-01T10:00:00Z")
    with_sender["sender"] = "Example Shop"
    assert _cal([with_sender]).event.summary == "Example Shop"
    bare = _parcel("C", planned_from="2099-01-01T10:00:00Z")
    assert _cal([bare]).event.summary == "Parcel C"
