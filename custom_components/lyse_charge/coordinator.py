"""Data update coordinator for the Lyse Charge integration."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import functools
import logging
import random
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import LyseAPI, LyseAPIError, LyseAuthError
from .const import DOMAIN, MAX_CONSECUTIVE_FAILURES, POLL_INTERVAL, WS_MAX_BACKOFF

_LOGGER = logging.getLogger(__name__)


class LyseCoordinator(DataUpdateCoordinator):
    """Coordinate polled and websocket-pushed Lyse Charge data."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: LyseAPI,
        entry: ConfigEntry,
        charger_configs: list[dict],
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=POLL_INTERVAL),
        )
        self.api = api
        self._entry = entry
        self.charger_configs = charger_configs
        # charger_id → latest activity dict from WS
        self.live: dict[str, dict] = {}
        self._ws_tasks: list[asyncio.Task] = []
        self._consecutive_failures = 0

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            if await self.api.ensure_token():
                self.hass.config_entries.async_update_entry(
                    self._entry, data={**self._entry.data, **self.api.token_data}
                )
        except LyseAuthError as err:
            raise ConfigEntryAuthFailed(f"Login failed: {err}") from err
        except LyseAPIError as err:
            raise UpdateFailed(f"Login failed: {err}") from err

        try:
            charger_ids = [c["charger_id"] for c in self.charger_configs]
            chargers = await self.api.get_chargers(charger_ids)
            active_sessions = await self.api.get_active_sessions()
        except LyseAPIError as err:
            if self.data is None or self._consecutive_failures + 1 >= MAX_CONSECUTIVE_FAILURES:
                raise UpdateFailed(str(err)) from err
            self._consecutive_failures += 1
            _LOGGER.warning(
                "Update failed (%s/%s consecutive failures), serving last-known data: %s",
                self._consecutive_failures,
                MAX_CONSECUTIVE_FAILURES,
                err,
            )
            return {**self.data, "live": self.live}

        self._consecutive_failures = 0

        # Index active sessions by (chargerId, socketId)
        session_index: dict[tuple[str, int], dict] = {}
        for s in active_sessions:
            key = (s.get("chargerId", ""), int(s.get("socketId", 0)))
            session_index[key] = s

        return {
            "chargers": {c["chargerId"]: c for c in chargers},
            "active_sessions": session_index,
            "live": self.live,
        }

    async def start_websockets(self) -> None:
        """Start one websocket subscription per charger socket."""
        for cfg in self.charger_configs:
            charger_id = cfg["charger_id"]
            # Start one subscription per socket; discover sockets from first poll
            charger = (self.data or {}).get("chargers", {}).get(charger_id)
            sockets = [s["id"] for s in (charger or {}).get("sockets", [])] if charger else [1]
            for socket_id in sockets:
                task = self.hass.async_create_background_task(
                    self._ws_loop(charger_id, socket_id),
                    name=f"lyse_charge_ws_{charger_id}_{socket_id}",
                )
                self._ws_tasks.append(task)
        _LOGGER.debug("Started %d websocket subscription(s)", len(self._ws_tasks))

    async def stop_websockets(self) -> None:
        """Cancel and await all running websocket subscriptions."""
        _LOGGER.debug("Stopping %d websocket subscription(s)", len(self._ws_tasks))
        for task in self._ws_tasks:
            task.cancel()
        if self._ws_tasks:
            await asyncio.gather(*self._ws_tasks, return_exceptions=True)
        self._ws_tasks.clear()

    async def _ws_loop(self, charger_id: str, socket_id: int) -> None:
        backoff = 2.0
        while True:
            try:
                _LOGGER.debug("WS connecting %s socket %s", charger_id, socket_id)
                await self.api.subscribe_activity(
                    charger_id,
                    socket_id,
                    functools.partial(self._on_ws_event, charger_id),
                )
                backoff = 2.0
            except asyncio.CancelledError:
                return
            except Exception as err:  # noqa: BLE001 - this loop should never stop
                _LOGGER.warning("WS %s/%s disconnected: %s — retry in %ss", charger_id, socket_id, err, backoff)
                try:
                    await asyncio.sleep(random.uniform(0, backoff))
                except asyncio.CancelledError:
                    return
                backoff = min(backoff * 2, WS_MAX_BACKOFF)

    def _on_ws_event(self, charger_id: str, activity: dict) -> None:
        self.live[charger_id] = activity
        if self.data:
            self.data["live"] = self.live
            self.async_set_updated_data(self.data)
