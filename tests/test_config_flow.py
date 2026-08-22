"""Tests for the PPL CZ config, reauth and options flows."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ppl_cz.api import PPLCZApiError, PPLCZAuthError, PPLCZInvalidPin
from custom_components.ppl_cz.const import (
    CONF_ACCESS_TOKEN,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_EMAIL,
    CONF_INCLUDE_HISTORY,
    CONF_REFRESH_INTERVAL,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)

EMAIL = "user@example.test"


def _mock_client(**overrides) -> MagicMock:
    client = MagicMock()
    client.async_request_pin = AsyncMock()
    client.async_confirm_pin = AsyncMock(return_value="onetimepass")
    client.async_exchange_password = AsyncMock()
    client.access_token = "access-abc"
    client.refresh_token = "refresh-abc"
    client.token_expires_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for key, value in overrides.items():
        setattr(client, key, value)
    return client


def _patch(client):
    return patch(
        "custom_components.ppl_cz.config_flow.PPLCZApiClient", return_value=client
    )


# --- user step ---------------------------------------------------------------


async def test_full_login_flow(hass):
    client = _mock_client()
    with _patch(client):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        assert result["step_id"] == "user"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: EMAIL}
        )
        assert result["step_id"] == "code"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"pin": "1234"}
        )

    assert result["type"] == "create_entry"
    assert result["title"] == EMAIL
    assert result["data"][CONF_EMAIL] == EMAIL
    assert result["data"][CONF_ACCESS_TOKEN] == "access-abc"
    assert result["data"][CONF_REFRESH_TOKEN] == "refresh-abc"
    client.async_request_pin.assert_awaited_once()
    client.async_confirm_pin.assert_awaited_once()
    client.async_exchange_password.assert_awaited_once_with(EMAIL, "onetimepass")


async def test_request_pin_cannot_connect(hass):
    client = _mock_client()
    client.async_request_pin.side_effect = PPLCZApiError("x")
    with _patch(client):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: EMAIL}
        )
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_invalid_pin(hass):
    client = _mock_client()
    client.async_confirm_pin.side_effect = PPLCZInvalidPin
    with _patch(client):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: EMAIL}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"pin": "0000"}
        )
    assert result["step_id"] == "code"
    assert result["errors"] == {"base": "invalid_pin"}


async def test_exchange_password_auth_error(hass):
    client = _mock_client()
    client.async_exchange_password.side_effect = PPLCZAuthError("HTTP 401")
    with _patch(client):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: EMAIL}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"pin": "1234"}
        )
    assert result["step_id"] == "code"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_code_step_cannot_connect(hass):
    client = _mock_client()
    client.async_exchange_password.side_effect = PPLCZApiError("HTTP 500")
    with _patch(client):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: EMAIL}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"pin": "1234"}
        )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_aborts_on_duplicate_account(hass):
    MockConfigEntry(domain=DOMAIN, unique_id=EMAIL).add_to_hass(hass)
    client = _mock_client()
    with _patch(client):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: EMAIL}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"pin": "1234"}
        )
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


# --- reauth --------------------------------------------------------------


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=EMAIL,
        data={
            CONF_EMAIL: EMAIL,
            CONF_ACCESS_TOKEN: "stale",
            CONF_REFRESH_TOKEN: "stale-refresh",
        },
    )


async def test_reauth_flow(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _mock_client()
    with _patch(client):
        result = await entry.start_reauth_flow(hass)
        assert result["step_id"] == "reauth_confirm"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "code"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"pin": "1234"}
        )
    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_REFRESH_TOKEN] == "refresh-abc"


async def test_reauth_request_pin_error(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _mock_client()
    client.async_request_pin.side_effect = PPLCZApiError("x")
    with _patch(client):
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "cannot_connect"}


# No dedicated "wrong_account" test: unlike vinted-go (whose account id comes
# back from a separate server call that could name a different account),
# PPL CZ's reauth always re-runs the PIN request against the *stored* e-mail
# with no field to change it, so `self._email` — and therefore the unique_id
# checked in `async_step_code` — can never differ from the reauth entry's own
# unique_id. `_abort_if_unique_id_mismatch` stays as defensive, suite-wide
# convention rather than a reachable path here; `test_reauth_flow` above
# already exercises that line without triggering the abort.


# --- options -----------------------------------------------------------------


async def test_options_flow(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=EMAIL,
        data={CONF_EMAIL: EMAIL, CONF_ACCESS_TOKEN: "a", CONF_REFRESH_TOKEN: "r"},
        options={},
    )
    entry.add_to_hass(hass)
    with patch("homeassistant.config_entries.ConfigEntries.async_schedule_reload"):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "delivered": {
                    CONF_DELIVERED_FILTER_TYPE: "parcels",
                    CONF_DELIVERED_FILTER_AMOUNT: 5,
                },
                "history": {CONF_INCLUDE_HISTORY: True},
                "polling": {CONF_REFRESH_INTERVAL: "120"},
            },
        )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_REFRESH_INTERVAL] == 120
    assert result["data"][CONF_INCLUDE_HISTORY] is True
    assert result["data"][CONF_DELIVERED_FILTER_AMOUNT] == 5
