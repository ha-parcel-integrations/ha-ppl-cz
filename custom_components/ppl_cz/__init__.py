"""PPL CZ parcel tracker custom component for Home Assistant."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PPLCZApiClient
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRES_AT,
    PLATFORMS,
)
from .coordinator import PPLCZCoordinator
from .parcels import parse_iso

_LOGGER = logging.getLogger(__name__)


@dataclass
class PPLCZData:
    """Runtime data attached to a PPL CZ config entry."""

    client: PPLCZApiClient
    coordinator: PPLCZCoordinator


type PPLCZConfigEntry = ConfigEntry[PPLCZData]


async def async_setup_entry(
    hass: HomeAssistant, entry: PPLCZConfigEntry
) -> bool:
    """Set up PPL CZ from a config entry."""
    refresh_token = entry.data.get(CONF_REFRESH_TOKEN)
    if not refresh_token:
        # A pre-account-model entry (or a corrupted one) has no refresh
        # token — send the user through reauth to sign in again rather than
        # crash-loop.
        raise ConfigEntryAuthFailed("No PPL CZ refresh token stored")

    @callback
    def _persist_tokens(
        access_token: str, refresh_token: str, expires_at: datetime
    ) -> None:
        # The refresh grant rotates the refresh token every time — persist
        # the new pair immediately, or a restart hands the API a refresh
        # token it has already invalidated.
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_ACCESS_TOKEN: access_token,
                CONF_REFRESH_TOKEN: refresh_token,
                CONF_TOKEN_EXPIRES_AT: expires_at.isoformat(),
            },
        )

    client = PPLCZApiClient(
        async_get_clientsession(hass),
        access_token=entry.data.get(CONF_ACCESS_TOKEN),
        refresh_token=refresh_token,
        token_expires_at=parse_iso(entry.data.get(CONF_TOKEN_EXPIRES_AT)),
        on_tokens_updated=_persist_tokens,
    )

    coordinator = PPLCZCoordinator(hass, client, entry)

    # First refresh here, before forwarding to platforms: a failing fetch
    # fails the whole entry cleanly (ConfigEntryNotReady -> backoff), and an
    # expired refresh token raises ConfigEntryAuthFailed -> reauth. Doing it
    # in a forwarded platform would be too late for HA to catch.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = PPLCZData(client=client, coordinator=coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # No entry.add_update_listener: the options flow calls
    # async_schedule_reload itself. Combining an update listener with a
    # reload-on-update flow is deprecated and becomes an error in HA 2026.12+.
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: PPLCZConfigEntry
) -> bool:
    """Unload a PPL CZ config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
