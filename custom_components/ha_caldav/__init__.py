"""The CalDAV integration with full event management."""

from __future__ import annotations

import asyncio
import logging

import caldav
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, Platform
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
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
    UNREACHABLE,
    account_key,
    build_client,
    calendar_key,
    connection_kwargs,
    display_name,
    size_pool,
    url_candidates,
)
from .const import (
    CONF_CALENDARS,
    CONF_DAYS,
    CONF_INCLUDE_ALL_DAY,
    CONF_READ_ONLY,
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
from .errors import rejected
from .options import calendar_settings, poll_interval, request_timeout
from .patches import apply as apply_patches
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CALENDAR, Platform.TODO]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the account-level services, which outlive any single entry."""
    apply_patches()
    async_register_services(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: HaCaldavConfigEntry) -> bool:
    """Normalize the account key of an entry, and the keys of its entities.

    Home Assistant runs this whenever the stored MINOR_VERSION differs from
    the handler's, a higher one left by a downgrade included, so the number
    has to move with every change of key shape.
    """
    if entry.version > 1:
        return False
    if entry.minor_version >= 2:
        return True
    _LOGGER.debug("Migrating %s to the normalized keys", entry.title)
    _async_migrate_entity_keys(hass, entry)
    unique_id = account_key(entry.data[CONF_URL], entry.data[CONF_USERNAME])
    held = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, unique_id)
    if held is not None and held.entry_id != entry.entry_id:
        _LOGGER.warning("%s is set up twice, as %s too", entry.title, held.title)
        unique_id = entry.unique_id
    hass.config_entries.async_update_entry(entry, unique_id=unique_id, minor_version=2)
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
    """Return a unique id rebuilt on the calendar key, or None if it is not ours."""
    if (key := _registered_key(entry_id, unique_id, domain)) is None:
        return None
    suffix = "-todo" if domain == Platform.TODO else ""
    return f"{entry_id}-{calendar_key(key)}{suffix}"


def _registered_key(entry_id: str, unique_id: str, domain: str) -> str | None:
    """Return the calendar part of an entity's unique id, or None if it is not ours.

    The half comes from the registry domain: a collection url may itself end
    in "-todo".
    """
    prefix = f"{entry_id}-"
    if not unique_id.startswith(prefix):
        return None
    rest = unique_id.removeprefix(prefix)
    return rest.removesuffix("-todo") if domain == Platform.TODO else rest


async def async_setup_entry(hass: HomeAssistant, entry: HaCaldavConfigEntry) -> bool:
    """Set up the CalDAV account from a config entry."""
    client, calendars = await _async_connect(hass, entry)
    _async_key_by_url(hass, entry, calendars)

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

    scan_interval = poll_interval(entry)
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
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="no_calendar_read"
        )

    entry.runtime_data = HaCaldavRuntimeData(
        client=client,
        colors=colors,
        calendars=managed,
        address_set=address_set,
        sync_collection=sync_collection,
    )
    _async_drop_account_device(hass, entry)
    _async_prune_entities(hass, entry, managed, calendars)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(
        _async_follow_the_server(
            hass,
            entry,
            client,
            colors,
            {calendar_key(calendar.url) for calendar in calendars},
            {calendar_key(item.calendar.url) for item in managed},
        )
    )
    _async_check_for_issues(hass, entry)
    return True


async def _async_connect(
    hass: HomeAssistant, entry: HaCaldavConfigEntry
) -> tuple[caldav.DAVClient, list[caldav.Calendar]]:
    """Connect and list the calendars, trying the RFC 6764 bootstrap url too."""
    kwargs = connection_kwargs(entry.data, request_timeout(entry))
    last_error: Exception | None = None
    refused: Exception | None = None
    # Driven from the executor: reaching the bootstrap url asks the server.
    candidates = url_candidates(entry.data[CONF_URL], kwargs)
    while url := await hass.async_add_executor_job(next, candidates, None):
        # In the executor: a niquests session imports its resolver on creation.
        client = await hass.async_add_executor_job(
            build_client,
            url,
            entry.data[CONF_USERNAME],
            entry.data[CONF_PASSWORD],
            kwargs,
        )
        try:
            calendars = await hass.async_add_executor_job(_list_calendars, client)
        except Exception as err:  # noqa: BLE001
            # A captive portal answers 207 with html, which is a TypeError in caldav.
            await hass.async_add_executor_job(client.close)
            last_error = err
            # The bootstrap may sit behind another auth realm, as the flow allows.
            if rejected(err):
                refused = err
            if isinstance(err, UNREACHABLE):
                break
            continue
        size_pool(client, len(calendars))
        return client, calendars
    if refused is not None:
        _LOGGER.debug("Authorization failed: %s", refused)
        raise ConfigEntryAuthFailed("Authorization failed") from refused
    # caldav puts the whole response body into its error text.
    _LOGGER.debug("Cannot connect to CalDAV server: %s", last_error)
    raise ConfigEntryNotReady(
        translation_domain=DOMAIN,
        translation_key="cannot_connect",
        translation_placeholders={"error": type(last_error).__name__},
    )


@callback
def _async_key_by_url(
    hass: HomeAssistant, entry: HaCaldavConfigEntry, calendars: list[caldav.Calendar]
) -> None:
    """Rewrite a selection kept by display name onto the calendars' urls.

    Entries written before v1.2.0 selected by name, which a rename breaks. A
    name no calendar carries right now stays as it is.
    """
    keys = {calendar_key(calendar.url) for calendar in calendars}
    shown: dict[str, list[str]] = {}
    for calendar in calendars:
        shown.setdefault(display_name(calendar), []).append(calendar_key(calendar.url))
    options = dict(entry.options)
    if selected := options.get(CONF_CALENDARS):
        rewritten: list[str] = []
        for item in selected:
            for key in [item] if item in keys else shown.get(item, [item]):
                if key not in rewritten:
                    rewritten.append(key)
        options[CONF_CALENDARS] = rewritten
    if options != entry.options:
        _LOGGER.debug("Keying the calendars of %s by url", entry.title)
        hass.config_entries.async_update_entry(entry, options=options)


def _list_calendars(client: caldav.DAVClient) -> list[caldav.Calendar]:
    return client.principal().calendars()


def _is_selected(calendar: caldav.Calendar, selected: list[str]) -> bool:
    """Return whether this calendar is one the entry was told to load.

    Entries written before v1.2.0 selected by display name rather than url.
    """
    return calendar_key(calendar.url) in selected or display_name(calendar) in selected


@callback
def _async_drop_account_device(hass: HomeAssistant, entry: HaCaldavConfigEntry) -> None:
    """Remove the account device earlier versions put every entity under.

    Removing a device removes its entities too, so they are moved off it first.
    """
    devices = dr.async_get(hass)
    registry = er.async_get(hass)
    for device in dr.async_entries_for_config_entry(devices, entry.entry_id):
        if (DOMAIN, entry.entry_id) not in device.identifiers:
            continue
        for record in er.async_entries_for_device(
            registry, device.id, include_disabled_entities=True
        ):
            disabled_by = record.disabled_by
            if disabled_by is er.RegistryEntryDisabler.DEVICE:
                disabled_by = er.RegistryEntryDisabler.USER
            registry.async_update_entity(
                record.entity_id,
                device_id=None,
                area_id=record.area_id or device.area_id,
                disabled_by=disabled_by,
            )
        devices.async_remove_device(device.id)


_GONE_POLLS = 2


@callback
def _async_follow_the_server(
    hass: HomeAssistant,
    entry: HaCaldavConfigEntry,
    client: caldav.DAVClient,
    colors: HaCaldavColorCoordinator,
    listed: set[str],
    loaded: set[str],
) -> CALLBACK_TYPE:
    """Keep the entry in step with the calendars the color poll finds.

    It reloads once a loaded calendar is renamed or gone, and once the poll
    finds a calendar the entry should load that the listing left out. The
    entities of a calendar missing from _GONE_POLLS polls in a row go.
    """
    selected = entry.options.get(CONF_CALENDARS)
    previous = colors.data
    missing = dict.fromkeys(_unlisted_keys(hass, entry, listed), 0)

    @callback
    def check() -> None:
        nonlocal previous
        if (found := colors.data) is None or found is previous:
            return
        # The poll reads every child of the home set, the inbox and outbox too;
        # one whose kind went unsaid counts as there but not as new.
        calendars = {key for key, item in found.items() if item.calendar is not False}
        changed = previous is not None and any(
            key in previous
            and (key not in calendars or found[key].name != previous[key].name)
            for key in loaded
        )
        previous = found
        if changed:
            _LOGGER.debug("Reloading %s for a change on the server", entry.title)
            hass.config_entries.async_schedule_reload(entry.entry_id)
            return
        if wanted := {
            key
            for key, item in found.items()
            if item.calendar and key not in listed and (not selected or key in selected)
        }:
            entry.async_create_task(
                hass, _async_reload_once_listed(hass, entry, client, wanted)
            )
        for key in missing:
            missing[key] = 0 if key in calendars else missing[key] + 1
        if gone := {key for key, polls in missing.items() if polls >= _GONE_POLLS}:
            _async_forget_calendars(hass, entry, gone)
            for key in gone:
                del missing[key]

    return colors.async_add_listener(check)


async def _async_reload_once_listed(
    hass: HomeAssistant,
    entry: HaCaldavConfigEntry,
    client: caldav.DAVClient,
    wanted: set[str],
) -> None:
    """Reload once the calendar listing has one of these calendars too.

    Asked again rather than trusted: two answers that keep disagreeing would
    otherwise reload the entry on every poll.
    """
    try:
        calendars = await hass.async_add_executor_job(_list_calendars, client)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Could not list the calendars again: %s", err)
        return
    if wanted & {calendar_key(calendar.url) for calendar in calendars}:
        _LOGGER.debug("Reloading %s for a calendar it did not list", entry.title)
        hass.config_entries.async_schedule_reload(entry.entry_id)


@callback
def _unlisted_keys(
    hass: HomeAssistant, entry: HaCaldavConfigEntry, listed: set[str]
) -> set[str]:
    """Return the calendars the entry has entities for but the listing left out."""
    registry = er.async_get(hass)
    return {
        key
        for record in er.async_entries_for_config_entry(registry, entry.entry_id)
        if (key := _registered_key(entry.entry_id, record.unique_id, record.domain))
        is not None
        and key not in listed
    }


@callback
def _async_forget_calendars(
    hass: HomeAssistant, entry: HaCaldavConfigEntry, keys: set[str]
) -> None:
    """Remove the entities of calendars gone from the server."""
    registry = er.async_get(hass)
    for record in er.async_entries_for_config_entry(registry, entry.entry_id):
        if _registered_key(entry.entry_id, record.unique_id, record.domain) in keys:
            _LOGGER.debug("Removing %s; its calendar is gone", record.entity_id)
            registry.async_remove(record.entity_id)


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
        pair
        for calendar in listed
        if not _is_selected(calendar, selected)
        for pair in (
            (Platform.CALENDAR, calendar_unique_id(entry.entry_id, calendar.url)),
            (Platform.TODO, todo_unique_id(entry.entry_id, calendar.url)),
        )
    }
    for record in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (record.domain, record.unique_id) in deselected:
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


@callback
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
