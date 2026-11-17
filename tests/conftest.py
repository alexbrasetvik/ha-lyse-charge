"""Shared fixtures for Lyse Charge tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Generator
import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lyse_charge.api import LyseAPI
from custom_components.lyse_charge.const import AUTH_AUDIENCE, DOMAIN

CHARGER_ID = "SMARTLY-0001"
ALIAS = "Garage"

CHARGER = {
    "chargerId": CHARGER_ID,
    "chargerName": "Garage charger",
    "chargerStatus": "Available",
    "chargerModel": "Pro",
    "serialNumber": "SN123",
    "groupName": "Group",
    "publicGroupName": "Public group",
    "customProperties": {"alias": ALIAS, "public": False},
    "sockets": [{"id": 1, "status": "Available"}],
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load custom_components/lyse_charge in every test."""


def entry_data(**overrides: Any) -> dict[str, Any]:
    """Return config entry data as the current version of the integration writes it."""
    data: dict[str, Any] = {
        "email": "user@example.com",
        "password": "hunter2",
        "access_token": "access",
        "refresh_token": "refresh",
        "expires_at": time.time() + 3600,
        "audience": AUTH_AUDIENCE,
        "chargers": [{"charger_id": CHARGER_ID, "alias": ALIAS}],
    }
    data.update(overrides)
    return data


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Return a config entry with a complete, current set of data."""
    return MockConfigEntry(domain=DOMAIN, title="Test User", unique_id="user@example.com", data=entry_data())


class FakeLyseAPI:
    """Stubs for every LyseAPI network call, patched onto the real class.

    Patching the class (not a module attribute) means every LyseAPI instance,
    whether made in setup, the coordinator, or a config flow, uses these stubs,
    while the real constructor and token bookkeeping still run.
    """

    def __init__(self) -> None:
        """Create stubs returning a single idle charger."""
        self.ensure_token = AsyncMock(return_value=False)
        self.get_me = AsyncMock(return_value={"auth0Id": "auth0|1", "dataverse": {"fullName": "Test User"}})
        self.get_charger_ids_from_history = AsyncMock(return_value=[{"charger_id": CHARGER_ID, "alias": ALIAS}])
        self.get_chargers = AsyncMock(return_value=[CHARGER])
        self.get_active_sessions = AsyncMock(return_value=[])
        self.start_charging = AsyncMock(return_value="Accepted")
        self.stop_charging = AsyncMock(return_value="Accepted")
        # Websocket activities to deliver when a subscription starts.
        self.activities: list[dict] = []

    async def subscribe_activity(self, charger_id: str, socket_id: int, on_event: Callable[[dict], None]) -> None:
        """Deliver this socket's queued activities, then stay connected until cancelled, like a live socket."""
        for activity in self.activities:
            if (activity["chargerId"], activity["socketId"]) == (charger_id, socket_id):
                on_event(activity)
        await asyncio.Event().wait()


@pytest.fixture
def fake_api() -> Generator[FakeLyseAPI]:
    """Patch LyseAPI's network methods with FakeLyseAPI stubs."""
    fake = FakeLyseAPI()
    names = [
        "ensure_token",
        "get_me",
        "get_charger_ids_from_history",
        "get_chargers",
        "get_active_sessions",
        "start_charging",
        "stop_charging",
    ]
    patches = [patch.object(LyseAPI, name, getattr(fake, name)) for name in names]
    patches.append(
        patch.object(
            LyseAPI,
            "subscribe_activity",
            lambda _self, charger_id, socket_id, on_event: fake.subscribe_activity(charger_id, socket_id, on_event),
        )
    )
    for p in patches:
        p.start()
    yield fake
    for p in reversed(patches):
        p.stop()
