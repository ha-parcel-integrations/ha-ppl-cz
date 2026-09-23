"""Config flow for the PPL CZ parcel tracker integration.

Two sources, picked from a menu (``async_step_user``):

* **Account** — the mojePPL passwordless e-mail + PIN login
  (``async_step_account`` -> ``async_step_code``): step 1 requests an e-mail
  PIN, step 2 confirms it, then immediately exchanges the resulting one-time
  password for a bearer token pair. Reauth reuses the same two steps against
  the same e-mail — mirrors ha-vinted-go's passwordless flow shape (this
  suite's other multi-step, non-password login), not copied field-for-field
  since PPL CZ's PIN+password-exchange mechanics are its own. This is the
  only source that can reauth: the tracking source has no credential.
* **Tracking codes** — ``async_step_tracking``, the website
  tracking-by-number surface. No credential, so it makes no network call:
  there is nothing to validate without a barcode, which is added afterwards
  through the options flow.
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

from .account.api import (
    PPLCZApiClient,
    PPLCZApiError,
    PPLCZAuthError,
    PPLCZInvalidPin,
)
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_BARCODE,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_DIRECTION,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_SOURCE,
    CONF_TOKEN_EXPIRES_AT,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DIRECTION_INCOMING,
    DIRECTION_OUTGOING,
    DOMAIN,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)
from .session import async_create_ppl_session
from .tracking.parcels import tracked_direction

_LOGGER = logging.getLogger(__name__)

_EMAIL_SCHEMA = vol.Schema({vol.Required(CONF_EMAIL): str})
_PIN_SCHEMA = vol.Schema({vol.Required("pin"): str})


def normalize_barcode(value: str) -> str:
    """Return the tracking code trimmed, nothing else.

    No published regex or checksum exists for a PPL CZ tracking code (the two
    real numbers on file start with 7 and 4, contradicting the "usually
    starts with 1" claim), so this deliberately does not upper-case or strip
    separators — guessing a transform here could turn a valid code into one
    the endpoint rejects.
    """
    return (value or "").strip()


def valid_barcode(value: str) -> bool:
    """Whether ``value`` is a non-empty tracking code."""
    return bool(value)


def _current_parcels(entry: ConfigEntry) -> list[dict[str, str]]:
    """Return a mutable copy of the tracked parcels list."""
    return [dict(item) for item in entry.options.get(CONF_PARCELS, [])]


def _clean_barcodes(values: list[str] | None) -> list[str]:
    """Normalise, drop blanks, and de-duplicate tracking codes."""
    codes: list[str] = []
    for value in values or []:
        code = normalize_barcode(value)
        if code and code not in codes:
            codes.append(code)
    return codes


class PPLCZConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the menu-driven configuration flow for PPL CZ."""

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
        """Offer the two sources.

        A mojePPL account has exactly one sign-in, so choosing Account here
        signs the mobile app out; the menu's own description says so before
        either option is picked, since it is the last point where the user
        can still act on that trade-off — once the account flow's PIN step
        is confirmed, the app session is already gone.
        """
        return self.async_show_menu(
            step_id="user", menu_options=[SOURCE_ACCOUNT, SOURCE_TRACKING]
        )

    async def async_step_tracking(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the tracking-code hub.

        No network call: PPL's lookup needs only a shipment id and no
        postcode, so there is nothing to validate before a barcode exists.
        Barcodes are added afterwards through the options flow. Only one
        tracking hub is allowed — the unique id is fixed, not user-supplied
        — mirroring the effect manifest.json's single_config_entry flag has
        for single-source carriers, applied to just this source; account
        hubs stay multi-entry, keyed on the e-mail address.
        """
        await self.async_set_unique_id(f"{DOMAIN}_{SOURCE_TRACKING}")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="PPL CZ (tracking codes)",
            data={CONF_SOURCE: SOURCE_TRACKING},
            options={
                CONF_PARCELS: [],
                CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
                CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
                CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY,
            },
        )

    async def async_step_account(
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
            client = PPLCZApiClient(async_create_ppl_session(self.hass, auto_cleanup=True))
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
            step_id=SOURCE_ACCOUNT, data_schema=_EMAIL_SCHEMA, errors=errors
        )

    async def async_step_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: confirm the PIN and mint the account's token pair."""
        errors: dict[str, str] = {}
        if user_input is not None:
            client = PPLCZApiClient(async_create_ppl_session(self.hass, auto_cleanup=True))
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
                    CONF_SOURCE: SOURCE_ACCOUNT,
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
        """Start reauth — the stored sign-in no longer works.

        Account-only: the tracking source has no credential, so nothing can
        expire and this entry point is never reached from a tracking entry.
        """
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
            client = PPLCZApiClient(async_create_ppl_session(self.hass, auto_cleanup=True))
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
    """Manage each source's own options.

    An account hub keeps its single sectioned form (delivered retention +
    history) with a reload on submit — unchanged from before this source
    split. A tracking hub instead shows a menu (mirrors Packeta, the other
    account-less carrier in the suite): ``incoming_parcels``/
    ``outgoing_parcels`` each manage one direction's tracked barcode list —
    the website payload cannot tell the two apart, so the user declares it
    by filing a code in one list or the other — with a live coordinator
    refresh, no reload; ``settings`` holds the same delivered retention +
    history fields. Polling cadence is not configurable on either source —
    the coordinator drives it from what the tracked parcels are doing.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Branch on the entry's source."""
        if self.config_entry.data.get(CONF_SOURCE, SOURCE_ACCOUNT) == SOURCE_TRACKING:
            return self.async_show_menu(
                step_id="init",
                menu_options=["incoming_parcels", "outgoing_parcels", "settings"],
            )
        return await self._async_step_account_settings(user_input)

    async def _async_step_account_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the account hub's single sectioned options form."""
        if user_input is not None:
            delivered = user_input["delivered"]
            history = user_input["history"]
            # Reload so a changed option takes effect immediately. No update
            # listener is registered for account entries — combining the two
            # is deprecated.
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
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_incoming_parcels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the tracked-code list for parcels being received."""
        return await self._async_step_parcel_list(DIRECTION_INCOMING, user_input)

    async def async_step_outgoing_parcels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the tracked-code list for parcels being sent."""
        return await self._async_step_parcel_list(DIRECTION_OUTGOING, user_input)

    async def _async_step_parcel_list(
        self, direction: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Show and handle one direction's complete tracked-code list.

        Both directions share a single ``CONF_PARCELS`` list, so a submission
        replaces this direction's entries and leaves the other direction's
        alone — except for a code submitted here that was filed the other
        way, which moves rather than being rejected: re-entering a code in
        the other list is how a user corrects a parcel they filed wrongly.
        Mirrors ha-packeta's ``_async_step_parcel_list`` exactly, since PPL
        CZ's tracking source has the same account-less, direction-cannot-be-
        inferred shape.
        """
        step_id = f"{direction}_parcels"
        errors: dict[str, str] = {}
        if user_input is not None:
            codes = _clean_barcodes(user_input.get("tracking_codes"))
            if any(not valid_barcode(code) for code in codes):
                errors["base"] = "invalid_tracking_code"
            else:
                kept = [
                    parcel
                    for parcel in _current_parcels(self.config_entry)
                    if tracked_direction(parcel) != direction
                    and parcel.get(CONF_BARCODE) not in codes
                ]
                return self.async_create_entry(
                    title="",
                    data={
                        **self.config_entry.options,
                        CONF_PARCELS: kept
                        + [
                            {CONF_BARCODE: code, CONF_DIRECTION: direction}
                            for code in codes
                        ],
                    },
                )

        current_codes = [
            parcel[CONF_BARCODE]
            for parcel in _current_parcels(self.config_entry)
            if tracked_direction(parcel) == direction
        ]
        schema = vol.Schema(
            {
                vol.Optional("tracking_codes"): selector.TextSelector(
                    selector.TextSelectorConfig(multiple=True)
                )
            }
        )
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                schema, {"tracking_codes": current_codes}
            ),
            errors=errors,
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle non-parcel settings for the tracking hub.

        Same fields as the account hub's form, but a live coordinator
        refresh via the update listener applies them — no reload, since
        tracking entries register one (see __init__.py).
        """
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    **self.config_entry.options,
                    CONF_DELIVERED_FILTER_TYPE: user_input[CONF_DELIVERED_FILTER_TYPE],
                    CONF_DELIVERED_FILTER_AMOUNT: int(
                        user_input[CONF_DELIVERED_FILTER_AMOUNT]
                    ),
                    CONF_INCLUDE_HISTORY: bool(user_input[CONF_INCLUDE_HISTORY]),
                },
            )
        current = self.config_entry.options
        schema: dict[Any, Any] = {
            vol.Required(
                CONF_DELIVERED_FILTER_TYPE,
                default=current.get(
                    CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
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
                    CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=365, step=1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(
                CONF_INCLUDE_HISTORY,
                default=current.get(CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY),
            ): selector.BooleanSelector(),
        }
        return self.async_show_form(step_id="settings", data_schema=vol.Schema(schema))
