"""Config and options flow for the CalDAV integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any
from urllib.parse import urlparse

import caldav
from caldav.lib.error import AuthorizationError
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_TIMEOUT,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)
import voluptuous as vol

from .connection import (
    account_key,
    build_client,
    calendar_key,
    connection_kwargs,
    display_name,
    url_candidates,
    without_userinfo,
)
from .const import (
    CONF_CA_BUNDLE,
    CONF_CALENDAR_OPTIONS,
    CONF_CALENDARS,
    CONF_CLIENT_CERT,
    CONF_CLIENT_KEY,
    CONF_DAYS,
    CONF_INCLUDE_ALL_DAY,
    CONF_READ_ONLY,
    DEFAULT_DAYS,
    DEFAULT_INCLUDE_ALL_DAY,
    DEFAULT_READ_ONLY,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEOUT,
    DOMAIN,
)
from .errors import CONNECTION_ERRORS
from .options import account_settings, request_timeout

_LOGGER = logging.getLogger(__name__)

CONF_RESET = "reset"
CONF_CALENDAR = "calendar"
CONF_CERTIFICATES = "certificates"

CERTIFICATE_PATHS = (CONF_CA_BUNDLE, CONF_CLIENT_CERT, CONF_CLIENT_KEY)

CERTIFICATES_SECTION = section(
    vol.Schema(
        {
            vol.Optional(CONF_CA_BUNDLE, default=""): cv.string,
            vol.Optional(CONF_CLIENT_CERT, default=""): cv.string,
            vol.Optional(CONF_CLIENT_KEY, default=""): cv.string,
        }
    ),
    {"collapsed": True},
)

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): cv.string,
        vol.Required(CONF_USERNAME): cv.string,
        vol.Optional(CONF_PASSWORD, default=""): cv.string,
        vol.Optional(CONF_VERIFY_SSL, default=True): cv.boolean,
        vol.Optional(CONF_CERTIFICATES): CERTIFICATES_SECTION,
    }
)

# The account stays put on reconfigure and the password belongs to reauth.
RECONFIGURE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): cv.string,
        vol.Optional(CONF_VERIFY_SSL, default=True): cv.boolean,
        vol.Optional(CONF_CERTIFICATES): CERTIFICATES_SECTION,
    }
)

REAUTH_SCHEMA = vol.Schema({vol.Optional(CONF_PASSWORD, default=""): cv.string})


class HaCaldavConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the account setup."""

    VERSION = 1
    # Moves with every change of key shape; see async_migrate_entry.
    MINOR_VERSION = 2

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for connection details and validate them."""
        errors: dict[str, str] = {}
        if user_input is not None:
            cleaned = _cleaned(user_input)
            error, url = await self.hass.async_add_executor_job(
                _test_connection, cleaned
            )
            if error is None:
                # The url that answered, so a bootstrapped account matches itself.
                await self.async_set_unique_id(account_key(url, cleaned[CONF_USERNAME]))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=_title(url, cleaned[CONF_USERNAME]),
                    data={**cleaned, CONF_URL: url},
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the server url or the TLS settings of an account.

        The account itself is fixed: the entry is keyed on it.
        """
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            cleaned = {
                **_cleaned(user_input),
                CONF_USERNAME: entry.data[CONF_USERNAME],
                CONF_PASSWORD: entry.data[CONF_PASSWORD],
            }
            error, url = await self.hass.async_add_executor_job(
                _test_connection, cleaned, request_timeout(entry)
            )
            if error is None:
                unique_id = account_key(url, cleaned[CONF_USERNAME])
                await self.async_set_unique_id(unique_id)
                # Only landing on a url another entry holds is a conflict.
                if any(
                    other.unique_id == unique_id and other.entry_id != entry.entry_id
                    for other in self._async_current_entries()
                ):
                    return self.async_abort(reason="already_configured")
                title = entry.title
                if title == _title(entry.data[CONF_URL], cleaned[CONF_USERNAME]):
                    title = _title(url, cleaned[CONF_USERNAME])
                # A full replacement, so a cleared certificate path is gone.
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=unique_id,
                    title=title,
                    data={**cleaned, CONF_URL: url},
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                RECONFIGURE_SCHEMA, _suggested(entry.data)
            ),
            errors=errors,
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
            error, _ = await self.hass.async_add_executor_job(
                _test_connection, {**entry.data, **user_input}, request_timeout(entry)
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
    def async_get_options_flow(config_entry: ConfigEntry) -> HaCaldavOptionsFlow:
        """Return the options flow."""
        return HaCaldavOptionsFlow()


class HaCaldavOptionsFlow(OptionsFlowWithReload):
    """Handle the entity options."""

    def __init__(self) -> None:
        """Initialize the options flow."""
        self._calendar: str | None = None
        self._choices: dict[str, str] | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the account-wide settings and the per-calendar overrides."""
        # The per-calendar step can only offer what this entry loaded.
        if not await self._async_calendar_choices() or not self._managed():
            return await self.async_step_account()
        return self.async_show_menu(step_id="init", menu_options=["account", "pick"])

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage which calendars are used and how the next event is picked."""
        errors: dict[str, str] = {}
        options = self.config_entry.options
        choices = await self._async_calendar_choices()
        if user_input is not None:
            # Here rather than in the schema: a voluptuous message reaches the
            # form untranslated.
            if CONF_CALENDARS in user_input and not user_input[CONF_CALENDARS]:
                errors[CONF_CALENDARS] = "no_calendars"
            else:
                # Merged: the calendars field is absent while the server is unreachable.
                return self.async_create_entry(
                    data={**options, **_selection(options, choices, user_input)}
                )

        fields: dict[Any, Any] = {}
        if choices:
            fields[
                vol.Optional(CONF_CALENDARS, default=_selected_keys(options, choices))
            ] = cv.multi_select(choices)
        fields[
            vol.Optional(
                CONF_SCAN_INTERVAL,
                default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            )
        ] = _minutes()
        fields[
            vol.Optional(CONF_TIMEOUT, default=request_timeout(self.config_entry))
        ] = _seconds()
        fields[
            vol.Optional(CONF_DAYS, default=options.get(CONF_DAYS, DEFAULT_DAYS))
        ] = _days()
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

        return self.async_show_form(
            step_id="account", data_schema=vol.Schema(fields), errors=errors
        )

    async def async_step_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the calendar whose settings should differ from the account's."""
        if user_input is not None:
            self._calendar = user_input[CONF_CALENDAR]
            return await self.async_step_calendar()
        return self.async_show_form(
            step_id="pick",
            data_schema=vol.Schema(
                {vol.Required(CONF_CALENDAR): vol.In(_labelled(self._managed()))}
            ),
        )

    async def async_step_calendar(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Override the account settings for one calendar."""
        # Keyed on the url, so a rename on the server keeps the settings.
        key = self._calendar or ""
        managed = next(
            (
                item
                for item in self._managed()
                if calendar_key(item.calendar.url) == key
            ),
            None,
        )
        name = managed.name if managed is not None else key
        overrides = dict(self.config_entry.options.get(CONF_CALENDAR_OPTIONS, {}))
        account = account_settings(self.config_entry)
        if user_input is not None:
            reset = user_input.pop(CONF_RESET, False)
            overrides.pop(name, None)
            overrides.pop(key, None)
            if not reset:
                # Only what differs, so an account-wide change still reaches
                # the rest.
                changed = {
                    field: value
                    for field, value in user_input.items()
                    if account.get(field) != value
                }
                if changed:
                    overrides[key] = changed
            return self.async_create_entry(
                data={
                    **self.config_entry.options,
                    CONF_CALENDAR_OPTIONS: overrides,
                }
            )

        options = self.config_entry.options
        current = overrides.get(key, overrides.get(name, {}))
        return self.async_show_form(
            step_id="calendar",
            description_placeholders={"calendar": name},
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_DAYS,
                        default=current.get(
                            CONF_DAYS, options.get(CONF_DAYS, DEFAULT_DAYS)
                        ),
                    ): _days(),
                    vol.Optional(
                        CONF_INCLUDE_ALL_DAY,
                        default=current.get(
                            CONF_INCLUDE_ALL_DAY,
                            options.get(CONF_INCLUDE_ALL_DAY, DEFAULT_INCLUDE_ALL_DAY),
                        ),
                    ): cv.boolean,
                    vol.Optional(
                        CONF_READ_ONLY,
                        default=current.get(
                            CONF_READ_ONLY,
                            options.get(CONF_READ_ONLY, DEFAULT_READ_ONLY),
                        ),
                    ): cv.boolean,
                    vol.Optional(CONF_RESET, default=False): cv.boolean,
                }
            ),
        )

    def _managed(self) -> list[Any]:
        data = getattr(self.config_entry, "runtime_data", None)
        return list(data.calendars) if data is not None else []

    async def _async_calendar_choices(self) -> dict[str, str]:
        """Return key -> name for every calendar, empty if unreachable.

        The full list, so a deselected calendar can be selected again.
        """
        if self._choices is not None:
            return self._choices
        data = getattr(self.config_entry, "runtime_data", None)
        if data is None:
            return {}
        try:
            self._choices = await self.hass.async_add_executor_job(
                _calendar_choices, data.client
            )
        except Exception as err:  # noqa: BLE001
            # As broad as the setup path: the options have to stay reachable
            # while the server misbehaves.
            _LOGGER.debug("Could not list calendars: %s", err)
            return {}
        return self._choices


def _minutes() -> Any:
    """Return the poll-interval field, bounded by the frontend itself.

    A voluptuous range answers with its own English message.
    """
    return NumberSelector(
        NumberSelectorConfig(
            min=1,
            max=1440,
            step=1,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement="min",
        )
    )


def _seconds() -> Any:
    """Return the request-timeout field, bounded by the frontend itself.

    caldav retries a failed request unauthenticated until the account has
    authenticated once, so a connection attempt can cost three times this.
    """
    return NumberSelector(
        NumberSelectorConfig(
            min=5, max=120, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="s"
        )
    )


def _days() -> Any:
    """Return the look-ahead field, bounded by the frontend itself."""
    return NumberSelector(
        NumberSelectorConfig(
            min=1, max=365, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="d"
        )
    )


def _selected_keys(options: Mapping[str, Any], choices: dict[str, str]) -> list[str]:
    """Return the stored selection as keys, defaulting to every calendar.

    Entries written before v1.2.0 selected by display name.
    """
    stored = options.get(CONF_CALENDARS)
    if stored is None:
        return list(choices)
    return [key for key, name in choices.items() if key in stored or name in stored]


def _selection(
    options: Mapping[str, Any], choices: dict[str, str], user_input: Mapping[str, Any]
) -> dict[str, Any]:
    """Return the submitted settings with the calendar list made safe to store.

    A calendar the server left out of this one listing must not read as
    deselected, and an entry without a selection keeps following the server.
    """
    submitted = user_input.get(CONF_CALENDARS)
    if submitted is None:
        return dict(user_input)
    stored = options.get(CONF_CALENDARS)
    if stored is None:
        if set(submitted) == set(choices):
            return {
                key: value for key, value in user_input.items() if key != CONF_CALENDARS
            }
        return dict(user_input)
    names = set(choices.values())
    unlisted = [item for item in stored if item not in choices and item not in names]
    return {**user_input, CONF_CALENDARS: [*submitted, *unlisted]}


def _title(url: str, username: str) -> str:
    """Return the entry title, telling one username on two servers apart."""
    host = urlparse(url).hostname or url
    return f"{username}@{host}"


def _suggested(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return stored data in the shape of the form, paths under their section."""
    suggested = {
        key: value for key, value in data.items() if key not in CERTIFICATE_PATHS
    }
    suggested[CONF_CERTIFICATES] = {
        key: data[key] for key in CERTIFICATE_PATHS if key in data
    }
    return suggested


def _cleaned(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """Return the input as it should be stored.

    The section is flattened, text is trimmed and blank paths go; an empty
    password is a legitimate value.
    """
    flat = {key: value for key, value in user_input.items() if key != CONF_CERTIFICATES}
    flat.update(user_input.get(CONF_CERTIFICATES) or {})
    cleaned: dict[str, Any] = {}
    for key, value in flat.items():
        if isinstance(value, str) and key != CONF_PASSWORD:
            value = value.strip()
        if value == "" and key in CERTIFICATE_PATHS:
            continue
        cleaned[key] = value
    if CONF_URL in cleaned:
        cleaned[CONF_URL] = without_userinfo(cleaned[CONF_URL])
    return cleaned


def _calendar_choices(client: caldav.DAVClient) -> dict[str, str]:
    """Return key -> display name for every calendar on the account."""
    return {
        calendar_key(calendar.url): display_name(calendar)
        for calendar in client.principal().calendars()
    }


def _labelled(managed: list[Any]) -> dict[str, str]:
    """Return url key -> menu label for the loaded calendars.

    A shared calendar keeps its owner's name, so two can share one.
    """
    names = [item.name for item in managed]
    labels = {}
    for item in managed:
        key = calendar_key(item.calendar.url)
        if names.count(item.name) > 1:
            labels[key] = f"{item.name} ({_distinctive(key, managed)})"
        else:
            labels[key] = item.name
    return dict(sorted(labels.items(), key=lambda pair: pair[1]))


def _distinctive(key: str, managed: list[Any]) -> str:
    """Return the shortest tail of a calendar path that no other one shares."""
    others = [
        calendar_key(item.calendar.url)
        for item in managed
        if calendar_key(item.calendar.url) != key
    ]
    parts = key.split("/")
    for depth in range(1, len(parts) + 1):
        tail = "/".join(parts[-depth:])
        if not any(other.endswith(tail) for other in others):
            return tail
    return key


def _test_connection(
    user_input: Mapping[str, Any], timeout: float = DEFAULT_TIMEOUT
) -> tuple[str | None, str]:
    """Return (error key or None, the url that worked)."""
    kwargs = connection_kwargs(user_input, timeout)
    entered = user_input[CONF_URL]
    error = "cannot_connect"
    for url in url_candidates(entered, kwargs):
        candidate_error = "cannot_connect"
        client = build_client(
            url, user_input[CONF_USERNAME], user_input[CONF_PASSWORD], kwargs
        )
        try:
            client.principal()
        except AuthorizationError as err:
            if err.reason == "Unauthorized":
                # Remembered, not returned: a bare host may sit behind another
                # auth realm while the bootstrap candidate works.
                candidate_error = "invalid_auth"
            else:
                _LOGGER.debug("CalDAV authorization error: %s", err)
        except CONNECTION_ERRORS as err:
            _LOGGER.debug("CalDAV connection error: %s", err)
        except Exception:
            _LOGGER.exception("Unexpected error connecting to the CalDAV server")
            candidate_error = "unknown"
        else:
            return None, url
        finally:
            client.close()
        # Rejected credentials outrank a candidate that would not answer.
        if error != "invalid_auth":
            error = candidate_error
    return error, entered
