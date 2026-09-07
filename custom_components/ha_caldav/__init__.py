"""The CalDAV integration with full event management."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

import caldav
from caldav.lib.error import AuthorizationError
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_URL,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import (
    config_validation as cv,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.typing import ConfigType

from .capability import (
    capability_for,
    fetch_address_set,
    fetch_capabilities,
    supports_sync_collection,
)
from .connection import (
    account_key,
    build_client,
    calendar_key,
    connection_kwargs,
    display_name,
    url_candidates,
)
from .const import (
    CONF_CALENDARS,
    CONF_DAYS,
    CONF_INCLUDE_ALL_DAY,
    CONF_READ_ONLY,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ISSUE_BUILTIN_CALDAV,
    ISSUE_NO_SYNC_COLLECTION,
)
from .coordinator import (
    HaCaldavColorCoordinator,
    HaCaldavConfigEntry,
    HaCaldavCoordinator,
    HaCaldavRuntimeData,
    ManagedCalendar,
    calendar_unique_id,
    todo_unique_id,
)
from .options import calendar_settings, request_timeout
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CALENDAR, Platform.TODO]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the account-level services, which outlive any single entry."""
    async_register_services(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: HaCaldavConfigEntry) -> bool:
    """Normalize the account key of an entry, and the keys of its entities.

    Home Assistant runs this only while the stored MINOR_VERSION is behind the
    handler's, so the number has to move with every change of key shape.
    """
    _LOGGER.debug("Migrating %s to the normalized keys", entry.title)
    _async_migrate_entity_keys(hass, entry)
    hass.config_entries.async_update_entry(
        entry,
        unique_id=account_key(entry.data[CONF_URL], entry.data[CONF_USERNAME]),
        minor_version=2,
    )
    return True


@callback
def _async_migrate_entity_keys(hass: HomeAssistant, entry: HaCaldavConfigEntry) -> None:
    """Re-key the entities of an entry onto the normalized calendar key."""
    registry = er.async_get(hass)
    for record in er.async_entries_for_config_entry(registry, entry.entry_id):
        wanted = _normalized_unique_id(entry.entry_id, record.unique_id, record.domain)
        if wanted is None or wanted == record.unique_id:
            continue
        try:
            registry.async_update_entity(record.entity_id, new_unique_id=wanted)
        except ValueError:
            # Two urls that only differed in what the normalization drops.
            _LOGGER.warning(
                "Leaving %s under its old key; %s is already taken",
                record.entity_id,
                wanted,
            )


def _normalized_unique_id(entry_id: str, unique_id: str, domain: str) -> str | None:
    """Return a unique id rebuilt on the calendar key, or None if it is not ours.

    The half comes from the registry domain: a collection url may itself end
    in "-todo".
    """
    prefix = f"{entry_id}-"
    if not unique_id.startswith(prefix):
        return None
    rest = unique_id.removeprefix(prefix)
    if domain == Platform.TODO:
        rest = rest.removesuffix("-todo")
        return f"{prefix}{calendar_key(rest)}-todo"
    return f"{prefix}{calendar_key(rest)}"


async def async_setup_entry(hass: HomeAssistant, entry: HaCaldavConfigEntry) -> bool:
    """Set up the CalDAV account from a config entry."""
    client, calendars = await _async_connect(hass, entry)

    async def close_client() -> None:
        # Tearing down pooled sockets blocks.
        await hass.async_add_executor_job(client.close)

    # First, so a setup that gives up part way still hands the session back.
    entry.async_on_unload(close_client)

    try:
        capabilities = await hass.async_add_executor_job(fetch_capabilities, client)
    except Exception as err:  # noqa: BLE001
        # Every calendar then keeps the permissive default.
        _LOGGER.debug("Could not read calendar capabilities: %s", err)
        capabilities = {}
    address_set = await hass.async_add_executor_job(fetch_address_set, client)
    # A property of the server, so one calendar is enough to ask.
    sync_collection = True
    if calendars:
        sync_collection = await hass.async_add_executor_job(
            supports_sync_collection, calendars[0]
        )

    scan_interval = timedelta(
        minutes=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )
    selected = entry.options.get(CONF_CALENDARS)

    colors = HaCaldavColorCoordinator(hass, entry, client, scan_interval)
    await colors.async_refresh()

    managed: list[ManagedCalendar] = []
    for calendar in calendars:
        if selected and not _is_selected(calendar, selected):
            continue
        capability = capability_for(capabilities, calendar)
        settings = calendar_settings(entry, calendar)
        managed.append(
            ManagedCalendar(
                calendar=calendar,
                capability=capability,
                coordinator=HaCaldavCoordinator(
                    hass,
                    entry,
                    calendar,
                    capability,
                    settings[CONF_DAYS],
                    settings[CONF_INCLUDE_ALL_DAY],
                    scan_interval,
                    sync_collection,
                ),
                read_only=settings[CONF_READ_ONLY],
            )
        )

    # One broken calendar must not stop the others; they retry on their own.
    await asyncio.gather(
        *(item.coordinator.async_refresh() for item in managed),
    )
    if managed and not any(item.coordinator.last_update_success for item in managed):
        for item in managed:
            if isinstance(item.coordinator.last_exception, ConfigEntryAuthFailed):
                raise item.coordinator.last_exception
        raise ConfigEntryNotReady("No calendar on this account could be read")

    entry.runtime_data = HaCaldavRuntimeData(
        client=client,
        colors=colors,
        calendars=managed,
        address_set=address_set,
        sync_collection=sync_collection,
    )
    _async_prune_entities(hass, entry, managed, calendars)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_check_for_issues(hass, entry)
    return True


async def _async_connect(
    hass: HomeAssistant, entry: HaCaldavConfigEntry
) -> tuple[caldav.DAVClient, list[caldav.Calendar]]:
    """Connect and list the calendars, trying the RFC 6764 bootstrap url too."""
    kwargs = connection_kwargs(entry.data, request_timeout(entry))
    last_error: Exception | None = None
    # Driven from the executor: reaching the bootstrap url asks the server.
    candidates = url_candidates(entry.data[CONF_URL], kwargs)
    while url := await hass.async_add_executor_job(next, candidates, None):
        client = build_client(
            url, entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD], kwargs
        )
        try:
            calendars = await hass.async_add_executor_job(_list_calendars, client)
        except AuthorizationError as err:
            await hass.async_add_executor_job(client.close)
            # caldav raises this for a 403 as well; only a 401 is a bad password.
            if err.reason == "Unauthorized":
                _LOGGER.debug("Authorization failed: %s", err)
                raise ConfigEntryAuthFailed("Authorization failed") from err
            last_error = err
            continue
        except Exception as err:  # noqa: BLE001
            # A captive portal answers 207 with html, which is a TypeError in caldav.
            await hass.async_add_executor_job(client.close)
            last_error = err
            continue
        return client, calendars
    # caldav puts the whole response body into its error text.
    _LOGGER.debug("Cannot connect to CalDAV server: %s", last_error)
    raise ConfigEntryNotReady(
        f"Cannot connect to CalDAV server: {type(last_error).__name__}"
    )


def _list_calendars(client: caldav.DAVClient) -> list[caldav.Calendar]:
    return client.principal().calendars()


def _is_selected(calendar: caldav.Calendar, selected: list[str]) -> bool:
    """Return whether this calendar is one the entry was told to load.

    Entries written before v1.2.0 selected by display name rather than url.
    """
    return calendar_key(calendar.url) in selected or display_name(calendar) in selected


@callback
def _async_prune_entities(
    hass: HomeAssistant,
    entry: HaCaldavConfigEntry,
    managed: list[ManagedCalendar],
    listed: list[caldav.Calendar],
) -> None:
    """Drop entities for a component the calendar turns out not to carry."""
    registry = er.async_get(hass)
    for item in managed:
        url = item.calendar.url
        for domain, unique_id, supported in (
            (
                Platform.CALENDAR,
                calendar_unique_id(entry.entry_id, url),
                item.capability.supports_events,
            ),
            (
                Platform.TODO,
                todo_unique_id(entry.entry_id, url),
                item.capability.supports_todos,
            ),
        ):
            if supported:
                continue
            if entity_id := registry.async_get_entity_id(domain, DOMAIN, unique_id):
                _LOGGER.debug(
                    "Removing %s; the calendar does not hold those", entity_id
                )
                registry.async_remove(entity_id)
    _async_prune_deselected(hass, entry, listed, registry)


@callback
def _async_prune_deselected(
    hass: HomeAssistant,
    entry: HaCaldavConfigEntry,
    listed: list[caldav.Calendar],
    registry: er.EntityRegistry,
) -> None:
    """Drop the entities of a calendar the user took out of the selection.

    Decided against what the account listed, not what was loaded: a calendar
    missing from one listing is a server having a bad minute, and removing an
    entity takes its history and everything pointing at it.
    """
    selected = entry.options.get(CONF_CALENDARS)
    if not selected:
        return
    deselected = {
        unique_id
        for calendar in listed
        if not _is_selected(calendar, selected)
        for unique_id in (
            calendar_unique_id(entry.entry_id, calendar.url),
            todo_unique_id(entry.entry_id, calendar.url),
        )
    }
    for record in er.async_entries_for_config_entry(registry, entry.entry_id):
        if record.unique_id in deselected:
            _LOGGER.debug(
                "Removing %s; its calendar is no longer selected", record.entity_id
            )
            registry.async_remove(record.entity_id)


@callback
def _async_check_for_issues(hass: HomeAssistant, entry: HaCaldavConfigEntry) -> None:
    """Raise the repair issues for this entry."""
    _async_check_builtin_conflict(hass, entry)
    issue_id = f"{ISSUE_NO_SYNC_COLLECTION}_{entry.entry_id}"
    data = entry.runtime_data
    # With no calendar loaded there is nothing to warn about.
    if not data.calendars or data.sync_collection:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_NO_SYNC_COLLECTION,
        translation_placeholders={"account": entry.title},
    )


def _async_check_builtin_conflict(
    hass: HomeAssistant, entry: HaCaldavConfigEntry
) -> None:
    """Warn when the built-in caldav integration serves the same account."""
    ours = account_key(entry.data[CONF_URL], entry.data[CONF_USERNAME])
    clash = any(
        account_key(
            str(other.data.get(CONF_URL, "")), str(other.data.get(CONF_USERNAME, ""))
        )
        == ours
        for other in hass.config_entries.async_entries("caldav")
    )
    issue_id = f"{ISSUE_BUILTIN_CALDAV}_{entry.entry_id}"
    if not clash:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_BUILTIN_CALDAV,
        translation_placeholders={"account": entry.title},
    )


async def async_unload_entry(hass: HomeAssistant, entry: HaCaldavConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: HaCaldavConfigEntry) -> None:
    """Drop the issues raised for this entry.

    Not on unload: that would drop the record of a dismissal on every reload.
    """
    for issue in (ISSUE_BUILTIN_CALDAV, ISSUE_NO_SYNC_COLLECTION):
        ir.async_delete_issue(hass, DOMAIN, f"{issue}_{entry.entry_id}")
