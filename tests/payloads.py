"""Sample PPL CZ API payloads shared by the test modules.

Every shape here mirrors the *reconstructed* (not live-confirmed) item shape
in the private mechanics doc — the test account used for the live capture had
zero shipments. Kept in one module so a future correction (once a real
shipment is captured) only has to change one place.
"""
from __future__ import annotations

INCOMING_CODE = "10000000001"
OUTGOING_CODE = "20000000002"

# --- auth ---------------------------------------------------------------

REGISTRATION_CONFIRM_BODY = {
    "registrationSessionId": "reg-session-1",
    "deviceId": "device-1",
    "password": "S0meAzureP4ss!!",
}

# expires_in arrives as a *string* on the password grant. A real response
# also carries a refresh_token (Azure grants one since the request asks for
# offline_access), but the client never stores or uses it — see api.py.
PASSWORD_GRANT_TOKENS = {
    "access_token": "access.eyJ.token",
    "token_type": "Bearer",
    "expires_in": "3600",
    "refresh_token": "refresh-abc",
}

# A second, distinct password-grant response — used to prove a re-mint
# actually replaces the access token, not the refresh grant (the app never
# sends grant_type=refresh_token at all).
REMINTED_TOKENS = {
    "access_token": "access.eyJ.new",
    "token_type": "Bearer",
    "expires_in": "3600",
    "refresh_token": "refresh-def",
}


# --- shipment list --------------------------------------------------------


def delivery_point(
    *, point_type: str = "PARCEL_SHOP", name: str = "AlzaBox Central Station"
) -> dict:
    """A ``ShipmentAccessPointResponseDto`` sample."""
    return {
        "id": "AP1",
        "type": point_type,
        "name": name,
        "street": "Wenceslas Square 1",
        "city": "Praha",
        "zipCode": "11000",
        "countryCode": "CZ",
    }


def incoming_shipment(
    code: str = INCOMING_CODE,
    *,
    status: str = "IN_TRANSPORT",
    to_delivery_point: dict | None = None,
) -> dict:
    """An ``IncomingShipmentResponseDto`` list item."""
    return {
        "number": code,
        "id": f"id-{code}",
        "lastShipmentEvent": status,
        "ownership": "OWNER",
        "cod": None,
        "toDeliveryPoint": to_delivery_point,
        "toAddress": {"street": "Home 1", "city": "Brno", "zipCode": "60200"},
        "phaseText": "In transport",
        "discriminator": "Incoming",
        "sender": "Example Shop s.r.o.",
        "isPaidOnline": False,
        "necessaryOnlinePayment": False,
        "canPayOnline": False,
        "codPaidStatus": None,
    }


def outgoing_shipment(
    code: str = OUTGOING_CODE,
    *,
    status: str = "ORDER",
) -> dict:
    """An ``OutgoingShipmentResponseDto`` list item."""
    return {
        "number": code,
        "id": f"id-{code}",
        "lastShipmentEvent": status,
        "ownership": "OWNER",
        "cod": None,
        "toDeliveryPoint": None,
        "toAddress": {"street": "Recipient 2", "city": "Ostrava", "zipCode": "70200"},
        "phaseText": "Order created",
        "discriminator": "Outgoing",
        "recipient": "Jane Doe",
        "isWaitingForSync": False,
    }


def shipments_envelope(items: list[dict]) -> dict:
    """The confirmed-live list envelope, wrapping reconstructed items."""
    return {
        "metadata": {"pagination": {"offset": 0, "limit": 20, "total": len(items)}},
        "items": items,
    }


# --- event history --------------------------------------------------------


def shipment_event(
    event_code: str, created_at: str, *, message: str | None = None, code: str | None = None
) -> dict:
    """A ``ShipmentEventResponseDto`` sample."""
    return {"event": event_code, "createdAt": created_at, "message": message, "code": code}


def events_for_delivered(code: str = INCOMING_CODE) -> list[dict]:
    """A representative event history for a delivered incoming parcel."""
    return [
        shipment_event(
            "ORDER", "2026-04-27T23:03:58Z", message="Shipment announced", code="Active"
        ),
        shipment_event(
            "IN_TRANSPORT",
            "2026-04-28T15:52:17Z",
            message="Taken over from sender",
            code="ShipmentInTransport.TakeOverFromSender",
        ),
        shipment_event(
            "DELIVERING", "2026-04-29T08:46:00Z", message="Out for delivery", code="OutForDelivery"
        ),
        shipment_event(
            "DELIVERED", "2026-04-29T13:12:42Z", message="Delivered", code="Delivered"
        ),
    ]
