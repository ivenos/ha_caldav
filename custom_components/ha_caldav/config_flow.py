"""Config and options flow for the CalDAV integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

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
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import callback
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
    DOMAIN,
)
from .errors import CONNECTION_ERRORS
from .options import account_settings

_LOGGER = logging.getLogger(__name__)

CONF_RESET = "reset"
CONF_CALENDAR = "calendar"

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): str,
        vol.Required(CONF_USERNAME): cv.string,
        vol.Optional(CONF_PASSWORD, default=""): cv.string,
        vol.Optional(CONF_VERIFY_SSL, default=True): cv.boolean,
        vol.Optional(CONF_CA_BUNDLE, default=""): cv.string,
        vol.Optional(CONF_CLIENT_CERT, default=""): cv.string,
        vol.Optional(CONF_CLIENT_KEY, default=""): cv.string,
    }
)

# The account stays put on reconfigure and the password belongs to reauth, so
# neither is offered here, and the stored password never reaches the browser.
RECONFIGURE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): str,
        vol.Optional(CONF_VERIFY_SSL, default=True): cv.boolean,
        vol.Optional(CONF_CA_BUNDLE, default=""): cv.string,
        vol.Optional(CONF_CLIENT_CERT, default=""): cv.string,
        vol.Optional(CONF_CLIENT_KEY, default=""): cv.string,
    }
)

REAUTH_SCHEMA = vol.Schema({vol.Optional(CONF_PASSWORD, default=""): cv.string})


class HaCaldavConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the account setup."""

    VERSION = 1
    # Moved with the key normalization in async_migrate_entry. Home Assistant
    # runs no migration while the stored numbers match the handler's, so an
    # entry written by an older release would keep its old account key and be
    # set up a second time under the new one.
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
                # The url that answered, not the one typed: an account reached
                # through the bootstrap would otherwise never match itself.
                await self.async_set_unique_id(account_key(url, cleaned[CONF_USERNAME]))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=cleaned[CONF_USERNAME], data={**cleaned, CONF_URL: url}
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the server url or the TLS settings of an account.

        The account itself is fixed: the entry is keyed on it, and pointing one
        at a different account would silently adopt its calendars.
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
                _test_connection, cleaned
            )
            if error is None:
                unique_id = account_key(url, cleaned[CONF_USERNAME])
                await self.async_set_unique_id(unique_id)
                # The url is the one thing this step exists to change, and it
                # is what the entry is keyed on, so refusing a moved one would
                # refuse every reconfigure that does anything. The account is
                # pinned above; only landing on one another entry already holds
                # is a conflict.
                if any(
                    other.unique_id == unique_id and other.entry_id != entry.entry_id
                    for other in self._async_current_entries()
                ):
                    return self.async_abort(reason="already_configured")
                # A full replacement rather than a merge, so a TLS path the
                # user cleared is really gone instead of surviving underneath.
                return self.async_update_reload_and_abort(
                    entry, unique_id=unique_id, data={**cleaned, CONF_URL: url}
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                RECONFIGURE_SCHEMA, entry.data
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
        # Both lists have to hold something: the account form needs the names
        # the server reports, and the override step can only offer calendars
        # this entry actually loaded. A stored selection that matches none of
        # them leaves the second empty, and the menu would lead to a dropdown
        # with nothing in it and no way back out.
        if not await self._async_calendar_choices() or not self._managed():
            return await self.async_step_account()
        return self.async_show_menu(step_id="init", menu_options=["account", "pick"])

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage which calendars are used and how the next event is picked."""
        errors: dict[str, str] = {}
        if user_input is not None:
            # Checked here rather than in the schema: voluptuous hands its own
            # message straight to the frontend, so a bound broken in the form
            # reads as untranslated English however the user has it set.
            if CONF_CALENDARS in user_input and not user_input[CONF_CALENDARS]:
                errors[CONF_CALENDARS] = "no_calendars"
            else:
                # Merge: the calendars field is absent while the server is
                # unreachable, and plain user_input would drop the selection.
                return self.async_create_entry(
                    data={**self.config_entry.options, **user_input}
                )

        options = self.config_entry.options
        choices = await self._async_calendar_choices()

        fields: dict[Any, Any] = {}
        if choices:
            # At least one: an account with every box unticked has no entities
            # at all, which reads as a broken integration rather than a choice.
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
        # Only the loaded ones: a calendar the account does not include has no
        # coordinator for an override to apply to.
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
        # Keyed on the url, so renaming the calendar on the server does not
        # detach its settings, and two calendars of one name stay apart.
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
                # Only what actually differs, so a later account-wide change
                # still reaches the fields this calendar never overrode.
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

        The full list, not only the loaded ones: the account form is where a
        calendar that was deselected has to be selectable again.
        """
        if self._choices is not None:
            return self._choices
        data = getattr(self.config_entry, "runtime_data", None)
        if data is None:
            return {}
        try:
            # Cached: the menu decision and the form each need the list, and
            # asking twice is two round-trips to the server per dialog.
            self._choices = await self.hass.async_add_executor_job(
                _calendar_choices, data.client
            )
        except Exception as err:  # noqa: BLE001
            # As broad as the setup path, which catches the same call: a
            # captive portal or a failing proxy answers 207 with html and
            # caldav comes out of that with a TypeError. Narrower here, the
            # options dialog would be unreachable while the server misbehaves,
            # so the poll interval could not even be lengthened to work around
            # it.
            _LOGGER.debug("Could not list calendars: %s", err)
            return {}
        return self._choices


def _minutes() -> Any:
    """Return the poll-interval field, bounded by the frontend itself.

    A raw voluptuous range answers a value outside it with its own English
    message, which Home Assistant passes to the form verbatim.
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


def _days() -> Any:
    """Return the look-ahead field, bounded by the frontend itself."""
    return NumberSelector(
        NumberSelectorConfig(
            min=1, max=365, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="d"
        )
    )


def _selected_keys(options: Mapping[str, Any], choices: dict[str, str]) -> list[str]:
    """Return the stored selection as keys, defaulting to every calendar.

    A selection written before this keyed on the display name.
    """
    stored = options.get(CONF_CALENDARS)
    if stored is None:
        return list(choices)
    return [key for key, name in choices.items() if key in stored or name in stored]


def _cleaned(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """Return the input as it should be stored.

    Blank optional TLS fields go, and only those three: an empty password is a
    legitimate value, and an empty url or username has to reach validation
    rather than vanish.
    """
    optional = (CONF_CA_BUNDLE, CONF_CLIENT_CERT, CONF_CLIENT_KEY)
    cleaned = {
        key: value
        for key, value in user_input.items()
        if value != "" or key not in optional
    }
    if CONF_URL in cleaned:
        cleaned[CONF_URL] = without_userinfo(cleaned[CONF_URL])
    return cleaned


def _calendar_choices(client: caldav.DAVClient) -> dict[str, str]:
    """Return key -> display name for every calendar on the account.

    Keyed on the url, so a rename on the server does not drop the calendar out
    of the selection.
    """
    return {
        calendar_key(calendar.url): display_name(calendar)
        for calendar in client.principal().calendars()
    }


def _labelled(managed: list[Any]) -> dict[str, str]:
    """Return url key -> menu label for the loaded calendars.

    A shared calendar keeps its owner's name, so one account can hold two of
    the same name; picking by name would settle on whichever the server listed
    first and write the override onto the wrong calendar.
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
    """Return the shortest tail of a calendar path that no other one shares.

    Two calendars of the same name usually differ in the owner segment rather
    than the last one, so `/dav/iven/personal` and `/dav/alice/personal` both
    end in "personal" and one label would stand for both.
    """
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


def _test_connection(user_input: Mapping[str, Any]) -> tuple[str | None, str]:
    """Return (error key or None, the url that worked)."""
    kwargs = connection_kwargs(user_input)
    entered = user_input[CONF_URL]
    error = "cannot_connect"
    for url in url_candidates(entered, kwargs):
        # Reset per candidate: a bootstrap attempt that went wrong must not
        # relabel a plain connection failure on the url the user typed.
        candidate_error = "cannot_connect"
        client = build_client(
            url, user_input[CONF_USERNAME], user_input[CONF_PASSWORD], kwargs
        )
        try:
            client.principal()
        except AuthorizationError as err:
            if err.reason == "Unauthorized":
                # Remembered rather than returned: a bare host whose root sits
                # behind another auth realm answers 401 while the bootstrap
                # candidate behind it would have worked, and giving up here
                # reports bad credentials for credentials that are fine.
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
        # Rejected credentials outrank a candidate that would not answer at
        # all: it is the one thing the user can actually act on.
        if error != "invalid_auth":
            error = candidate_error
    return error, entered
