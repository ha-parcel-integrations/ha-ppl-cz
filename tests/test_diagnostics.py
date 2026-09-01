"""Tests for PPL CZ diagnostics."""
from datetime import timedelta
from unittest.mock import MagicMock

from custom_components.ppl_cz.diagnostics import async_get_config_entry_diagnostics


async def test_diagnostics_redacts_and_counts(hass):
    """Diagnostics get pasted into public issues — nothing identifying survives."""
    entry = MagicMock()
    entry.data = {
        "email": "fam@example.com",
        "password": "S0meAzureP4ss!!",
        "access_token": "secret-access",
        "token_expires_at": "2026-01-01T00:00:00+00:00",
    }
    entry.options = {"refresh_interval": 30}
    parcel = {
        "barcode": "10000000001",
        "status": "in_transit",
        "sender": "Example Shop",
        "pickup_point": "AlzaBox Central",
        "url": "https://www.ppl.cz/vyhledat-zasilku?shipmentId=10000000001",
        "raw": {
            "ownership": "OWNER",
            "cod": 199.0,
            "phaseText": "In transport",
            "discriminator": "Incoming",
        },
    }
    entry.runtime_data.coordinator.data = [parcel]
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.outgoing = []
    entry.runtime_data.coordinator.delivered_outgoing = []
    entry.runtime_data.coordinator.current_tier_minutes = 45
    entry.runtime_data.coordinator.update_interval = timedelta(minutes=45)

    result = await async_get_config_entry_diagnostics(None, entry)

    assert result["counts"]["incoming_active"] == 1
    assert result["polling"]["current_tier_minutes"] == 45
    assert result["polling"]["update_interval_seconds"] == 45 * 60
    assert result["entry_data"]["email"] == "**REDACTED**"
    assert result["entry_data"]["password"] == "**REDACTED**"
    assert result["entry_data"]["access_token"] == "**REDACTED**"
    incoming = result["incoming"][0]
    assert incoming["barcode"] == "**REDACTED**"
    assert incoming["sender"] == "**REDACTED**"
    assert incoming["pickup_point"] == "**REDACTED**"
    assert incoming["url"] == "**REDACTED**"
    assert incoming["raw"]["cod"] == "**REDACTED**"
    # non-identifying fields survive
    assert incoming["status"] == "in_transit"
    assert incoming["raw"]["ownership"] == "OWNER"


async def test_diagnostics_polling_handles_no_update_interval(hass):
    """A fixed-interval entry has no current tier; ``update_interval`` can
    also be ``None`` (e.g. before the first successful refresh)."""
    entry = MagicMock()
    entry.data = {"email": "fam@example.com"}
    entry.options = {"refresh_interval": 30}
    entry.runtime_data.coordinator.data = []
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.outgoing = []
    entry.runtime_data.coordinator.delivered_outgoing = []
    entry.runtime_data.coordinator.current_tier_minutes = None
    entry.runtime_data.coordinator.update_interval = None

    result = await async_get_config_entry_diagnostics(None, entry)

    assert result["polling"]["current_tier_minutes"] is None
    assert result["polling"]["update_interval_seconds"] is None
