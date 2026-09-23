"""PPL CZ parcel tracker custom component for Home Assistant."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed

from .account.api import PPLCZApiClient
from .account.coordinator import PPLCZCoordinator
from .account.parcels import parse_iso
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_SOURCE,
    CONF_TOKEN_EXPIRES_AT,
    PLATFORMS,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)
from .session import async_create_ppl_session
from .tracking.api import PPLCZTrackingApiClient
from .tracking.coordinator import PPLCZTrackingCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass
class PPLCZData:
    """Runtime data attached to a PPL CZ config entry."""

    client: PPLCZApiClient | PPLCZTrackingApiClient
    coordinator: PPLCZCoordinator | PPLCZTrackingCoordinator


type PPLCZConfigEntry = ConfigEntry[PPLCZData]


async def async_setup_entry(
    hass: HomeAssistant, entry: PPLCZConfigEntry
) -> bool:
    """Set up PPL CZ from a config entry.

    A pre-0.12.0 entry predates ``CONF_SOURCE`` entirely — it was an account
    entry before a second source existed, so every read here defaults to
    ``SOURCE_ACCOUNT``. Getting this backwards would silently convert every
    existing user's account hub into an empty tracking hub on upgrade.
    """
    source = entry.data.get(CONF_SOURCE, SOURCE_ACCOUNT)

    session = async_create_ppl_session(hass)
    entry.async_on_unload(session.close)

    if source == SOURCE_TRACKING:
        client: PPLCZApiClient | PPLCZTrackingApiClient = PPLCZTrackingApiClient(session)
        coordinator: PPLCZCoordinator | PPLCZTrackingCoordinator = PPLCZTrackingCoordinator(
            hass, client, entry
        )
    else:
        email = entry.data.get(CONF_EMAIL)
        password = entry.data.get(CONF_PASSWORD)
        if not email or not password:
            # A pre-re-mint-model entry (stored a refresh token instead, from
            # before 0.9.2) or a corrupted one — send the user through reauth
            # to sign in again rather than crash-loop on a refresh grant
            # PPL's tenant would revoke within the hour anyway.
            raise ConfigEntryAuthFailed("No PPL CZ credentials stored")

        @callback
        def _persist_tokens(access_token: str, expires_at: datetime) -> None:
            # Persist the freshly re-minted access token immediately, or a
            # restart re-mints again needlessly instead of reusing a still-
            # valid one.
            hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    CONF_ACCESS_TOKEN: access_token,
                    CONF_TOKEN_EXPIRES_AT: expires_at.isoformat(),
                },
            )

        client = PPLCZApiClient(
            session,
            email=email,
            password=password,
            access_token=entry.data.get(CONF_ACCESS_TOKEN),
            token_expires_at=parse_iso(entry.data.get(CONF_TOKEN_EXPIRES_AT)),
            on_tokens_updated=_persist_tokens,
        )
        coordinator = PPLCZCoordinator(hass, client, entry)

    # First refresh here, before forwarding to platforms: a failing fetch
    # fails the whole entry cleanly (ConfigEntryNotReady -> backoff), and an
    # expired account session raises ConfigEntryAuthFailed -> reauth. Doing
    # it in a forwarded platform would be too late for HA to catch.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = PPLCZData(client=client, coordinator=coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS[source])

    if source == SOURCE_TRACKING:
        # Apply option changes (added/removed barcodes, delivered/history
        # settings) live via a coordinator refresh — no reload — so
        # per-parcel sensors appear and disappear immediately. This is also
        # the resume path after polling fully suspended: adding a barcode
        # back triggers this refresh, which recomputes the tier and re-arms
        # scheduling. The account source keeps its own reload-on-submit
        # options flow instead (unchanged) — combining a reload flow with an
        # update listener is deprecated, so only one entry type gets each.
        entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: PPLCZConfigEntry
) -> None:
    """Apply changed tracking-hub options by refreshing the coordinator."""
    await entry.runtime_data.coordinator.async_request_refresh()


async def async_unload_entry(
    hass: HomeAssistant, entry: PPLCZConfigEntry
) -> bool:
    """Unload a PPL CZ config entry."""
    source = entry.data.get(CONF_SOURCE, SOURCE_ACCOUNT)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS[source])
