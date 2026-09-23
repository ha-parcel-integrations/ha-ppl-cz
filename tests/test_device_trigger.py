"""Tests for PPL CZ device triggers."""
from unittest.mock import AsyncMock, patch

from custom_components.ppl_cz.const import DOMAIN
from custom_components.ppl_cz.device_trigger import (
    TRIGGER_EVENTS,
    async_attach_trigger,
    async_get_triggers,
)


async def test_get_triggers_returns_incoming_and_outgoing(hass):
    triggers = await async_get_triggers(hass, "device123")
    types = {t["type"] for t in triggers}
    # No *_delivery_time_changed — PPL CZ's list DTOs expose no ETA. Incoming
    # get registered/status/delivered; outgoing get status/delivered.
    assert types == {
        "parcel_registered",
        "parcel_status_changed",
        "parcel_delivered",
        "outgoing_parcel_status_changed",
        "outgoing_parcel_delivered",
    }
    for trigger in triggers:
        assert trigger["domain"] == DOMAIN
        assert trigger["device_id"] == "device123"


def test_trigger_events_map_to_domain_prefix():
    assert TRIGGER_EVENTS["parcel_registered"] == f"{DOMAIN}_parcel_registered"


async def test_attach_trigger_delegates_to_the_event_trigger(hass):
    config = {
        "platform": "device",
        "domain": DOMAIN,
        "device_id": "device123",
        "type": "parcel_delivered",
    }
    action = AsyncMock()
    with patch(
        "custom_components.ppl_cz.device_trigger.event_trigger.async_attach_trigger",
        new=AsyncMock(return_value=lambda: None),
    ) as attach:
        await async_attach_trigger(hass, config, action, {})
    assert attach.await_args is not None
    event_config = attach.await_args.args[1]
    assert f"{DOMAIN}_parcel_delivered" in str(event_config["event_type"])
    assert event_config["event_data"] == {"device_id": "device123"}
