"""Data update coordinator for the CalDAV integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
import logging
from math import isfinite
import threading
from typing import Any

import caldav
from caldav.elements import dav
from caldav.lib.error import AuthorizationError
from homeassistant.components.calendar import CalendarEvent
from homeassistant.components.todo import TodoItem, TodoItemStatus
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
import homeassistant.util.dt as dt_util

from .capability import Capability
from .color import fetch_colors
from .connection import calendar_key
from .errors import NETWORK_ERRORS
from .event import read_extras

_LOGGER = logging.getLogger(__name__)

# How many polls a half may serve its previous result before its entity goes
# unavailable, and how long a sync token is trusted without a read behind it.
_MAX_KEPT_POLLS = 3
_MAX_AGE = timedelta(hours=1)

# Entries a uid cache may hold; a panel window is not the polled one, so
# nothing else evicts what it left behind.
_CACHE_LIMIT = 2000


def _bounded[T](cache: dict[str, T]) -> dict[str, T]:
    """Return the cache trimmed to its limit; callers put the newest first."""
    if (excess := len(cache) - _CACHE_LIMIT) <= 0:
        return cache
    _LOGGER.debug("Dropping %s cache entries past the limit", excess)
    return dict(list(cache.items())[:_CACHE_LIMIT])


# CalDAV knows four states, Home Assistant two; cancelled folds onto completed.
TODO_STATUS = {
    "NEEDS-ACTION": TodoItemStatus.NEEDS_ACTION,
    "IN-PROCESS": TodoItemStatus.NEEDS_ACTION,
    "COMPLETED": TodoItemStatus.COMPLETED,
    "CANCELLED": TodoItemStatus.COMPLETED,
}
TODO_STATUS_INV = {
    TodoItemStatus.NEEDS_ACTION: "NEEDS-ACTION",
    TodoItemStatus.COMPLETED: "COMPLETED",
}


@dataclass
class CalendarSnapshot:
    """One poll's worth of state for a single calendar."""

    next_event: CalendarEvent | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    todos: list[TodoItem] = field(default_factory=list)


@dataclass
class ManagedCalendar:
    """A calendar on the account together with what the server allows on it."""

    calendar: caldav.Calendar
    capability: Capability
    coordinator: HaCaldavCoordinator
    read_only: bool
    # Shared by the calendar entity and the to-do list of the same collection.
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def name(self) -> str:
        """Return the display name, falling back for a nameless collection."""
        return self.calendar.name or "CalDAV"

    @property
    def writable(self) -> bool:
        """Return whether writes are both permitted here and wanted."""
        return self.capability.writable and not self.read_only


@dataclass
class HaCaldavRuntimeData:
    """Everything the platforms and services share for one account."""

    client: caldav.DAVClient
    colors: HaCaldavColorCoordinator
    calendars: list[ManagedCalendar]
    address_set: list[str]
    sync_collection: bool = True


type HaCaldavConfigEntry = ConfigEntry[HaCaldavRuntimeData]


@dataclass
class _WindowRead:
    """One read of the event window, before any of it is kept."""

    vevents: list[Any]
    etags: dict[str, str] | None
    rules: dict[str, str | None]
    epoch: int = 0


@dataclass
class _TodoRead:
    """One read of the to-do list, before any of it is kept."""

    items: list[TodoItem]
    etags: dict[str, str] | None
    epoch: int = 0


@dataclass
class _Half:
    """One component type of the collection, as the poll tracks it.

    A failed read keeps serving the last result for a few polls. Dead is a
    half with nothing left to serve, and its entity goes unavailable; the poll
    itself fails only once every half is dead.
    """

    supported: bool
    cached: Any = None
    misses: int = 0
    dead: bool = False
    error: Exception | None = None


def _rejected(err: Exception | None) -> bool:
    """Return whether an error is a 401; caldav raises the same type for 403."""
    return isinstance(err, AuthorizationError) and err.reason == "Unauthorized"


def calendar_unique_id(entry_id: str, url: object) -> str:
    """Return the unique id of a calendar entity.

    Through calendar_key, so the scheme, host and port the reconfigure step
    changes are not part of it. Changing the shape needs a migration.
    """
    return f"{entry_id}-{calendar_key(url)}"


def todo_unique_id(entry_id: str, url: object) -> str:
    """Return the unique id of a to-do list entity."""
    return f"{calendar_unique_id(entry_id, url)}-todo"


class HaCaldavCoordinator(DataUpdateCoordinator[CalendarSnapshot]):
    """Fetch the events and to-do items of a single calendar.

    Both live in one collection under one sync token, so one poll serves the
    calendar entity and the to-do list alike.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: HaCaldavConfigEntry,
        calendar: caldav.Calendar,
        capability: Capability,
        days: int,
        include_all_day: bool,
        scan_interval: timedelta,
        sync_collection: bool = True,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=calendar.name or "CalDAV",
            update_interval=scan_interval,
            config_entry=entry,
        )
        self.calendar = calendar
        self.capability = capability
        self.days = days
        self.include_all_day = include_all_day
        self.etags: dict[str, str] = {}
        # The poll and a panel read both merge into the etag caches, from the
        # executor.
        self.etag_lock = threading.Lock()
        # Moved by every write that invalidates an etag, so a read that started
        # before the write can be told from one that started after it.
        self._etag_epoch = 0
        self._etag_window: set[str] = set()
        self.rrules: dict[str, str] = {}
        self.todo_etags: dict[str, str] = {}
        self._sync_collection = sync_collection
        self._sync_token: str | None = None
        self._window: tuple[datetime, datetime] | None = None
        self._etags_missed = False
        self._fetched_at: datetime | None = None
        self.halves = {
            "events": _Half(capability.supports_events),
            "todos": _Half(capability.supports_todos),
        }

    @property
    def poll_health(self) -> dict[str, Any]:
        """Return how far behind each half of this collection is."""
        halves = {name: half for name, half in self.halves.items() if half.supported}
        fetched = self._fetched_at
        return {
            "last_full_read": fetched.isoformat() if fetched else None,
            "kept_polls": {name: half.misses for name, half in halves.items()},
            "dead": {name: half.dead for name, half in halves.items()},
        }

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Return all events between two dates."""
        if not self.capability.supports_events:
            return []
        return await hass.async_add_executor_job(
            self._searched_events, start_date, end_date
        )

    def _searched_events(
        self, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Return the events of one window. Blocking, the vobject parse included.

        The rules are read first: expanding a series strips the RRULE from
        every occurrence, and a series that stopped recurring has to be
        forgotten before its occurrences are built.
        """
        epoch = self._etag_epoch
        etags, rules = self._window_index(start_date, end_date)
        self._keep_rules(rules)
        if etags is not None:
            # Merged: a panel window need not overlap the polled one.
            with self.etag_lock:
                if epoch == self._etag_epoch:
                    kept = {
                        uid: tag for uid, tag in self.etags.items() if uid not in etags
                    }
                    self.etags = _bounded(etags | kept)
        results = self.calendar.search(
            start=start_date,
            end=end_date,
            event=True,
            expand=True,
            split_expanded=False,
        )
        return [
            event
            for item in results
            for vevent in components_of(item, "vevent")
            if (event := to_event(vevent, self._rule_for(vevent))) is not None
        ]

    def forget_etags(self, name: str, keys: tuple[str, ...]) -> None:
        """Drop the etags a write has just made stale, and move the epoch.

        The cache is named because a poll replaces the dict rather than
        emptying it.
        """
        with self.etag_lock:
            cache = getattr(self, name)
            for key in keys:
                cache.pop(key, None)
            self._etag_epoch += 1

    def _rule_for(self, vevent: Any) -> str | None:
        uid = getattr(vevent, "uid", None)
        return self.rrules.get(str(uid.value)) if uid is not None else None

    async def _async_update_data(self) -> CalendarSnapshot:
        """Return the next upcoming event and the to-do items.

        UpdateFailed so a failure is logged once; a rejected password reauths.
        """
        try:
            return await self._async_snapshot()
        except AuthorizationError as err:
            if err.reason == "Unauthorized":
                _LOGGER.debug("Authorization failed for %s: %s", self.name, err)
                raise ConfigEntryAuthFailed("Authorization failed") from err
            raise self._failure(err) from err
        except Exception as err:
            # caldav asserts its way out of a response it does not expect.
            raise self._failure(err) from err

    def _failure(self, err: Exception) -> UpdateFailed:
        """Return the poll failure to log, without the collection url."""
        _LOGGER.debug("Could not read %s: %s", self.name, err)
        return UpdateFailed(f"Could not read {self.name}: {type(err).__name__}")

    async def _async_snapshot(self) -> CalendarSnapshot:
        """Refresh what the window needs and map it onto a snapshot.

        A sync-collection REPORT (RFC 6578) gates the fetch. The token is
        committed only after a complete fetch that brought its etags.
        """
        start = dt_util.start_of_local_day()
        end = start + timedelta(days=self.days)
        changed, token = await self.hass.async_add_executor_job(self._sync_changed)
        stale = changed or any(half.cached is None for half in self.halves.values())
        moved = self._window != (start, end)
        # A server may hand back the same token forever.
        aged = (
            self._fetched_at is None or dt_util.utcnow() - self._fetched_at >= _MAX_AGE
        )
        self._etags_missed = False
        if (stale or moved or aged) and await self._async_fetch(start, end):
            self._fetched_at = dt_util.utcnow()
            if not self._etags_missed:
                self._window = (start, end)
                self._sync_token = token
        upcoming = self._next_event()
        return CalendarSnapshot(
            next_event=upcoming[1] if upcoming is not None else None,
            extras=read_extras(upcoming[0]) if upcoming is not None else {},
            todos=list(self.halves["todos"].cached or []),
        )

    async def _async_fetch(self, start: datetime, end: datetime) -> bool:
        """Refresh both halves; a half that failed keeps its last result.

        Each half commits its data and etags together before the other one
        can fail. The poll fails only once every half is dead, and reauths
        only when a 401 is all that is left to explain the failure.
        """
        events, todos = self.halves["events"], self.halves["todos"]
        window = await self._async_read(
            events,
            self._fetch_events(start, end),
            _WindowRead(vevents=[], etags={}, rules={}, epoch=self._etag_epoch),
        )
        if window is not None:
            self._commit_window(window)
        read = await self._async_read(
            todos,
            self._fetch_todos,
            _TodoRead(items=[], etags={}, epoch=self._etag_epoch),
        )
        if read is not None:
            self._commit_todos(read)
        failed = [half for half in self.halves.values() if half.error is not None]
        if failed and len(failed) == self._halves:
            rejected = [half.error for half in failed if _rejected(half.error)]
            if rejected and all(half.dead or _rejected(half.error) for half in failed):
                raise rejected[0]
            if all(half.dead for half in failed):
                raise failed[0].error
        return not failed

    @property
    def _halves(self) -> int:
        return sum(half.supported for half in self.halves.values())

    async def _async_read(self, half: _Half, job: Any, empty: Any) -> Any | None:
        """Run one half's read; None when it failed, with the half updated.

        An unsupported half reads as empty rather than skipped, or its side of
        the snapshot would count as never fetched on every poll.
        """
        half.error = None
        if not half.supported:
            return empty
        try:
            result = await self.hass.async_add_executor_job(job)
        except Exception as err:  # noqa: BLE001
            half.error = err
            if half.cached is None or half.misses >= _MAX_KEPT_POLLS:
                half.dead = True
                return None
            half.misses += 1
            half.dead = False
            _LOGGER.debug("Keeping the previous result for %s: %s", self.name, err)
            return None
        half.misses = 0
        half.dead = False
        return result

    def _commit_window(self, window: _WindowRead) -> None:
        self.halves["events"].cached = window.vevents
        self._keep_rules(window.rules)
        # Committing the token without the etags would freeze every etag here.
        if window.etags is None or not self._merge_etags(window.etags, window.epoch):
            self._etags_missed = True

    def _commit_todos(self, read: _TodoRead) -> None:
        self.halves["todos"].cached = read.items
        with self.etag_lock:
            if read.etags is None or read.epoch != self._etag_epoch:
                self._etags_missed = True
            else:
                self.todo_etags = read.etags

    def _fetch_events(self, start: datetime, end: datetime) -> Any:
        def fetch() -> _WindowRead:
            epoch = self._etag_epoch
            # caldav's split reparses the whole expanded object per occurrence.
            results = self.calendar.search(
                start=start, end=end, event=True, expand=True, split_expanded=False
            )
            etags, rules = self._window_index(start, end)
            # RFC 5545 makes DTSTART optional once the object carries a METHOD.
            return _WindowRead(
                vevents=[
                    vevent
                    for item in results
                    for vevent in components_of(item, "vevent")
                    if _dated(vevent)
                ],
                etags=etags,
                rules=rules,
                epoch=epoch,
            )

        return fetch

    def _keep_rules(self, rules: dict[str, str | None]) -> None:
        """Record the rules of a window, forgetting the series that lost theirs."""
        outside = {uid: rule for uid, rule in self.rrules.items() if uid not in rules}
        found = {uid: rule for uid, rule in rules.items() if rule is not None}
        self.rrules = _bounded(found | outside)

    def _merge_etags(self, etags: dict[str, str], epoch: int) -> bool:
        """Take in the etags of a freshly read window, unless a write beat it.

        Kept rather than replaced: a panel window need not overlap the polled
        one. Only a uid this same window carried before and no longer does is
        gone for certain.
        """
        with self.etag_lock:
            if epoch != self._etag_epoch:
                return False
            gone = self._etag_window - set(etags)
            self._etag_window = set(etags)
            kept = {
                uid: tag
                for uid, tag in self.etags.items()
                if uid not in gone and uid not in etags
            }
            self.etags = _bounded(etags | kept)
            return True

    def _next_event(self) -> tuple[Any, CalendarEvent] | None:
        # The server is not required to return results in any order.
        for vevent in sorted(self.halves["events"].cached or [], key=sort_key):
            try:
                over = is_over(vevent)
            except _UNMAPPABLE as err:
                _LOGGER.debug("Skipping an event that cannot be placed: %s", err)
                continue
            if over or not (self.include_all_day or not is_all_day(vevent)):
                continue
            if (event := to_event(vevent, self._rule_for(vevent))) is not None:
                return vevent, event
        return None

    def _fetch_todos(self) -> _TodoRead:
        """Return every to-do item, completed ones included, in sort-order."""
        epoch = self._etag_epoch
        results = self.calendar.search(
            todo=True, include_completed=True, props=[dav.GetEtag()]
        )
        found = []
        etags: dict[str, str] = {}
        for resource in results:
            vtodo = component_of(resource, "vtodo")
            if vtodo is None or (item := to_todo(vtodo)) is None:
                continue
            found.append((sort_order(vtodo), item))
            etag = resource.props.get(dav.GetEtag.tag)
            if isinstance(etag, str) and item.uid:
                etags[item.uid] = etag
        return _TodoRead(
            items=[item for _, item in sorted(found, key=lambda pair: pair[0])],
            # Items without a single etag is a server not saying, not an empty cache.
            etags=etags if etags or not found else None,
            epoch=epoch,
        )

    def _sync_changed(self) -> tuple[bool, str | None]:
        """Return (changed, token) since the last poll (RFC 6578), uncommitted.

        A server without the report, and any failure, means a full refresh.
        """
        if not self._sync_collection:
            return True, None
        try:
            collection = self.calendar.objects_by_sync_token(self._sync_token)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("sync-collection failed, refreshing in full: %s", err)
            # Dropped: a restored server 403s forever on a token it lost.
            return True, None
        return collection.sync_token != self._sync_token, collection.sync_token

    def _window_index(
        self, start: datetime, end: datetime
    ) -> tuple[dict[str, str] | None, dict[str, str | None]]:
        """Return the etags and the rules of a window, without keeping either.

        The etags are None when the server did not say. The rules map every
        uid in the window, to None where it has none, so a series that stopped
        recurring can be told apart from one this window never saw.
        """
        try:
            items = self.calendar.search(
                start=start, end=end, event=True, expand=False, props=[dav.GetEtag()]
            )
        except NETWORK_ERRORS as err:
            _LOGGER.debug("Could not refresh etags: %s", err)
            return None, {}
        etags: dict[str, str] = {}
        rules: dict[str, str | None] = {}
        for item in items:
            vevent = master_of(item)
            if vevent is None or not hasattr(vevent, "uid"):
                continue
            uid = str(vevent.uid.value)
            rule = getattr(vevent, "rrule", None)
            rules[uid] = str(rule.value) if rule is not None else None
            if isinstance(etag := item.props.get(dav.GetEtag.tag), str):
                etags[uid] = etag
        if items and not etags:
            _LOGGER.debug("The window came back without an etag on anything")
            return None, rules
        return etags, rules


class HaCaldavColorCoordinator(DataUpdateCoordinator[dict[str, str | None]]):
    """Track the color each calendar carries on the server."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: HaCaldavConfigEntry,
        client: caldav.DAVClient,
        scan_interval: timedelta,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="CalDAV colors",
            update_interval=scan_interval,
            config_entry=entry,
        )
        self.client = client

    async def _async_update_data(self) -> dict[str, str | None]:
        """Return the color of every calendar on the account.

        A failure keeps the previous colors; an empty result means the server
        reports none and clears them.
        """
        try:
            return await self.hass.async_add_executor_job(fetch_colors, self.client)
        except Exception as err:
            # A caldav error carries the url it was reading.
            _LOGGER.debug("Could not read calendar colors: %s", err)
            raise UpdateFailed(
                f"Could not read calendar colors: {type(err).__name__}"
            ) from err


def sort_order(vtodo: Any) -> tuple[int, float]:
    """Return the sort key of a to-do, unordered items last."""
    # vobject lowercases property names and turns dashes into underscores.
    value = get_attr_value(vtodo, "x_apple_sort_order")
    if value is None:
        return (1, 0.0)
    try:
        position = float(value)
    except TypeError, ValueError:
        return (1, 0.0)
    # nan compares false against everything, and the write path refuses it too.
    return (0, position) if isfinite(position) else (1, 0.0)


def to_todo(vtodo: Any) -> TodoItem | None:
    """Map a vobject VTODO onto a Home Assistant to-do item.

    None without a uid or a summary, which core needs, and for a date beyond
    what a datetime holds once a zone offset reaches it.
    """
    uid = get_attr_value(vtodo, "uid")
    summary = get_attr_value(vtodo, "summary")
    if uid is None or summary is None:
        return None
    try:
        due: date | datetime | None = None
        if (value := get_attr_value(vtodo, "due")) is not None:
            due = dt_util.as_local(value) if isinstance(value, datetime) else value
        completed: datetime | None = None
        if isinstance(value := get_attr_value(vtodo, "completed"), datetime):
            completed = dt_util.as_local(value)
    except _UNMAPPABLE as err:
        _LOGGER.debug("Skipping a to-do that cannot be mapped: %s", err)
        return None
    return TodoItem(
        uid=uid,
        summary=summary,
        status=TODO_STATUS.get(
            get_attr_value(vtodo, "status") or "", TodoItemStatus.NEEDS_ACTION
        ),
        due=due,
        description=get_attr_value(vtodo, "description"),
        completed=completed,
    )


def get_attr_value(obj: Any, attribute: str) -> Any | None:
    """Return the value of a vobject attribute or None if absent."""
    if (value := getattr(obj, attribute, None)) is not None:
        return value.value
    return None


def get_end_date(vevent: Any) -> datetime | date:
    """Return the end date derived from dtend, duration or the start's type.

    RFC 5545 3.6.1: without either property, a timed event ends at its start
    and an all-day one lasts the day.
    """
    start = vevent.dtstart.value
    if hasattr(vevent, "dtend"):
        end = vevent.dtend.value
    elif hasattr(vevent, "duration"):
        end = start + vevent.duration.value
    elif isinstance(start, datetime):
        end = start
    else:
        end = start + timedelta(days=1)
    if not isinstance(end, datetime) and end == start:
        end += timedelta(days=1)
    return _spanning(start, end)


def _spanning(start: datetime | date, end: datetime | date) -> datetime | date:
    """Return an end the expansion turned around, put back the way it was meant.

    RFC 5545 3.3.5 resolves a start in a DST gap with the offset from before
    the gap, so the start gains an hour the end does not and the two instants
    come back reversed while the wall clock is still right. Two datetimes in
    the same zone object compare by wall clock, hence the explicit UTC.
    """
    if not (isinstance(start, datetime) and isinstance(end, datetime)):
        return end
    if start.tzinfo is None or end.tzinfo is None:
        return end
    if end.astimezone(UTC) >= start.astimezone(UTC):
        return end
    if start.utcoffset() == end.utcoffset():
        # Nothing was resolved across a gap; the object is simply malformed.
        return end
    return start + max(end.replace(tzinfo=None) - start.replace(tzinfo=None), _NOTHING)


_NOTHING = timedelta(0)


def to_local(value: datetime | date) -> datetime | date:
    """Return datetimes as local time and leave dates unchanged."""
    if isinstance(value, datetime):
        return dt_util.as_local(value)
    return value


# ArithmeticError for the OverflowError a date near the year 9999 raises once a
# zone offset is added to it.
_UNMAPPABLE = (
    HomeAssistantError,
    ArithmeticError,
    AttributeError,
    TypeError,
    ValueError,
)


def _dated(vevent: Any) -> bool:
    """Return whether the component carries a start that is really a date.

    A DTSTART with an empty value, VALUE=TEXT or VALUE=DURATION leaves vobject
    holding "" or None.
    """
    start = getattr(getattr(vevent, "dtstart", None), "value", None)
    return isinstance(start, date)


def sort_key(vevent: Any) -> datetime:
    """Return the start as an aware datetime so mixed types sort together."""
    start = vevent.dtstart.value
    if not isinstance(start, datetime):
        start = datetime.combine(start, time.min)
    if start.tzinfo is None:
        start = start.replace(tzinfo=dt_util.get_default_time_zone())
    return start


def is_all_day(vevent: Any) -> bool:
    """Return whether the event covers a whole day."""
    return not isinstance(vevent.dtstart.value, datetime)


def is_over(vevent: Any) -> bool:
    """Return whether the event has already ended."""
    end = to_local(get_end_date(vevent))
    if isinstance(end, datetime):
        return dt_util.now() >= end
    return dt_util.now().date() >= end


def components_of(item: Any, name: str) -> list[Any]:
    """Return every named component of a search result, empty if unreadable.

    An expanded object carries one per occurrence.
    """
    try:
        return list(item.vobject_instance.contents.get(name, []))
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Skipping a calendar object that cannot be read: %s", err)
        return []


def component_of(item: Any, name: str) -> Any | None:
    """Return the first named component of a search result, or None."""
    found = components_of(item, name)
    return found[0] if found else None


def master_of(item: Any) -> Any | None:
    """Return the series master of a search result, or its only component.

    RFC 5545 leaves the component order open, so a detached occurrence may
    come first.
    """
    found = components_of(item, "vevent")
    for vevent in found:
        if not hasattr(vevent, "recurrence_id"):
            return vevent
    return found[0] if found else None


def to_event(vevent: Any, rrule: str | None = None) -> CalendarEvent | None:
    """Map a vobject VEVENT onto a Home Assistant calendar event.

    The rule is passed in because expanding a series strips RRULE from every
    occurrence. None for an object core refuses, such as an end before its start.
    """
    try:
        return CalendarEvent(
            rrule=rrule,
            summary=get_attr_value(vevent, "summary") or "",
            start=to_local(vevent.dtstart.value),
            end=to_local(get_end_date(vevent)),
            location=get_attr_value(vevent, "location"),
            description=get_attr_value(vevent, "description"),
            uid=get_attr_value(vevent, "uid"),
            recurrence_id=(
                str(value)
                if (value := get_attr_value(vevent, "recurrence_id")) is not None
                else None
            ),
        )
    except _UNMAPPABLE as err:
        if rrule is not None:
            # Core refuses rules its own editor cannot offer, FREQ=HOURLY among
            # them; the rule only decides what the recurrence editor opens with.
            _LOGGER.debug("Keeping an event whose rule was refused: %s", err)
            return to_event(vevent)
        _LOGGER.debug("Skipping an event that cannot be mapped: %s", err)
        return None
