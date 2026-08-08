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

# Returned by a half of the poll that failed but had a previous result to keep.
_KEEP = object()

# Returned by a half with nothing left to serve: it never answered, or it has
# been failing long enough that its snapshot cannot be told apart from a fresh
# one. Such a half fails the whole poll, but only once both have been tried.
_DEAD = object()

# How long a half may go on serving that previous result before the entity says
# so, and how long a sync token is trusted without a read behind it.
_MAX_KEPT_POLLS = 3
_MAX_AGE = timedelta(hours=1)

# Entries a uid cache may hold. A window the panel asked for is not the polled
# one, so nothing later evicts what it left behind.
_CACHE_LIMIT = 2000


def _bounded[T](cache: dict[str, T]) -> dict[str, T]:
    """Return the cache trimmed to its limit, from the far end.

    Callers put the window they just read at the front, so what goes is what
    has been out of view longest. Built the other way round the same leading
    entries were dropped on every poll, and those uids never got an etag again:
    their next edit was written with nothing to check against, silently, on
    exactly the large calendars where a clash is likeliest.
    """
    if (excess := len(cache) - _CACHE_LIMIT) <= 0:
        return cache
    _LOGGER.debug("Dropping %s cache entries past the limit", excess)
    return dict(list(cache.items())[:_CACHE_LIMIT])


# CalDAV knows four states, Home Assistant two. Cancelled items are folded into
# completed because they are equally "not outstanding".
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
    # Shared by the calendar entity and the to-do list of the same collection,
    # because both write to the objects of it.
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


def calendar_unique_id(entry_id: str, url: object) -> str:
    """Return the unique id of a calendar entity.

    Through calendar_key, not off the url as it stands: the url carries the
    scheme, the host and the port, and the reconfigure step exists to change
    exactly those. Keyed on the raw one, moving an account from http to https
    renamed every entity to _2 and left the originals unavailable, taking the
    automations, dashboard cards and history that named them with it.

    Changing the shape orphans every entity already in the registry, so it
    needs a migration, and :mod:`.services` reverses it to find a calendar from
    a target entity.
    """
    return f"{entry_id}-{calendar_key(url)}"


def todo_unique_id(entry_id: str, url: object) -> str:
    """Return the unique id of a to-do list entity."""
    return f"{calendar_unique_id(entry_id, url)}-todo"


class HaCaldavCoordinator(DataUpdateCoordinator[CalendarSnapshot]):
    """Fetch the events and to-do items of a single calendar.

    Both live in the same collection and are covered by one sync token, so a
    single poll serves the calendar entity and the to-do list entity alike.
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
        # Both the poll and a panel read merge into the etag caches, and both
        # run in the executor, so the read-modify-write needs holding together.
        self.etag_lock = threading.Lock()
        # Moved by every write that invalidates an etag. A read that started
        # before it carries the value from before the write, and the lock alone
        # cannot tell the two apart: it covers the moment the dict is changed,
        # not the span between a read and the commit that follows it.
        self._etag_epoch = 0
        self._etag_window: set[str] = set()
        self.rrules: dict[str, str] = {}
        self.todo_etags: dict[str, str] = {}
        self._sync_token: str | None = None
        self._window_events: list[Any] | None = None
        self._window: tuple[datetime, datetime] | None = None
        self._todos: list[TodoItem] | None = None
        self._etags_missed = False
        self._fetched_at: datetime | None = None
        self._misses = {"events": 0, "todos": 0}
        # Per half, because one entity each reads from them. A collection whose
        # to-do report the server refuses is still a calendar that reads.
        self.dead = {"events": False, "todos": False}

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
        """Return the events of one window. Blocking, and not only for the read.

        vobject parses an item the first time its components are asked for, so
        building the list costs as much as the request does. The window is the
        caller's to choose, and one year of a handful of daily series is
        seconds of it, which on the event loop is all of Home Assistant waiting.

        The rules are read first: expanding a series strips the RRULE from every
        occurrence it produces, so the rule shown against one comes from the
        second search, and a series that stopped recurring has to be forgotten
        before the occurrences are built from it.
        """
        epoch = self._etag_epoch
        etags, rules = self._window_index(start_date, end_date)
        self._keep_rules(rules)
        if etags is not None:
            # Merged, not replaced: the panel window need not overlap the polled
            # one and an edit made from it still has to be conflict-checked. The
            # window just read comes first, so a cache at its limit gives up
            # what is furthest out of view rather than these.
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
        """Drop the etags a write has just made stale.

        The epoch moves with them, so a read already on the wire cannot put back
        what this took out. The cache is named rather than handed over, because
        a poll landing meanwhile replaces the dict rather than emptying it.
        """
        with self.etag_lock:
            cache = getattr(self, name)
            for key in keys:
                cache.pop(key, None)
            self._etag_epoch += 1

    def _rule_for(self, vevent: Any) -> str | None:
        """Return the recurrence rule recorded for this occurrence's series."""
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
            # caldav asserts its way out of a response it does not expect, so
            # the type of a server-side failure is not worth predicting.
            raise self._failure(err) from err

    def _failure(self, err: Exception) -> UpdateFailed:
        """Return the poll failure to log, without the collection url.

        A caldav error prints the url it was reading, and with it the account
        name, into a line users are asked to paste into an issue.
        """
        _LOGGER.debug("Could not read %s: %s", self.name, err)
        return UpdateFailed(f"Could not read {self.name}: {type(err).__name__}")

    async def _async_snapshot(self) -> CalendarSnapshot:
        """Refresh what the window needs and map it onto a snapshot.

        A sync-collection REPORT (RFC 6578) gates the fetch.
        """
        start = dt_util.start_of_local_day()
        end = start + timedelta(days=self.days)
        changed, token = await self.hass.async_add_executor_job(self._sync_changed)
        stale = self._window_events is None or self._todos is None or changed
        moved = self._window != (start, end)
        # A server that hands back the same token forever would otherwise never
        # be read again, and it would report nothing wrong while doing it.
        aged = (
            self._fetched_at is None or dt_util.utcnow() - self._fetched_at >= _MAX_AGE
        )
        self._etags_missed = False
        # Committed only after a complete fetch; a half that failed, or a window
        # whose etags did not arrive, leaves the token behind for the next poll.
        if (
            (stale or moved or aged)
            and await self._async_fetch(start, end)
            and not self._etags_missed
        ):
            self._window = (start, end)
            self._sync_token = token
            self._fetched_at = dt_util.utcnow()
        upcoming = self._next_event()
        return CalendarSnapshot(
            next_event=upcoming[1] if upcoming is not None else None,
            extras=read_extras(upcoming[0]) if upcoming is not None else {},
            todos=list(self._todos or []),
        )

    async def _async_fetch(self, start: datetime, end: datetime) -> bool:
        """Refresh both halves; a half that failed before keeps its last result.

        A poll where no half came back at all still fails.
        """
        events, events_error = await self._async_half(
            self._fetch_events(start, end),
            self.capability.supports_events,
            self._window_events,
            "events",
            _WindowRead(vevents=[], etags={}, rules={}, epoch=self._etag_epoch),
        )
        todos, todos_error = await self._async_half(
            self._fetch_todos,
            self.capability.supports_todos,
            self._todos,
            "todos",
            _TodoRead(items=[], etags={}, epoch=self._etag_epoch),
        )
        errors = [error for error in (events_error, todos_error) if error is not None]
        # A half commits its data and its etags together, so neither can end up
        # describing a revision the other never saw. Committed before the raises
        # below, because a half that read cleanly must not be thrown away over
        # the other one failing: its entity would sit unavailable on frozen data
        # for as long as the broken half stays broken, and pay for the read that
        # is discarded on every poll.
        if isinstance(events, _WindowRead):
            self._window_events = events.vevents
            self._keep_rules(events.rules)
            # The window was read, its etags were not, or a write overtook them.
            # Committing the token on that would leave every etag here frozen
            # with no later poll to repair them, and each refuses the next edit.
            if events.etags is None or not self._merge_etags(
                events.etags, events.epoch
            ):
                self._etags_missed = True
        if isinstance(todos, _TodoRead):
            self._todos = todos.items
            with self.etag_lock:
                if todos.etags is None or todos.epoch != self._etag_epoch:
                    self._etags_missed = True
                else:
                    self.todo_etags = todos.etags
        for half, result, error in (
            ("events", events, events_error),
            ("todos", todos, todos_error),
        ):
            self.dead[half] = result is _DEAD
            # Rejected credentials belong in reauth however few halves saw them.
            if result is _DEAD and isinstance(error, AuthorizationError):
                raise error
        # Only when nothing at all came back. A half that keeps failing takes
        # its own entity down through HaCaldavEntity.available; failing the poll
        # over it would take the other half's entity with it, on data that then
        # freezes for as long as this one stays broken.
        if errors and len(errors) == self._halves:
            raise errors[0]
        return not errors

    @property
    def _halves(self) -> int:
        return self.capability.supports_events + self.capability.supports_todos

    async def _async_half(
        self, job: Any, supported: bool, cached: Any, half: str, empty: Any
    ) -> tuple[Any, Exception | None]:
        if not supported:
            # An empty read, not a skipped one: the caller keeps what a half
            # hands back, and a half that hands back nothing at all would leave
            # its side of the snapshot unset, which reads as never fetched and
            # runs the expensive search again on every poll for good.
            return empty, None
        try:
            result = await self.hass.async_add_executor_job(job)
        except Exception as err:  # noqa: BLE001
            # Nothing read before is nothing to fall back on: a failed poll, not
            # a stale one, or the entity would look healthy and empty forever.
            # The same goes for one that keeps failing, which serves a snapshot
            # nobody can tell is old and validates writes against it. Reported
            # rather than raised, so the other half is still fetched before the
            # poll gives up and its cache does not freeze along with this one.
            if cached is None or self._misses[half] >= _MAX_KEPT_POLLS:
                return _DEAD, err
            self._misses[half] += 1
            _LOGGER.debug("Keeping the previous result for %s: %s", self.name, err)
            return _KEEP, err
        self._misses[half] = 0
        return result, None

    def _fetch_events(self, start: datetime, end: datetime) -> Any:
        def fetch() -> _WindowRead:
            epoch = self._etag_epoch
            # Unsplit: caldav's split copies and reparses the whole expanded
            # object once per occurrence, which is quadratic in their number.
            results = self.calendar.search(
                start=start, end=end, event=True, expand=True, split_expanded=False
            )
            # Read here rather than only when the frontend asks, so a write made
            # from an automation after a restart is still conflict-checked.
            etags, rules = self._window_index(start, end)
            # DTSTART is optional in RFC 5545 once the object carries a METHOD,
            # and ordering, the window filter and the state all need one.
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

        Kept rather than replaced: a panel window the user paged to need not
        overlap the polled one, and dropping its etags would let a write from
        it overwrite a change made elsewhere without noticing. Only a uid this
        same window carried before and no longer does is gone for certain.

        False when the read is older than the last write: it holds the etag from
        before that write, and putting it back would have the user's own next
        edit of the object refused as somebody else's change.
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
        for vevent in sorted(self._window_events or [], key=sort_key):
            try:
                over = is_over(vevent)
            except _UNMAPPABLE as err:
                # Outside to_event, and reached before it, so it needs the same
                # forbearance: an end no datetime can hold fails here first.
                _LOGGER.debug("Skipping an event that cannot be placed: %s", err)
                continue
            if over or not (self.include_all_day or not is_all_day(vevent)):
                continue
            if (event := to_event(vevent, self._rule_for(vevent))) is not None:
                return vevent, event
        return None

    def _fetch_todos(self) -> list[TodoItem]:
        """Return every to-do item, completed ones included, in sort-order.

        Nothing expands a to-do, so this search can carry the etags.
        """
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
            # The list held items and the server put an etag on none of them.
            # Taken at face value that empties the cache, and the next edit of
            # every one of those items goes out with nothing to check against.
            etags=etags if etags or not found else None,
            epoch=epoch,
        )

    def _sync_changed(self) -> tuple[bool, str | None]:
        """Return (changed, token) since the last poll (RFC 6578), uncommitted.

        Any failure degrades to a full refresh.
        """
        try:
            collection = self.calendar.objects_by_sync_token(self._sync_token)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("sync-collection failed, refreshing in full: %s", err)
            # Dropped, not kept: a restored server 403s forever on a token it
            # lost.
            return True, None
        return collection.sync_token != self._sync_token, collection.sync_token

    def _window_index(
        self, start: datetime, end: datetime
    ) -> tuple[dict[str, str] | None, dict[str, str | None]]:
        """Return the etags and the rules of a window, without keeping either.

        The etags are None when the server did not say. The rules map every uid
        the window held, to its rule or to None where it has none, so a series
        that stopped recurring can be told apart from one this window never saw.

        Handed back rather than stored: the caller commits both only once the
        whole poll is known to stand, or a half whose data is discarded would
        still have left its etags describing what the next write is checked
        against.
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
            # The window held objects and the server put an etag on none of
            # them. Read as an empty window that would drop every etag kept
            # here, and leave the next edit of each of those unchecked.
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

        Failing keeps the previous colors and leaves data None until a first
        fetch succeeds, which an empty result must not be confused with: that
        one means the server reports no colors and clears them. UpdateFailed
        rather than the original error, because only that one is logged once
        instead of as a traceback on every poll.
        """
        try:
            return await self.hass.async_add_executor_job(fetch_colors, self.client)
        except Exception as err:
            # Only the type, like the event poll: a caldav error carries the
            # url it was reading and the body it got back, and this one is
            # logged at error level rather than debug.
            _LOGGER.debug("Could not read calendar colors: %s", err)
            raise UpdateFailed(
                f"Could not read calendar colors: {type(err).__name__}"
            ) from err


def sort_order(vtodo: Any) -> tuple[int, float]:
    """Return the sort key of a to-do, unordered items last.

    There is no RFC 5545 ordering property; the Apple extension is what the
    task clients that can reorder at all agree on.
    """
    # vobject lowercases property names and turns dashes into underscores.
    value = get_attr_value(vtodo, "x_apple_sort_order")
    if value is None:
        return (1, 0.0)
    try:
        position = float(value)
    except TypeError, ValueError:
        return (1, 0.0)
    # nan compares false against everything, so one item carrying it decides
    # nothing consistently and the whole list comes out in an order that
    # depends on where the sort happened to meet it. The write path already
    # refuses one; the read path has to agree, or a drag reorders around a
    # position it will not store.
    return (0, position) if isfinite(position) else (1, 0.0)


def to_todo(vtodo: Any) -> TodoItem | None:
    """Map a vobject VTODO onto a Home Assistant to-do item.

    Items without a uid or summary are skipped: Home Assistant needs both to
    address and render them. So is one dated beyond what a datetime holds once
    a zone offset reaches it, because nothing bounds a to-do search by a window
    the user could page away from, and raising would fail every poll for good.
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

    RFC 5545 3.6.1 splits the neither-property case by value type: an event
    dated with a time ends at that same time, one dated with a day lasts the
    day. A day for both would hold the entity on for twenty-four hours over
    every zero-length marker another client writes.
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
    """Return an end an expansion turned around, put back the way it was meant.

    An occurrence generated for a local time that does not exist keeps the wall
    clock of both ends, and RFC 5545 3.3.5 resolves the missing start with the
    offset from before the gap, so the start gains an hour the end does not and
    the two instants come back reversed. The wall clock is then the only thing
    they still agree on.

    Compared as instants, deliberately. Two datetimes carrying the same zone
    object compare by wall clock, which is exactly the comparison that hides
    this, and it is also what lets the occurrence through unnoticed until
    to_local converts both into some other zone and the event vanishes.

    A value still carrying no zone is left alone: to_local settles it against
    the local zone, and comparing before that only raises.
    """
    if not (isinstance(start, datetime) and isinstance(end, datetime)):
        return end
    if start.tzinfo is None or end.tzinfo is None:
        return end
    if end.astimezone(UTC) >= start.astimezone(UTC):
        return end
    if start.utcoffset() == end.utcoffset():
        # One offset on both ends, so nothing was resolved across a gap: the
        # object is simply malformed, and to_event drops it as such.
        return end
    return start + max(end.replace(tzinfo=None) - start.replace(tzinfo=None), _NOTHING)


_NOTHING = timedelta(0)


def to_local(value: datetime | date) -> datetime | date:
    """Return datetimes as local time and leave dates unchanged."""
    if isinstance(value, datetime):
        return dt_util.as_local(value)
    return value


# ArithmeticError for the OverflowError a date near the year 9999 raises as soon
# as a zone offset is added to it: a single such object, which any client may
# store, would otherwise fail every poll and take both entities of the
# collection down for as long as it sits in the window.
_UNMAPPABLE = (
    HomeAssistantError,
    ArithmeticError,
    AttributeError,
    TypeError,
    ValueError,
)


def _dated(vevent: Any) -> bool:
    """Return whether the component carries a start that is really a date.

    Present is not enough: a DTSTART written with an empty value, or with
    VALUE=TEXT or VALUE=DURATION, leaves vobject holding "" or None. Ordering
    is the one thing _next_event does outside its own guard, so such an object
    failed every poll for as long as it sat in the window, and the entity and
    the to-do list of the collection stayed unavailable with nothing in the log
    naming either the object or the property.
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
    """Return whether the event has already ended.

    An all-day event ends on a date, which cannot be compared against a
    datetime, so the comparison is made per type.
    """
    end = to_local(get_end_date(vevent))
    if isinstance(end, datetime):
        return dt_util.now() >= end
    return dt_util.now().date() >= end


def components_of(item: Any, name: str) -> list[Any]:
    """Return every named component of a search result, empty if unreadable.

    An expanded object carries one per occurrence. vobject raises on a body it
    cannot parse, and another client is free to leave one on the server; a
    single such object must not fail the whole poll.
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

    RFC 5545 leaves the order of the components of an object open, so a server
    may put a detached occurrence first. Reading the rule off that one records
    no rule for a series that has one, and drops the rule already recorded.
    """
    found = components_of(item, "vevent")
    for vevent in found:
        if not hasattr(vevent, "recurrence_id"):
            return vevent
    return found[0] if found else None


def to_event(vevent: Any, rrule: str | None = None) -> CalendarEvent | None:
    """Map a vobject VEVENT onto a Home Assistant calendar event.

    The rule is passed in because expanding a series strips RRULE from every
    occurrence it produces, and without it the panel shows no "repeats weekly"
    and opens the recurrence editor blank.

    None for an object core refuses: another client can leave an end before its
    start, and one such event must not take the whole collection down.
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
            # Home Assistant validates the rule it is handed against what its own
            # editor can offer, and FREQ=HOURLY or FREQ=MINUTELY is RFC 5545 and
            # writable from every other client. Passing it on took the whole
            # occurrence off the panel; the event is the data, the rule only
            # decides what the recurrence editor opens with.
            _LOGGER.debug("Keeping an event whose rule was refused: %s", err)
            return to_event(vevent)
        _LOGGER.debug("Skipping an event that cannot be mapped: %s", err)
        return None
