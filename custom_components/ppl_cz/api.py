"""mojePPL account API client.

Passwordless email+PIN login: request a PIN by email, confirm it for a
freshly-minted Azure AD B2C password, then exchange that password for a
bearer token pair via the standard Azure ROPC grant.

* :meth:`async_request_pin` -- step 1, ``POST registrations``.
* :meth:`async_confirm_pin` -- step 2, ``PUT registrations/{id}`` -> a
  one-time password.
* :meth:`async_exchange_password` -- step 3, Azure ROPC ``grant_type=password``.
* :meth:`async_get_parcels` -- the account inbox (both directions, one call).
* :meth:`async_get_shipment_events` -- one parcel's history (a fan-out call).

Every call to the ``/mobapp`` gateway also carries the static, shared
``dhl-api-key`` header (see ``const.py`` for why shipping it is an accepted
risk here). The Azure host needs no such header.

Only the refresh token is meant to survive a restart; the access token is
refreshed automatically (:meth:`_async_ensure_fresh_token`), and the refresh
token itself rotates on every use — a rotated pair is handed back through
``on_tokens_updated`` so the caller can persist it. A failed refresh raises
:class:`PPLCZAuthError`, which the coordinator maps to reauth — this, not a
plain access-token expiry, is the actual "log in again" signal.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

from .const import (
    AZURE_CLIENT_ID,
    AZURE_SCOPE,
    AZURE_TOKEN_URL,
    DHL_API_KEY,
    DHL_API_KEY_HEADER,
    REGISTRATION_CONFIRM_URL,
    REGISTRATIONS_URL,
    SHIPMENT_EVENTS_URL,
    SHIPMENTS_URL,
)

_LOGGER = logging.getLogger(__name__)

# Refresh well before the access token's stated expiry (3600s on the one live
# sample) rather than waiting for a 401 — a request that races an
# about-to-expire token is a needless extra round-trip.
_REFRESH_MARGIN = timedelta(seconds=120)

# expires_in arrives as a JSON string on the password grant and a number on
# the refresh grant (both confirmed live) — this is the fallback only if
# neither form parses, not the expected path.
_DEFAULT_EXPIRES_IN_SECONDS = 3600


class PPLCZApiError(Exception):
    """Raised when a PPL CZ API call fails for a non-auth reason."""

    def __init__(self, detail: str) -> None:
        """Store the detail that triggered the error."""
        super().__init__(f"PPL CZ API request failed: {detail}")
        self.detail = detail


class PPLCZAuthError(PPLCZApiError):
    """Raised when PPL CZ rejects the account's tokens.

    Distinct from :class:`PPLCZApiError` on purpose: only this one may
    trigger Home Assistant's reauth flow.
    """


class PPLCZInvalidPin(PPLCZApiError):
    """Raised when the PIN confirm step rejects the code (config-flow only)."""

    def __init__(self, detail: str = "PIN rejected") -> None:
        """Store the detail, defaulting since the caller usually has none."""
        super().__init__(detail)


class PPLCZApiClient:
    """Client for the mojePPL account API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        access_token: str | None = None,
        refresh_token: str | None = None,
        token_expires_at: datetime | None = None,
        on_tokens_updated: (
            Callable[[str, str, datetime], None] | None
        ) = None,
    ) -> None:
        """Initialise the client, optionally with an existing token pair."""
        self._session = session
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._token_expires_at = token_expires_at
        self._on_tokens_updated = on_tokens_updated
        # Azure rotates the refresh token on every use (single-use). Without
        # this, two callers racing _authed_request (e.g. the manual refresh
        # button firing alongside the scheduled poll) can both see a stale
        # token as "expiring soon" and both redeem it — the loser gets a 400
        # and the integration wrongly treats a live account as logged out.
        self._refresh_lock = asyncio.Lock()

    @property
    def access_token(self) -> str | None:
        """The current access token, if any."""
        return self._access_token

    @property
    def refresh_token(self) -> str | None:
        """The current (possibly rotated) refresh token, if any."""
        return self._refresh_token

    @property
    def token_expires_at(self) -> datetime | None:
        """When the current access token expires, if known."""
        return self._token_expires_at

    # --- login (config flow) ------------------------------------------------

    async def async_request_pin(
        self, email: str, device_id: str, registration_session_id: str
    ) -> None:
        """Ask PPL CZ to e-mail a PIN. A ``204`` means it is on its way."""
        async with self._session.post(
            REGISTRATIONS_URL,
            headers={DHL_API_KEY_HEADER: DHL_API_KEY},
            json={
                "email": email,
                "deviceId": device_id,
                "registrationSessionId": registration_session_id,
            },
        ) as response:
            if response.status != 204:
                raise PPLCZApiError(f"registration HTTP {response.status}")

    async def async_confirm_pin(
        self, registration_session_id: str, pin: str, device_id: str
    ) -> str:
        """Exchange the e-mailed PIN for a freshly-minted Azure password.

        Raises :class:`PPLCZInvalidPin` when the PIN is wrong, expired or the
        registration session is unknown.
        """
        url = REGISTRATION_CONFIRM_URL.format(
            registration_session_id=registration_session_id
        )
        async with self._session.put(
            url,
            headers={DHL_API_KEY_HEADER: DHL_API_KEY},
            json={"pin": pin, "deviceId": device_id},
        ) as response:
            if response.status in (400, 401, 404, 422):
                raise PPLCZInvalidPin
            if response.status != 200:
                raise PPLCZApiError(f"registration confirm HTTP {response.status}")
            payload = await _json(response)
        password = payload.get("password") if isinstance(payload, dict) else None
        if not password:
            raise PPLCZApiError("registration confirm response missing a password")
        return password

    async def async_exchange_password(self, email: str, password: str) -> None:
        """Trade the one-time Azure password for a bearer token pair.

        Only used once, right after :meth:`async_confirm_pin` — ongoing
        renewal always goes through :meth:`_async_refresh` instead.
        """
        async with self._session.post(
            AZURE_TOKEN_URL,
            data={
                "client_id": AZURE_CLIENT_ID,
                "scope": AZURE_SCOPE,
                "grant_type": "password",
                "username": email,
                "password": password,
            },
        ) as response:
            if response.status in (400, 401):
                # The one-time password was rejected — the email+PIN
                # round-trip has to run again, not a plain connectivity blip.
                raise PPLCZAuthError(f"token exchange HTTP {response.status}")
            if response.status != 200:
                raise PPLCZApiError(f"token exchange HTTP {response.status}")
            payload = await _json(response)
        self._store_tokens(payload)

    # --- session management --------------------------------------------------

    def _store_tokens(self, payload: dict[str, Any]) -> None:
        """Store a token pair from a password/refresh grant response."""
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        if not access_token or not refresh_token:
            raise PPLCZApiError("token response missing a token")
        expires_in = _parse_expires_in(payload.get("expires_in"))
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._token_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=expires_in
        )
        if self._on_tokens_updated is not None:
            self._on_tokens_updated(
                self._access_token, self._refresh_token, self._token_expires_at
            )

    async def _async_refresh(self) -> None:
        """Mint a fresh access token from the stored refresh token."""
        if not self._refresh_token:
            raise PPLCZAuthError("no refresh token to refresh with")
        try:
            async with self._session.post(
                AZURE_TOKEN_URL,
                data={
                    "client_id": AZURE_CLIENT_ID,
                    "scope": AZURE_SCOPE,
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                },
            ) as response:
                if response.status in (400, 401, 403):
                    # The refresh token itself lapsed (14 days idle on the one
                    # live sample) or was revoked — this is the real "log in
                    # again" signal, not an access-token expiry.
                    raise PPLCZAuthError(f"refresh HTTP {response.status}")
                if response.status != 200:
                    raise PPLCZApiError(f"refresh HTTP {response.status}")
                payload = await _json(response)
        except aiohttp.ClientError as err:
            raise PPLCZApiError(f"refresh network error ({err})") from err
        self._store_tokens(payload)

    def _token_is_fresh(self) -> bool:
        """Whether the access token exists and is outside the refresh margin."""
        return (
            self._access_token is not None
            and self._token_expires_at is not None
            and datetime.now(timezone.utc) < self._token_expires_at - _REFRESH_MARGIN
        )

    async def _async_ensure_fresh_token(self) -> None:
        """Refresh proactively when the access token is missing or stale."""
        if self._token_is_fresh():
            return
        async with self._refresh_lock:
            # Re-check: a concurrent caller (e.g. the manual refresh button
            # firing alongside the scheduled poll) may have already refreshed
            # while we were waiting for the lock.
            if not self._token_is_fresh():
                await self._async_refresh()

    async def _async_refresh_if_current(self, observed_access_token: str | None) -> None:
        """Refresh unless a concurrent caller already rotated the token.

        Guards the reactive 401 path the same way: without this, two
        requests racing a 401 would both redeem the same (single-use, Azure
        rotates it) refresh token and the loser would get a 400.
        """
        async with self._refresh_lock:
            if self._access_token == observed_access_token:
                await self._async_refresh()

    async def _authed_request(self, method: str, url: str) -> Any:
        """Issue an authenticated ``/mobapp`` request, retrying once on 401."""
        await self._async_ensure_fresh_token()
        for attempt in range(2):
            access_token = self._access_token
            headers = {
                DHL_API_KEY_HEADER: DHL_API_KEY,
                "Authorization": f"Bearer {access_token}",
            }
            async with self._session.request(method, url, headers=headers) as response:
                if response.status == 401 and attempt == 0:
                    await self._async_refresh_if_current(access_token)
                    continue
                if response.status == 401:
                    raise PPLCZAuthError("PPL CZ rejected the access token")
                if response.status != 200:
                    raise PPLCZApiError(f"HTTP {response.status}")
                return await _json(response)
        raise PPLCZAuthError("PPL CZ rejected the access token")

    # --- data ------------------------------------------------------------

    async def async_get_parcels(self) -> list[dict[str, Any]]:
        """Return every shipment on the account — both directions, one call.

        The envelope is confirmed live:
        ``{"metadata": {"pagination": {...}}, "items": [...]}``. The item
        shape itself is not — every field downstream must still guard.
        """
        payload = await self._authed_request("GET", SHIPMENTS_URL)
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise PPLCZApiError("shipments response missing an items list")
        return [item for item in items if isinstance(item, dict)]

    async def async_get_shipment_events(
        self, shipment_id: str
    ) -> list[dict[str, Any]] | None:
        """Return one shipment's event history, or ``None`` on failure.

        Best-effort: a bad history call must not fail the whole poll (the
        list call already has the current status). An auth failure still
        propagates — that is a real "log in again" signal, not a per-parcel
        hiccup. The envelope is unconfirmed: a bare list and an
        ``{"items": [...]}`` wrapper are both accepted.
        """
        url = SHIPMENT_EVENTS_URL.format(shipment_id=shipment_id)
        try:
            payload = await self._authed_request("GET", url)
        except PPLCZAuthError:
            raise
        except (PPLCZApiError, aiohttp.ClientError):
            return None
        if isinstance(payload, list):
            events = payload
        elif isinstance(payload, dict):
            events = payload.get("items")
        else:
            events = None
        if not isinstance(events, list):
            return None
        return [event for event in events if isinstance(event, dict)]


def _parse_expires_in(value: Any) -> int:
    """Parse ``expires_in``, tolerating a JSON string or a number."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return _DEFAULT_EXPIRES_IN_SECONDS


async def _json(response: aiohttp.ClientResponse) -> Any:
    """Parse a JSON body, tolerating a non-JSON content type."""
    try:
        return await response.json(content_type=None)
    except ValueError as err:
        raise PPLCZApiError(f"unparseable body ({err})") from err
