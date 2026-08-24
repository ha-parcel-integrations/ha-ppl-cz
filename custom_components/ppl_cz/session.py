"""A cookie-free aiohttp session for the PPL CZ / Azure B2C endpoints."""
from __future__ import annotations

import aiohttp
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession


@callback
def async_create_ppl_session(
    hass: HomeAssistant, *, auto_cleanup: bool = False
) -> aiohttp.ClientSession:
    """Return a session that never stores or replays cookies.

    ``auto_cleanup`` hands the closing over to Home Assistant's own shutdown,
    for the short-lived sessions a config flow needs; the config entry closes
    its own on unload instead.

    Confirmed live (2026-08-24): on HA's *shared* session
    (``async_get_clientsession``), the Azure B2C token endpoint eventually
    starts answering the ROPC grant with a ``200`` carrying its own HTML
    exception page — ``"AADB2C: We are unable to sign you in. Please contact
    the administrator to adjust the number of authentication steps."`` — and
    keeps doing so until Home Assistant is restarted. A restart is exactly
    what clears the shared cookie jar (the stored token, password and email
    all survive it in the config entry, so they can't be the trigger), and
    the failure follows the client to a different Azure data centre, so it
    travels in the request rather than living on one bad backend.

    B2C stamps ``x-ms-cpim-*`` cookies onto every ROPC exchange, among them
    the one tracking in-flight journey transactions; accumulated across a
    long-lived shared jar they eventually break the very journey step the
    error names. None of this integration's calls need a cookie, so the fix
    is to never keep one: a dedicated session with a
    :class:`aiohttp.DummyCookieJar`.
    """
    return async_create_clientsession(
        hass,
        auto_cleanup=auto_cleanup,
        cookie_jar=aiohttp.DummyCookieJar(),
    )
