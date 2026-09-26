"""How the account-wide and per-calendar settings combine."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import caldav
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL, CONF_TIMEOUT

from .connection import calendar_key
from .const import (
    CONF_CALENDAR_OPTIONS,
    CONF_DAYS,
    CONF_INCLUDE_ALL_DAY,
    CONF_READ_ONLY,
    DEFAULT_DAYS,
    DEFAULT_INCLUDE_ALL_DAY,
    DEFAULT_READ_ONLY,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEOUT,
)


def poll_interval(entry: ConfigEntry) -> timedelta:
    """Return how often the account is polled."""
    return timedelta(
        minutes=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
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

    An override holds only the keys the user set.
    """
    overrides = entry.options.get(CONF_CALENDAR_OPTIONS, {})
    return {**account_settings(entry), **overrides.get(calendar_key(calendar.url), {})}
