"""Tests for the PPL CZ setup / unload / reauth lifecycle."""
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ppl_cz.account.api import PPLCZApiError, PPLCZAuthError
from custom_components.ppl_cz.account.coordinator import PPLCZCoordinator
from custom_components.ppl_cz.const import (
    CONF_ACCESS_TOKEN,
    CONF_BARCODE,
    CONF_DIRECTION,
    CONF_EMAIL,
    CONF_PARCELS,
    CONF_SOURCE,
    DIRECTION_INCOMING,
    DOMAIN,
    SOURCE_TRACKING,
)
from custom_components.ppl_cz.tracking.coordinator import PPLCZTrackingCoordinator

from .payloads import TRACKING_CODE, incoming_shipment, tracking_shipment

EMAIL = "user@example.test"


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=EMAIL,
        unique_id=EMAIL,
        data={
            CONF_EMAIL: EMAIL,
            CONF_PASSWORD: "S0meAzureP4ss!!",
            CONF_ACCESS_TOKEN: "access-abc",
        },
    )


def _client(items=None) -> MagicMock:
    client = MagicMock()
    client.async_get_parcels = AsyncMock(return_value=items or [])
    client.async_get_shipment_events = AsyncMock(return_value=None)
    client.async_get_delivery_info = AsyncMock(return_value=None)
    return client


def _patch(client):
    return patch("custom_components.ppl_cz.PPLCZApiClient", return_value=client)


async def test_setup_uses_a_cookie_free_session(hass, stub_ppl_session):
    """The entry must not ride on Home Assistant's shared cookie jar.

    Azure B2C's ``x-ms-cpim-*`` cookies accumulate there and eventually break
    the ROPC grant until HA restarts — see ``session.py``.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    with _patch(_client([incoming_shipment()])):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    jar = stub_ppl_session.call_args.kwargs["cookie_jar"]
    assert isinstance(jar, aiohttp.DummyCookieJar)
    # Closed on unload, so a reload doesn't leak a session per attempt.
    assert stub_ppl_session.call_args.kwargs["auto_cleanup"] is False
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    stub_ppl_session.return_value.close.assert_awaited()


async def test_setup_and_unload(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    with _patch(_client([incoming_shipment()])):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    incoming = hass.states.get("sensor.ppl_cz_user_example_test_incoming_parcels")
    assert incoming is not None
    assert incoming.state == "1"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_missing_password_triggers_reauth(hass):
    """A pre-re-mint-model entry (no stored password) goes to reauth, not a crash."""
    entry = MockConfigEntry(
        domain=DOMAIN, title=EMAIL, unique_id=EMAIL, data={CONF_EMAIL: EMAIL}
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_expired_session_starts_reauth(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client()
    client.async_get_parcels.side_effect = PPLCZAuthError("HTTP 401")
    with _patch(client):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(
        flow["context"]["source"] == "reauth"
        for flow in hass.config_entries.flow.async_progress()
    )


async def test_outage_retries_instead_of_reauth(hass):
    """A 5xx must retry with backoff — never push the user into reauth."""
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client()
    client.async_get_parcels.side_effect = PPLCZApiError("HTTP 500")
    with _patch(client):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not hass.config_entries.flow.async_progress()


async def test_token_rotation_persisted(hass):
    """A re-minted access token from a proactive/401 renewal is saved to the entry."""
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client([incoming_shipment()])
    captured: dict = {}

    def _capture(session, **kwargs):
        captured.update(kwargs)
        return client

    with patch("custom_components.ppl_cz.PPLCZApiClient", side_effect=_capture):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    from datetime import datetime, timezone

    captured["on_tokens_updated"]("new-access", datetime.now(timezone.utc))

    assert entry.data[CONF_ACCESS_TOKEN] == "new-access"


async def test_per_parcel_sensor_spawn_and_remove(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client([incoming_shipment()])
    with _patch(client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(hass)
        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{incoming_shipment()['number']}"
        )

        client.async_get_parcels.return_value = [incoming_shipment("70000000001")]
        await entry.runtime_data.coordinator.async_request_refresh()
        await hass.async_block_till_done()

        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_70000000001"
        )
        assert (
            registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_{incoming_shipment()['number']}"
            )
            is None
        )


# --- source dispatch ---------------------------------------------------------


async def test_missing_conf_source_defaults_to_an_account_hub(hass):
    """The tripwire: a pre-0.12.0 entry has no CONF_SOURCE at all.

    Getting the default backwards would silently convert every existing
    user's account hub into an empty tracking hub on upgrade.
    """
    entry = _entry()
    assert CONF_SOURCE not in entry.data
    entry.add_to_hass(hass)
    with _patch(_client([incoming_shipment()])):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert isinstance(entry.runtime_data.coordinator, PPLCZCoordinator)
    # No calendar entity on an account hub, even implicitly.
    assert hass.states.get("calendar.ppl_cz_user_example_test_deliveries") is None


def _tracking_entry(**options) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="PPL CZ (tracking codes)",
        unique_id=f"{DOMAIN}_{SOURCE_TRACKING}",
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={CONF_PARCELS: [], **options},
    )


async def test_tracking_hub_setup_and_unload(hass):
    entry = _tracking_entry(
        parcels=[{CONF_BARCODE: TRACKING_CODE, CONF_DIRECTION: DIRECTION_INCOMING}]
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = tracking_shipment(TRACKING_CODE)
    with patch("custom_components.ppl_cz.PPLCZTrackingApiClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data.coordinator, PPLCZTrackingCoordinator)
    # A tracking hub does get a calendar entity, unlike an account hub.
    assert hass.states.get("calendar.ppl_cz_tracking_codes_deliveries") is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_tracking_hub_options_update_refreshes_without_a_reload(hass):
    entry = _tracking_entry(
        parcels=[{CONF_BARCODE: TRACKING_CODE, CONF_DIRECTION: DIRECTION_INCOMING}]
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = tracking_shipment(
        TRACKING_CODE, phase="WaitingForShipment"
    )
    with patch("custom_components.ppl_cz.PPLCZTrackingApiClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    client.async_get_parcel.return_value = tracking_shipment(
        TRACKING_CODE, phase="ShipmentInTransport"
    )
    # A genuinely different options value, so HA actually fires the update
    # listener — resubmitting an identical dict would be a no-op.
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "include_history": True}
    )
    await hass.async_block_till_done()

    sensor = hass.states.get(f"sensor.ppl_cz_tracking_codes_parcel_{TRACKING_CODE}")
    assert sensor.state == "in_transit"
