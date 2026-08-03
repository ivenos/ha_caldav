"""Diagnostics for the CalDAV integration."""

from __future__ import annotations

from typing import Any

import caldav
from caldav.lib.error import DAVError
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME
from homeassistant.core import HomeAssistant
import requests

from . import HaCaldavConfigEntry
from .color import calendar_key, fetch_colors

TO_REDACT = {CONF_PASSWORD, CONF_URL, CONF_USERNAME}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HaCaldavConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    # runtime_data is unset when the entry never finished setup or was unloaded.
    client = getattr(entry, "runtime_data", None)
    calendars: Any = {"error": "entry not loaded"}
    if client is not None:
        calendars = await hass.async_add_executor_job(_calendars, client)
    return {
        "data": async_redact_data(entry.data, TO_REDACT),
        "options": dict(entry.options),
        "calendars": calendars,
    }


def _calendars(client: caldav.DAVClient) -> Any:
    """Return each calendar with its color and the components it accepts."""
    try:
        found = client.principal().calendars()
    except (requests.RequestException, DAVError) as err:
        # Error text embeds the server URL and username, which the redaction
        # above strips; only the error type is safe to include.
        return {"error": type(err).__name__}
    colors: dict[str, str | None] = {}
    color_error = None
    try:
        colors = fetch_colors(client)
    except Exception as err:  # noqa: BLE001
        # A malformed multistatus has caldav raising anything from
        # AssertionError to TypeError.
        color_error = type(err).__name__
    calendars = []
    for calendar in found:
        info: dict[str, Any] = {"name": calendar.name}
        # Reported apart from a missing color, which is a thing a server is
        # allowed to say.
        if color_error is not None:
            info["color_error"] = color_error
        else:
            info["color"] = colors.get(calendar_key(calendar.url))
        try:
            info["components"] = calendar.get_supported_components()
        except (requests.RequestException, DAVError) as err:
            info["components_error"] = type(err).__name__
        calendars.append(info)
    return calendars
