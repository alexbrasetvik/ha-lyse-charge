"""Switch platform for the Lyse Charge integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import LyseAPIError
from .const import DOMAIN
from .coordinator import LyseCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Lyse Charge switches from a config entry."""
    coordinator: LyseCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchEntity] = []

    chargers = (coordinator.data or {}).get("chargers", {})
    for cfg in coordinator.charger_configs:
        charger_id = cfg["charger_id"]
        alias = cfg["alias"]
        charger = chargers.get(charger_id, {})
        sockets = [s["id"] for s in charger.get("sockets", [])] or [1]

        entities.extend(LyseChargingSwitch(coordinator, charger_id, alias, socket_id, charger) for socket_id in sockets)

    async_add_entities(entities)


class LyseChargingSwitch(CoordinatorEntity[LyseCoordinator], SwitchEntity):
    """Switch to start and stop charging on a charger socket."""

    _attr_icon = "mdi:ev-station"

    def __init__(
        self,
        coordinator: LyseCoordinator,
        charger_id: str,
        alias: str,
        socket_id: int,
        charger: dict,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._charger_id = charger_id
        self._socket_id = socket_id
        self._attr_unique_id = f"{charger_id}_socket_{socket_id}_charging"
        self._attr_name = f"{alias} socket {socket_id} charging"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, charger_id)},
            name=charger.get("chargerName") or charger_id,
            manufacturer=charger.get("manufacturer"),
            model=charger.get("chargerModel"),
        )

    _CHARGING_STATUSES = {"charging", "occupied"}

    @property
    def is_on(self) -> bool:
        """Return whether the socket is currently charging."""
        # Live WS socket status is most up-to-date
        live = self.coordinator.data.get("live", {}).get(self._charger_id)
        if live and (socket_status := live.get("socketStatus")):
            return socket_status.lower() in self._CHARGING_STATUSES

        # Fall back to polled socket status
        charger = self.coordinator.data.get("chargers", {}).get(self._charger_id)
        for sock in (charger or {}).get("sockets", []):
            if sock["id"] == self._socket_id:
                return (sock.get("status") or "").lower() in self._CHARGING_STATUSES
        return False

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start charging on this socket."""
        try:
            result = await self.coordinator.api.start_charging(self._charger_id, self._socket_id)
            _LOGGER.debug("start_charging %s/%s: %s", self._charger_id, self._socket_id, result)
        except LyseAPIError as err:
            _LOGGER.error("Failed to start charging: %s", err)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop charging on this socket."""
        try:
            result = await self.coordinator.api.stop_charging(self._charger_id, self._socket_id)
            _LOGGER.debug("stop_charging %s/%s: %s", self._charger_id, self._socket_id, result)
        except LyseAPIError as err:
            _LOGGER.error("Failed to stop charging: %s", err)
        await self.coordinator.async_request_refresh()
