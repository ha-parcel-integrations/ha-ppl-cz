"""Diagnostics support for the PPL CZ parcel tracker integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import PPLCZConfigEntry

# Diagnostics are pasted into public issues, so redact anything that
# identifies a person, an address, the account or a specific parcel.
# Over-redacting is cheap; under-redacting leaks a user's home address (or
# their access token) into a GitHub thread.
#
# Per the mechanics doc: sender/recipient names and addresses, toAddress,
# toDeliveryPoint's street/city/zipCode, any cod amount, the tracking number
# itself, and the access_token/dhl-api-key headers.
TO_REDACT = {
    # account / session (entry.data) — "password" is the stored PIN-exchange
    # Azure password the client re-authenticates with (see api.py); it is a
    # live, reusable credential, not a token, so it must never appear
    # unredacted in diagnostics pasted into a public issue.
    "email",
    "password",
    "access_token",
    "token_expires_at",
    "dhl-api-key",
    # canonical fields we publish ourselves
    "tracking_code",
    "barcode",
    "sender",
    "receiver",
    "pickup_point",
    "url",
    # PPL CZ payload fields
    "number",
    "toAddress",
    "toDeliveryPoint",
    "street",
    "city",
    "zipCode",
    "countryCode",
    "cod",
    "name",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PPLCZConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the PPL CZ config entry."""
    coordinator = entry.runtime_data.coordinator

    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        "counts": {
            "incoming_active": len(coordinator.data or []),
            "incoming_delivered": len(coordinator.delivered or []),
            "outgoing_active": len(coordinator.outgoing or []),
            "outgoing_delivered": len(coordinator.delivered_outgoing or []),
        },
        "polling": {
            "current_tier_minutes": coordinator.current_tier_minutes,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
        },
        "incoming": async_redact_data(coordinator.data or [], TO_REDACT),
        "incoming_delivered": async_redact_data(
            coordinator.delivered or [], TO_REDACT
        ),
        "outgoing": async_redact_data(coordinator.outgoing or [], TO_REDACT),
        "outgoing_delivered": async_redact_data(
            coordinator.delivered_outgoing or [], TO_REDACT
        ),
    }
