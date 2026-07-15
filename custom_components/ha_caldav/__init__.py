"""The CalDAV integration with full event management."""

from __future__ import annotations

import caldav
from caldav.lib.error import AuthorizationError, DAVError
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from requests import ConnectionError as RequestsConnectionError, Timeout

from .const import REQUEST_TIMEOUT

PLATFORMS = [Platform.CALENDAR, Platform.TODO]

type HaCaldavConfigEntry = ConfigEntry[caldav.DAVClient]


async def async_setup_entry(hass: HomeAssistant, entry: HaCaldavConfigEntry) -> bool:
    """Set up the CalDAV account from a config entry."""
    client = caldav.DAVClient(
        entry.data[CONF_URL],
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        ssl_verify_cert=entry.data[CONF_VERIFY_SSL],
        timeout=REQUEST_TIMEOUT,
    )

    try:
        await hass.async_add_executor_job(client.principal)
    except AuthorizationError as err:
        client.close()
        raise ConfigEntryAuthFailed(f"Authorization failed: {err}") from err
    except (RequestsConnectionError, Timeout, DAVError) as err:
        client.close()
        raise ConfigEntryNotReady(f"Cannot connect to CalDAV server: {err}") from err

    entry.runtime_data = client
    entry.async_on_unload(client.close)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HaCaldavConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_entry(hass: HomeAssistant, entry: HaCaldavConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
