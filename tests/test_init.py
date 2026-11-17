"""Tests for setting up and unloading Lyse Charge config entries."""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lyse_charge.api import LyseAPIError, LyseAuthError
from custom_components.lyse_charge.const import DOMAIN

from .conftest import ALIAS, CHARGER_ID, FakeLyseAPI, entry_data


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _reauth_flows(hass: HomeAssistant, entry: MockConfigEntry) -> list[dict]:
    return [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"]["source"] == SOURCE_REAUTH and flow["context"]["entry_id"] == entry.entry_id
    ]


async def test_setup_and_unload(hass: HomeAssistant, config_entry: MockConfigEntry, fake_api: FakeLyseAPI) -> None:
    """A complete entry loads, creates its entities, and unloads cleanly."""
    await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    registry = er.async_get(hass)
    unique_ids = {e.unique_id for e in er.async_entries_for_config_entry(registry, config_entry.entry_id)}
    assert f"{CHARGER_ID}_socket_1_charging" in unique_ids
    assert f"{CHARGER_ID}_socket_1_session_energy" in unique_ids
    fake_api.get_chargers.assert_awaited_with([CHARGER_ID])

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED


async def test_legacy_entry_without_credentials_starts_reauth(hass: HomeAssistant, fake_api: FakeLyseAPI) -> None:
    """An entry from an early version, with no email/password, asks to re-authenticate.

    Regression test: this used to crash setup with KeyError: 'email'.
    """
    data = entry_data(audience="")
    del data["email"], data["password"]
    entry = MockConfigEntry(domain=DOMAIN, title="Legacy", data=data)

    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert len(_reauth_flows(hass, entry)) == 1
    fake_api.ensure_token.assert_not_awaited()


@pytest.mark.parametrize("chargers", [None, []], ids=["missing", "empty"])
async def test_entry_without_chargers_loads_and_raises_issue(
    hass: HomeAssistant, fake_api: FakeLyseAPI, chargers: list | None
) -> None:
    """An entry with no chargers loads empty and raises a repair issue, rather than guessing.

    Charging history includes public chargers, so setup doesn't pick chargers from it
    behind the user's back; the user selects them in the options flow instead.
    """
    data = entry_data()
    if chargers is None:
        del data["chargers"]
    else:
        data["chargers"] = chargers
    entry = MockConfigEntry(domain=DOMAIN, title="No chargers", data=data)

    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id) == []
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"no_chargers_{entry.entry_id}")
    assert entry.data.get("chargers") == chargers  # nothing guessed or persisted
    fake_api.get_charger_ids_from_history.assert_not_awaited()
    fake_api.get_chargers.assert_not_awaited()  # no ChargersByIds with an empty ID list
    fake_api.get_active_sessions.assert_not_awaited()  # nothing would use the result
    assert hass.data[DOMAIN][entry.entry_id].update_interval is None  # no polling

    await hass.config_entries.async_remove(entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"no_chargers_{entry.entry_id}") is None


async def test_no_chargers_issue_cleared_once_chargers_exist(hass: HomeAssistant, fake_api: FakeLyseAPI) -> None:
    """The repair issue goes away when the entry is reloaded with chargers selected."""
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(chargers=[]))
    await _setup(hass, entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"no_chargers_{entry.entry_id}")

    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "chargers": [{"charger_id": CHARGER_ID, "alias": ALIAS}]}
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert ir.async_get(hass).async_get_issue(DOMAIN, f"no_chargers_{entry.entry_id}") is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_login_failure_starts_reauth(
    hass: HomeAssistant, config_entry: MockConfigEntry, fake_api: FakeLyseAPI
) -> None:
    """Rejected credentials ask the user to re-authenticate."""
    fake_api.ensure_token.side_effect = LyseAuthError("wrong password")

    await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert len(_reauth_flows(hass, config_entry)) == 1


async def test_transient_login_failure_retries_setup(
    hass: HomeAssistant, config_entry: MockConfigEntry, fake_api: FakeLyseAPI
) -> None:
    """An Auth0 outage during login retries setup instead of demanding new credentials.

    Every entry does a fresh password login on its first start after the API move,
    so a blip at that moment must not push users into reauth.
    """
    fake_api.ensure_token.side_effect = LyseAPIError("auth server returned 503")

    await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert _reauth_flows(hass, config_entry) == []


async def test_api_down_retries_setup(
    hass: HomeAssistant, config_entry: MockConfigEntry, fake_api: FakeLyseAPI
) -> None:
    """The API being unreachable at startup, as when the old host died, retries setup."""
    fake_api.get_chargers.side_effect = LyseAPIError("HTTP 504")

    await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
