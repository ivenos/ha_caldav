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
    # Unset when the entry never finished setup or was unloaded.
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
    """Return the options with each collection path cut to its last segment.

    The path usually spells out the account name the redaction strips.
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
    return str(key).rstrip("/").rsplit("/", 1)[-1]


def _calendars(data: HaCaldavRuntimeData) -> Any:
    """Return each calendar with its color and what the server allows on it."""
    try:
        found = data.client.principal().calendars()
    except Exception as err:  # noqa: BLE001
        # The error text embeds the server url and the username.
        return {"error": type(err).__name__}
    colors: dict[str, str | None] = {}
    color_error = None
    try:
        colors = fetch_colors(data.client)
    except Exception as err:  # noqa: BLE001
        # Anything from AssertionError to TypeError on a malformed multistatus.
        color_error = type(err).__name__
    managed = {item.calendar.url: item for item in data.calendars}
    calendars = []
    for calendar in found:
        info: dict[str, Any] = {"name": calendar.name}
        # Apart from a missing color, which a server is allowed to report.
        if color_error is not None:
            info["color_error"] = color_error
        else:
            info["color"] = colors.get(calendar_key(calendar.url))
        if (item := managed.get(calendar.url)) is not None:
            info["components"] = sorted(item.capability.components)
            info["writable"] = item.capability.writable
            info["read_only_option"] = item.read_only
            info["poll"] = item.coordinator.poll_health
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
