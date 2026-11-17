"""Sensor platform for the Lyse Charge integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import LyseCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Lyse Charge sensors from a config entry."""
    coordinator: LyseCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []

    chargers = (coordinator.data or {}).get("chargers", {})
    for cfg in coordinator.charger_configs:
        charger_id = cfg["charger_id"]
        alias = cfg["alias"]
        charger = chargers.get(charger_id, {})
        sockets = [s["id"] for s in charger.get("sockets", [])] or [1]

        entities.append(LyseChargerStatusSensor(coordinator, charger_id, alias, charger))

        for socket_id in sockets:
            entities += [
                LyseSocketStatusSensor(coordinator, charger_id, alias, socket_id, charger),
                LysePowerSensor(coordinator, charger_id, alias, socket_id, charger),
                LyseSessionEnergySensor(coordinator, charger_id, alias, socket_id, charger),
                LyseSessionCostSensor(coordinator, charger_id, alias, socket_id, charger),
                LyseTariffSensor(coordinator, charger_id, alias, socket_id, charger),
            ]

    async_add_entities(entities)


def _device_info(charger_id: str, alias: str, charger: dict) -> DeviceInfo:
    return {
        "identifiers": {(DOMAIN, charger_id)},
        "name": charger.get("chargerName") or charger_id,
        "model": charger.get("chargerModel"),
        "serial_number": charger.get("serialNumber"),
    }


class LyseChargerStatusSensor(CoordinatorEntity[LyseCoordinator], SensorEntity):
    """Sensor reporting the overall status of a charger."""

    _attr_icon = "mdi:ev-station"

    def __init__(self, coordinator: LyseCoordinator, charger_id: str, alias: str, charger: dict) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._charger_id = charger_id
        self._alias = alias
        self._attr_unique_id = f"{charger_id}_charger_status"
        self._attr_name = f"{alias} charger status"
        self._attr_device_info = _device_info(charger_id, alias, charger)

    @property
    def native_value(self) -> str | None:
        """Return the charger status."""
        live = self.coordinator.data.get("live", {}).get(self._charger_id)
        if live and live.get("chargerStatus"):
            return str(live["chargerStatus"]).title()
        charger = self.coordinator.data.get("chargers", {}).get(self._charger_id)
        return (charger or {}).get("chargerStatus")


class LyseSocketStatusSensor(CoordinatorEntity[LyseCoordinator], SensorEntity):
    """Sensor reporting the status of a single charger socket."""

    _attr_icon = "mdi:power-socket-eu"

    def __init__(
        self, coordinator: LyseCoordinator, charger_id: str, alias: str, socket_id: int, charger: dict
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._charger_id = charger_id
        self._socket_id = socket_id
        self._attr_unique_id = f"{charger_id}_socket_{socket_id}_status"
        self._attr_name = f"{alias} socket {socket_id} status"
        self._attr_device_info = _device_info(charger_id, alias, charger)

    @property
    def native_value(self) -> str | None:
        """Return the socket status."""
        live = self.coordinator.data.get("live", {}).get(self._charger_id)
        if live and live.get("socketStatus"):
            return str(live["socketStatus"]).title()
        charger = self.coordinator.data.get("chargers", {}).get(self._charger_id)
        for sock in (charger or {}).get("sockets", []):
            if sock["id"] == self._socket_id:
                status = sock.get("status")
                return str(status) if status is not None else None
        return None


class _LiveSessionSensor(CoordinatorEntity[LyseCoordinator], SensorEntity):
    def __init__(
        self, coordinator: LyseCoordinator, charger_id: str, alias: str, socket_id: int, charger: dict
    ) -> None:
        super().__init__(coordinator)
        self._charger_id = charger_id
        self._socket_id = socket_id
        self._charger = charger
        self._alias = alias
        self._attr_device_info = _device_info(charger_id, alias, charger)

    def _active_session(self) -> dict | None:
        live = self.coordinator.data.get("live", {}).get(self._charger_id)
        if live:
            session = live.get("activeChargeSession")
            return dict(session) if session is not None else None
        return None


class LysePowerSensor(_LiveSessionSensor):
    """Sensor reporting the current charging power of an active session."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_icon = "mdi:lightning-bolt"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the sensor."""
        super().__init__(*args, **kwargs)
        charger_id, alias, socket_id = self._charger_id, self._alias, self._socket_id
        self._attr_unique_id = f"{charger_id}_socket_{socket_id}_power"
        self._attr_name = f"{alias} socket {socket_id} power"

    @property
    def native_value(self) -> float | None:
        """Return the current charging power in kW."""
        session = self._active_session()
        return (session or {}).get("currentPowerKw")


class LyseSessionEnergySensor(_LiveSessionSensor):
    """Sensor reporting the energy charged during the active session."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:battery-charging"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the sensor."""
        super().__init__(*args, **kwargs)
        charger_id, alias, socket_id = self._charger_id, self._alias, self._socket_id
        self._attr_unique_id = f"{charger_id}_socket_{socket_id}_session_energy"
        self._attr_name = f"{alias} socket {socket_id} session energy"

    @property
    def native_value(self) -> float | None:
        """Return the energy charged in kWh during the active session."""
        session = self._active_session()
        return (session or {}).get("currentEnergyChargedKwh")


class LyseSessionCostSensor(_LiveSessionSensor):
    """Sensor reporting the cost of the active charging session."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:currency-usd"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the sensor."""
        super().__init__(*args, **kwargs)
        charger_id, alias, socket_id = self._charger_id, self._alias, self._socket_id
        self._attr_unique_id = f"{charger_id}_socket_{socket_id}_session_cost"
        self._attr_name = f"{alias} socket {socket_id} session cost"
        self._attr_native_unit_of_measurement = "NOK"

    @property
    def native_value(self) -> float | None:
        """Return the cost, including VAT, of the active charging session."""
        session = self._active_session()
        return (session or {}).get("currentCostInclMva")


class LyseTariffSensor(CoordinatorEntity[LyseCoordinator], SensorEntity):
    """Sensor reporting the current energy tariff for a charger socket."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:cash-clock"

    def __init__(
        self, coordinator: LyseCoordinator, charger_id: str, alias: str, socket_id: int, charger: dict
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._charger_id = charger_id
        self._socket_id = socket_id
        self._attr_unique_id = f"{charger_id}_socket_{socket_id}_tariff"
        self._attr_name = f"{alias} socket {socket_id} tariff"
        self._attr_device_info = _device_info(charger_id, alias, charger)

    @property
    def native_value(self) -> float | None:
        """Return the current energy price, including VAT."""
        live = self.coordinator.data.get("live", {}).get(self._charger_id)
        if not live:
            return None
        tariff = live.get("tariff") or {}
        energy = tariff.get("energyComponent") or {}
        return energy.get("priceInclMva")

    @property
    def native_unit_of_measurement(self) -> str:
        """Return the currency-per-kWh unit for the current tariff."""
        live = self.coordinator.data.get("live", {}).get(self._charger_id)
        currency = ((live or {}).get("tariff") or {}).get("currency") or "NOK"
        return f"{currency}/kWh"
