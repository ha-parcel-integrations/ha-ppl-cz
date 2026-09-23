"""The device every entity of this integration belongs to.

One place, because sensors, the button and the calendar must all land on the
*same* device entry — and because the device is named per source/account
here, several devices can exist: one per configured account, plus at most
one tracking-codes device.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo

from .const import CONF_SOURCE, DOMAIN, SOURCE_ACCOUNT, SOURCE_TRACKING

CONFIGURATION_URL = "https://www.ppl.cz"

ATTRIBUTION = "Data provided by PPL CZ"


def build_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Return the DeviceInfo shared by every entity of this hub.

    An account hub's identifier is part of the name, so two configured
    accounts do not produce two indistinguishable devices. Entities inherit
    it via ``has_entity_name``, yielding names like
    "PPL CZ (you@example.com) Incoming parcels". Only one tracking hub can
    ever exist (config_flow.py's fixed unique id), so its name is fixed too.
    """
    if entry.data.get(CONF_SOURCE, SOURCE_ACCOUNT) == SOURCE_TRACKING:
        name = "PPL CZ (tracking codes)"
    else:
        name = f"PPL CZ ({entry.title})"
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=name,
        manufacturer="PPL CZ",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=CONFIGURATION_URL,
    )
