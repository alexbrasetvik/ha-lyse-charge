"""Tests for Lyse Charge sensors and switches, fed through the websocket path."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lyse_charge.const import DOMAIN

from .conftest import CHARGER, CHARGER_ID, FakeLyseAPI

CHARGING_ACTIVITY = {
    "chargerId": CHARGER_ID,
    "socketId": 1,
    "chargerStatus": "CHARGING",
    "socketStatus": "OCCUPIED",
    "message": None,
    "activeChargeSession": {
        "transactionId": 42,
        "startTime": "2026-11-17T16:00:00Z",
        "startMeterValueKwh": 1200.5,
        "currentMeterValueKwh": 1207.25,
        "currentPowerKw": 7.2,
        "currentCostInclMva": 14.3,
    },
    "tariff": {
        "currency": "NOK",
        "energyComponent": {"priceExclMva": 1.6, "priceInclMva": 2.0, "mvaPercentage": 25},
        "spotComponent": {"priceExclMva": 0.8, "priceInclMva": 1.0, "mvaPercentage": 25},
    },
}


@pytest.fixture
async def loaded_entry(
    hass: HomeAssistant, config_entry: MockConfigEntry, fake_api: FakeLyseAPI
) -> AsyncGenerator[MockConfigEntry]:
    """Set up the entry with one charging activity pushed over the websocket."""
    fake_api.activities = [CHARGING_ACTIVITY]
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    yield config_entry
    assert await hass.config_entries.async_unload(config_entry.entry_id)


def _entity_id(hass: HomeAssistant, domain: str, suffix: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{CHARGER_ID}_{suffix}")
    assert entity_id, f"No {domain} entity with unique ID suffix {suffix}"
    return entity_id


@pytest.mark.usefixtures("loaded_entry")
@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        # Derived from the meter readings: the app's API no longer has currentEnergyChargedKwh.
        ("socket_1_session_energy", "6.75"),
        ("socket_1_power", "7.2"),
        ("socket_1_session_cost", "14.3"),
        ("socket_1_tariff", "2.0"),
        ("charger_status", "Charging"),
        ("socket_1_status", "Occupied"),
    ],
)
async def test_sensors_from_live_activity(hass: HomeAssistant, suffix: str, expected: str) -> None:
    """Sensor states come from the latest chargerActivityStream event."""
    state = hass.states.get(_entity_id(hass, "sensor", suffix))
    assert state is not None
    assert state.state == expected


@pytest.mark.usefixtures("loaded_entry")
async def test_charging_switch(hass: HomeAssistant, fake_api: FakeLyseAPI) -> None:
    """The switch reflects the live socket status and starts charging via the API."""
    entity_id = _entity_id(hass, "switch", "socket_1_charging")
    assert hass.states.get(entity_id).state == "on"

    await hass.services.async_call("switch", SERVICE_TURN_ON, {ATTR_ENTITY_ID: entity_id}, blocking=True)

    fake_api.start_charging.assert_awaited_once_with(CHARGER_ID, 1)


async def test_session_energy_never_negative(
    hass: HomeAssistant, config_entry: MockConfigEntry, fake_api: FakeLyseAPI
) -> None:
    """A meter reading below the session start reports unknown, not a negative total."""
    session = {**CHARGING_ACTIVITY["activeChargeSession"], "currentMeterValueKwh": 0}
    fake_api.activities = [{**CHARGING_ACTIVITY, "activeChargeSession": session}]
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(_entity_id(hass, "sensor", "socket_1_session_energy")).state == "unknown"
    assert await hass.config_entries.async_unload(config_entry.entry_id)


async def test_each_socket_shows_its_own_activity(
    hass: HomeAssistant, config_entry: MockConfigEntry, fake_api: FakeLyseAPI
) -> None:
    """On a two-socket charger, one socket's events don't overwrite the other's sensors.

    Regression test: live activity was stored per charger, so whichever socket
    reported last showed up on both sockets' entities.
    """
    fake_api.get_chargers.return_value = [
        {**CHARGER, "sockets": [{"id": 1, "status": "Occupied"}, {"id": 2, "status": "Available"}]}
    ]
    idle = {**CHARGING_ACTIVITY, "socketId": 2, "socketStatus": "AVAILABLE", "activeChargeSession": None}
    # Socket 2 reports last, as it would if its stream ticked after socket 1's.
    fake_api.activities = [CHARGING_ACTIVITY, idle]
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    def state(domain: str, suffix: str) -> str:
        return hass.states.get(_entity_id(hass, domain, suffix)).state

    assert state("sensor", "socket_1_power") == "7.2"
    assert state("sensor", "socket_1_session_energy") == "6.75"
    assert state("sensor", "socket_1_status") == "Occupied"
    assert state("switch", "socket_1_charging") == "on"
    assert state("sensor", "socket_2_power") == "unknown"
    assert state("sensor", "socket_2_status") == "Available"
    assert state("switch", "socket_2_charging") == "off"
    assert await hass.config_entries.async_unload(config_entry.entry_id)
