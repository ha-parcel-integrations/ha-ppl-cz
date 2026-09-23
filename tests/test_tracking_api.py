"""Tests for the website tracking-by-number API client."""
import aiohttp
import pytest

from custom_components.ppl_cz.const import TRACKING_API_URL, TRACKING_DHL_API_KEY
from custom_components.ppl_cz.tracking.api import (
    PPLCZTrackingApiClient,
    PPLCZTrackingApiError,
    PPLCZTrackingNotFound,
)

from .payloads import TRACKING_CODE, tracking_shipment

URL = TRACKING_API_URL.format(tracking_code=TRACKING_CODE)


class _Resp:
    def __init__(self, status: int, body=None) -> None:
        self.status = status
        self._body = body

    async def json(self, content_type=None):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    """Records every POST call's kwargs and hands back one queued response."""

    def __init__(self, response: _Resp) -> None:
        self._response = response
        self.calls: list[dict] = []

    def post(self, url, *, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self._response


async def test_get_parcel_sends_the_empty_json_body_and_the_website_key():
    """The 415 trap: omitting the body must never happen."""
    session = _Session(_Resp(200, tracking_shipment()))
    await PPLCZTrackingApiClient(session).async_get_parcel(TRACKING_CODE)

    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == URL
    assert call["json"] == {}
    assert call["headers"]["dhl-api-key"] == TRACKING_DHL_API_KEY


async def test_get_parcel_returns_the_payload():
    payload = tracking_shipment()
    session = _Session(_Resp(200, payload))
    result = await PPLCZTrackingApiClient(session).async_get_parcel(TRACKING_CODE)
    assert result == payload


async def test_not_found_matches_on_detail_not_status_alone():
    session = _Session(
        _Resp(
            400,
            {
                "title": "BadRequest",
                "status": 400,
                "detail": "service.TrackAndTrace.ShipmentNotFound",
            },
        )
    )
    with pytest.raises(PPLCZTrackingNotFound):
        await PPLCZTrackingApiClient(session).async_get_parcel(TRACKING_CODE)


async def test_a_real_400_that_is_not_the_not_found_detail_raises_a_plain_error():
    """A malformed request or a rejected key must not be read as 'not found'."""
    session = _Session(
        _Resp(400, {"title": "BadRequest", "status": 400, "detail": "something.else"})
    )
    with pytest.raises(PPLCZTrackingApiError) as excinfo:
        await PPLCZTrackingApiClient(session).async_get_parcel(TRACKING_CODE)
    assert not isinstance(excinfo.value, PPLCZTrackingNotFound)


async def test_400_with_unparseable_body_still_raises_a_plain_error():
    session = _Session(_Resp(400, ValueError("bad json")))
    with pytest.raises(PPLCZTrackingApiError) as excinfo:
        await PPLCZTrackingApiClient(session).async_get_parcel(TRACKING_CODE)
    assert not isinstance(excinfo.value, PPLCZTrackingNotFound)


async def test_a_rejected_key_is_a_compatibility_failure_not_an_auth_error():
    """No PPLCZAuthError path exists here — nothing can expire without a credential."""
    session = _Session(_Resp(401, {}))
    with pytest.raises(PPLCZTrackingApiError):
        await PPLCZTrackingApiClient(session).async_get_parcel(TRACKING_CODE)


async def test_unexpected_status_raises():
    session = _Session(_Resp(500, {}))
    with pytest.raises(PPLCZTrackingApiError):
        await PPLCZTrackingApiClient(session).async_get_parcel(TRACKING_CODE)


async def test_unparseable_200_body_raises():
    session = _Session(_Resp(200, ValueError("bad json")))
    with pytest.raises(PPLCZTrackingApiError):
        await PPLCZTrackingApiClient(session).async_get_parcel(TRACKING_CODE)


async def test_non_object_200_body_raises():
    session = _Session(_Resp(200, ["not", "an", "object"]))
    with pytest.raises(PPLCZTrackingApiError):
        await PPLCZTrackingApiClient(session).async_get_parcel(TRACKING_CODE)


async def test_network_error_propagates():
    class _FailingSession:
        def post(self, url, *, headers=None, json=None):
            raise aiohttp.ClientConnectionError("boom")

    with pytest.raises(aiohttp.ClientError):
        await PPLCZTrackingApiClient(_FailingSession()).async_get_parcel(TRACKING_CODE)
