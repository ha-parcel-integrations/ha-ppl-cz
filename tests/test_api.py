"""Tests for the mojePPL account API client."""
import asyncio
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest

from custom_components.ppl_cz.api import (
    PPLCZApiClient,
    PPLCZApiError,
    PPLCZAuthError,
    PPLCZInvalidPin,
    _parse_expires_in,
)
from custom_components.ppl_cz.const import (
    AZURE_TOKEN_URL,
    REGISTRATION_CONFIRM_URL,
    REGISTRATIONS_URL,
    SHIPMENT_EVENTS_URL,
    SHIPMENTS_URL,
)

from .payloads import (
    PASSWORD_GRANT_TOKENS,
    REGISTRATION_CONFIRM_BODY,
    REMINTED_TOKENS,
    events_for_delivered,
    incoming_shipment,
    shipments_envelope,
)

CONFIRM_URL = REGISTRATION_CONFIRM_URL.format(registration_session_id="reg-session-1")
EVENTS_URL = SHIPMENT_EVENTS_URL.format(shipment_id="id-1")

EMPTY_BODY = object()  # a 200 with a zero-byte body, the stale-connection symptom
HTML_ERROR_PAGE = object()  # a 200 with an Azure B2C outage page instead of JSON
_HTML_ERROR_PAGE_BODY = b"<!DOCTYPE html>\r\n<!-- CorrelationId: test -->\r\n<html/>"


class _Resp:
    def __init__(self, status: int, body: object = None) -> None:
        self.status = status
        self._body = body

    async def read(self) -> bytes:
        if self._body is EMPTY_BODY:
            return b""
        if self._body is HTML_ERROR_PAGE:
            return _HTML_ERROR_PAGE_BODY
        return b"x"

    async def json(self, content_type=None):
        if isinstance(self._body, Exception):
            raise self._body
        if self._body is HTML_ERROR_PAGE:
            raise ValueError("unexpected character: line 1 column 1 (char 0)")
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    """Routes post/put/request to queued responses keyed by (method, url)."""

    def __init__(self, routes: dict) -> None:
        self._routes = {k: list(v) for k, v in routes.items()}
        self.calls: list[tuple[str, str]] = []

    def _pop(self, method: str, url: str):
        self.calls.append((method, url))
        queue = self._routes.get((method, url))
        if not queue:
            raise AssertionError(f"unexpected {method} {url}")
        item = queue.pop(0)
        if isinstance(item, aiohttp.ClientError):
            raise item
        return _Resp(*item)

    def post(self, url, json=None, data=None, headers=None):
        return self._pop("post", url)

    def put(self, url, json=None, headers=None):
        return self._pop("put", url)

    def request(self, method, url, headers=None):
        return self._pop(method.lower(), url)


# --- _parse_expires_in --------------------------------------------------


def test_parse_expires_in_string_and_number():
    assert _parse_expires_in("3600") == 3600
    assert _parse_expires_in(3600) == 3600


def test_parse_expires_in_garbage_falls_back():
    assert _parse_expires_in(None) == 3600
    assert _parse_expires_in("not-a-number") == 3600


# --- request PIN ----------------------------------------------------------


async def test_request_pin_ok():
    session = _Session({("post", REGISTRATIONS_URL): [(204, None)]})
    await PPLCZApiClient(session).async_request_pin("a@b.c", "dev1", "sess1")
    assert ("post", REGISTRATIONS_URL) in session.calls


async def test_request_pin_error_raises():
    session = _Session({("post", REGISTRATIONS_URL): [(500, {})]})
    with pytest.raises(PPLCZApiError):
        await PPLCZApiClient(session).async_request_pin("a@b.c", "dev1", "sess1")


# --- confirm PIN ------------------------------------------------------------


async def test_confirm_pin_returns_password():
    session = _Session({("put", CONFIRM_URL): [(200, REGISTRATION_CONFIRM_BODY)]})
    password = await PPLCZApiClient(session).async_confirm_pin("reg-session-1", "1234", "dev1")
    assert password == REGISTRATION_CONFIRM_BODY["password"]


@pytest.mark.parametrize("status", [400, 401, 404, 422])
async def test_confirm_pin_bad_pin_raises_invalid(status):
    session = _Session({("put", CONFIRM_URL): [(status, {})]})
    with pytest.raises(PPLCZInvalidPin):
        await PPLCZApiClient(session).async_confirm_pin("reg-session-1", "0000", "dev1")


async def test_confirm_pin_other_status_raises_api_error():
    session = _Session({("put", CONFIRM_URL): [(503, {})]})
    with pytest.raises(PPLCZApiError):
        await PPLCZApiClient(session).async_confirm_pin("reg-session-1", "1234", "dev1")


async def test_confirm_pin_missing_password_raises():
    session = _Session({("put", CONFIRM_URL): [(200, {"registrationSessionId": "x"})]})
    with pytest.raises(PPLCZApiError):
        await PPLCZApiClient(session).async_confirm_pin("reg-session-1", "1234", "dev1")


async def test_confirm_pin_unparseable_body_raises():
    session = _Session({("put", CONFIRM_URL): [(200, ValueError("nope"))]})
    with pytest.raises(PPLCZApiError):
        await PPLCZApiClient(session).async_confirm_pin("reg-session-1", "1234", "dev1")


async def test_unparseable_body_logs_raw_content(caplog):
    session = _Session({("put", CONFIRM_URL): [(200, ValueError("nope"))]})
    with caplog.at_level("DEBUG"):
        with pytest.raises(PPLCZApiError):
            await PPLCZApiClient(session).async_confirm_pin("reg-session-1", "1234", "dev1")
    assert "unparseable body" in caplog.text
    assert "b'x'" in caplog.text


# --- exchange password / token storage -------------------------------------


async def test_exchange_password_stores_tokens_and_reports_update():
    updated = []
    session = _Session({("post", AZURE_TOKEN_URL): [(200, PASSWORD_GRANT_TOKENS)]})
    client = PPLCZApiClient(session, on_tokens_updated=lambda *args: updated.append(args))
    await client.async_exchange_password("a@b.c", "onetimepass")

    assert client.access_token == PASSWORD_GRANT_TOKENS["access_token"]
    assert client.token_expires_at is not None
    assert len(updated) == 1
    assert updated[0] == (client.access_token, client.token_expires_at)


@pytest.mark.parametrize("status", [400, 401])
async def test_exchange_password_rejected_raises_auth_error(status):
    session = _Session({("post", AZURE_TOKEN_URL): [(status, {})]})
    with pytest.raises(PPLCZAuthError):
        await PPLCZApiClient(session).async_exchange_password("a@b.c", "bad")


async def test_exchange_password_outage_raises_api_error():
    session = _Session({("post", AZURE_TOKEN_URL): [(500, {})]})
    with pytest.raises(PPLCZApiError):
        await PPLCZApiClient(session).async_exchange_password("a@b.c", "onetimepass")


async def test_exchange_password_missing_token_raises():
    session = _Session({("post", AZURE_TOKEN_URL): [(200, {"expires_in": "3600"})]})
    with pytest.raises(PPLCZApiError):
        await PPLCZApiClient(session).async_exchange_password("a@b.c", "onetimepass")


# --- re-mint (renewal via a fresh password grant, not a refresh grant) ------


async def test_remint_replaces_the_access_token():
    """PPL's B2C tenant hard-revokes the refresh-token lineage ~1h after
    login regardless of intervening refreshes, and the app itself never
    sends grant_type=refresh_token — so renewal re-runs the password grant
    from the stored credential instead (see api.py's module docstring)."""
    session = _Session(
        {
            ("post", AZURE_TOKEN_URL): [(200, REMINTED_TOKENS)],
            ("get", SHIPMENTS_URL): [(200, shipments_envelope([]))],
        }
    )
    client = PPLCZApiClient(
        session,
        email="a@b.c",
        password="pw",
        access_token="stale",
        token_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    await client.async_get_parcels()
    assert client.access_token == REMINTED_TOKENS["access_token"]


async def test_concurrent_requests_share_a_single_remint():
    """Two callers racing a stale token (e.g. the refresh button firing
    alongside the scheduled poll) must not both re-mint independently —
    wasteful, not harmful, but worth avoiding."""
    session = _Session(
        {
            ("post", AZURE_TOKEN_URL): [(200, REMINTED_TOKENS)],
            ("get", SHIPMENTS_URL): [
                (200, shipments_envelope([])),
                (200, shipments_envelope([])),
            ],
        }
    )
    client = PPLCZApiClient(
        session,
        email="a@b.c",
        password="pw",
        access_token="stale",
        token_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    results = await asyncio.gather(
        client.async_get_parcels(), client.async_get_parcels()
    )
    assert results == [[], []]
    assert client.access_token == REMINTED_TOKENS["access_token"]
    assert session.calls.count(("post", AZURE_TOKEN_URL)) == 1


async def test_remint_without_credentials_raises_auth_error():
    client = PPLCZApiClient(_Session({}))
    with pytest.raises(PPLCZAuthError):
        await client.async_get_parcels()


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_remint_rejected_raises_auth_error(status):
    session = _Session({("post", AZURE_TOKEN_URL): [(status, {})]})
    client = PPLCZApiClient(session, email="a@b.c", password="pw")
    with pytest.raises(PPLCZAuthError):
        await client.async_get_parcels()


async def test_remint_rejected_logs_azure_error_body(caplog):
    body = {"error": "invalid_grant", "error_description": "AADB2C90129: revoked"}
    session = _Session({("post", AZURE_TOKEN_URL): [(400, body)]})
    client = PPLCZApiClient(session, email="a@b.c", password="pw")
    with caplog.at_level("DEBUG"):
        with pytest.raises(PPLCZAuthError):
            await client.async_get_parcels()
    assert "invalid_grant" in caplog.text
    assert "AADB2C90129: revoked" in caplog.text


async def test_remint_rejected_unparseable_body_does_not_mask_auth_error():
    session = _Session({("post", AZURE_TOKEN_URL): [(400, ValueError("nope"))]})
    client = PPLCZApiClient(session, email="a@b.c", password="pw")
    with pytest.raises(PPLCZAuthError):
        await client.async_get_parcels()


async def test_remint_outage_raises_api_error():
    session = _Session({("post", AZURE_TOKEN_URL): [(500, {})]})
    client = PPLCZApiClient(session, email="a@b.c", password="pw")
    with pytest.raises(PPLCZApiError):
        await client.async_get_parcels()


async def test_remint_network_error_is_api_error():
    session = _Session({("post", AZURE_TOKEN_URL): [aiohttp.ClientError()]})
    client = PPLCZApiClient(session, email="a@b.c", password="pw")
    with pytest.raises(PPLCZApiError):
        await client.async_get_parcels()


async def test_remint_retries_once_on_empty_body():
    """A stale pooled connection (e.g. after the host wakes from sleep) can
    hand back a syntactically valid 200 with a zero-byte body instead of
    erroring outright — worth one retry rather than treating it as an
    auth rejection."""
    session = _Session(
        {
            ("post", AZURE_TOKEN_URL): [(200, EMPTY_BODY), (200, REMINTED_TOKENS)],
            ("get", SHIPMENTS_URL): [(200, shipments_envelope([]))],
        }
    )
    client = PPLCZApiClient(session, email="a@b.c", password="pw")
    await client.async_get_parcels()
    assert client.access_token == REMINTED_TOKENS["access_token"]
    assert session.calls.count(("post", AZURE_TOKEN_URL)) == 2


async def test_remint_raises_after_two_empty_bodies():
    session = _Session(
        {("post", AZURE_TOKEN_URL): [(200, EMPTY_BODY), (200, EMPTY_BODY)]}
    )
    client = PPLCZApiClient(session, email="a@b.c", password="pw")
    with pytest.raises(PPLCZApiError):
        await client.async_get_parcels()


async def test_remint_retries_once_on_html_error_page():
    """Confirmed live 2026-08-24: Azure B2C can hand back its own HTML
    outage page on a 200 instead of the token JSON — worth one retry like
    the empty-body case, not an immediate auth rejection."""
    session = _Session(
        {
            ("post", AZURE_TOKEN_URL): [(200, HTML_ERROR_PAGE), (200, REMINTED_TOKENS)],
            ("get", SHIPMENTS_URL): [(200, shipments_envelope([]))],
        }
    )
    client = PPLCZApiClient(session, email="a@b.c", password="pw")
    await client.async_get_parcels()
    assert client.access_token == REMINTED_TOKENS["access_token"]
    assert session.calls.count(("post", AZURE_TOKEN_URL)) == 2


async def test_remint_raises_after_two_html_error_pages():
    session = _Session(
        {("post", AZURE_TOKEN_URL): [(200, HTML_ERROR_PAGE), (200, HTML_ERROR_PAGE)]}
    )
    client = PPLCZApiClient(session, email="a@b.c", password="pw")
    with pytest.raises(PPLCZApiError):
        await client.async_get_parcels()


# --- authed requests / retry-on-401 -----------------------------------------


async def test_401_triggers_one_remint_and_retry():
    session = _Session(
        {
            ("post", AZURE_TOKEN_URL): [(200, PASSWORD_GRANT_TOKENS), (200, REMINTED_TOKENS)],
            ("get", SHIPMENTS_URL): [(401, {}), (200, shipments_envelope([]))],
        }
    )
    client = PPLCZApiClient(session, email="a@b.c", password="pw")
    assert await client.async_get_parcels() == []
    assert client.access_token == REMINTED_TOKENS["access_token"]


async def test_persistent_401_raises_auth_error():
    session = _Session(
        {
            ("post", AZURE_TOKEN_URL): [(200, PASSWORD_GRANT_TOKENS), (200, REMINTED_TOKENS)],
            ("get", SHIPMENTS_URL): [(401, {}), (401, {})],
        }
    )
    client = PPLCZApiClient(session, email="a@b.c", password="pw")
    with pytest.raises(PPLCZAuthError):
        await client.async_get_parcels()


async def test_authed_request_non_200_raises_api_error():
    session = _Session(
        {
            ("post", AZURE_TOKEN_URL): [(200, PASSWORD_GRANT_TOKENS)],
            ("get", SHIPMENTS_URL): [(503, {})],
        }
    )
    client = PPLCZApiClient(session, email="a@b.c", password="pw")
    with pytest.raises(PPLCZApiError):
        await client.async_get_parcels()


async def test_authed_request_retries_once_on_empty_body():
    session = _Session(
        {
            ("post", AZURE_TOKEN_URL): [(200, PASSWORD_GRANT_TOKENS)],
            ("get", SHIPMENTS_URL): [(200, EMPTY_BODY), (200, shipments_envelope([]))],
        }
    )
    client = PPLCZApiClient(session, email="a@b.c", password="pw")
    assert await client.async_get_parcels() == []
    assert session.calls.count(("get", SHIPMENTS_URL)) == 2


async def test_authed_request_raises_after_two_empty_bodies():
    session = _Session(
        {
            ("post", AZURE_TOKEN_URL): [(200, PASSWORD_GRANT_TOKENS)],
            ("get", SHIPMENTS_URL): [(200, EMPTY_BODY), (200, EMPTY_BODY)],
        }
    )
    client = PPLCZApiClient(session, email="a@b.c", password="pw")
    with pytest.raises(PPLCZApiError):
        await client.async_get_parcels()


async def test_authed_request_retries_once_on_html_error_page():
    session = _Session(
        {
            ("post", AZURE_TOKEN_URL): [(200, PASSWORD_GRANT_TOKENS)],
            ("get", SHIPMENTS_URL): [(200, HTML_ERROR_PAGE), (200, shipments_envelope([]))],
        }
    )
    client = PPLCZApiClient(session, email="a@b.c", password="pw")
    assert await client.async_get_parcels() == []
    assert session.calls.count(("get", SHIPMENTS_URL)) == 2


# --- shipments ---------------------------------------------------------------


async def test_get_parcels_returns_items():
    items = [incoming_shipment()]
    session = _Session(
        {
            ("post", AZURE_TOKEN_URL): [(200, PASSWORD_GRANT_TOKENS)],
            ("get", SHIPMENTS_URL): [(200, shipments_envelope(items))],
        }
    )
    client = PPLCZApiClient(session, email="a@b.c", password="pw")
    parcels = await client.async_get_parcels()
    assert len(parcels) == 1
    assert parcels[0]["number"] == items[0]["number"]


async def test_get_parcels_skips_non_dict_entries():
    envelope = shipments_envelope([incoming_shipment(), "junk"])
    session = _Session(
        {
            ("post", AZURE_TOKEN_URL): [(200, PASSWORD_GRANT_TOKENS)],
            ("get", SHIPMENTS_URL): [(200, envelope)],
        }
    )
    client = PPLCZApiClient(session, email="a@b.c", password="pw")
    assert len(await client.async_get_parcels()) == 1


async def test_get_parcels_missing_items_raises():
    session = _Session(
        {
            ("post", AZURE_TOKEN_URL): [(200, PASSWORD_GRANT_TOKENS)],
            ("get", SHIPMENTS_URL): [(200, {"metadata": {}})],
        }
    )
    client = PPLCZApiClient(session, email="a@b.c", password="pw")
    with pytest.raises(PPLCZApiError):
        await client.async_get_parcels()


# --- events ------------------------------------------------------------------


async def test_get_shipment_events_accepts_bare_list():
    events = events_for_delivered()
    session = _Session(
        {
            ("post", AZURE_TOKEN_URL): [(200, PASSWORD_GRANT_TOKENS)],
            ("get", EVENTS_URL): [(200, events)],
        }
    )
    client = PPLCZApiClient(session, email="a@b.c", password="pw")
    result = await client.async_get_shipment_events("id-1")
    assert len(result) == len(events)


async def test_get_shipment_events_accepts_items_wrapper():
    events = events_for_delivered()
    session = _Session(
        {
            ("post", AZURE_TOKEN_URL): [(200, PASSWORD_GRANT_TOKENS)],
            ("get", EVENTS_URL): [(200, {"items": events})],
        }
    )
    client = PPLCZApiClient(session, email="a@b.c", password="pw")
    result = await client.async_get_shipment_events("id-1")
    assert len(result) == len(events)


async def test_get_shipment_events_unexpected_shape_returns_none():
    session = _Session(
        {
            ("post", AZURE_TOKEN_URL): [(200, PASSWORD_GRANT_TOKENS)],
            ("get", EVENTS_URL): [(200, "nope")],
        }
    )
    client = PPLCZApiClient(session, email="a@b.c", password="pw")
    assert await client.async_get_shipment_events("id-1") is None


async def test_get_shipment_events_outage_returns_none():
    session = _Session(
        {
            ("post", AZURE_TOKEN_URL): [(200, PASSWORD_GRANT_TOKENS)],
            ("get", EVENTS_URL): [(500, {})],
        }
    )
    client = PPLCZApiClient(session, email="a@b.c", password="pw")
    assert await client.async_get_shipment_events("id-1") is None


async def test_get_shipment_events_auth_error_propagates():
    session = _Session(
        {
            ("post", AZURE_TOKEN_URL): [(200, PASSWORD_GRANT_TOKENS), (200, REMINTED_TOKENS)],
            ("get", EVENTS_URL): [(401, {}), (401, {})],
        }
    )
    client = PPLCZApiClient(session, email="a@b.c", password="pw")
    with pytest.raises(PPLCZAuthError):
        await client.async_get_shipment_events("id-1")
