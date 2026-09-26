"""Diagnostics for the CalDAV integration."""

from __future__ import annotations

from collections import Counter
from typing import Any

import caldav
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .color import Collection, fetch_collections
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

    The path usually spells out the account name the redaction strips, Google's
    in the segment before the last.
    """
    options = dict(entry.options)
    per_calendar = options.get(CONF_CALENDAR_OPTIONS)
    per_calendar = per_calendar if isinstance(per_calendar, dict) else None
    selected = options.get(CONF_CALENDARS)
    selected = selected if isinstance(selected, list) else None
    leaves = _leaves([*(selected or []), *(per_calendar or {})])
    if per_calendar is not None:
        options[CONF_CALENDAR_OPTIONS] = {
            leaves[key]: value for key, value in per_calendar.items()
        }
    if selected is not None:
        options[CONF_CALENDARS] = [leaves[key] for key in selected]
    return options


def _leaves(keys: list[Any]) -> dict[Any, str]:
    """Return key -> its last path segment, numbered where two share one."""
    seen: Counter[str] = Counter()
    leaves: dict[Any, str] = {}
    for key in dict.fromkeys(keys):
        leaf = str(key).rstrip("/").rsplit("/", 1)[-1]
        seen[leaf] += 1
        leaves[key] = leaf if seen[leaf] == 1 else f"{leaf} ({seen[leaf]})"
    return leaves


def _calendars(data: HaCaldavRuntimeData) -> Any:
    """Return each calendar with its color and what the server allows on it."""
    try:
        found = data.client.principal().calendars()
    except Exception as err:  # noqa: BLE001
        # The error text embeds the server url and the username.
        return {"error": type(err).__name__}
    collections: dict[str, Collection] = {}
    color_error = None
    try:
        collections = fetch_collections(data.client)
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
            reported = collections.get(calendar_key(calendar.url))
            info["color"] = None if reported is None else reported.color
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
