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
from .options import calendar_settings
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

    One account spelled with a trailing slash, another case in the host or an
    explicit default port used to key as a different account, which let it be
    set up a second time. Its entities were keyed on the calendar url as it
    stood, so moving the account to another address renamed every one of them
    to _2 and left what the automations pointed at behind, unavailable.

    Reached only because MINOR_VERSION moved with it. Home Assistant compares
    the stored version against the handler's and returns before loading this
    module when they agree, so a migration written for entries that already
    carry the current number never runs at all.
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
            # Another entity already holds the normalized key, which means the
            # two urls only differed in what the normalization drops. Leaving
            # this one alone keeps the duplicate visible instead of failing the
            # whole setup over it.
            _LOGGER.warning(
                "Leaving %s under its old key; %s is already taken",
                record.entity_id,
                wanted,
            )


def _normalized_unique_id(entry_id: str, unique_id: str, domain: str) -> str | None:
    """Return a unique id rebuilt on the calendar key, or None if it is not ours.

    Which half an entity is comes from the registry, not from reading a suffix
    off the key. A collection url may itself end in "-todo", and calendar_key
    drops a trailing slash and a query string, so text alone cannot tell the
    two apart: it produced a wrong key for one shape, and for another it mapped
    both entities of a calendar onto the same key, where the second re-key is
    refused and that entity stays unavailable for good.
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
        # Tearing down pooled sockets is blocking, and the callback runs on the
        # event loop.
        await hass.async_add_executor_job(client.close)

    # Registered before anything else can fail, so a setup that gives up part
    # way still hands the session back.
    entry.async_on_unload(close_client)

    try:
        capabilities = await hass.async_add_executor_job(fetch_capabilities, client)
    except Exception as err:  # noqa: BLE001
        # A server that will not answer the property is not a reason to refuse
        # setup; every calendar then keeps the permissive default.
        _LOGGER.debug("Could not read calendar capabilities: %s", err)
        capabilities = {}
    address_set = await hass.async_add_executor_job(fetch_address_set, client)

    scan_interval = timedelta(
        minutes=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )
    selected = entry.options.get(CONF_CALENDARS)

    colors = HaCaldavColorCoordinator(hass, entry, client, scan_interval)
    # Up front, so the first sync already has colors.
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
                ),
                read_only=settings[CONF_READ_ONLY],
            )
        )

    # One broken calendar must not stop the others; they retry on their own.
    await asyncio.gather(
        *(item.coordinator.async_refresh() for item in managed),
    )
    if managed and not any(item.coordinator.last_update_success for item in managed):
        # A password revoked between the principal lookup and the first poll
        # belongs in reauth, not in the retry loop.
        for item in managed:
            if isinstance(item.coordinator.last_exception, ConfigEntryAuthFailed):
                raise item.coordinator.last_exception
        raise ConfigEntryNotReady("No calendar on this account could be read")

    entry.runtime_data = HaCaldavRuntimeData(
        client=client, colors=colors, calendars=managed, address_set=address_set
    )
    _async_prune_entities(hass, entry, managed, calendars)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await _async_check_for_issues(hass, entry, managed)
    return True


async def _async_connect(
    hass: HomeAssistant, entry: HaCaldavConfigEntry
) -> tuple[caldav.DAVClient, list[caldav.Calendar]]:
    """Connect and list the calendars, trying the RFC 6764 bootstrap url too."""
    kwargs = connection_kwargs(entry.data)
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
            # caldav raises this for 403 as well, which a reverse proxy or a
            # rate limiter can produce transiently; only 401 is a bad password.
            if err.reason == "Unauthorized":
                _LOGGER.debug("Authorization failed: %s", err)
                raise ConfigEntryAuthFailed("Authorization failed") from err
            last_error = err
            continue
        except Exception as err:  # noqa: BLE001
            # Broad on purpose: a captive portal or a failing proxy answers 207
            # with html, and caldav comes out of that with a TypeError. Letting
            # it escape would fail setup for good instead of retrying.
            await hass.async_add_executor_job(client.close)
            last_error = err
            continue
        return client, calendars
    # Only the kind of failure: caldav puts the whole response body where its
    # error prints a url, and this text is both logged and shown on the card.
    _LOGGER.debug("Cannot connect to CalDAV server: %s", last_error)
    raise ConfigEntryNotReady(
        f"Cannot connect to CalDAV server: {type(last_error).__name__}"
    )


def _list_calendars(client: caldav.DAVClient) -> list[caldav.Calendar]:
    return client.principal().calendars()


def _is_selected(calendar: caldav.Calendar, selected: list[str]) -> bool:
    """Return whether this calendar is one the entry was told to load.

    Keyed on the url. A selection written before that named the calendar, which
    a rename on the server silently dropped out of it.
    """
    return calendar_key(calendar.url) in selected or display_name(calendar) in selected


@callback
def _async_prune_entities(
    hass: HomeAssistant,
    entry: HaCaldavConfigEntry,
    managed: list[ManagedCalendar],
    listed: list[caldav.Calendar],
) -> None:
    """Drop entities for a component the calendar turns out not to carry.

    They would otherwise stay in the registry as permanently unavailable.
    """
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

    Decided against what the account listed, not against what was loaded out of
    it. Those differ whenever a calendar the selection names is missing from a
    single answer, and that is a server having a bad minute rather than a
    decision to stop tracking it. Answered by deletion it costs the recorder
    history, the automations, the dashboard cards and the name overrides that
    point at the entity, irreversibly, while setup still reports success. Only
    a calendar the account did list and the selection leaves out was really
    deselected.

    Only with an explicit selection at all: without one there is nothing for a
    calendar to have been left out of.
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


async def _async_check_for_issues(
    hass: HomeAssistant, entry: HaCaldavConfigEntry, managed: list[ManagedCalendar]
) -> None:
    """Raise the repair issues for this entry."""
    _async_check_builtin_conflict(hass, entry)
    if not managed:
        return
    # sync-collection is a property of the server, not of a collection, and
    # the issue is worded for the account, so one calendar is enough to ask.
    supported = await hass.async_add_executor_job(
        supports_sync_collection, managed[0].calendar
    )
    issue_id = f"{ISSUE_NO_SYNC_COLLECTION}_{entry.entry_id}"
    if supported:
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
    # Through the account key, not the text as typed: the built-in integration
    # is set up on its own, and the one spelling that would not warn is the one
    # where the two were typed differently, which is most of them.
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

    Not on unload: deleting an issue drops the record that the user dismissed
    it, and an options change reloads the entry.
    """
    for issue in (ISSUE_BUILTIN_CALDAV, ISSUE_NO_SYNC_COLLECTION):
        ir.async_delete_issue(hass, DOMAIN, f"{issue}_{entry.entry_id}")
