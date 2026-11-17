"""Tests for the Lyse Charge config, reauth, and options flows."""

from __future__ import annotations

import json
from pathlib import Path
import re

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lyse_charge.api import LyseAPIError, LyseAuthError
from custom_components.lyse_charge.const import AUTH_AUDIENCE, DOMAIN

from .conftest import ALIAS, CHARGER_ID, FakeLyseAPI, entry_data

COMPONENT = Path(__file__).parent.parent / "custom_components" / "lyse_charge"
CREDENTIALS = {"email": " user@example.com ", "password": "hunter2"}


async def test_user_flow_creates_entry(hass: HomeAssistant, fake_api: FakeLyseAPI) -> None:
    """Signing in and picking chargers creates an entry with credentials, token audience and chargers."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "chargers"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"charger_ids": [CHARGER_ID]})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Test User"
    assert result["result"].unique_id == "user@example.com"
    data = result["data"]
    assert data["email"] == "user@example.com"
    assert data["password"] == "hunter2"
    assert data["audience"] == AUTH_AUDIENCE
    assert data["chargers"] == [{"charger_id": CHARGER_ID, "alias": ALIAS}]


async def test_user_flow_invalid_auth(hass: HomeAssistant, fake_api: FakeLyseAPI) -> None:
    """Rejected credentials show an error on the sign-in form."""
    fake_api.ensure_token.side_effect = LyseAuthError("nope")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})

    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_cannot_connect(hass: HomeAssistant, fake_api: FakeLyseAPI) -> None:
    """An unreachable API shows a connection error on the sign-in form."""
    fake_api.get_me.side_effect = LyseAPIError("HTTP 504")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})

    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reauth_backfills_legacy_entry(hass: HomeAssistant, fake_api: FakeLyseAPI) -> None:
    """Re-authenticating a legacy entry adds credentials, keeps its other data, and loads it."""
    data = entry_data(audience="")
    del data["email"], data["password"]
    entry = MockConfigEntry(domain=DOMAIN, title="Legacy", data=data)
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["email"] == "user@example.com"
    assert entry.data["password"] == "hunter2"
    assert entry.data["audience"] == AUTH_AUDIENCE
    assert entry.data["chargers"] == data["chargers"]
    assert entry.unique_id == "user@example.com"  # legacy entries had none, so duplicates went undetected
    assert entry.state is ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_reauth_invalid_auth(hass: HomeAssistant, fake_api: FakeLyseAPI) -> None:
    """Wrong credentials during reauth keep the form open with an error."""
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data())
    entry.add_to_hass(hass)
    fake_api.ensure_token.side_effect = LyseAuthError("nope")

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_reauth_rejects_other_account(hass: HomeAssistant, fake_api: FakeLyseAPI) -> None:
    """Reauth cannot switch an entry to a different Lyse account."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="user@example.com", data=entry_data())
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "Other@Example.com", "password": "pw"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data["email"] == "user@example.com"


async def test_reauth_accepts_same_account_in_other_case(hass: HomeAssistant, fake_api: FakeLyseAPI) -> None:
    """The account match ignores the case of the email address."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="user@example.com", data=entry_data())
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "User@Example.com", "password": "new-pw"}
    )
    await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "new-pw"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_legacy_reauth_aborts_when_account_already_configured(hass: HomeAssistant, fake_api: FakeLyseAPI) -> None:
    """A legacy entry can't adopt an account that another entry already holds."""
    MockConfigEntry(domain=DOMAIN, unique_id="user@example.com", data=entry_data()).add_to_hass(hass)
    data = entry_data()
    del data["email"], data["password"]
    legacy = MockConfigEntry(domain=DOMAIN, data=data)
    legacy.add_to_hass(hass)

    result = await legacy.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert legacy.unique_id is None
    assert "email" not in legacy.data


async def test_options_flow_on_legacy_entry_requires_reauth(hass: HomeAssistant, fake_api: FakeLyseAPI) -> None:
    """The options flow on an entry without credentials aborts without attempting a login."""
    data = entry_data()
    del data["email"], data["password"]
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_required"
    fake_api.ensure_token.assert_not_awaited()


async def test_options_flow_selects_chargers_and_reloads(hass: HomeAssistant, fake_api: FakeLyseAPI) -> None:
    """Picking chargers in options creates their entities right away and clears the repair issue."""
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(chargers=[]))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"no_chargers_{entry.entry_id}")

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"charger_ids": [CHARGER_ID]})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data["chargers"] == [{"charger_id": CHARGER_ID, "alias": ALIAS}]
    assert er.async_get(hass).async_get_entity_id("switch", DOMAIN, f"{CHARGER_ID}_socket_1_charging")
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"no_chargers_{entry.entry_id}") is None
    assert await hass.config_entries.async_unload(entry.entry_id)


def test_every_flow_step_has_strings() -> None:
    """Each flow step has a title in strings.json, and en.json mirrors it.

    The reauth step's strings used to sit outside "config", so its form had no labels.
    """
    strings = json.loads((COMPONENT / "strings.json").read_text())
    assert set(strings["config"]["step"]) == {"user", "chargers", "reauth_confirm"}
    assert set(strings["issues"]) == {"no_chargers"}
    assert set(strings["options"]["step"]) == {"init"}
    for step in [*strings["config"]["step"].values(), *strings["options"]["step"].values()]:
        assert step["title"]
    assert json.loads((COMPONENT / "translations" / "en.json").read_text()) == strings


def test_every_reason_in_code_has_a_string() -> None:
    """Each abort reason and form error the flows can return has user-facing text."""
    source = (COMPONENT / "config_flow.py").read_text()
    strings = json.loads((COMPONENT / "strings.json").read_text())
    known = set(strings["config"]["abort"]) | set(strings["config"]["error"]) | set(strings["options"].get("abort", {}))
    used = set(re.findall(r'reason="(\w+)"', source)) | set(re.findall(r'errors\["base"\] = "(\w+)"', source))
    # Abort reasons that Home Assistant helpers the flows call produce; config_flow.py doesn't name them.
    used |= {"already_configured", "reauth_successful"}
    assert used <= known, f"Missing strings for {sorted(used - known)}"


async def test_options_flow_keeps_configured_charger_missing_from_history(
    hass: HomeAssistant, fake_api: FakeLyseAPI
) -> None:
    """A configured charger that isn't in charging history survives saving the form.

    Regression test: the submit matched choices against a fresh history lookup, so a
    ticked charger missing from history was dropped.
    """
    old = {"charger_id": "OLD-1", "alias": "Cabin"}
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(chargers=[old]))
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"charger_ids": ["OLD-1", CHARGER_ID]}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data["chargers"] == [{"charger_id": CHARGER_ID, "alias": ALIAS}, old]
    fake_api.get_charger_ids_from_history.assert_awaited_once()  # not again on submit
    await hass.config_entries.async_unload(entry.entry_id)


async def test_options_flow_offers_configured_chargers_when_history_fails(
    hass: HomeAssistant, fake_api: FakeLyseAPI
) -> None:
    """If the history lookup fails, the form still offers, and keeps, the configured chargers."""
    fake_api.get_charger_ids_from_history.side_effect = LyseAPIError("HTTP 504")
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data())
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"charger_ids": [CHARGER_ID]})
    await hass.async_block_till_done()

    assert entry.data["chargers"] == [{"charger_id": CHARGER_ID, "alias": ALIAS}]
    await hass.config_entries.async_unload(entry.entry_id)


async def test_legacy_reauth_with_user_flow_in_progress(hass: HomeAssistant, fake_api: FakeLyseAPI) -> None:
    """A legacy entry's reauth succeeds even while a user flow for the same account is open.

    Regression test: setting the unique ID raised already_in_progress.
    """
    data = entry_data()
    del data["email"], data["password"]
    legacy = MockConfigEntry(domain=DOMAIN, data=data)
    legacy.add_to_hass(hass)

    user = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    user = await hass.config_entries.flow.async_configure(user["flow_id"], CREDENTIALS)
    assert user["step_id"] == "chargers"  # the user flow now holds unique ID user@example.com

    result = await legacy.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert legacy.unique_id == "user@example.com"
    await hass.config_entries.async_unload(legacy.entry_id)
