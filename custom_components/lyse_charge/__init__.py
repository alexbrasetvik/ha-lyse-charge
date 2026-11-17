"""The Lyse Charge integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import LyseAPI, LyseAPIError, LyseAuthError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_AUDIENCE,
    CONF_CHARGERS,
    CONF_EMAIL,
    CONF_EXPIRES_AT,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)
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
    if not entry.data.get(CONF_EMAIL) or not entry.data.get(CONF_PASSWORD):
        raise ConfigEntryAuthFailed("Stored credentials are missing; please re-enter email and password")

    session = async_get_clientsession(hass)
    api = LyseAPI(
        session,
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
        access_token=entry.data.get(CONF_ACCESS_TOKEN, ""),
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN, ""),
        expires_at=entry.data.get(CONF_EXPIRES_AT, 0),
        audience=entry.data.get(CONF_AUDIENCE, ""),
    )

    try:
        if await api.ensure_token():
            hass.config_entries.async_update_entry(entry, data={**entry.data, **api.token_data})
    except LyseAuthError as err:
        raise ConfigEntryAuthFailed(f"Login failed: {err}") from err
    except LyseAPIError as err:
        raise ConfigEntryNotReady(f"Login failed: {err}") from err

    coordinator = LyseCoordinator(hass, api, entry, entry.data[CONF_CHARGERS])
    await coordinator.async_config_entry_first_refresh()
    await coordinator.start_websockets()

    hass.data[DOMAIN][entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    _LOGGER.debug("Entry %s set up with %d charger(s)", entry.entry_id, len(entry.data[CONF_CHARGERS]))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading entry %s", entry.entry_id)
    coordinator: LyseCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
    await coordinator.stop_websockets()
    return await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
