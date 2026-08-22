"""The device every entity of this integration belongs to.

One place, because sensors, the button and the calendar must all land on the
*same* device entry — and because the device is named per account here, an
account-based integration can have several.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN

CONFIGURATION_URL = "https://www.ppl.cz"

ATTRIBUTION = "Data provided by PPL CZ"


def build_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Return the DeviceInfo shared by every entity of this account.

    The account identifier is part of the name, so two configured accounts do
    not produce two indistinguishable devices. Entities inherit it via
    ``has_entity_name``, yielding names like
    "PPL CZ (you@example.com) Incoming parcels".
    """
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"PPL CZ ({entry.title})",
        manufacturer="PPL CZ",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=CONFIGURATION_URL,
    )
