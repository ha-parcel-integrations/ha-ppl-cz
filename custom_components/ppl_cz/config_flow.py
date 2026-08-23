"""Config flow for the PPL CZ parcel tracker integration.

Two user-facing steps, three network calls under the hood
(``async_step_user`` -> ``async_step_code``): step 1 requests an e-mail PIN,
step 2 confirms it, then immediately exchanges the resulting one-time
password for a bearer token pair. Reauth reuses the same two steps against
the same e-mail — mirrors ha-vinted-go's passwordless flow shape (this
suite's other multi-step, non-password login), not copied field-for-field
since PPL CZ's PIN+password-exchange mechanics are its own.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    PPLCZApiClient,
    PPLCZApiError,
    PPLCZAuthError,
    PPLCZInvalidPin,
)
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_REFRESH_INTERVAL,
    CONF_TOKEN_EXPIRES_AT,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DEFAULT_REFRESH_INTERVAL,
    DOMAIN,
    REFRESH_INTERVAL_OPTIONS,
)

_LOGGER = logging.getLogger(__name__)

_EMAIL_SCHEMA = vol.Schema({vol.Required(CONF_EMAIL): str})
_PIN_SCHEMA = vol.Schema({vol.Required("pin"): str})


def _interval_selector() -> selector.SelectSelector:
    """Return the refresh-interval dropdown selector (options translated via strings)."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[str(minutes) for minutes in REFRESH_INTERVAL_OPTIONS],
            translation_key=CONF_REFRESH_INTERVAL,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


class PPLCZConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the two-step e-mail + PIN login flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise transient flow state."""
        self._email: str | None = None
        self._device_id: str | None = None
        self._registration_session_id: str | None = None
        self._reauth_entry: ConfigEntry | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> PPLCZOptionsFlowHandler:
        """Return the options flow handler."""
        return PPLCZOptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: collect the e-mail and request a PIN."""
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            # Client-chosen, arbitrary per the mechanics doc — a UUIDv4 is
            # confirmed working; nothing requires that specific format.
            device_id = str(uuid.uuid4())
            registration_session_id = str(uuid.uuid4())
            client = PPLCZApiClient(async_get_clientsession(self.hass))
            try:
                await client.async_request_pin(
                    email, device_id, registration_session_id
                )
            except (PPLCZApiError, aiohttp.ClientError):
                errors["base"] = "cannot_connect"
            else:
                self._email = email
                self._device_id = device_id
                self._registration_session_id = registration_session_id
                return await self.async_step_code()

        return self.async_show_form(
            step_id="user", data_schema=_EMAIL_SCHEMA, errors=errors
        )

    async def async_step_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: confirm the PIN and mint the account's token pair."""
        errors: dict[str, str] = {}
        if user_input is not None:
            client = PPLCZApiClient(async_get_clientsession(self.hass))
            try:
                password = await client.async_confirm_pin(
                    self._registration_session_id,
                    user_input["pin"],
                    self._device_id,
                )
                await client.async_exchange_password(self._email, password)
            except PPLCZInvalidPin:
                errors["base"] = "invalid_pin"
            except PPLCZAuthError:
                errors["base"] = "invalid_auth"
            except (PPLCZApiError, aiohttp.ClientError):
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(self._email)
                # The password, not a refresh token, is what has to survive a
                # restart — PPL's B2C tenant hard-revokes the refresh-token
                # lineage ~1h after login regardless, and the app itself
                # re-mints from this same stored credential (see api.py).
                data = {
                    CONF_EMAIL: self._email,
                    CONF_PASSWORD: password,
                    CONF_ACCESS_TOKEN: client.access_token,
                    CONF_TOKEN_EXPIRES_AT: client.token_expires_at.isoformat(),
                }
                if self._reauth_entry is not None:
                    self._abort_if_unique_id_mismatch(reason="wrong_account")
                    return self.async_update_reload_and_abort(
                        self._reauth_entry, data=data
                    )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=self._email,
                    data=data,
                    options={
                        CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
                        CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
                        CONF_REFRESH_INTERVAL: DEFAULT_REFRESH_INTERVAL,
                        CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY,
                    },
                )

        return self.async_show_form(
            step_id="code",
            data_schema=_PIN_SCHEMA,
            errors=errors,
            description_placeholders={"email": self._email or ""},
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauth — the stored refresh token no longer works."""
        self._reauth_entry = self._get_reauth_entry()
        self._email = entry_data.get(CONF_EMAIL)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-request a PIN for the same e-mail."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._device_id = str(uuid.uuid4())
            self._registration_session_id = str(uuid.uuid4())
            client = PPLCZApiClient(async_get_clientsession(self.hass))
            try:
                await client.async_request_pin(
                    self._email, self._device_id, self._registration_session_id
                )
            except (PPLCZApiError, aiohttp.ClientError):
                errors["base"] = "cannot_connect"
            else:
                return await self.async_step_code()

        return self.async_show_form(
            step_id="reauth_confirm",
            errors=errors,
            description_placeholders={"email": self._email or ""},
        )


class PPLCZOptionsFlowHandler(OptionsFlow):
    """Manage delivered retention, history and polling in one sectioned form."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the single sectioned options form."""
        if user_input is not None:
            delivered = user_input["delivered"]
            history = user_input["history"]
            polling = user_input["polling"]
            # Reload so a changed interval takes effect immediately. No update
            # listener is registered — combining the two is deprecated.
            self.hass.config_entries.async_schedule_reload(
                self.config_entry.entry_id
            )
            return self.async_create_entry(
                title="",
                data={
                    CONF_DELIVERED_FILTER_TYPE: delivered[CONF_DELIVERED_FILTER_TYPE],
                    CONF_DELIVERED_FILTER_AMOUNT: int(
                        delivered[CONF_DELIVERED_FILTER_AMOUNT]
                    ),
                    CONF_INCLUDE_HISTORY: bool(history[CONF_INCLUDE_HISTORY]),
                    CONF_REFRESH_INTERVAL: int(polling[CONF_REFRESH_INTERVAL]),
                },
            )

        current = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required("delivered"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_DELIVERED_FILTER_TYPE,
                                default=current.get(
                                    CONF_DELIVERED_FILTER_TYPE,
                                    DEFAULT_DELIVERED_FILTER_TYPE,
                                ),
                            ): selector.SelectSelector(
                                selector.SelectSelectorConfig(
                                    options=["days", "parcels"],
                                    translation_key=CONF_DELIVERED_FILTER_TYPE,
                                    mode=selector.SelectSelectorMode.LIST,
                                )
                            ),
                            vol.Required(
                                CONF_DELIVERED_FILTER_AMOUNT,
                                default=current.get(
                                    CONF_DELIVERED_FILTER_AMOUNT,
                                    DEFAULT_DELIVERED_FILTER_AMOUNT,
                                ),
                            ): selector.NumberSelector(
                                selector.NumberSelectorConfig(
                                    min=1,
                                    max=365,
                                    step=1,
                                    mode=selector.NumberSelectorMode.BOX,
                                )
                            ),
                        }
                    ),
                    {"collapsed": False},
                ),
                vol.Required("history"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_INCLUDE_HISTORY,
                                default=current.get(
                                    CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
                                ),
                            ): selector.BooleanSelector(),
                        }
                    ),
                    {"collapsed": True},
                ),
                vol.Required("polling"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_REFRESH_INTERVAL,
                                # str(): selector option values are strings, so
                                # a stored int default trips "expected str".
                                default=str(
                                    current.get(
                                        CONF_REFRESH_INTERVAL,
                                        DEFAULT_REFRESH_INTERVAL,
                                    )
                                ),
                            ): _interval_selector(),
                        }
                    ),
                    {"collapsed": True},
                ),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)
