"""Diagnostics for the CalDAV integration."""

from __future__ import annotations

from typing import Any

import caldav
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .color import fetch_colors
from .connection import calendar_key
from .const import (
    CONF_CA_BUNDLE,
    CONF_CALENDAR_OPTIONS,
    CONF_CALENDARS,
    CONF_CLIENT_CERT,
    CONF_CLIENT_KEY,
)
from .coordinator import HaCaldavConfigEntry, HaCaldavRuntimeData

TO_REDACT = {
    CONF_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
    CONF_CLIENT_CERT,
    CONF_CLIENT_KEY,
    CONF_CA_BUNDLE,
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HaCaldavConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    # runtime_data is unset when the entry never finished setup or was unloaded.
    data: HaCaldavRuntimeData | None = getattr(entry, "runtime_data", None)
    calendars: Any = {"error": "entry not loaded"}
    scheduling = False
    if data is not None:
        calendars = await hass.async_add_executor_job(_calendars, data)
        scheduling = bool(data.address_set)
    return {
        "data": async_redact_data(entry.data, TO_REDACT),
        "options": _options(entry),
        "scheduling": scheduling,
        "calendars": calendars,
    }


def _options(entry: HaCaldavConfigEntry) -> dict[str, Any]:
    """Return the options with the account out of every collection path.

    Both the selection and the per-calendar settings are keyed by collection
    path, which on most servers spells out the account name the redaction above
    strips; only the last segment names the calendar.
    """
    options = dict(entry.options)
    if isinstance(per_calendar := options.get(CONF_CALENDAR_OPTIONS), dict):
        options[CONF_CALENDAR_OPTIONS] = {
            _leaf(key): value for key, value in per_calendar.items()
        }
    if isinstance(selected := options.get(CONF_CALENDARS), list):
        options[CONF_CALENDARS] = [_leaf(key) for key in selected]
    return options


def _leaf(key: object) -> str:
    """Return the last path segment of a collection key."""
    return str(key).rstrip("/").rsplit("/", 1)[-1]


def _calendars(data: HaCaldavRuntimeData) -> Any:
    """Return each calendar with its color and what the server allows on it."""
    try:
        found = data.client.principal().calendars()
    except Exception as err:  # noqa: BLE001
        # Error text embeds the server URL and username, which the redaction
        # above strips; only the error type is safe to include.
        return {"error": type(err).__name__}
    colors: dict[str, str | None] = {}
    color_error = None
    try:
        colors = fetch_colors(data.client)
    except Exception as err:  # noqa: BLE001
        # A malformed multistatus has caldav raising anything from
        # AssertionError to TypeError.
        color_error = type(err).__name__
    managed = {item.calendar.url: item for item in data.calendars}
    calendars = []
    for calendar in found:
        info: dict[str, Any] = {"name": calendar.name}
        # Reported apart from a missing color, which is a thing a server is
        # allowed to say.
        if color_error is not None:
            info["color_error"] = color_error
        else:
            info["color"] = colors.get(calendar_key(calendar.url))
        if (item := managed.get(calendar.url)) is not None:
            info["components"] = sorted(item.capability.components)
            info["writable"] = item.capability.writable
            info["read_only_option"] = item.read_only
        else:
            info.update(_probe(calendar))
        calendars.append(info)
    return calendars


def _probe(calendar: caldav.Calendar) -> dict[str, Any]:
    """Report on a calendar the entry does not manage, e.g. a deselected one."""
    try:
        return {"components": calendar.get_supported_components()}
    except Exception as err:  # noqa: BLE001
        return {"components_error": type(err).__name__}
