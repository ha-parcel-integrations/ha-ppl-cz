"""Tests for the contents action, its routing and the store lifecycle."""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD
from homeassistant.exceptions import ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ppl_cz import async_unload_entry
from custom_components.ppl_cz.const import (
    CONF_BARCODE,
    CONF_CONFIG_ENTRY_ID,
    CONF_CONTENTS,
    CONF_DIRECTION,
    CONF_EMAIL,
    CONF_PARCELS,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    DIRECTION_INCOMING,
    DIRECTION_OUTGOING,
    DOMAIN,
    SOURCE_TRACKING,
)

from .payloads import (
    INCOMING_CODE,
    OUTGOING_CODE,
    TRACKING_CODE,
    incoming_shipment,
    outgoing_shipment,
    tracking_shipment,
)

UNLISTED = "99999999999"
DELIVERED_CODE = "30000000003"
STORE_KEY = f"{DOMAIN}.contents.%s"
ACCOUNT_CLIENT = "custom_components.ppl_cz.PPLCZApiClient"
TRACKING_CLIENT = "custom_components.ppl_cz.PPLCZTrackingApiClient"


def _account_entry(name: str = "a") -> MockConfigEntry:
    email = f"{name}@example.test"
    return MockConfigEntry(
        domain=DOMAIN,
        title=email,
        unique_id=email,
        data={CONF_EMAIL: email, CONF_PASSWORD: "synthetic"},
    )


def _tracking_entry(*codes: tuple[str, str]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="PPL CZ (tracking codes)",
        unique_id=f"{DOMAIN}_{SOURCE_TRACKING}",
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={
            CONF_PARCELS: [
                {CONF_BARCODE: code, CONF_DIRECTION: direction}
                for code, direction in codes
            ]
        },
    )


def _account_client(items: list[dict]) -> MagicMock:
    client = MagicMock()
    client.async_get_parcels = AsyncMock(return_value=items)
    client.async_get_shipment_events = AsyncMock(return_value=None)
    client.async_get_delivery_info = AsyncMock(return_value=None)
    return client


def _tracking_client() -> MagicMock:
    client = MagicMock()
    client.async_get_parcel = AsyncMock(
        side_effect=lambda code: tracking_shipment(code)
    )
    return client


async def _setup_account(hass, entry=None, items=None) -> MockConfigEntry:
    entry = entry or _account_entry()
    entry.add_to_hass(hass)
    client = _account_client([incoming_shipment()] if items is None else items)
    with patch(ACCOUNT_CLIENT, return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    entry.client = client
    return entry


async def _setup_tracking(hass, entry=None) -> MockConfigEntry:
    entry = entry or _tracking_entry((TRACKING_CODE, DIRECTION_INCOMING))
    entry.add_to_hass(hass)
    client = _tracking_client()
    with patch(TRACKING_CLIENT, return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    entry.client = client
    return entry


async def _call(hass, data: dict) -> None:
    await hass.services.async_call(DOMAIN, "update_contents", data, blocking=True)
    await hass.async_block_till_done()


def _contents(entry, code: str = INCOMING_CODE) -> str | None:
    parcels = {p["barcode"]: p for p in entry.runtime_data.coordinator.data}
    return parcels[code]["contents"]


async def test_a_parcel_has_no_contents_without_a_stored_value(hass):
    assert _contents(await _setup_account(hass)) is None
    entry = await _setup_tracking(hass)
    assert _contents(entry, TRACKING_CODE) is None


@pytest.mark.parametrize("kind", ["account", "tracking"])
async def test_update_contents_sets_replaces_and_clears(hass, kind):
    if kind == "account":
        entry, code = await _setup_account(hass), INCOMING_CODE
    else:
        entry, code = await _setup_tracking(hass), TRACKING_CODE
    await _call(hass, {CONF_TRACKING_CODE: code, CONF_CONTENTS: " Running  shoes "})
    assert _contents(entry, code) == "Running shoes"
    await _call(hass, {CONF_TRACKING_CODE: code, CONF_CONTENTS: "Winter socks"})
    assert _contents(entry, code) == "Winter socks"
    await _call(hass, {CONF_TRACKING_CODE: code, CONF_CONTENTS: "  "})
    assert _contents(entry, code) is None
    await _call(hass, {CONF_TRACKING_CODE: code, CONF_CONTENTS: "Shoes"})
    await _call(hass, {CONF_TRACKING_CODE: code})
    assert _contents(entry, code) is None


async def test_update_contents_normalizes_the_code(hass):
    entry = await _setup_tracking(hass)
    await _call(
        hass,
        {CONF_TRACKING_CODE: f" {TRACKING_CODE.lower()} ", CONF_CONTENTS: "Shoes"},
    )
    assert _contents(entry, TRACKING_CODE) == "Shoes"


async def test_update_contents_reaches_delivered_and_outgoing_parcels(hass):
    entry = await _setup_account(
        hass,
        items=[
            incoming_shipment(DELIVERED_CODE, status="DELIVERED"),
            outgoing_shipment(),
        ],
    )
    coordinator = entry.runtime_data.coordinator
    await _call(hass, {CONF_TRACKING_CODE: DELIVERED_CODE, CONF_CONTENTS: "Shoes"})
    await _call(hass, {CONF_TRACKING_CODE: OUTGOING_CODE, CONF_CONTENTS: "Socks"})
    assert [p["contents"] for p in coordinator.delivered] == ["Shoes"]
    assert [p["contents"] for p in coordinator.outgoing] == ["Socks"]


async def test_tracking_update_reaches_the_outgoing_list(hass):
    entry = await _setup_tracking(
        hass, _tracking_entry((TRACKING_CODE, DIRECTION_OUTGOING))
    )
    await _call(hass, {CONF_TRACKING_CODE: TRACKING_CODE, CONF_CONTENTS: "Socks"})
    assert [p["contents"] for p in entry.runtime_data.coordinator.outgoing] == ["Socks"]


async def test_update_contents_works_for_a_code_that_is_not_listed(hass):
    entry = await _setup_account(hass)
    await _call(hass, {CONF_TRACKING_CODE: UNLISTED, CONF_CONTENTS: "Shoes"})
    assert entry.runtime_data.coordinator.contents_store.get(UNLISTED) == "Shoes"


@pytest.mark.parametrize("kind", ["account", "tracking"])
async def test_update_contents_never_polls_the_carrier(hass, kind):
    if kind == "account":
        entry, code = await _setup_account(hass), INCOMING_CODE
        fetches = [
            entry.client.async_get_parcels,
            entry.client.async_get_shipment_events,
        ]
    else:
        entry, code = await _setup_tracking(hass), TRACKING_CODE
        fetches = [entry.client.async_get_parcel]
    for fetch in fetches:
        fetch.reset_mock()
    coordinator = entry.runtime_data.coordinator
    data = {CONF_TRACKING_CODE: code, CONF_CONTENTS: "Running shoes"}
    with (
        patch.object(coordinator, "async_request_refresh", new=AsyncMock()) as refresh,
        patch.object(coordinator, "async_refresh", new=AsyncMock()) as full_refresh,
        patch.object(
            coordinator,
            "async_update_listeners",
            wraps=coordinator.async_update_listeners,
        ) as notify,
    ):
        await _call(hass, data)
        assert notify.call_count == 1
        await _call(hass, data)
        assert notify.call_count == 1
        await _call(hass, {CONF_TRACKING_CODE: UNLISTED})
        assert notify.call_count == 1
    for fetch in fetches:
        fetch.assert_not_called()
    refresh.assert_not_called()
    full_refresh.assert_not_called()


async def test_an_unchanged_value_writes_nothing(hass):
    entry = await _setup_account(hass)
    await _call(hass, {CONF_TRACKING_CODE: INCOMING_CODE, CONF_CONTENTS: "Shoes"})
    store = entry.runtime_data.coordinator.contents_store
    with patch.object(store._store, "async_delay_save") as save:
        await _call(hass, {CONF_TRACKING_CODE: INCOMING_CODE, CONF_CONTENTS: "Shoes"})
    save.assert_not_called()


async def test_a_newly_tracked_code_still_refreshes_through_the_options_listener(
    hass,
):
    entry = await _setup_tracking(hass)
    new_code = "10000000077"
    await _call(
        hass,
        {
            CONF_TRACKING_CODE: new_code,
            CONF_CONTENTS: "Shoes",
            CONF_CONFIG_ENTRY_ID: entry.entry_id,
        },
    )
    entry.client.async_get_parcel.reset_mock()
    with patch(TRACKING_CLIENT, return_value=entry.client):
        hass.config_entries.async_update_entry(
            entry,
            options={
                CONF_PARCELS: [
                    *entry.options[CONF_PARCELS],
                    {CONF_BARCODE: new_code, CONF_DIRECTION: DIRECTION_INCOMING},
                ]
            },
        )
        await hass.async_block_till_done()
    entry.client.async_get_parcel.assert_any_call(new_code)
    assert _contents(entry, new_code) == "Shoes"


async def test_a_contents_change_alone_fires_no_event(hass):
    await _setup_account(hass)
    events = []
    for name in ("registered", "status_changed", "delivered"):
        hass.bus.async_listen(f"{DOMAIN}_parcel_{name}", events.append)
    await _call(hass, {CONF_TRACKING_CODE: INCOMING_CODE, CONF_CONTENTS: "Shoes"})
    assert events == []


async def test_a_poll_keeps_the_stored_value_and_it_rides_on_events(hass):
    entry = await _setup_account(hass)
    coordinator = entry.runtime_data.coordinator
    await _call(hass, {CONF_TRACKING_CODE: INCOMING_CODE, CONF_CONTENTS: "Shoes"})
    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", events.append)
    entry.client.async_get_parcels.return_value = [
        incoming_shipment(status="DELIVERED")
    ]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert [e.data["contents"] for e in events] == ["Shoes"]


async def test_known_codes_list_both_directions(hass):
    entry = await _setup_account(hass, items=[incoming_shipment(), outgoing_shipment()])
    assert entry.runtime_data.coordinator.known_codes == {INCOMING_CODE, OUTGOING_CODE}
    tracking = await _setup_tracking(hass)
    assert tracking.runtime_data.coordinator.known_codes == {TRACKING_CODE}


async def test_update_contents_rejects_an_empty_code(hass):
    await _setup_account(hass)
    with pytest.raises(ServiceValidationError):
        await _call(hass, {CONF_TRACKING_CODE: "  "})


async def test_update_contents_needs_a_loaded_entry(hass):
    entry = await _setup_account(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    try:
        with pytest.raises(ServiceValidationError):
            await _call(hass, {CONF_TRACKING_CODE: INCOMING_CODE, CONF_CONTENTS: "A"})
    finally:
        entry.mock_state(hass, ConfigEntryState.LOADED)


async def test_two_entries_listing_the_code_both_get_the_value(hass):
    first = await _setup_account(hass, _account_entry("a"))
    second = await _setup_account(hass, _account_entry("b"))
    tracking = await _setup_tracking(
        hass, _tracking_entry((INCOMING_CODE, DIRECTION_INCOMING))
    )
    await _call(hass, {CONF_TRACKING_CODE: INCOMING_CODE, CONF_CONTENTS: "Shoes"})
    assert _contents(first) == "Shoes"
    assert _contents(second) == "Shoes"
    assert _contents(tracking) == "Shoes"


async def test_a_single_entry_is_targeted_directly(hass):
    entry = await _setup_account(hass)
    await _call(hass, {CONF_TRACKING_CODE: UNLISTED, CONF_CONTENTS: "Shoes"})
    assert entry.runtime_data.coordinator.contents_store.get(UNLISTED) == "Shoes"


async def test_entry_required_when_no_entry_lists_the_code(hass):
    await _setup_account(hass, _account_entry("a"))
    await _setup_account(hass, _account_entry("b"))
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, {CONF_TRACKING_CODE: UNLISTED, CONF_CONTENTS: "Shoes"})
    assert err.value.translation_key == "update_contents_entry_required"


async def test_config_entry_id_targets_exactly_one_entry(hass):
    first = await _setup_account(hass, _account_entry("a"))
    second = await _setup_account(hass, _account_entry("b"))
    await _call(
        hass,
        {
            CONF_TRACKING_CODE: INCOMING_CODE,
            CONF_CONTENTS: "Shoes",
            CONF_CONFIG_ENTRY_ID: second.entry_id,
        },
    )
    assert _contents(first) is None
    assert _contents(second) == "Shoes"

    await _call(
        hass,
        {
            CONF_TRACKING_CODE: UNLISTED,
            CONF_CONTENTS: "Socks",
            CONF_CONFIG_ENTRY_ID: first.entry_id,
        },
    )
    assert first.runtime_data.coordinator.contents_store.get(UNLISTED) == "Socks"
    assert second.runtime_data.coordinator.contents_store.get(UNLISTED) is None


async def test_config_entry_id_of_an_unloaded_entry_is_rejected(hass):
    first = await _setup_account(hass, _account_entry("a"))
    second = await _setup_account(hass, _account_entry("b"))
    second.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    try:
        with pytest.raises(ServiceValidationError):
            await _call(
                hass,
                {
                    CONF_TRACKING_CODE: INCOMING_CODE,
                    CONF_CONTENTS: "Shoes",
                    CONF_CONFIG_ENTRY_ID: second.entry_id,
                },
            )
        await _call(hass, {CONF_TRACKING_CODE: INCOMING_CODE, CONF_CONTENTS: "Shoes"})
        assert _contents(first) == "Shoes"
        assert second.runtime_data.coordinator.contents_store.get(INCOMING_CODE) is None
    finally:
        second.mock_state(hass, ConfigEntryState.LOADED)


async def test_the_action_stays_until_the_last_entry_unloads(hass):
    first = await _setup_account(hass, _account_entry("a"))
    second = await _setup_tracking(hass)
    assert await hass.config_entries.async_unload(first.entry_id)
    assert hass.services.has_service(DOMAIN, "update_contents")
    assert await hass.config_entries.async_unload(second.entry_id)
    assert not hass.services.has_service(DOMAIN, "update_contents")


async def test_a_failed_unload_keeps_the_action(hass):
    entry = await _setup_account(hass)
    with patch.object(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)
    ):
        assert not await async_unload_entry(hass, entry)
    assert hass.services.has_service(DOMAIN, "update_contents")


async def test_a_stored_value_survives_a_restart_of_the_entry(hass, hass_storage):
    entry = await _setup_account(hass)
    await _call(hass, {CONF_TRACKING_CODE: INCOMING_CODE, CONF_CONTENTS: "Shoes"})
    store = entry.runtime_data.coordinator.contents_store
    await store._store.async_save(store._data_to_save())

    assert await hass.config_entries.async_unload(entry.entry_id)
    with patch(ACCOUNT_CLIENT, return_value=entry.client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert _contents(entry) == "Shoes"


async def test_an_old_entry_without_a_store_still_loads(hass, hass_storage):
    entry = await _setup_account(hass)
    tracking = await _setup_tracking(hass)
    assert STORE_KEY % entry.entry_id not in hass_storage
    assert STORE_KEY % tracking.entry_id not in hass_storage
    assert _contents(entry) is None
    assert _contents(tracking, TRACKING_CODE) is None


@pytest.mark.parametrize("kind", ["account", "tracking"])
async def test_removing_the_entry_removes_its_store(hass, hass_storage, kind):
    entry = await (_setup_account(hass) if kind == "account" else _setup_tracking(hass))
    code = INCOMING_CODE if kind == "account" else TRACKING_CODE
    await _call(hass, {CONF_TRACKING_CODE: code, CONF_CONTENTS: "Shoes"})
    store = entry.runtime_data.coordinator.contents_store
    await store._store.async_save(store._data_to_save())
    key = STORE_KEY % entry.entry_id
    assert key in hass_storage

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage
