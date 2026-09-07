"""How the account-wide and per-calendar settings combine."""

from __future__ import annotations

from typing import Any

import caldav
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_TIMEOUT

from .connection import calendar_key
from .const import (
    CONF_CALENDAR_OPTIONS,
    CONF_DAYS,
    CONF_INCLUDE_ALL_DAY,
    CONF_READ_ONLY,
    DEFAULT_DAYS,
    DEFAULT_INCLUDE_ALL_DAY,
    DEFAULT_READ_ONLY,
    DEFAULT_TIMEOUT,
)


def request_timeout(entry: ConfigEntry) -> float:
    """Return the seconds one request to this account may take."""
    return entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)


def account_settings(entry: ConfigEntry) -> dict[str, Any]:
    """Return the settings that apply to every calendar of the account."""
    options = entry.options
    return {
        CONF_DAYS: options.get(CONF_DAYS, DEFAULT_DAYS),
        CONF_INCLUDE_ALL_DAY: options.get(
            CONF_INCLUDE_ALL_DAY, DEFAULT_INCLUDE_ALL_DAY
        ),
        CONF_READ_ONLY: options.get(CONF_READ_ONLY, DEFAULT_READ_ONLY),
    }


def calendar_settings(entry: ConfigEntry, calendar: caldav.Calendar) -> dict[str, Any]:
    """Return the effective settings for one calendar.

    An override holds only the keys the user set. Entries written before
    v1.2.0 keyed overrides on the display name rather than the url.
    """
    overrides = entry.options.get(CONF_CALENDAR_OPTIONS, {})
    override = overrides.get(calendar_key(calendar.url))
    if override is None:
        override = overrides.get(calendar.name or "", {})
    return {**account_settings(entry), **override}
