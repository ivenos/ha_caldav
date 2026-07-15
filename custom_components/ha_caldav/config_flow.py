"""Config and options flow for the CalDAV integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import caldav
from caldav.lib.error import AuthorizationError, DAVError
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import callback
import homeassistant.helpers.config_validation as cv
import requests
import voluptuous as vol

from .const import (
    CONF_CALENDARS,
    CONF_DAYS,
    CONF_INCLUDE_ALL_DAY,
    CONF_READ_ONLY,
    DEFAULT_DAYS,
    DEFAULT_INCLUDE_ALL_DAY,
    DEFAULT_READ_ONLY,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    REQUEST_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): str,
        vol.Required(CONF_USERNAME): cv.string,
        vol.Optional(CONF_PASSWORD, default=""): cv.string,
        vol.Optional(CONF_VERIFY_SSL, default=True): cv.boolean,
    }
)

REAUTH_SCHEMA = vol.Schema({vol.Optional(CONF_PASSWORD, default=""): cv.string})


class HaCaldavConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the account setup."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for connection details and validate them."""
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(
                f"{user_input[CONF_URL]}#{user_input[CONF_USERNAME]}"
            )
            self._abort_if_unique_id_configured()
            error = await self.hass.async_add_executor_job(_test_connection, user_input)
            if error is None:
                return self.async_create_entry(
                    title=user_input[CONF_USERNAME], data=user_input
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication after the credentials stopped working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new password and verify it."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            error = await self.hass.async_add_executor_job(
                _test_connection, {**entry.data, **user_input}
            )
            if error is None:
                return self.async_update_reload_and_abort(
                    entry, data_updates=user_input
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={CONF_USERNAME: entry.data[CONF_USERNAME]},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Any) -> HaCaldavOptionsFlow:
        """Return the options flow."""
        return HaCaldavOptionsFlow()


class HaCaldavOptionsFlow(OptionsFlow):
    """Handle the entity options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage which calendars are used and how the next event is picked."""
        if user_input is not None:
            # Merge, because the calendars field is left out of the form while
            # the server is unreachable; plain user_input would drop the
            # stored selection in that case.
            return self.async_create_entry(
                data={**self.config_entry.options, **user_input}
            )

        options = self.config_entry.options
        names = await self._async_calendar_names()

        fields: dict[Any, Any] = {}
        if names:
            fields[
                vol.Optional(CONF_CALENDARS, default=options.get(CONF_CALENDARS, names))
            ] = cv.multi_select(names)
        fields[
            vol.Optional(
                CONF_SCAN_INTERVAL,
                default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            )
        ] = vol.All(vol.Coerce(int), vol.Range(min=1, max=1440))
        fields[
            vol.Optional(CONF_DAYS, default=options.get(CONF_DAYS, DEFAULT_DAYS))
        ] = vol.All(vol.Coerce(int), vol.Range(min=1, max=365))
        fields[
            vol.Optional(
                CONF_INCLUDE_ALL_DAY,
                default=options.get(CONF_INCLUDE_ALL_DAY, DEFAULT_INCLUDE_ALL_DAY),
            )
        ] = cv.boolean
        fields[
            vol.Optional(
                CONF_READ_ONLY, default=options.get(CONF_READ_ONLY, DEFAULT_READ_ONLY)
            )
        ] = cv.boolean

        return self.async_show_form(step_id="init", data_schema=vol.Schema(fields))

    async def _async_calendar_names(self) -> list[str]:
        """Return the calendars on the account, or none if it is unreachable."""
        client = getattr(self.config_entry, "runtime_data", None)
        if client is None:
            return []
        try:
            return await self.hass.async_add_executor_job(_calendar_names, client)
        except (requests.ConnectionError, requests.Timeout, DAVError) as err:
            _LOGGER.debug("Could not list calendars: %s", err)
            return []


def _calendar_names(client: caldav.DAVClient) -> list[str]:
    return [
        calendar.name for calendar in client.principal().calendars() if calendar.name
    ]


def _test_connection(user_input: Mapping[str, Any]) -> str | None:
    """Return an error key or None if the connection succeeds."""
    client = caldav.DAVClient(
        user_input[CONF_URL],
        username=user_input[CONF_USERNAME],
        password=user_input[CONF_PASSWORD],
        ssl_verify_cert=user_input[CONF_VERIFY_SSL],
        timeout=REQUEST_TIMEOUT,
    )
    try:
        client.principal()
    except AuthorizationError as err:
        if err.reason == "Unauthorized":
            return "invalid_auth"
        _LOGGER.debug("CalDAV authorization error: %s", err)
        return "cannot_connect"
    except (requests.ConnectionError, requests.Timeout, DAVError) as err:
        _LOGGER.debug("CalDAV connection error: %s", err)
        return "cannot_connect"
    except Exception:
        _LOGGER.exception("Unexpected error connecting to the CalDAV server")
        return "unknown"
    finally:
        client.close()
    return None
