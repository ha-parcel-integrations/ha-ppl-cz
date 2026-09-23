"""PPL CZ website tracking-by-number API client.

One POST per barcode against the public website's own backend
(``api.dhl.com/ecs/ppl/webapi/TrackAndTrace``) — a second, independent
surface from the mojePPL account's ``/mobapp`` gateway, needing no
credential of the user's own. Three transport rules, all load-bearing:

* **The empty JSON body is required.** Omitting it is a ``415
  UnsupportedMediaType``, not a ``400`` — this client always sends ``{}``.
* **No reCAPTCHA token, cookie, ``Origin`` or ``Referer`` is needed.**
  Stripping all of them still returns ``200``; do not add them "to look
  like a browser".
* **Not found is a ``400``, not a ``404``**, with
  ``detail: "service.TrackAndTrace.ShipmentNotFound"``. Match on ``detail``,
  never on the status alone — a real ``400`` (malformed request, a rejected
  key) is otherwise indistinguishable, and treating one as "not found" would
  silently hide a broken integration.

Reuses the account source's :class:`~custom_components.ppl_cz.account.api.PPLCZApiError`
hierarchy is deliberately **not** done here — this route has no credential at
all, so nothing can expire and there is no auth-error path. A rejected key is
a compatibility failure, never a reason to raise into reauth.
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from ..const import TRACKING_API_URL, TRACKING_DHL_API_KEY, TRACKING_NOT_FOUND_DETAIL

_LOGGER = logging.getLogger(__name__)

_DHL_API_KEY_HEADER = "dhl-api-key"


class PPLCZTrackingApiError(Exception):
    """Raised when the tracking lookup fails for a non-"not found" reason."""

    def __init__(self, detail: str) -> None:
        """Store the detail that triggered the error."""
        super().__init__(f"PPL CZ tracking request failed: {detail}")
        self.detail = detail


class PPLCZTrackingNotFound(PPLCZTrackingApiError):
    """Raised when PPL confirms no shipment matches this barcode.

    Distinct from :class:`PPLCZTrackingApiError` so the coordinator can mark
    one barcode as unknown rather than failing the whole poll — matched on
    the response body's ``detail`` field, never on the ``400`` status alone.
    """

    def __init__(self) -> None:
        """No extra detail needed — the not-found signal is the whole story."""
        super().__init__(TRACKING_NOT_FOUND_DETAIL)


class PPLCZTrackingApiClient:
    """Client for the keyless-to-the-user, keyed-to-us website tracking route."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise the client with an aiohttp session."""
        self._session = session

    async def async_get_parcel(self, tracking_code: str) -> dict[str, Any]:
        """Fetch one shipment by tracking code.

        Raises :class:`PPLCZTrackingNotFound` when PPL reports the code as
        unknown, :class:`PPLCZTrackingApiError` for any other failure.
        Network errors propagate as ``aiohttp.ClientError``.
        """
        url = TRACKING_API_URL.format(tracking_code=tracking_code)
        async with self._session.post(
            url,
            headers={_DHL_API_KEY_HEADER: TRACKING_DHL_API_KEY},
            json={},
        ) as response:
            if response.status == 400:
                body = await _safe_json(response)
                detail = body.get("detail") if isinstance(body, dict) else None
                if detail == TRACKING_NOT_FOUND_DETAIL:
                    raise PPLCZTrackingNotFound
                raise PPLCZTrackingApiError(f"HTTP 400 ({detail or 'no detail'})")
            if response.status != 200:
                raise PPLCZTrackingApiError(f"HTTP {response.status}")
            try:
                payload = await response.json(content_type=None)
            except ValueError as err:
                raise PPLCZTrackingApiError(f"unparseable body ({err})") from err

        if not isinstance(payload, dict):
            raise PPLCZTrackingApiError("unexpected body (not a JSON object)")
        return payload


async def _safe_json(response: aiohttp.ClientResponse) -> Any:
    """Best-effort JSON body from a ``400`` response.

    Never raises — this only runs on a path that is already about to raise
    its own error; an unreadable body reads as "no detail" to the caller.
    """
    try:
        return await response.json(content_type=None)
    except ValueError:
        return None
