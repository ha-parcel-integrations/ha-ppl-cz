"""Coordinator for the PPL CZ parcel tracker integration.

Fetches the account's shipment inbox in **one** call (both directions —
`shipment_type=ALL`), enriches each shipment with its event history only when
the opt-in history option is on, and publishes the canonical parcel lists.
Event firing lives here too; the parcel mapping is in :mod:`.parcels`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PPLCZApiClient, PPLCZApiError, PPLCZAuthError
from .const import (
    CONF_INCLUDE_HISTORY,
    CONF_REFRESH_INTERVAL,
    DEFAULT_INCLUDE_HISTORY,
    DEFAULT_REFRESH_INTERVAL,
    DIRECTION_OUTGOING,
    DOMAIN,
    ParcelStatus,
)
from .parcels import (
    apply_delivered_filter,
    build_history,
    normalize_parcel,
    note_events_shape,
    note_items_shape,
    shipment_direction,
    sort_parcels_by_ts,
)

_LOGGER = logging.getLogger(__name__)


def _refresh_interval(entry: ConfigEntry) -> timedelta:
    """Return the configured refresh interval as a ``timedelta``."""
    minutes = int(entry.options.get(CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL))
    return timedelta(minutes=minutes)


class PPLCZCoordinator(DataUpdateCoordinator[list[dict]]):
    """Polls the account inbox and publishes incoming/outgoing parcel lists.

    ``coordinator.data`` is the active **incoming** parcels;
    ``self.delivered`` the delivered incoming ones; ``self.outgoing`` /
    ``self.delivered_outgoing`` the sent ones. Both directions come from the
    *same* list call — split by
    :func:`~custom_components.ppl_cz.parcels.shipment_direction`, not by two
    separate endpoints (unlike DHL NL / DPD).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: PPLCZApiClient,
        entry: ConfigEntry,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=_refresh_interval(entry),
        )
        self._client = client
        self.delivered: list[dict] = []
        self.outgoing: list[dict] = []
        self.delivered_outgoing: list[dict] = []
        # barcode -> last seen ParcelStatus, per direction. ``None`` on the
        # first refresh so events are suppressed for parcels that already
        # existed when the integration started.
        self._known_state: dict[str, ParcelStatus] | None = None
        self._known_outgoing_state: dict[str, ParcelStatus] | None = None
        # barcode -> (last seen lastShipmentEvent, canonical history list).
        # GET .../events is a fan-out — one request per parcel — so it's
        # refetched only when the shipment's coarse status changed since the
        # last poll, mirroring DHL NL's track-trace cache.
        self._history_cache: dict[str, tuple[str | None, list[dict]]] = {}
        self._cached_device_id: str | None = None
        self.last_success_time: datetime | None = None

    def _device_id(self) -> str | None:
        """Resolve (and cache) this entry's device id for event payloads."""
        if self._cached_device_id is not None:
            return self._cached_device_id
        registry = dr.async_get(self.hass)
        device = next(
            iter(
                dr.async_entries_for_config_entry(registry, self.config_entry.entry_id)
            ),
            None,
        )
        if device is not None:
            self._cached_device_id = device.id
        return self._cached_device_id

    @property
    def _include_history(self) -> bool:
        """Whether the opt-in per-parcel history option is enabled."""
        return bool(
            self.config_entry.options.get(
                CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
            )
        )

    async def _async_update_data(self) -> list[dict]:
        """Fetch the account inbox, enrich, split by direction and publish."""
        try:
            raws = await self._client.async_get_parcels()
            note_items_shape(raws)

            include_history = self._include_history
            barcodes_seen: set[str] = set()
            incoming: list[dict] = []
            outgoing: list[dict] = []
            for raw in raws:
                barcode = raw.get("number")
                history = None
                if include_history and barcode:
                    history = await self._history_for(raw, barcode)
                if barcode:
                    barcodes_seen.add(barcode)
                parcel = normalize_parcel(raw, history=history)
                if shipment_direction(raw) == DIRECTION_OUTGOING:
                    outgoing.append(parcel)
                else:
                    incoming.append(parcel)
        except PPLCZAuthError as err:
            raise ConfigEntryAuthFailed("PPL CZ session expired") from err
        except PPLCZApiError as err:
            raise UpdateFailed(f"PPL CZ error: {err}") from err
        # aiohttp.ClientError is wrapped automatically by DataUpdateCoordinator.

        # Drop cache entries for shipments no longer on the account.
        self._history_cache = {
            barcode: value
            for barcode, value in self._history_cache.items()
            if barcode in barcodes_seen
        }

        delivered_incoming = [p for p in incoming if p["delivered"]]
        active_incoming = [p for p in incoming if not p["delivered"]]
        delivered_outgoing = [p for p in outgoing if p["delivered"]]
        active_outgoing = [p for p in outgoing if not p["delivered"]]

        self.delivered = apply_delivered_filter(
            sort_parcels_by_ts(delivered_incoming, "delivered_at", descending=True),
            self.config_entry,
        )
        normalized_active = sort_parcels_by_ts(active_incoming, "planned_from")
        self.outgoing = sort_parcels_by_ts(active_outgoing, "planned_from")
        self.delivered_outgoing = apply_delivered_filter(
            sort_parcels_by_ts(delivered_outgoing, "delivered_at", descending=True),
            self.config_entry,
        )

        # Incoming = active + delivered, combined so the transition to
        # delivered is visible in one set.
        combined_in = normalized_active + self.delivered
        self._fire_change_events(combined_in)
        self._known_state = self._status_map(combined_in)

        # Outgoing = active + delivered, same reasoning.
        combined_out = self.outgoing + self.delivered_outgoing
        self._fire_outgoing_change_events(combined_out)
        self._known_outgoing_state = self._status_map(combined_out)

        self.last_success_time = datetime.now(timezone.utc)
        return normalized_active

    async def _history_for(self, raw: dict, barcode: str) -> list[dict] | None:
        """Return the cached/fresh canonical history for one shipment.

        Best-effort: a failed events call keeps whatever history was cached
        (or ``None``) rather than blanking the attribute or failing the poll
        over one parcel — the list call already carries the current status.
        """
        raw_status = raw.get("lastShipmentEvent")
        cached = self._history_cache.get(barcode)
        if cached is not None and cached[0] == raw_status:
            return cached[1]
        shipment_id = raw.get("id")
        if not shipment_id:
            return cached[1] if cached else None
        events = await self._client.async_get_shipment_events(shipment_id)
        if events is None:
            return cached[1] if cached else None
        note_events_shape(events)
        history = build_history(events)
        self._history_cache[barcode] = (raw_status, history)
        return history

    @staticmethod
    def _status_map(parcels: list[dict]) -> dict[str, ParcelStatus]:
        """Map barcode -> status, for the next poll's change detection."""
        return {p["barcode"]: p["status"] for p in parcels if p.get("barcode")}

    def _fire_change_events(self, parcels: list[dict]) -> None:
        """Fire incoming registered / status-changed / delivered events.

        Silent on the very first refresh. There is no
        ``parcel_delivery_time_changed`` — PPL CZ's list DTOs expose no ETA.
        The hop **to** ``delivered`` fires only ``_parcel_delivered``; a
        barcode first seen already-delivered fires nothing; ``registered``
        fires only for a new, not-yet-delivered barcode.
        """
        if self._known_state is None:
            return
        device_id = self._device_id()
        for parcel in parcels:
            barcode = parcel.get("barcode")
            if not barcode:
                continue
            new_status = parcel["status"]
            if barcode not in self._known_state:
                if new_status != ParcelStatus.DELIVERED:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_registered",
                        {**parcel, "device_id": device_id},
                    )
                continue
            if self._known_state[barcode] != new_status:
                if new_status == ParcelStatus.DELIVERED:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_delivered",
                        {**parcel, "device_id": device_id},
                    )
                else:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_status_changed",
                        {
                            **parcel,
                            "device_id": device_id,
                            "old_status": self._known_state[barcode],
                            "new_status": new_status,
                        },
                    )

    def _fire_outgoing_change_events(self, parcels: list[dict]) -> None:
        """Fire outgoing status-changed / delivered events.

        No ``registered`` and no delivery-time event for outgoing (deliberate,
        same as the rest of the suite). The hop to ``delivered`` fires only
        ``_outgoing_parcel_delivered``.
        """
        if self._known_outgoing_state is None:
            return
        device_id = self._device_id()
        for parcel in parcels:
            barcode = parcel.get("barcode")
            if not barcode or barcode not in self._known_outgoing_state:
                continue
            old_status = self._known_outgoing_state[barcode]
            new_status = parcel["status"]
            if new_status == old_status:
                continue
            if new_status == ParcelStatus.DELIVERED:
                self.hass.bus.async_fire(
                    f"{DOMAIN}_outgoing_parcel_delivered",
                    {**parcel, "device_id": device_id},
                )
            else:
                self.hass.bus.async_fire(
                    f"{DOMAIN}_outgoing_parcel_status_changed",
                    {
                        **parcel,
                        "device_id": device_id,
                        "old_status": old_status,
                        "new_status": new_status,
                    },
                )
