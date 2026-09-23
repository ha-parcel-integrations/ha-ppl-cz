"""Calendar platform for the PPL CZ parcel tracker integration.

Tracking-source only — forwarded through ``PLATFORMS[SOURCE_TRACKING]``.
The account source's DTOs carry no ETA at all (see const.py), so it never
forwards this platform; an account hub must never get a permanently-empty
calendar entity.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import PPLCZConfigEntry
from .device import ATTRIBUTION, build_device_info
from .tracking.parcels import parse_iso

PARALLEL_UPDATES = 0

_DEFAULT_EVENT_DURATION = timedelta(hours=1)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PPLCZConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the PPL CZ deliveries calendar from a config entry."""
    async_add_entities([PPLCZDeliveriesCalendar(entry.runtime_data.coordinator, entry)])


class PPLCZDeliveriesCalendar(CoordinatorEntity, CalendarEntity):
    """A read-only calendar of expected PPL CZ deliveries.

    Each active tracked parcel with a known ``expectedDeliveryDate`` becomes
    an event. No extra API call — a pure view over coordinator data.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "deliveries"
    _attr_attribution = ATTRIBUTION

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        """Initialize the calendar."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_deliveries"
        self._attr_device_info = build_device_info(entry)

    def _events(self) -> list[CalendarEvent]:
        events: list[CalendarEvent] = []
        for parcel in self.coordinator.data or []:
            start = parse_iso(parcel.get("planned_from"))
            if start is None:
                continue
            end = parse_iso(parcel.get("planned_to"))
            if end is None or end <= start:
                end = start + _DEFAULT_EVENT_DURATION

            barcode = parcel.get("barcode") or ""
            summary = f"Parcel {barcode}" if barcode else "PPL CZ parcel"
            description_parts = [
                f"Barcode: {barcode}" if barcode else None,
                f"Status: {parcel.get('status')}" if parcel.get("status") else None,
                parcel.get("url"),
            ]
            description = "\n".join(p for p in description_parts if p)
            location = parcel.get("pickup_point") if parcel.get("pickup") else None

            events.append(
                CalendarEvent(
                    start=start,
                    end=end,
                    summary=summary,
                    description=description or None,
                    location=location,
                    uid=barcode or None,
                )
            )
        return events

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming calendar event."""
        now = dt_util.now()
        upcoming = [event for event in self._events() if event.end > now]
        return min(upcoming, key=lambda event: event.start) if upcoming else None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return calendar events within a datetime range."""
        return [
            event
            for event in self._events()
            if event.start < end_date and event.end > start_date
        ]
