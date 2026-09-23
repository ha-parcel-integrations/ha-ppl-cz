"""Coordinator for the tracking-code source.

Fetches each tracked barcode individually against the website tracking
surface, splits by the user-declared direction (see ``tracked_direction`` in
``parcels.py`` — this route cannot infer it the way the account source
does), and publishes the same canonical parcel lists / event set the account
coordinator does. Unlike the account source there is no discovery — the user
adds barcodes explicitly through the options flow — so this coordinator may
suspend polling entirely once nothing is tracked or everything tracked is
delivered, mirroring bpost's/Packeta's barcode-based model rather than the
account source's "never fully stops" one.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_BARCODE,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    DEFAULT_INCLUDE_HISTORY,
    DIRECTION_OUTGOING,
    DOMAIN,
    HOT_INTERVAL_MINUTES,
    HOT_LOOKAHEAD_HOURS,
    MID_INTERVAL_MINUTES,
    QUIET_WINDOW_END_HOUR,
    QUIET_WINDOW_START_HOUR,
    STAGGER_MINUTES,
    ParcelStatus,
)
from .api import PPLCZTrackingApiClient, PPLCZTrackingApiError, PPLCZTrackingNotFound
from .parcels import (
    apply_delivered_filter,
    normalize_parcel,
    sort_parcels_by_ts,
    tracked_direction,
)

_LOGGER = logging.getLogger(__name__)


def _stagger_minutes(entry_id: str) -> int:
    """Deterministic per-install offset, stable across restarts."""
    digest = hashlib.sha256(entry_id.encode()).hexdigest()
    return int(digest, 16) % STAGGER_MINUTES


def _in_quiet_window(moment: datetime) -> bool:
    """Whether ``moment`` (local time) falls in the no-polling window."""
    return QUIET_WINDOW_START_HOUR <= moment.hour < QUIET_WINDOW_END_HOUR


def _next_anchor(now: datetime) -> datetime:
    """Return the next of the two daily anchors (00:00 / 06:00 local)."""
    six_today = now.replace(
        hour=QUIET_WINDOW_END_HOUR, minute=0, second=0, microsecond=0
    )
    if now < six_today:
        return six_today
    midnight_tomorrow = (now + timedelta(days=1)).replace(
        hour=QUIET_WINDOW_START_HOUR, minute=0, second=0, microsecond=0
    )
    return midnight_tomorrow


def _hottest_tier_minutes(active_parcels: list[dict], now: datetime) -> int | None:
    """Tier for the barcode-based model. ``None`` means "stop polling"."""
    if not active_parcels:
        return None

    for parcel in active_parcels:
        if parcel["status"] != ParcelStatus.OUT_FOR_DELIVERY:
            continue
        planned_from = parcel.get("planned_from")
        if not planned_from:
            return HOT_INTERVAL_MINUTES
        planned_dt = dt_util.parse_datetime(planned_from)
        if planned_dt is None:
            return HOT_INTERVAL_MINUTES
        if dt_util.as_utc(now) >= dt_util.as_utc(planned_dt) - timedelta(
            hours=HOT_LOOKAHEAD_HOURS
        ):
            return HOT_INTERVAL_MINUTES

    return MID_INTERVAL_MINUTES


def _next_update_interval(
    now: datetime, tier_minutes: int | None, entry_id: str
) -> timedelta | None:
    """Turn a tier into the coordinator's next ``update_interval``."""
    if tier_minutes is None:
        return None

    if _in_quiet_window(now):
        return _next_anchor(now) - now

    stagger = timedelta(minutes=_stagger_minutes(entry_id))
    candidate = now + timedelta(minutes=tier_minutes) + stagger
    if _in_quiet_window(candidate):
        return _next_anchor(now) - now
    return candidate - now


class PPLCZTrackingCoordinator(DataUpdateCoordinator[list[dict]]):
    """Polls every tracked barcode and publishes the canonical parcel lists.

    ``coordinator.data`` is the active **incoming** parcels;
    ``self.delivered`` the delivered incoming ones; ``self.outgoing`` /
    ``self.delivered_outgoing`` the sent ones — same shape as the account
    coordinator, but split by the user's declared direction rather than a
    payload field.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: PPLCZTrackingApiClient,
        entry: ConfigEntry,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=HOT_INTERVAL_MINUTES),
        )
        self._client = client
        self.delivered: list[dict] = []
        self.outgoing: list[dict] = []
        self.delivered_outgoing: list[dict] = []
        # barcode -> last successful raw payload, so a transient fetch
        # failure or a not-found blip keeps the parcel visible instead of
        # dropping its sensor. Lives for the integration's lifetime.
        self._raw_cache: dict[str, dict] = {}
        # Barcodes confirmed delivered on a prior refresh — excluded from
        # the fetch this cycle, since a delivered parcel's payload can never
        # change again.
        self._delivered_codes: set[str] = set()
        self._current_tier_minutes: int | None = None
        self._known_state: dict[str, ParcelStatus] | None = None
        self._known_outgoing_state: dict[str, ParcelStatus] | None = None
        self._cached_device_id: str | None = None
        self.last_success_time: datetime | None = None

    @property
    def current_tier_minutes(self) -> int | None:
        """Tier minutes computed on the last refresh (diagnostics only)."""
        return self._current_tier_minutes

    @property
    def delivered_codes(self) -> set[str]:
        """Barcodes currently skipped from the fetch (diagnostics only)."""
        return self._delivered_codes

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

    def _tracked(self) -> dict[str, str]:
        """Return the configured tracking codes mapped to their declared direction."""
        return {
            item[CONF_BARCODE]: tracked_direction(item)
            for item in self.config_entry.options.get(CONF_PARCELS, [])
            if item.get(CONF_BARCODE)
        }

    @property
    def _include_history(self) -> bool:
        """Whether the opt-in per-parcel history option is enabled."""
        return bool(
            self.config_entry.options.get(
                CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
            )
        )

    async def _async_update_data(self) -> list[dict]:
        """Fetch every tracked barcode, split by direction and by delivered."""
        directions = self._tracked()
        tracked = list(directions)
        tracked_set = set(tracked)

        self._raw_cache = {
            code: raw for code, raw in self._raw_cache.items() if code in tracked_set
        }
        self._delivered_codes &= tracked_set

        to_fetch = [code for code in tracked if code not in self._delivered_codes]

        results = await asyncio.gather(
            *(self._client.async_get_parcel(code) for code in to_fetch),
            return_exceptions=True,
        )

        raws: dict[str, dict] = {}
        errors = 0
        for code, result in zip(to_fetch, results):
            if isinstance(result, PPLCZTrackingNotFound):
                # Not found is a real answer, not a fetch failure — publish
                # it as an unknown/pending placeholder rather than keeping a
                # stale cached payload around.
                raws[code] = {}
                continue
            if isinstance(result, BaseException):
                if not isinstance(result, (PPLCZTrackingApiError, aiohttp.ClientError)):
                    raise result
                errors += 1
                _LOGGER.warning("PPL CZ tracking fetch failed for %s: %s", code, result)
                cached = self._raw_cache.get(code)
                if cached is not None:
                    raws[code] = cached
                continue

            self._raw_cache[code] = result
            raws[code] = result

        # Codes skipped from the fetch above (already confirmed delivered) —
        # re-add their cached payload so the delivered sensor keeps its data
        # until the retention filter drops it.
        for code in tracked:
            if code in self._delivered_codes:
                cached = self._raw_cache.get(code)
                if cached is not None:
                    raws[code] = cached

        if to_fetch and errors == len(to_fetch) and not raws:
            raise UpdateFailed("PPL CZ tracking unreachable for all tracked parcels")

        include_history = self._include_history
        entries = [
            (
                code,
                normalize_parcel(
                    raws.get(code, {}), tracking_code=code, include_history=include_history
                ),
            )
            for code in tracked
        ]
        self._delivered_codes = {code for code, parcel in entries if parcel["delivered"]}

        outgoing_codes = {
            code for code in directions if directions[code] == DIRECTION_OUTGOING
        }
        sent = [parcel for code, parcel in entries if code in outgoing_codes]
        received = [parcel for code, parcel in entries if code not in outgoing_codes]

        self.delivered = apply_delivered_filter(
            sort_parcels_by_ts(
                [p for p in received if p["delivered"]], "delivered_at", descending=True
            ),
            self.config_entry,
        )
        self.delivered_outgoing = apply_delivered_filter(
            sort_parcels_by_ts(
                [p for p in sent if p["delivered"]], "delivered_at", descending=True
            ),
            self.config_entry,
        )
        normalized_active = sort_parcels_by_ts(
            [p for p in received if not p["delivered"]], "planned_from"
        )
        self.outgoing = sort_parcels_by_ts(
            [p for p in sent if not p["delivered"]], "planned_from"
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

        if not to_fetch or errors < len(to_fetch):
            self.last_success_time = datetime.now(timezone.utc)

        now = dt_util.now()
        self._current_tier_minutes = _hottest_tier_minutes(
            normalized_active + self.outgoing, now
        )
        self.update_interval = _next_update_interval(
            now, self._current_tier_minutes, self.config_entry.entry_id
        )
        return normalized_active

    @staticmethod
    def _status_map(parcels: list[dict]) -> dict[str, ParcelStatus]:
        """Map barcode -> status, for the next poll's change detection."""
        return {p["barcode"]: p["status"] for p in parcels if p.get("barcode")}

    def _fire_change_events(self, parcels: list[dict]) -> None:
        """Fire incoming registered / status-changed / delivered events.

        Silent on the very first refresh, same as the account source. No
        delivery-time event — this source has no ETA range, only a single
        ``expectedDeliveryDate``.
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
        same as the account source and the rest of the suite): a parcel the
        user handed over is not news when it appears, and its ETA is the
        recipient's business. The hop to ``delivered`` fires only
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
