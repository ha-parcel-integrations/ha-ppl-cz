"""Tests for the PPL CZ coordinator: fetch, split, cache and events."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import CONF_PASSWORD
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ppl_cz.api import PPLCZApiError, PPLCZAuthError
from custom_components.ppl_cz.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_EMAIL,
    CONF_INCLUDE_HISTORY,
    CONF_REFRESH_INTERVAL,
    DOMAIN,
    HOT_INTERVAL_MINUTES,
    MID_INTERVAL_MINUTES,
    REFRESH_INTERVAL_AUTO,
    STAGGER_MINUTES,
    ParcelStatus,
)
from custom_components.ppl_cz.coordinator import (
    PPLCZCoordinator,
    _hottest_tier_minutes,
    _in_quiet_window,
    _next_anchor,
    _next_update_interval,
    _refresh_interval,
    _refresh_setting,
    _stagger_minutes,
)

from .payloads import (
    INCOMING_CODE,
    OUTGOING_CODE,
    events_for_delivered,
    incoming_shipment,
    outgoing_shipment,
    shipment_event,
)

IN = INCOMING_CODE
OUT = OUTGOING_CODE


def _entry(**options) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "a@b.c", CONF_PASSWORD: "pw"},
        options={
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
            CONF_INCLUDE_HISTORY: False,
            **options,
        },
        unique_id="a@b.c",
    )


def _client(items, events_by_id=None) -> AsyncMock:
    client = AsyncMock()
    client.async_get_parcels.return_value = items
    events_by_id = events_by_id or {}
    client.async_get_shipment_events.side_effect = lambda sid: events_by_id.get(sid)
    client.async_get_delivery_info.return_value = None
    return client


async def test_update_splits_incoming_and_outgoing(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    coord = PPLCZCoordinator(
        hass, _client([incoming_shipment(IN), outgoing_shipment(OUT)]), entry
    )

    data = await coord._async_update_data()

    assert [p["barcode"] for p in data] == [IN]
    assert [p["barcode"] for p in coord.outgoing] == [OUT]
    assert coord.delivered == []
    assert coord.delivered_outgoing == []
    assert coord.last_success_time is not None


async def test_delivered_goes_to_delivered_lists(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    coord = PPLCZCoordinator(
        hass, _client([incoming_shipment(IN, status="DELIVERED")]), entry
    )

    data = await coord._async_update_data()
    assert data == []
    assert [p["barcode"] for p in coord.delivered] == [IN]


async def test_history_disabled_by_default_skips_events_call(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client([incoming_shipment(IN)])
    coord = PPLCZCoordinator(hass, client, entry)

    await coord._async_update_data()
    client.async_get_shipment_events.assert_not_awaited()


async def test_history_enabled_fetches_and_caches_until_status_changes(hass):
    entry = _entry(**{CONF_INCLUDE_HISTORY: True})
    entry.add_to_hass(hass)
    shipment_id = incoming_shipment(IN)["id"]
    client = _client(
        [incoming_shipment(IN)], {shipment_id: events_for_delivered(IN)}
    )
    coord = PPLCZCoordinator(hass, client, entry)

    await coord._async_update_data()
    await coord._async_update_data()  # same lastShipmentEvent -> cache hit

    assert client.async_get_shipment_events.await_count == 1


async def test_history_refetches_when_status_changes(hass):
    entry = _entry(**{CONF_INCLUDE_HISTORY: True})
    entry.add_to_hass(hass)
    shipment_id = incoming_shipment(IN)["id"]
    client = _client(
        [incoming_shipment(IN, status="IN_TRANSPORT")],
        {shipment_id: [shipment_event("IN_TRANSPORT", "2026-04-28T10:00:00Z")]},
    )
    coord = PPLCZCoordinator(hass, client, entry)
    await coord._async_update_data()

    client.async_get_parcels.return_value = [
        incoming_shipment(IN, status="DELIVERED")
    ]
    client.async_get_shipment_events.side_effect = lambda sid: events_for_delivered(IN)
    await coord._async_update_data()

    assert client.async_get_shipment_events.await_count == 2


async def test_history_call_failure_keeps_cached_history(hass):
    entry = _entry(**{CONF_INCLUDE_HISTORY: True})
    entry.add_to_hass(hass)
    shipment_id = incoming_shipment(IN)["id"]
    client = _client(
        [incoming_shipment(IN, status="IN_TRANSPORT")],
        {shipment_id: [shipment_event("IN_TRANSPORT", "2026-04-28T10:00:00Z")]},
    )
    coord = PPLCZCoordinator(hass, client, entry)
    data = await coord._async_update_data()
    assert data[0]["history"] is not None

    client.async_get_parcels.return_value = [
        incoming_shipment(IN, status="DELIVERING")
    ]
    client.async_get_shipment_events.side_effect = lambda sid: None
    data = await coord._async_update_data()
    assert data[0]["history"] is not None


async def test_delivery_info_probed_once_per_shipment(hass):
    """Research probe: called once per shipment id, cached across polls."""
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client([incoming_shipment(IN, status="IN_TRANSPORT")])
    client.async_get_delivery_info.return_value = None
    coord = PPLCZCoordinator(hass, client, entry)

    await coord._async_update_data()
    await coord._async_update_data()  # same shipment id -> no re-probe

    assert client.async_get_delivery_info.await_count == 1


async def test_delivery_info_not_probed_for_delivered_parcels(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client([incoming_shipment(IN, status="DELIVERED")])
    coord = PPLCZCoordinator(hass, client, entry)

    await coord._async_update_data()

    client.async_get_delivery_info.assert_not_awaited()


async def test_delivery_info_populated_response_warns_once(hass, caplog):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client([incoming_shipment(IN, status="IN_TRANSPORT")])
    client.async_get_delivery_info.return_value = {
        "deliveryWindowFrom": "2026-09-01T09:00:00Z"
    }
    coord = PPLCZCoordinator(hass, client, entry)

    await coord._async_update_data()

    assert "deliveryInfo" in caplog.text
    assert "deliveryWindowFrom: str" in caplog.text


async def test_auth_error_becomes_config_entry_auth_failed(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcels.side_effect = PPLCZAuthError("expired")
    coord = PPLCZCoordinator(hass, client, entry)
    with pytest.raises(ConfigEntryAuthFailed):
        await coord._async_update_data()


async def test_api_error_becomes_update_failed(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcels.side_effect = PPLCZApiError("boom")
    coord = PPLCZCoordinator(hass, client, entry)
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()


async def test_first_refresh_fires_nothing(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    coord = PPLCZCoordinator(hass, _client([incoming_shipment(IN)]), entry)

    fired = []
    for suffix in (
        "parcel_registered", "parcel_status_changed", "parcel_delivered",
        "outgoing_parcel_status_changed", "outgoing_parcel_delivered",
    ):
        hass.bus.async_listen(f"{DOMAIN}_{suffix}", lambda e: fired.append(e))

    await coord._async_update_data()
    await hass.async_block_till_done()
    assert fired == []


async def test_incoming_status_changed_event(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_shipment_events.return_value = None
    coord = PPLCZCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e))

    client.async_get_parcels.return_value = [incoming_shipment(IN, status="IN_TRANSPORT")]
    await coord._async_update_data()  # first refresh: suppressed

    client.async_get_parcels.return_value = [incoming_shipment(IN, status="PICKUP_POINT")]
    await coord._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["old_status"] == ParcelStatus.IN_TRANSIT
    assert events[0].data["new_status"] == ParcelStatus.AT_PICKUP_POINT


async def test_incoming_delivered_event_only(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_shipment_events.return_value = None
    coord = PPLCZCoordinator(hass, client, entry)

    delivered, changed = [], []
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: delivered.append(e))
    hass.bus.async_listen(f"{DOMAIN}_parcel_status_changed", lambda e: changed.append(e))

    client.async_get_parcels.return_value = [incoming_shipment(IN, status="DELIVERING")]
    await coord._async_update_data()

    client.async_get_parcels.return_value = [incoming_shipment(IN, status="DELIVERED")]
    await coord._async_update_data()
    await hass.async_block_till_done()

    assert changed == []
    assert len(delivered) == 1
    assert delivered[0].data["barcode"] == IN


async def test_registered_event_for_new_incoming(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_shipment_events.return_value = None
    coord = PPLCZCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: events.append(e))

    client.async_get_parcels.return_value = []
    await coord._async_update_data()  # first refresh, empty

    client.async_get_parcels.return_value = [incoming_shipment(IN, status="ORDER")]
    await coord._async_update_data()
    await hass.async_block_till_done()

    assert [e.data["barcode"] for e in events] == [IN]


async def test_outgoing_events(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_shipment_events.return_value = None
    coord = PPLCZCoordinator(hass, client, entry)

    changed, delivered, registered = [], [], []
    hass.bus.async_listen(
        f"{DOMAIN}_outgoing_parcel_status_changed", lambda e: changed.append(e)
    )
    hass.bus.async_listen(
        f"{DOMAIN}_outgoing_parcel_delivered", lambda e: delivered.append(e)
    )
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: registered.append(e))

    client.async_get_parcels.return_value = [outgoing_shipment(OUT, status="ORDER")]
    await coord._async_update_data()  # first: suppressed, no registered for outgoing

    client.async_get_parcels.return_value = [outgoing_shipment(OUT, status="DELIVERED")]
    await coord._async_update_data()
    await hass.async_block_till_done()

    assert registered == []  # outgoing never fires registered
    assert changed == []
    assert len(delivered) == 1


async def test_shipment_without_number_is_skipped_from_history_but_still_normalized(hass):
    entry = _entry(**{CONF_INCLUDE_HISTORY: True})
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_shipment_events.return_value = None
    client.async_get_parcels.return_value = [
        {"lastShipmentEvent": "ORDER", "ownership": "OWNER"}
    ]
    coord = PPLCZCoordinator(hass, client, entry)
    data = await coord._async_update_data()
    assert len(data) == 1
    client.async_get_shipment_events.assert_not_awaited()


# ---------------------------------------------------------------------------
# Dynamic polling (dynamic-polling.md Section 2.2, account-based) — pure
# helpers
# ---------------------------------------------------------------------------

UTC = timezone.utc


def test_refresh_interval_reads_minutes_from_options():
    entry = _entry(**{CONF_REFRESH_INTERVAL: 120})
    assert _refresh_interval(entry).total_seconds() == 120 * 60


def test_refresh_interval_starts_hot_when_auto():
    entry = _entry(**{CONF_REFRESH_INTERVAL: REFRESH_INTERVAL_AUTO})
    assert _refresh_interval(entry).total_seconds() == HOT_INTERVAL_MINUTES * 60


def test_refresh_setting_passes_through_auto():
    entry = _entry(**{CONF_REFRESH_INTERVAL: REFRESH_INTERVAL_AUTO})
    assert _refresh_setting(entry) == REFRESH_INTERVAL_AUTO


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


def test_tier_is_mid_when_nothing_active():
    assert (
        _hottest_tier_minutes([], datetime(2026, 1, 1, 12, tzinfo=UTC))
        == MID_INTERVAL_MINUTES
    )


def test_tier_is_mid_for_non_hot_statuses():
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    parcels = [
        {"status": ParcelStatus.REGISTERED, "planned_from": None},
        {"status": ParcelStatus.PROBLEM, "planned_from": None},
        {"status": ParcelStatus.RETURNING, "planned_from": None},
    ]
    assert _hottest_tier_minutes(parcels, now) == MID_INTERVAL_MINUTES


def test_tier_is_hot_when_out_for_delivery_without_planned_from():
    """PPL CZ never populates planned_from at all — every out_for_delivery
    parcel hits this branch, not the lookahead one below."""
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    parcels = [
        {"status": ParcelStatus.IN_TRANSIT, "planned_from": None},
        {"status": ParcelStatus.OUT_FOR_DELIVERY, "planned_from": None},
    ]
    assert _hottest_tier_minutes(parcels, now) == HOT_INTERVAL_MINUTES


def test_tier_is_hot_when_planned_from_is_unparseable():
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    parcels = [
        {"status": ParcelStatus.OUT_FOR_DELIVERY, "planned_from": "not-a-date"}
    ]
    assert _hottest_tier_minutes(parcels, now) == HOT_INTERVAL_MINUTES


def test_tier_is_hot_within_lookahead_of_planned_from():
    planned = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    now = planned - timedelta(minutes=30)  # inside the 1h lookahead
    parcels = [
        {"status": ParcelStatus.OUT_FOR_DELIVERY, "planned_from": planned.isoformat()}
    ]
    assert _hottest_tier_minutes(parcels, now) == HOT_INTERVAL_MINUTES


def test_tier_is_mid_before_lookahead_of_planned_from():
    planned = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    now = planned - timedelta(hours=3)  # well outside the 1h lookahead
    parcels = [
        {"status": ParcelStatus.OUT_FOR_DELIVERY, "planned_from": planned.isoformat()}
    ]
    assert _hottest_tier_minutes(parcels, now) == MID_INTERVAL_MINUTES


def test_daytime_candidate_outside_window_is_tier_plus_stagger():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    interval = _next_update_interval(now, MID_INTERVAL_MINUTES, "entry-1")
    stagger = _stagger_minutes("entry-1")
    assert interval == timedelta(minutes=MID_INTERVAL_MINUTES + stagger)


def test_now_inside_quiet_window_jumps_to_next_anchor():
    now = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)  # an anchor poll itself
    interval = _next_update_interval(now, HOT_INTERVAL_MINUTES, "entry-1")
    assert now + interval == datetime(2026, 1, 1, 6, 0, tzinfo=UTC)


def test_candidate_landing_in_quiet_window_clamps_to_the_midnight_anchor():
    now = datetime(2026, 1, 1, 23, 50, tzinfo=UTC)
    interval = _next_update_interval(now, MID_INTERVAL_MINUTES, "entry-1")
    assert now + interval == datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Dynamic polling — wired into _async_update_data
# ---------------------------------------------------------------------------


async def test_auto_mode_recomputes_interval_and_never_stops(hass):
    """Zero pending parcels must not suspend polling — it's the only discovery path."""
    entry = _entry(**{CONF_REFRESH_INTERVAL: REFRESH_INTERVAL_AUTO})
    entry.add_to_hass(hass)
    coord = PPLCZCoordinator(hass, _client([]), entry)

    await coord._async_update_data()

    assert coord.current_tier_minutes == MID_INTERVAL_MINUTES
    assert coord.update_interval is not None


async def test_auto_mode_goes_hot_for_out_for_delivery(hass):
    entry = _entry(**{CONF_REFRESH_INTERVAL: REFRESH_INTERVAL_AUTO})
    entry.add_to_hass(hass)
    coord = PPLCZCoordinator(
        hass, _client([incoming_shipment(IN, status="DELIVERING")]), entry
    )

    await coord._async_update_data()

    assert coord.current_tier_minutes == HOT_INTERVAL_MINUTES


async def test_auto_mode_stays_mid_for_in_transit_only(hass):
    entry = _entry(**{CONF_REFRESH_INTERVAL: REFRESH_INTERVAL_AUTO})
    entry.add_to_hass(hass)
    coord = PPLCZCoordinator(
        hass, _client([incoming_shipment(IN, status="IN_TRANSPORT")]), entry
    )

    await coord._async_update_data()

    assert coord.current_tier_minutes == MID_INTERVAL_MINUTES


async def test_fixed_mode_keeps_configured_interval(hass):
    entry = _entry(**{CONF_REFRESH_INTERVAL: 60})
    entry.add_to_hass(hass)
    coord = PPLCZCoordinator(hass, _client([]), entry)

    await coord._async_update_data()

    assert coord.current_tier_minutes is None
    assert coord.update_interval == timedelta(minutes=60)
