"""Tests for the PPL CZ config, reauth and options flows."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_PASSWORD
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ppl_cz.api import PPLCZApiError, PPLCZAuthError, PPLCZInvalidPin
from custom_components.ppl_cz.const import (
    CONF_ACCESS_TOKEN,
    CONF_BARCODE,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_DIRECTION,
    CONF_EMAIL,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_SOURCE,
    DIRECTION_INCOMING,
    DIRECTION_OUTGOING,
    DOMAIN,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)

EMAIL = "user@example.test"
# The dropped Phase-1 polling option; asserted absent so it can't creep back.
CONF_REFRESH_INTERVAL_KEY = "refresh_interval"


def _mock_client(**overrides) -> MagicMock:
    client = MagicMock()
    client.async_request_pin = AsyncMock()
    client.async_confirm_pin = AsyncMock(return_value="onetimepass")
    client.async_exchange_password = AsyncMock()
    client.access_token = "access-abc"
    client.token_expires_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for key, value in overrides.items():
        setattr(client, key, value)
    return client


def _patch(client):
    return patch(
        "custom_components.ppl_cz.config_flow.PPLCZApiClient", return_value=client
    )


async def _select_account(hass):
    """Start the flow and pick the Account menu option."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "menu"
    assert result["step_id"] == "user"
    assert set(result["menu_options"]) == {SOURCE_ACCOUNT, SOURCE_TRACKING}
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": SOURCE_ACCOUNT}
    )


# --- the source menu -----------------------------------------------------


async def test_user_step_shows_the_source_menu(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "menu"
    assert result["step_id"] == "user"
    assert result["menu_options"] == [SOURCE_ACCOUNT, SOURCE_TRACKING]


# --- account step ----------------------------------------------------------


async def test_full_login_flow(hass):
    client = _mock_client()
    with _patch(client):
        result = await _select_account(hass)
        assert result["step_id"] == "account"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: EMAIL}
        )
        assert result["step_id"] == "code"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"pin": "1234"}
        )

    assert result["type"] == "create_entry"
    assert result["title"] == EMAIL
    assert result["data"][CONF_SOURCE] == SOURCE_ACCOUNT
    assert result["data"][CONF_EMAIL] == EMAIL
    assert result["data"][CONF_PASSWORD] == "onetimepass"
    assert result["data"][CONF_ACCESS_TOKEN] == "access-abc"
    client.async_request_pin.assert_awaited_once()
    client.async_confirm_pin.assert_awaited_once()
    client.async_exchange_password.assert_awaited_once_with(EMAIL, "onetimepass")


async def test_new_account_entry_seeds_the_default_options(hass):
    """A newly created account entry seeds its options; polling is not among them."""
    client = _mock_client()
    with _patch(client):
        result = await _select_account(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: EMAIL}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"pin": "1234"}
        )

    assert result["options"] == {
        CONF_DELIVERED_FILTER_TYPE: "days",
        CONF_DELIVERED_FILTER_AMOUNT: 7,
        CONF_INCLUDE_HISTORY: False,
    }


async def test_request_pin_cannot_connect(hass):
    client = _mock_client()
    client.async_request_pin.side_effect = PPLCZApiError("x")
    with _patch(client):
        result = await _select_account(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: EMAIL}
        )
    assert result["step_id"] == "account"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_invalid_pin(hass):
    client = _mock_client()
    client.async_confirm_pin.side_effect = PPLCZInvalidPin
    with _patch(client):
        result = await _select_account(hass)
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
        result = await _select_account(hass)
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
        result = await _select_account(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: EMAIL}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"pin": "1234"}
        )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_account_flow_aborts_on_duplicate_account(hass):
    MockConfigEntry(domain=DOMAIN, unique_id=EMAIL).add_to_hass(hass)
    client = _mock_client()
    with _patch(client):
        result = await _select_account(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: EMAIL}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"pin": "1234"}
        )
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


# --- tracking step -----------------------------------------------------------


async def test_tracking_step_creates_entry_without_a_network_call(hass):
    with patch("custom_components.ppl_cz.config_flow.PPLCZApiClient") as client_cls:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": SOURCE_TRACKING}
        )

    assert result["type"] == "create_entry"
    assert result["data"] == {CONF_SOURCE: SOURCE_TRACKING}
    assert result["options"] == {
        CONF_PARCELS: [],
        CONF_DELIVERED_FILTER_TYPE: "days",
        CONF_DELIVERED_FILTER_AMOUNT: 7,
        CONF_INCLUDE_HISTORY: False,
    }
    client_cls.assert_not_called()


async def test_only_one_tracking_hub_is_allowed(hass):
    MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{DOMAIN}_{SOURCE_TRACKING}",
        data={CONF_SOURCE: SOURCE_TRACKING},
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": SOURCE_TRACKING}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


async def test_a_second_account_hub_is_still_allowed_alongside_a_tracking_hub(hass):
    """Multi-account is deliberate — only the tracking source is singleton."""
    MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{DOMAIN}_{SOURCE_TRACKING}",
        data={CONF_SOURCE: SOURCE_TRACKING},
    ).add_to_hass(hass)

    client = _mock_client()
    with _patch(client):
        result = await _select_account(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: EMAIL}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"pin": "1234"}
        )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_SOURCE] == SOURCE_ACCOUNT


# --- reauth --------------------------------------------------------------


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=EMAIL,
        data={
            CONF_SOURCE: SOURCE_ACCOUNT,
            CONF_EMAIL: EMAIL,
            CONF_PASSWORD: "stale-password",
            CONF_ACCESS_TOKEN: "stale",
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
    assert entry.data[CONF_PASSWORD] == "onetimepass"


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
# already exercises that line without triggering the abort. Reauth is also
# account-only and unreachable from a tracking entry: a tracking coordinator
# never raises ConfigEntryAuthFailed, since it has no credential to reject.


# --- account options -----------------------------------------------------


async def test_account_options_flow(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=EMAIL,
        data={
            CONF_SOURCE: SOURCE_ACCOUNT,
            CONF_EMAIL: EMAIL,
            CONF_PASSWORD: "pw",
            CONF_ACCESS_TOKEN: "a",
        },
        options={},
    )
    entry.add_to_hass(hass)
    with patch("homeassistant.config_entries.ConfigEntries.async_schedule_reload"):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["step_id"] == "init"
        assert result["type"] != "menu"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "delivered": {
                    CONF_DELIVERED_FILTER_TYPE: "parcels",
                    CONF_DELIVERED_FILTER_AMOUNT: 5,
                },
                "history": {CONF_INCLUDE_HISTORY: True},
            },
        )
    assert result["type"] == "create_entry"
    assert CONF_REFRESH_INTERVAL_KEY not in result["data"]
    assert result["data"][CONF_INCLUDE_HISTORY] is True
    assert result["data"][CONF_DELIVERED_FILTER_AMOUNT] == 5


# --- tracking options ------------------------------------------------------


def _tracking_entry(**options) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{DOMAIN}_{SOURCE_TRACKING}",
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={
            CONF_PARCELS: [],
            CONF_DELIVERED_FILTER_TYPE: "days",
            CONF_DELIVERED_FILTER_AMOUNT: 7,
            CONF_INCLUDE_HISTORY: False,
            **options,
        },
    )


async def test_tracking_options_menu_offers_incoming_outgoing_and_settings(hass):
    entry = _tracking_entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "menu"
    assert result["menu_options"] == ["incoming_parcels", "outgoing_parcels", "settings"]


async def test_incoming_parcels_step_adds_a_barcode_as_incoming(hass):
    entry = _tracking_entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "incoming_parcels"}
    )
    assert result["step_id"] == "incoming_parcels"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": ["12345678901"]}
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == [
        {CONF_BARCODE: "12345678901", CONF_DIRECTION: DIRECTION_INCOMING}
    ]


async def test_outgoing_parcels_step_adds_a_barcode_as_outgoing(hass):
    entry = _tracking_entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "outgoing_parcels"}
    )
    assert result["step_id"] == "outgoing_parcels"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": ["99988877766"]}
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == [
        {CONF_BARCODE: "99988877766", CONF_DIRECTION: DIRECTION_OUTGOING}
    ]


async def test_editing_one_direction_leaves_the_other_intact(hass):
    entry = _tracking_entry(
        parcels=[
            {CONF_BARCODE: "INCOME1", CONF_DIRECTION: DIRECTION_INCOMING},
            {CONF_BARCODE: "OUTGO1", CONF_DIRECTION: DIRECTION_OUTGOING},
        ]
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "incoming_parcels"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": ["INCOME2"]}
    )

    codes = {
        (p[CONF_BARCODE], p[CONF_DIRECTION]) for p in result["data"][CONF_PARCELS]
    }
    assert codes == {
        ("INCOME2", DIRECTION_INCOMING),
        ("OUTGO1", DIRECTION_OUTGOING),
    }


async def test_re_entering_a_code_the_other_way_moves_it(hass):
    """Filing a code in the other direction's list corrects a mistake, not a duplicate."""
    entry = _tracking_entry(
        parcels=[{CONF_BARCODE: "MOVED1", CONF_DIRECTION: DIRECTION_INCOMING}]
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "outgoing_parcels"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": ["MOVED1"]}
    )

    assert result["data"][CONF_PARCELS] == [
        {CONF_BARCODE: "MOVED1", CONF_DIRECTION: DIRECTION_OUTGOING}
    ]


async def test_tracking_parcels_step_rejects_a_blank_code(hass):
    entry = _tracking_entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "incoming_parcels"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": [""]}
    )
    # A blank entry is normalised away entirely, not reported as invalid.
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == []


async def test_tracking_settings_step_updates_delivered_and_history(hass):
    entry = _tracking_entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    assert result["step_id"] == "settings"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 3,
            CONF_INCLUDE_HISTORY: True,
        },
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_DELIVERED_FILTER_AMOUNT] == 3
    assert result["data"][CONF_INCLUDE_HISTORY] is True
