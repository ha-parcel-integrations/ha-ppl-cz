"""Tests for the PPL CZ coordinator: fetch, split, cache and events."""
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
    DOMAIN,
    ParcelStatus,
)
from custom_components.ppl_cz.coordinator import PPLCZCoordinator

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
