"""Config flow for the Lyse Charge integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .api import LyseAPI, LyseAPIError, LyseAuthError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ALIAS,
    CONF_CHARGER_ID,
    CONF_CHARGERS,
    CONF_EMAIL,
    CONF_EXPIRES_AT,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

_CREDENTIALS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL)),
        vol.Required(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
    }
)


class LyseConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Lyse Charge."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._token_data: dict = {}
        self._me: dict = {}
        self._discovered_chargers: list[dict] = []

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Handle the initial step: collect email and password."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            password = user_input[CONF_PASSWORD]
            api = LyseAPI(async_get_clientsession(self.hass), email, password)
            try:
                await api.ensure_token()
                me = await api.get_me()
            except LyseAuthError as err:
                _LOGGER.debug("Auth error: %s", err)
                errors["base"] = "invalid_auth"
            except LyseAPIError as err:
                _LOGGER.debug("Auth error: %s", err)
                errors["base"] = "cannot_connect"
            else:
                _LOGGER.debug("Config flow: authentication succeeded")
                await self.async_set_unique_id(email.lower())
                self._abort_if_unique_id_configured()

                self._token_data = {
                    CONF_EMAIL: email,
                    CONF_PASSWORD: password,
                    **api.token_data,
                }
                self._me = me
                self._api = api

                try:
                    self._discovered_chargers = await api.get_charger_ids_from_history()
                except LyseAPIError as err:
                    _LOGGER.debug("Charger history lookup failed: %s", err)
                    self._discovered_chargers = []

                return await self.async_step_chargers()

        return self.async_show_form(
            step_id="user",
            data_schema=_CREDENTIALS_SCHEMA,
            errors=errors,
        )

    async def async_step_chargers(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Let the user pick which discovered chargers to add."""
        errors: dict[str, str] = {}

        if not self._discovered_chargers:
            # No history yet: skip selection and store an empty list; the user can add chargers via options
            return self._create_entry([])

        if user_input is not None:
            selected_ids = user_input.get("charger_ids", [])
            selected = [c for c in self._discovered_chargers if c[CONF_CHARGER_ID] in selected_ids]
            if not selected:
                errors["base"] = "no_chargers_selected"
            else:
                return self._create_entry(selected)

        options = [
            SelectOptionDict(value=c[CONF_CHARGER_ID], label=f"{c[CONF_ALIAS]} ({c[CONF_CHARGER_ID]})")
            for c in self._discovered_chargers
        ]
        default = [c[CONF_CHARGER_ID] for c in self._discovered_chargers]

        return self.async_show_form(
            step_id="chargers",
            data_schema=vol.Schema(
                {
                    vol.Required("charger_ids", default=default): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    def _create_entry(self, chargers: list[dict]) -> ConfigFlowResult:
        """Create the config entry from the collected credentials and chargers."""
        dv = self._me.get("dataverse") or {}
        title = dv.get("fullName") or dv.get("email") or "Lyse Charge"
        _LOGGER.debug("Creating config entry with %d charger(s)", len(chargers))
        return self.async_create_entry(
            title=title,
            data={**self._token_data, CONF_CHARGERS: chargers},
        )

    async def async_step_reauth(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Handle reauthorization triggered by an auth failure."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Confirm reauthorization with a fresh email and password."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            password = user_input[CONF_PASSWORD]
            api = LyseAPI(async_get_clientsession(self.hass), email, password)
            try:
                await api.ensure_token()
                await api.get_me()
            except LyseAuthError as err:
                _LOGGER.debug("Reauth failed: %s", err)
                errors["base"] = "invalid_auth"
            except LyseAPIError as err:
                _LOGGER.debug("Reauth failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                _LOGGER.debug("Reauth succeeded for entry %s", reauth_entry.entry_id)
                self.hass.config_entries.async_update_entry(
                    reauth_entry,
                    data={
                        **reauth_entry.data,
                        CONF_EMAIL: email,
                        CONF_PASSWORD: password,
                        **api.token_data,
                    },
                )
                await self.hass.config_entries.async_reload(reauth_entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL, default=reauth_entry.data.get(CONF_EMAIL, "")): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.EMAIL)
                    ),
                    vol.Required(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> LyseOptionsFlow:
        """Create the options flow for an existing config entry."""
        return LyseOptionsFlow(config_entry)


class LyseOptionsFlow(OptionsFlow):
    """Handle an options flow for Lyse Charge, to edit selected chargers."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize the options flow."""
        self._config_entry = config_entry
        self._discovered_chargers: list[dict] = []

    async def async_step_init(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Manage the chargers selected for this config entry."""
        entry = self._config_entry
        if not entry.data.get(CONF_EMAIL) or not entry.data.get(CONF_PASSWORD):
            # A legacy entry without credentials can't log in; setup has started reauth.
            return self.async_abort(reason="reauth_required")
        api = LyseAPI(
            async_get_clientsession(self.hass),
            entry.data[CONF_EMAIL],
            entry.data[CONF_PASSWORD],
            access_token=entry.data.get(CONF_ACCESS_TOKEN, ""),
            refresh_token=entry.data.get(CONF_REFRESH_TOKEN, ""),
            expires_at=entry.data.get(CONF_EXPIRES_AT, 0),
        )
        try:
            if await api.ensure_token():
                self.hass.config_entries.async_update_entry(entry, data={**entry.data, **api.token_data})
            self._discovered_chargers = await api.get_charger_ids_from_history()
        except LyseAPIError as err:
            _LOGGER.debug("Charger history lookup failed: %s", err)
            self._discovered_chargers = list(entry.data.get(CONF_CHARGERS, []))

        if user_input is not None:
            selected_ids = user_input.get("charger_ids", [])
            selected = [c for c in self._discovered_chargers if c[CONF_CHARGER_ID] in selected_ids]
            _LOGGER.debug("Updated selected chargers for entry %s: %d charger(s)", entry.entry_id, len(selected))
            self.hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_CHARGERS: selected})
            return self.async_create_entry(title="", data={})

        current_ids = [c[CONF_CHARGER_ID] for c in entry.data.get(CONF_CHARGERS, [])]
        all_ids = {c[CONF_CHARGER_ID] for c in self._discovered_chargers}
        # Include currently-configured chargers even if not in history
        for c in entry.data.get(CONF_CHARGERS, []):
            if c[CONF_CHARGER_ID] not in all_ids:
                self._discovered_chargers.append(c)

        options = [
            SelectOptionDict(value=c[CONF_CHARGER_ID], label=f"{c[CONF_ALIAS]} ({c[CONF_CHARGER_ID]})")
            for c in self._discovered_chargers
        ]

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("charger_ids", default=current_ids): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
        )
