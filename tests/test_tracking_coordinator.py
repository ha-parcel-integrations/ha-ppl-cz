"""Tests for the tracking-code coordinator: fetch, direction split, events."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ppl_cz.const import (
    CONF_BARCODE,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_DIRECTION,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    DIRECTION_INCOMING,
    DIRECTION_OUTGOING,
    DOMAIN,
    HOT_INTERVAL_MINUTES,
    MID_INTERVAL_MINUTES,
    STAGGER_MINUTES,
    ParcelStatus,
)
from custom_components.ppl_cz.tracking.api import (
    PPLCZTrackingApiError,
    PPLCZTrackingNotFound,
)
from custom_components.ppl_cz.tracking.coordinator import (
    PPLCZTrackingCoordinator,
    _hottest_tier_minutes,
    _in_quiet_window,
    _next_anchor,
    _next_update_interval,
    _stagger_minutes,
)

from .payloads import tracking_shipment

UTC = timezone.utc

CODE_A = "10000000001"
CODE_B = "20000000002"


def _entry(parcels: list[dict], **options) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_PARCELS: parcels,
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
            CONF_INCLUDE_HISTORY: False,
            **options,
        },
        unique_id=f"{DOMAIN}_tracking",
    )


def _client(by_code: dict) -> AsyncMock:
    """A client whose ``async_get_parcel`` resolves/raises per barcode."""
    client = AsyncMock()

    async def _get(code):
        result = by_code[code]
        if isinstance(result, BaseException):
            raise result
        return result

    client.async_get_parcel.side_effect = _get
    return client


async def test_update_splits_incoming_and_outgoing_by_declared_direction(hass):
    entry = _entry(
        [
            {CONF_BARCODE: CODE_A, CONF_DIRECTION: DIRECTION_INCOMING},
            {CONF_BARCODE: CODE_B, CONF_DIRECTION: DIRECTION_OUTGOING},
        ]
    )
    entry.add_to_hass(hass)
    client = _client(
        {
            CODE_A: tracking_shipment(CODE_A, phase="ShipmentInTransport"),
            CODE_B: tracking_shipment(CODE_B, phase="WaitingForShipment"),
        }
    )
    coord = PPLCZTrackingCoordinator(hass, client, entry)

    data = await coord._async_update_data()

    assert [p["barcode"] for p in data] == [CODE_A]
    assert [p["barcode"] for p in coord.outgoing] == [CODE_B]
    assert coord.delivered == []
    assert coord.delivered_outgoing == []


async def test_a_barcode_with_no_declared_direction_defaults_to_incoming(hass):
    entry = _entry([{CONF_BARCODE: CODE_A}])
    entry.add_to_hass(hass)
    client = _client({CODE_A: tracking_shipment(CODE_A)})
    coord = PPLCZTrackingCoordinator(hass, client, entry)

    data = await coord._async_update_data()
    assert [p["barcode"] for p in data] == [CODE_A]
    assert coord.outgoing == []


async def test_not_found_marks_one_barcode_unknown_and_leaves_the_poll_intact(hass):
    """The trap: PPLCZTrackingNotFound must not fail the whole poll."""
    entry = _entry(
        [
            {CONF_BARCODE: CODE_A, CONF_DIRECTION: DIRECTION_INCOMING},
            {CONF_BARCODE: CODE_B, CONF_DIRECTION: DIRECTION_INCOMING},
        ]
    )
    entry.add_to_hass(hass)
    client = _client(
        {
            CODE_A: PPLCZTrackingNotFound(),
            CODE_B: tracking_shipment(CODE_B, phase="ShipmentInTransport"),
        }
    )
    coord = PPLCZTrackingCoordinator(hass, client, entry)

    data = await coord._async_update_data()

    by_code = {p["barcode"]: p for p in data}
    assert by_code[CODE_A]["status"] == ParcelStatus.UNKNOWN
    assert by_code[CODE_B]["status"] == ParcelStatus.IN_TRANSIT


async def test_a_real_400_fails_the_poll_when_nothing_else_succeeds(hass):
    entry = _entry([{CONF_BARCODE: CODE_A, CONF_DIRECTION: DIRECTION_INCOMING}])
    entry.add_to_hass(hass)
    client = _client({CODE_A: PPLCZTrackingApiError("HTTP 400 (some.other.error)")})
    coord = PPLCZTrackingCoordinator(hass, client, entry)

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()


async def test_a_fetch_error_keeps_the_cached_payload(hass):
    entry = _entry([{CONF_BARCODE: CODE_A, CONF_DIRECTION: DIRECTION_INCOMING}])
    entry.add_to_hass(hass)
    client = _client({CODE_A: tracking_shipment(CODE_A, phase="ShipmentInTransport")})
    coord = PPLCZTrackingCoordinator(hass, client, entry)
    await coord._async_update_data()

    client.async_get_parcel.side_effect = AsyncMock(
        side_effect=PPLCZTrackingApiError("HTTP 500")
    )
    data = await coord._async_update_data()
    assert data[0]["status"] == ParcelStatus.IN_TRANSIT


async def test_delivered_parcels_are_skipped_from_the_next_fetch(hass):
    entry = _entry([{CONF_BARCODE: CODE_A, CONF_DIRECTION: DIRECTION_INCOMING}])
    entry.add_to_hass(hass)
    client = _client({CODE_A: tracking_shipment(CODE_A, phase="Delivered")})
    coord = PPLCZTrackingCoordinator(hass, client, entry)
    await coord._async_update_data()
    assert CODE_A in coord.delivered_codes

    client.async_get_parcel.reset_mock()
    await coord._async_update_data()
    client.async_get_parcel.assert_not_called()


async def test_removing_a_tracked_barcode_drops_its_cache(hass):
    entry = _entry([{CONF_BARCODE: CODE_A, CONF_DIRECTION: DIRECTION_INCOMING}])
    entry.add_to_hass(hass)
    client = _client({CODE_A: tracking_shipment(CODE_A, phase="Delivered")})
    coord = PPLCZTrackingCoordinator(hass, client, entry)
    await coord._async_update_data()

    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_PARCELS: []})
    await coord._async_update_data()
    assert coord.delivered_codes == set()


async def test_no_tracked_barcodes_suspends_polling(hass):
    entry = _entry([])
    entry.add_to_hass(hass)
    coord = PPLCZTrackingCoordinator(hass, _client({}), entry)
    await coord._async_update_data()
    assert coord.update_interval is None


# --- events ------------------------------------------------------------------


async def test_first_refresh_fires_no_events(hass):
    entry = _entry([{CONF_BARCODE: CODE_A, CONF_DIRECTION: DIRECTION_INCOMING}])
    entry.add_to_hass(hass)
    client = _client({CODE_A: tracking_shipment(CODE_A, phase="WaitingForShipment")})
    coord = PPLCZTrackingCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: events.append(e))
    await coord._async_update_data()
    await hass.async_block_till_done()
    assert events == []


async def test_status_change_fires_parcel_status_changed(hass):
    entry = _entry([{CONF_BARCODE: CODE_A, CONF_DIRECTION: DIRECTION_INCOMING}])
    entry.add_to_hass(hass)
    client = _client({CODE_A: tracking_shipment(CODE_A, phase="WaitingForShipment")})
    coord = PPLCZTrackingCoordinator(hass, client, entry)
    await coord._async_update_data()

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e.data)
    )
    client.async_get_parcel.side_effect = lambda code: tracking_shipment(
        code, phase="ShipmentInTransport"
    )
    await coord._async_update_data()
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0]["new_status"] == ParcelStatus.IN_TRANSIT


async def test_delivered_fires_parcel_delivered_not_status_changed(hass):
    entry = _entry([{CONF_BARCODE: CODE_A, CONF_DIRECTION: DIRECTION_INCOMING}])
    entry.add_to_hass(hass)
    client = _client({CODE_A: tracking_shipment(CODE_A, phase="ShipmentInTransport")})
    coord = PPLCZTrackingCoordinator(hass, client, entry)
    await coord._async_update_data()

    delivered_events = []
    status_events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_delivered", lambda e: delivered_events.append(e.data)
    )
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: status_events.append(e.data)
    )
    client.async_get_parcel.side_effect = lambda code: tracking_shipment(
        code, phase="Delivered"
    )
    await coord._async_update_data()
    await hass.async_block_till_done()
    assert len(delivered_events) == 1
    assert status_events == []


async def test_outgoing_status_change_fires_outgoing_event(hass):
    entry = _entry([{CONF_BARCODE: CODE_A, CONF_DIRECTION: DIRECTION_OUTGOING}])
    entry.add_to_hass(hass)
    client = _client({CODE_A: tracking_shipment(CODE_A, phase="WaitingForShipment")})
    coord = PPLCZTrackingCoordinator(hass, client, entry)
    await coord._async_update_data()

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_outgoing_parcel_status_changed", lambda e: events.append(e.data)
    )
    incoming_events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: incoming_events.append(e.data)
    )
    client.async_get_parcel.side_effect = lambda code: tracking_shipment(
        code, phase="ShipmentInTransport"
    )
    await coord._async_update_data()
    await hass.async_block_till_done()
    assert len(events) == 1
    assert incoming_events == []


async def test_outgoing_delivered_fires_outgoing_parcel_delivered(hass):
    entry = _entry([{CONF_BARCODE: CODE_A, CONF_DIRECTION: DIRECTION_OUTGOING}])
    entry.add_to_hass(hass)
    client = _client({CODE_A: tracking_shipment(CODE_A, phase="ShipmentInTransport")})
    coord = PPLCZTrackingCoordinator(hass, client, entry)
    await coord._async_update_data()

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_outgoing_parcel_delivered", lambda e: events.append(e.data)
    )
    client.async_get_parcel.side_effect = lambda code: tracking_shipment(
        code, phase="Delivered"
    )
    await coord._async_update_data()
    await hass.async_block_till_done()
    assert len(events) == 1


# --- dynamic polling — pure helpers -----------------------------------------


def test_quiet_window_is_midnight_to_six():
    assert _in_quiet_window(datetime(2026, 1, 1, 0, 0, tzinfo=UTC))
    assert _in_quiet_window(datetime(2026, 1, 1, 5, 59, tzinfo=UTC))
    assert not _in_quiet_window(datetime(2026, 1, 1, 6, 0, tzinfo=UTC))
    assert not _in_quiet_window(datetime(2026, 1, 1, 23, 59, tzinfo=UTC))


def test_next_anchor_before_six_is_six_today():
    now = datetime(2026, 1, 1, 2, 30, tzinfo=UTC)
    assert _next_anchor(now) == datetime(2026, 1, 1, 6, 0, tzinfo=UTC)


def test_next_anchor_after_six_is_midnight_tomorrow():
    now = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    assert _next_anchor(now) == datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


def test_stagger_is_stable_and_bounded():
    a = _stagger_minutes("entry-1")
    b = _stagger_minutes("entry-1")
    c = _stagger_minutes("entry-2")
    assert a == b
    assert 0 <= a < STAGGER_MINUTES
    assert 0 <= c < STAGGER_MINUTES


def test_tier_is_none_when_nothing_tracked():
    """Unlike the account source, the tracking coordinator may fully suspend."""
    assert _hottest_tier_minutes([], datetime(2026, 1, 1, 12, tzinfo=UTC)) is None


def test_tier_is_mid_for_non_hot_statuses():
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    parcels = [{"status": ParcelStatus.IN_TRANSIT, "planned_from": None}]
    assert _hottest_tier_minutes(parcels, now) == MID_INTERVAL_MINUTES


def test_tier_is_hot_when_out_for_delivery_without_planned_from():
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    parcels = [{"status": ParcelStatus.OUT_FOR_DELIVERY, "planned_from": None}]
    assert _hottest_tier_minutes(parcels, now) == HOT_INTERVAL_MINUTES


def test_tier_is_hot_when_planned_from_is_unparseable():
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    parcels = [
        {"status": ParcelStatus.OUT_FOR_DELIVERY, "planned_from": "not-a-date"}
    ]
    assert _hottest_tier_minutes(parcels, now) == HOT_INTERVAL_MINUTES


def test_tier_is_hot_within_lookahead_of_planned_from():
    planned = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    now = planned - timedelta(minutes=30)
    parcels = [
        {"status": ParcelStatus.OUT_FOR_DELIVERY, "planned_from": planned.isoformat()}
    ]
    assert _hottest_tier_minutes(parcels, now) == HOT_INTERVAL_MINUTES


def test_tier_is_mid_before_lookahead_of_planned_from():
    planned = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    now = planned - timedelta(hours=3)
    parcels = [
        {"status": ParcelStatus.OUT_FOR_DELIVERY, "planned_from": planned.isoformat()}
    ]
    assert _hottest_tier_minutes(parcels, now) == MID_INTERVAL_MINUTES


def test_next_update_interval_none_tier_suspends():
    assert _next_update_interval(datetime(2026, 1, 1, 12, tzinfo=UTC), None, "e1") is None


def test_now_inside_quiet_window_jumps_to_next_anchor():
    now = datetime(2026, 1, 1, 2, 0, tzinfo=UTC)
    interval = _next_update_interval(now, HOT_INTERVAL_MINUTES, "entry-1")
    assert now + interval == datetime(2026, 1, 1, 6, 0, tzinfo=UTC)


def test_candidate_landing_in_quiet_window_clamps_to_the_midnight_anchor():
    now = datetime(2026, 1, 1, 23, 50, tzinfo=UTC)
    interval = _next_update_interval(now, MID_INTERVAL_MINUTES, "entry-1")
    assert now + interval >= datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


# --- device id / property surface -------------------------------------------


async def test_device_id_is_none_before_setup(hass):
    entry = _entry([])
    entry.add_to_hass(hass)
    coord = PPLCZTrackingCoordinator(hass, _client({}), entry)
    assert coord._device_id() is None


async def test_current_tier_minutes_none_before_first_refresh(hass):
    entry = _entry([])
    entry.add_to_hass(hass)
    coord = PPLCZTrackingCoordinator(hass, _client({}), entry)
    assert coord.current_tier_minutes is None


async def test_unexpected_exception_from_the_client_propagates(hass):
    entry = _entry([{CONF_BARCODE: CODE_A, CONF_DIRECTION: DIRECTION_INCOMING}])
    entry.add_to_hass(hass)
    client = _client({CODE_A: ValueError("unexpected")})
    coord = PPLCZTrackingCoordinator(hass, client, entry)
    with pytest.raises(ValueError):
        await coord._async_update_data()


async def test_new_barcode_first_seen_delivered_fires_no_registered_event(hass):
    entry = _entry([{CONF_BARCODE: CODE_A, CONF_DIRECTION: DIRECTION_INCOMING}])
    entry.add_to_hass(hass)
    client = _client({CODE_A: tracking_shipment(CODE_A, phase="WaitingForShipment")})
    coord = PPLCZTrackingCoordinator(hass, client, entry)
    await coord._async_update_data()  # first refresh: silent, seeds known state

    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: events.append(e))
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PARCELS: [
                {CONF_BARCODE: CODE_A, CONF_DIRECTION: DIRECTION_INCOMING},
                {CONF_BARCODE: CODE_B, CONF_DIRECTION: DIRECTION_INCOMING},
            ],
        },
    )
    client.async_get_parcel.side_effect = lambda code: tracking_shipment(
        code, phase="Delivered"
    )
    await coord._async_update_data()
    await hass.async_block_till_done()
    assert events == []


async def test_new_barcode_not_yet_delivered_fires_registered(hass):
    entry = _entry([{CONF_BARCODE: CODE_A, CONF_DIRECTION: DIRECTION_INCOMING}])
    entry.add_to_hass(hass)
    client = _client({CODE_A: tracking_shipment(CODE_A, phase="WaitingForShipment")})
    coord = PPLCZTrackingCoordinator(hass, client, entry)
    await coord._async_update_data()

    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: events.append(e))
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PARCELS: [
                {CONF_BARCODE: CODE_A, CONF_DIRECTION: DIRECTION_INCOMING},
                {CONF_BARCODE: CODE_B, CONF_DIRECTION: DIRECTION_INCOMING},
            ],
        },
    )
    client.async_get_parcel.side_effect = lambda code: tracking_shipment(
        code, phase="WaitingForShipment"
    )
    await coord._async_update_data()
    await hass.async_block_till_done()
    assert len(events) == 1


async def test_outgoing_never_fires_registered(hass):
    """Same suite-wide rule as the account source: outgoing has no registered event."""
    entry = _entry([])
    entry.add_to_hass(hass)
    coord = PPLCZTrackingCoordinator(hass, _client({}), entry)
    await coord._async_update_data()

    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: events.append(e))
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PARCELS: [{CONF_BARCODE: CODE_A, CONF_DIRECTION: DIRECTION_OUTGOING}],
        },
    )
    entry.runtime_data = None
    client = _client({CODE_A: tracking_shipment(CODE_A, phase="WaitingForShipment")})
    coord._client = client
    await coord._async_update_data()
    await hass.async_block_till_done()
    assert events == []
