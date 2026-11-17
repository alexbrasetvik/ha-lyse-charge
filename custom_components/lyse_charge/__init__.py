"""The Lyse Charge integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import LyseAPI, LyseAPIError, LyseAuthError, has_credentials
from .const import CONF_CHARGERS, DOMAIN
from .coordinator import LyseCoordinator

_LOGGER = logging.getLogger(__name__)
_PLATFORMS = ["sensor", "switch"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Lyse Charge from a config entry."""
    _LOGGER.debug("Setting up entry %s", entry.entry_id)
    hass.data.setdefault(DOMAIN, {})

    # Entries created by early versions of this integration stored no email/password.
    # LyseAPI discards tokens for the old API host, so these entries need a password login.
    # Ask the user for credentials via reauth (which merges them into the entry).
    if not has_credentials(entry.data):
        raise ConfigEntryAuthFailed("Stored credentials are missing; please re-enter email and password")

    api = LyseAPI.from_entry_data(async_get_clientsession(hass), entry.data)

    try:
        if await api.ensure_token():
            hass.config_entries.async_update_entry(entry, data={**entry.data, **api.token_data})
    except LyseAuthError as err:
        raise ConfigEntryAuthFailed(f"Login failed: {err}") from err
    except LyseAPIError as err:
        raise ConfigEntryNotReady(f"Login failed: {err}") from err

    # Early versions may not have stored a charger list, and an account with no
    # charging history is set up with an empty one. Don't guess (the history also
    # holds public chargers); load without chargers and ask the user to pick some.
    chargers = entry.data.get(CONF_CHARGERS) or []
    _update_no_chargers_issue(hass, entry, has_chargers=bool(chargers))

    coordinator = LyseCoordinator(hass, api, entry, chargers)
    await coordinator.async_config_entry_first_refresh()
    await coordinator.start_websockets()

    hass.data[DOMAIN][entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    _LOGGER.debug("Entry %s set up with %d charger(s)", entry.entry_id, len(chargers))
    return True


def _no_chargers_issue_id(entry: ConfigEntry) -> str:
    """Return the ID of the repair issue raised while an entry has no chargers selected."""
    return f"no_chargers_{entry.entry_id}"


def _update_no_chargers_issue(hass: HomeAssistant, entry: ConfigEntry, *, has_chargers: bool) -> None:
    """Raise a repair issue while the entry has no chargers selected, and clear it once it has."""
    issue_id = _no_chargers_issue_id(entry)
    if has_chargers:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="no_chargers",
        translation_placeholders={"title": entry.title},
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading entry %s", entry.entry_id)
    coordinator: LyseCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
    await coordinator.stop_websockets()
    return await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up the entry's repair issue when it is deleted."""
    ir.async_delete_issue(hass, DOMAIN, _no_chargers_issue_id(entry))
