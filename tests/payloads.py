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


# --- website tracking-by-number source --------------------------------------
#
# Every field here is synthetic — no real tracking code, sender/recipient
# name, address or access-point identifier from any live probe ever appears
# in this repo. Shapes mirror api/tracking.md's "Payload — website surface"
# section (structure only, no captured values).

TRACKING_CODE = "10000000009"


def tracking_access_point(*, name: str = "Example Pickup Point") -> dict:
    """A synthetic ``accessPoint`` object."""
    return {
        "accessPointId": "AP-TEST",
        "code": "TEST01",
        "depot": "10",
        "name": name,
        "street": "Example Street 1",
        "city": "Testville",
        "zipCode": "00000",
        "country": "CZ",
        "parcelshopName": name,
        "gps": {"lat": 0.0, "lng": 0.0},
        "openHours": [],
    }


def tracking_event(code: str, event_date: str, *, event_text: str | None = None) -> dict:
    """A synthetic tracking-surface event entry."""
    return {"code": code, "eventDate": event_date, "eventText": event_text}


def tracking_shipment(
    code: str = TRACKING_CODE,
    *,
    phase: str = "ShipmentInTransport",
    access_point: dict | None = None,
    expected_delivery_date: str | None = None,
    events: list[dict] | None = None,
    weight: float | None = 1.2,
) -> dict:
    """A synthetic website-tracking-surface response body."""
    return {
        "shipmentId": code,
        "weight": weight,
        # Mirrors the confirmed live shape: a single entry keyed on an
        # integer type, not the string one an earlier fixture invented.
        "addresses": [{"type": 4, "country": "CZ", "name": "Example Sender"}],
        "phase": phase,
        "lastEventCode": phase,
        "lastEventText": "Example status text",
        "lastEventDate": events[-1]["eventDate"] if events else None,
        "expectedDeliveryDate": expected_delivery_date,
        "customerReference": None,
        "cod": None,
        "events": events or [],
        "accessPoint": access_point,
        "hierarchy": {"parentReference": None, "childReference": None},
        "packagesInSet": 1,
        "isBackToSender": False,
        "externalShipmentId": "JJD000000000000000000000000",
        "pinGenerated": True,
        "eveningDelivery": False,
        "deliveryChangeAllowed": False,
        "shipmentRefuseAllowed": False,
        "podReportVisible": True,
        "ePopReportVisible": False,
        # Presentational only — kept in the fixture so the curated-raw test
        # proves they are filtered out, not merely absent.
        "showDeliveryDate": False,
        "showParcelShop": False,
        "editMode": None,
    }


def tracking_events_for_delivered() -> list[dict]:
    """A representative event history for a delivered tracking-source parcel."""
    return [
        tracking_event("WaitingForShipment", "2026-05-01T08:00:00Z"),
        tracking_event("ShipmentInTransport", "2026-05-01T20:00:00Z"),
        tracking_event("PreparingForDelivery", "2026-05-02T05:00:00Z"),
        tracking_event("Delivered.Parcelshop", "2026-05-02T09:00:00Z"),
        tracking_event("Delivered", "2026-05-02T09:05:00Z"),
    ]
