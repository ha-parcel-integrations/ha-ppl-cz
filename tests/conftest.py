"""pytest configuration for the PPL CZ test suite."""
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.plugins import hass  # noqa: F401


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Make ``custom_components.ppl_cz`` loadable from config-flow / setup tests."""
    yield


@pytest.fixture(autouse=True)
def reset_one_shot_warnings():
    """Clear the "already warned this session" flags between tests.

    They are module-level by design -- a user must not be told about the
    same unmapped status or unconfirmed field on every poll -- but that also
    makes them leak across tests, so whether a warning fires would otherwise
    depend on test order.
    """
    from custom_components.ppl_cz import parcels

    parcels._unmapped_statuses_logged.clear()
    parcels._items_shape_logged = False
    parcels._events_shape_logged = False
    parcels._delivery_point_type_logged = False
    parcels._unknown_direction_logged.clear()
    yield


@pytest.fixture(autouse=True)
def stub_ppl_session():
    """Hand out a throwaway session instead of a real cookie-free one.

    The integration creates its own session and closes it on unload (see
    ``session.py``); the test harness resolves that back to Home Assistant's
    shared session, which raises rather than let itself be closed.
    """
    with patch(
        "custom_components.ppl_cz.session.async_create_clientsession",
        return_value=MagicMock(close=AsyncMock()),
    ) as create:
        yield create


if sys.platform == "win32":
    # pytest-homeassistant-custom-component blocks socket *creation*
    # (``disable_socket(allow_unix_socket=True)``) in its per-test setup hook.
    # That is fine on Linux, where asyncio's self-pipe is an AF_UNIX
    # socketpair — but Windows event loops build theirs from AF_INET sockets,
    # so every async test dies with ``SocketBlockedError`` while the event
    # loop fixture is being created. Neutralise the creation block on Windows
    # and keep the network guard as the plugin's connect-time allowlist
    # (``socket_allow_hosts(["127.0.0.1"])``, applied right before the
    # ``disable_socket`` call we swallow here).
    import pytest_socket

    pytest_socket.disable_socket = lambda allow_unix_socket=False: None

    # HA's aiohttp helper hardcodes aiohttp's AsyncResolver, whose aiodns
    # backend refuses the Proactor loop the suite runs on under Windows.
    # Swap in the threaded resolver for the tests — no test resolves DNS.
    import homeassistant.helpers.aiohttp_client as _ha_aiohttp_client
    from aiohttp.resolver import ThreadedResolver

    _ha_aiohttp_client.AsyncResolver = ThreadedResolver
