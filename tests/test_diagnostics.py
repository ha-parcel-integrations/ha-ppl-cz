"""Tests for PPL CZ diagnostics."""
from unittest.mock import MagicMock

from custom_components.ppl_cz.diagnostics import async_get_config_entry_diagnostics


async def test_diagnostics_redacts_and_counts(hass):
    """Diagnostics get pasted into public issues — nothing identifying survives."""
    entry = MagicMock()
    entry.data = {
        "email": "fam@example.com",
        "access_token": "secret-access",
        "refresh_token": "secret-refresh",
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

    result = await async_get_config_entry_diagnostics(None, entry)

    assert result["counts"]["incoming_active"] == 1
    assert result["entry_data"]["email"] == "**REDACTED**"
    assert result["entry_data"]["access_token"] == "**REDACTED**"
    assert result["entry_data"]["refresh_token"] == "**REDACTED**"
    incoming = result["incoming"][0]
    assert incoming["barcode"] == "**REDACTED**"
    assert incoming["sender"] == "**REDACTED**"
    assert incoming["pickup_point"] == "**REDACTED**"
    assert incoming["url"] == "**REDACTED**"
    assert incoming["raw"]["cod"] == "**REDACTED**"
    # non-identifying fields survive
    assert incoming["status"] == "in_transit"
    assert incoming["raw"]["ownership"] == "OWNER"
