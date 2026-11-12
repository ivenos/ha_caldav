"""CalDAV write operations; the recurring-series ones live in :mod:`.recurrence`."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
import logging
from math import isfinite
import re
from typing import Any
from urllib.parse import quote, urlparse
from uuid import uuid4

import caldav
from caldav.davclient import requests
from caldav.elements import cdav, dav, ical
from caldav.lib import vcal
from caldav.lib.error import (
    AuthorizationError,
    DeleteError,
    NotFoundError,
    PutError,
    ReportError,
    errmsg,
)
from caldav.lib.namespace import ns
from dateutil.rrule import rruleset
from homeassistant.util import dt as dt_util
from icalendar import (
    Calendar as ICalCalendar,
    FreeBusy as ICalFreeBusy,
    Timezone as ICalTimezone,
    vCalAddress,
    vDDDLists,
    vRecur,
    vText,
    vUri,
)

from .connection import display_name
from .const import (
    ATTR_ALARMS,
    ATTR_PARENT_UID,
    ATTR_PERCENT_COMPLETE,
    EVENT_ATTRIBUTES,
    SORT_ORDER_PROPERTY,
)
from .errors import NETWORK_ERRORS, WRITE_ERRORS, Refused, rejected
from .extras import (
    apply_extras,
    as_datetime,
    check_expandable,
    check_rule,
    comparable_address,
    date_values,
    framed_rule,
    holders,
    mailto,
    parse_rule,
    replace,
    rule_from,
    shifted,
    start_of,
    to_utc,
)
from .task import (
    Tree,
    apply_fields,
    closed,
    names_slot,
    parent_uid,
    set_parent,
    set_status,
    status_asked,
)

_LOGGER = logging.getLogger(__name__)

# Past this many uids, one read of the whole collection costs less than a
# request per uid.
_SCAN_THRESHOLD = 8

# Left between to-do positions so an item dropped between two neighbors has
# a whole number to take.
_SORT_GAP = 1024

EXTRA_KEYS = frozenset(EVENT_ATTRIBUTES)


def object_by_uid(
    calendar: caldav.Calendar, uid: str, todo: bool | None = False
) -> Any:
    """Return the calendar object carrying this uid.

    iCloud rejects the UID filter outright, so a refused lookup falls back to
    the resource named after the uid, then to a scan. A todo of None asks for
    the uid whatever kind it is stored as.
    """
    try:
        return _filtered(calendar, uid, todo)
    except Exception as err:
        if not _uid_search_refused(err):
            raise
    if (named := _named(calendar, uid, todo)) is not None:
        return named
    for item, found in _scan(calendar, todo):
        if found == uid:
            return item
    raise NotFoundError(f"{uid} not found on server")


def _uid_search_refused(err: Exception) -> bool:
    """Return whether a failed uid lookup is one to answer with a scan.

    A refusal reaches us as anything from ReportError to TypeError; a request
    that got no answer, or a 401, is not one.
    """
    if isinstance(err, NotFoundError | requests.RequestException) or rejected(err):
        return False
    _LOGGER.debug("Server refused the uid search, scanning instead: %s", err)
    return True


def _filtered(calendar: caldav.Calendar, uid: str, todo: bool | None) -> Any:
    """Ask the server for one uid. Raises when it will not filter on them.

    One kind at a time, as _scan explains.
    """
    if todo is not None:
        return calendar.todo_by_uid(uid) if todo else calendar.event_by_uid(uid)
    for lookup in (calendar.event_by_uid, calendar.todo_by_uid):
        try:
            return lookup(uid)
        except NotFoundError:
            continue
    return calendar.journal_by_uid(uid)


_KINDS = {"VEVENT": caldav.Event, "VTODO": caldav.Todo, "VJOURNAL": caldav.Journal}


def _named(calendar: caldav.Calendar, uid: str, todo: bool | None) -> Any:
    """Return the resource named as caldav names the one of this uid, if it holds it."""
    url = calendar.url.join(_resource_name(uid))
    try:
        response = calendar.client.request(str(url))
    except Exception as err:
        if not _uid_search_refused(err):
            raise
        return None
    if response.status != 200:
        return None
    kinds = tuple(_KINDS) if todo is None else ("VTODO",) if todo else ("VEVENT",)
    try:
        held = ICalCalendar.from_ical(response.raw).subcomponents
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Leaving an unparsable object to the scan: %s", err)
        return None
    for part in held:
        if part.name in kinds and str(_first(part.get("UID", ""))) == uid:
            named = _KINDS[part.name](
                calendar.client, url=url, data=response.raw, parent=calendar
            )
            if (tag := response.headers.get("Etag")) is not None:
                named.props[dav.GetEtag.tag] = tag
            return named
    return None


def _scan(calendar: caldav.Calendar, todo: bool | None) -> Iterator[tuple[Any, str]]:
    """Yield every object of the collection with the uid it carries.

    A filtered request for each kind: a filter naming only VCALENDAR is
    answered with nothing at all by a good few servers.
    """
    tagged = {"props": [dav.GetEtag()]}
    if todo is None:
        found = [
            *calendar.search(event=True, **tagged),
            *calendar.search(todo=True, include_completed=True, **tagged),
            *calendar.search(journal=True, **tagged),
        ]
    else:
        found = (
            calendar.search(todo=True, include_completed=True, **tagged)
            if todo
            else calendar.search(event=True, **tagged)
        )
    for item in found:
        try:
            component = item.icalendar_component
        except Exception as err:  # noqa: BLE001
            # Not iCalendar at all, or a component icalendar does not know.
            _LOGGER.debug("Reading the uid of an unparsable object as text: %s", err)
            yield item, _raw_uid(item)
            continue
        if component is not None:
            # RFC 2445-era clients wrote UID twice; icalendar then holds a list.
            yield item, str(_first(component.get("UID", "")))


_UID_LINE = re.compile(r"^UID:(.*)$", re.MULTILINE)
_FOLD = re.compile(r"\r?\n[ \t]")


def _raw_uid(item: Any) -> str:
    """Return the uid straight out of an object icalendar will not parse.

    Unfolded first: RFC 5545 3.1 breaks a line past 75 octets, and the uid
    Outlook writes is 112 characters.
    """
    try:
        text = _FOLD.sub("", str(item.data))
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Could not read an object at all: %s", err)
        return ""
    match = _UID_LINE.search(text)
    return match.group(1).strip() if match else ""


def create_event(
    calendar: caldav.Calendar, data: dict[str, Any], own_address: str | None = None
) -> None:
    """Create a new event from Home Assistant event fields.

    One PUT, under a random uid: the library's own carries the MAC address.
    """
    core = {
        key: value
        for key, value in data.items()
        if key not in EXTRA_KEYS and value is not None and value != ""
    }
    extras = {key: value for key, value in data.items() if key in EXTRA_KEYS}
    if rrule := core.get("rrule"):
        parse_rule(rrule)
    instance = ICalCalendar.from_ical(
        vcal.create_ical(objtype="VEVENT", uid=str(uuid4()), **core)
    )
    vevent = next(iter(instance.walk("VEVENT")))
    if (recur := vevent.get("RRULE")) is not None:
        start = vevent["DTSTART"].dt
        replace(vevent, "rrule", framed_rule(recur, start))
        check_rule(vevent["RRULE"], start)
    if extras:
        apply_extras(vevent, extras, own_address)
    save_document(calendar, _zoned(instance, vevent), as_todo=False)


def create_todo(
    calendar: caldav.Calendar,
    data: dict[str, Any],
    own_address: str | None = None,
    as_shown: bool = True,
    addresses: list[str] | None = None,
) -> None:
    """Create a new to-do item, as a subtask if it names a parent.

    As shown is core's item, which knows two of the four states.
    """
    ics = vcal.create_ical(
        objtype="VTODO", uid=str(uuid4()), summary=data.get("summary") or ""
    )
    instance = ICalCalendar.from_ical(ics)
    vtodo = next(iter(instance.walk("VTODO")))
    if "CREATED" not in vtodo:
        vtodo.add("CREATED", datetime.now(tz=UTC))
    apply_fields(
        vtodo,
        {key: value for key, value in data.items() if key != "summary"},
        own_address,
        as_shown,
    )
    if (parent := parent_uid(vtodo)) is not None:
        by_uid, tree = _todo_tree(calendar)
        if parent not in tree.parents:
            raise Refused("parent_not_found", parent=parent)
        if not closed(vtodo):
            _reopen(by_uid, [parent, *tree.ancestors(parent)], addresses)
    save_document(calendar, _zoned(instance, vtodo), as_todo=True)


def _zoned(instance: ICalCalendar, component: Any) -> ICalCalendar:
    """Return the document with a definition for every TZID it references.

    The library writes the reference without the definition RFC 5545 wants.
    """
    return _document([component], {}, instance.get("PRODID"))


_ROLLED = frozenset({"due", "start", "status", ATTR_PERCENT_COMPLETE})

_SERIES = frozenset({"rrule", ATTR_PARENT_UID})


def update_todo(
    calendar: caldav.Calendar,
    uid: str,
    data: dict[str, Any],
    expected_etag: str | None = None,
    own_address: str | None = None,
    as_shown: bool = True,
    addresses: list[str] | None = None,
) -> None:
    """Apply changed fields to an existing to-do item.

    As shown is core's whole item, so a field it left out was cleared.
    Closing an item closes its open subtasks and reopening it reopens the
    closed parents above it, those first: a call that failed halfway
    finishes when it is made again.
    """
    if as_shown:
        data = {"summary": "", "due": None, "description": None, **data}
    quiet = only_personal(data)
    todo = object_by_uid(calendar, uid, todo=True)
    tag = held_etag(todo, expected_etag)
    vtodo, shown = _shown_of(todo)
    was_closed = closed(shown)
    # Only on the move to done: core sends the status along with every edit.
    closing = status_asked(data) == "COMPLETED" and not was_closed
    if closing and _rolled(todo, vtodo, shown):
        data = {key: value for key, value in data.items() if key not in _ROLLED}
        if shown is not vtodo:
            data = {key: value for key, value in data.items() if key in _SERIES}
            shown = vtodo
    reparented = ATTR_PARENT_UID in data and data[ATTR_PARENT_UID] != parent_uid(vtodo)
    if shown is not vtodo:
        apply_fields(vtodo, {key: data[key] for key in _SERIES if key in data})
        data = {key: value for key, value in data.items() if key != "rrule"}
    apply_fields(shown, data, own_address, as_shown)
    closing = closing or (closed(shown) and not was_closed)
    reopening = was_closed and not closed(shown)
    if (closing or reopening) and closed(vtodo) != closed(shown):
        set_status(vtodo, str(shown.get("STATUS", "NEEDS-ACTION")).upper())
    if reparented or closing or reopening:
        by_uid, tree = _todo_tree(calendar)
        parent = parent_uid(vtodo)
        if reparented and parent is not None:
            if parent not in tree.parents:
                raise Refused("parent_not_found", parent=parent)
            if parent == uid or parent in tree.descendants(uid):
                raise Refused("parent_cycle")
            if (
                not (closing or closed(shown))
                and _status(by_uid[parent]) == "COMPLETED"
            ):
                closing = True
                shown = _close(todo, vtodo, shown, "COMPLETED")
        if closing:
            canceled = str(shown.get("STATUS", "")).upper() == "CANCELLED"
            _close_below(
                by_uid,
                tree,
                uid,
                "CANCELLED" if canceled else "COMPLETED",
                addresses,
            )
        elif reopening and parent in tree.parents:
            above = [parent, *tree.ancestors(parent)]
            _reopen(by_uid, [each for each in above if each != uid], addresses)
    _stamp(vtodo, shown, addresses, quiet)
    _save_todo(todo, tag, quiet)


def _shown_of(resource: Any) -> tuple[Any, Any]:
    """Return the master of a to-do and the component the list shows for it.

    That is the override of the master's current occurrence where there is
    one, as coordinator.shown_occurrence finds it. A series that excludes its
    own start first moves on to the occurrence the list shows.
    """
    master = collapse_repeated(vtodo_of(resource))
    if _start_excluded(master):
        _roll_todo(master)
    anchor = next((master[key].dt for key in ("DTSTART", "DUE") if key in master), None)
    for component in resource.icalendar_instance.walk("VTODO"):
        held = component.get("RECURRENCE-ID")
        if (
            component is not master
            and names_slot(getattr(_first(held), "dt", None), anchor)
            and not (closed(component) and not closed(master))
        ):
            return master, collapse_repeated(component)
    return master, master


def _start_excluded(vtodo: Any) -> bool:
    """Return whether an open recurring to-do's series excludes its own start."""
    anchor_key = "DTSTART" if "DTSTART" in vtodo else "DUE"
    if closed(vtodo) or "RRULE" not in vtodo or anchor_key not in vtodo:
        return False
    anchor = vtodo[anchor_key].dt
    start = as_datetime(anchor)
    try:
        check_expandable(vtodo["RRULE"], anchor)
        occurrences = _occurrences(vtodo, rule_from(vtodo["RRULE"], anchor), start)
        occurrences.rdate(start)
        return start not in occurrences
    except Refused, TypeError, ValueError:
        return False


def _rolled(resource: Any, vtodo: Any, shown: Any) -> bool:
    """Move a recurring to-do on to its next occurrence; False if it stays.

    The override of the occurrence completed goes, as it now names one
    before the series starts.
    """
    if closed(vtodo) or "RRULE" not in vtodo or not _roll_todo(vtodo):
        return False
    if shown is not vtodo:
        instance = resource.icalendar_instance
        instance.subcomponents = [
            part for part in instance.subcomponents if part is not shown
        ]
    return True


def _close(resource: Any, vtodo: Any, shown: Any, status: str) -> Any:
    """Close a to-do, a recurring one by moving it on; return what it shows now."""
    if status == "COMPLETED" and _rolled(resource, vtodo, shown):
        return vtodo
    for part in _parts(vtodo, shown):
        if not closed(part):
            set_status(part, status)
    return shown


def _parts(vtodo: Any, shown: Any) -> tuple[Any, ...]:
    return (vtodo,) if shown is vtodo else (vtodo, shown)


def _stamp(
    vtodo: Any, shown: Any, addresses: list[str] | None, personal: bool = False
) -> None:
    for part in _parts(vtodo, shown):
        stamp(part, addresses, personal)


def _todo_tree(calendar: caldav.Calendar) -> tuple[dict[str, Any], Tree]:
    """Read a whole to-do list: the resource of every uid, and how they nest."""
    by_uid: dict[str, Any] = {}
    parents: dict[str, str | None] = {}
    for item, uid in _scan(calendar, todo=True):
        by_uid[uid] = item
        try:
            parents[uid] = parent_uid(vtodo_of(item))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Leaving an unreadable to-do out of the tree: %s", err)
    return by_uid, Tree(parents)


def _status(resource: Any) -> str:
    return str(_shown_of(resource)[1].get("STATUS", "")).upper()


def _close_below(
    by_uid: dict[str, Any],
    tree: Tree,
    uid: str,
    status: str,
    addresses: list[str] | None = None,
) -> None:
    """Close the open subtasks of an item, and theirs, the deepest first.

    What sits below a subtask that was closed already is left as it is.
    """
    kept: set[str] = set()
    open_ones = []
    for each in tree.descendants(uid):
        vtodo, shown = _shown_of(by_uid[each])
        if tree.parents[each] in kept or closed(shown):
            kept.add(each)
        else:
            open_ones.append((by_uid[each], vtodo, shown))
    for resource, vtodo, shown in reversed(open_ones):
        tag = held_etag(resource, None)
        _stamp(vtodo, _close(resource, vtodo, shown, status), addresses)
        _save_todo(resource, tag)


def _reopen(
    by_uid: dict[str, Any], uids: list[str], addresses: list[str] | None = None
) -> None:
    """Reopen the closed parents above an item, up to the first open one.

    The farthest first, so a call that failed halfway still finds the rest closed.
    """
    pending = []
    for uid in uids:
        resource = by_uid[uid]
        vtodo, shown = _shown_of(resource)
        if not closed(shown):
            break
        pending.append((resource, vtodo, shown))
    for resource, vtodo, shown in reversed(pending):
        tag = held_etag(resource, None)
        for part in _parts(vtodo, shown):
            if closed(part):
                set_status(part, "NEEDS-ACTION")
        _stamp(vtodo, shown, addresses)
        _save_todo(resource, tag)


def vtodo_of(resource: Any) -> Any:
    """Return the VTODO of a resource.

    caldav's icalendar_component is the first non-timezone component, and a
    resource may hold a VEVENT or a detached instance ahead of the series.
    """
    found = list(resource.icalendar_instance.walk("VTODO"))
    if not found:
        raise Refused("no_todo_in_object")
    return next((item for item in found if "RECURRENCE-ID" not in item), found[0])


# RFC 5545 3.6.1 and 3.6.2 allow each of these once; RFC 2445-era clients
# wrote some twice, and icalendar then hands back a list.
_SINGLE = (
    "COMPLETED",
    "DTEND",
    "DTSTART",
    "DUE",
    "DURATION",
    "PERCENT-COMPLETE",
    "RECURRENCE-ID",
    "RRULE",
    "SEQUENCE",
    "STATUS",
    "UID",
)


def collapse_repeated(component: Any) -> Any:
    """Reduce every repeated single-value property of a component to its first."""
    for key in _SINGLE:
        if isinstance(value := component.get(key), list) and value:
            _LOGGER.debug("Keeping the first of %s repeated %s", len(value), key)
            component[key] = value[0]
    return component


def _save_todo(todo: Any, tag: str | None, quiet: bool = False) -> None:
    """Write a to-do back to the resource it was read from."""
    # A due date may reference a zone the stored item never defined.
    todo.icalendar_instance = zoned_document(todo.icalendar_instance)
    put_resource(todo, tag, quiet=quiet)


def reorder_todos(calendar: caldav.Calendar, uids: list[str]) -> None:
    """Write the given order onto the items.

    Core sends the whole ordering for a single drag, so the moved item is
    picked out and given a number between its new neighbors; renumbering
    everything would be a PUT per item.
    """
    by_uid = {uid: item for item, uid in _scan(calendar, todo=True)}
    # An item deleted between the drag and this write is nothing to order.
    known = [uid for uid in uids if uid in by_uid]
    positions = {uid: _sort_position(vtodo_of(by_uid[uid])) for uid in known}
    stored = _stored_order(known, positions)
    if stored == known:
        return
    if (
        stored is not None
        and (moved := _single_move(stored, known)) is not None
        and (place := _between(known, positions, moved)) is not None
    ):
        _write_position(by_uid[moved], place)
        return
    # Numbered with room between them, so the next drag has somewhere to land.
    for index, uid in enumerate(known):
        # Compared numerically: a server-written "03" is 3.
        if positions[uid] != float(index * _SORT_GAP):
            _write_position(by_uid[uid], index * _SORT_GAP)


def _stored_order(
    known: list[str], positions: dict[str, float | None]
) -> list[str] | None:
    """Return the order the stored positions give, or None if it is ambiguous.

    Two items on one number are separated by whatever the server listed
    first, which no tie-break here reproduces.
    """
    if any(position is None for position in positions.values()):
        return None
    stored = list(positions.values())
    if len(set(stored)) != len(stored):
        return None
    return sorted(known, key=lambda uid: (positions[uid], uid))


def _single_move(now: list[str], wanted: list[str]) -> str | None:
    """Return the one uid whose move turns the stored order into this one."""
    candidates = dict.fromkeys(
        side[index]
        for index in range(len(now))
        for side in (wanted, now)
        if now[index] != wanted[index]
    )
    for candidate in candidates:
        if [uid for uid in now if uid != candidate] == [
            uid for uid in wanted if uid != candidate
        ]:
            return candidate
    return None


def _between(
    wanted: list[str], positions: dict[str, float | None], moved: str
) -> int | None:
    """Return a whole number strictly between the moved item's new neighbors."""
    index = wanted.index(moved)
    low = int(positions[wanted[index - 1]]) if index else None
    high = int(positions[wanted[index + 1]]) if index + 1 < len(wanted) else None
    if low is None:
        return 0 if high is None else high - _SORT_GAP
    if high is None:
        return low + _SORT_GAP
    place = (low + high) // 2
    return place if low < place < high else None


def _write_position(todo: Any, position: int) -> None:
    tag = held_etag(todo, None)
    vtodo = collapse_repeated(vtodo_of(todo))
    vtodo[SORT_ORDER_PROPERTY] = str(position)
    stamp(vtodo, personal=True)
    _save_todo(todo, tag, quiet=True)


def _sort_position(vtodo: Any) -> float | None:
    """Return the stored sort position, or None where there is no usable one.

    int(inf) raises OverflowError, which is not a ValueError.
    """
    try:
        position = float(str(vtodo[SORT_ORDER_PROPERTY]))
    except KeyError, TypeError, ValueError:
        return None
    return position if isfinite(position) else None


def delete_components(
    resource: Any, kind: str, tag: str | None = None, quiet: bool = False
) -> None:
    """Remove the components of one kind, and the resource once none is left.

    A resource may hold a VEVENT beside a VTODO under one uid, and both
    halves of the collection then list their own.
    """
    instance = resource.icalendar_instance
    kept = [item for item in instance.subcomponents if item.name != kind]
    if not any(item.name != "VTIMEZONE" for item in kept):
        delete_resource(resource, tag, quiet)
        return
    _LOGGER.debug("Removing the %s of a resource that holds more than one kind", kind)
    instance.subcomponents = kept
    # A zone only the removed component referenced must not stay behind.
    resource.icalendar_instance = zoned_document(instance)
    put_resource(resource, tag, quiet=quiet)


def delete_todos(
    calendar: caldav.Calendar, uids: list[str], etags: dict[str, str]
) -> None:
    """Delete to-do items and their subtasks, every etag checked first."""
    by_uid, tree = _todo_tree(calendar)
    doomed: dict[str, None] = {}
    for uid in uids:
        if uid not in by_uid:
            raise NotFoundError(f"{uid} not found on server")
        # The deepest first, so what a failure leaves behind still has its parent.
        doomed.update(dict.fromkeys([*reversed(tree.descendants(uid)), uid]))
    tags = {uid: held_etag(by_uid[uid], etags.get(uid)) for uid in doomed}
    for uid in doomed:
        delete_components(by_uid[uid], "VTODO", tags[uid])


def move_todo(
    source: caldav.Calendar,
    target: caldav.Calendar,
    uid: str,
    keep_original: bool,
) -> None:
    """Copy a to-do and its subtasks to another list, optionally removing them here.

    The parent it had stays behind, so there it is a top-level item.
    """
    by_uid, tree = _todo_tree(source)
    if uid not in by_uid:
        raise NotFoundError(f"{uid} not found on server")
    moved = [uid, *tree.descendants(uid)]
    tags = {each: held_etag(by_uid[each], None) for each in moved}
    # RFC 4791 gives one uid one resource, and caldav names it after the uid.
    if clashing := sorted(_already_there(target, set(moved))):
        raise Refused("uid_clash", uids=", ".join(clashing[:5]))
    copies: dict[str, Any] = {}
    try:
        for each in moved:
            instance = ICalCalendar.from_ical(by_uid[each].data)
            vtodos = instance.walk("VTODO")
            if not vtodos:
                raise Refused("no_todo_in_object")
            if each == uid:
                for vtodo in vtodos:
                    set_parent(vtodo, None)
            zones = {
                str(zone.get("TZID", "")): zone for zone in instance.walk("VTIMEZONE")
            }
            copies[each] = _save_new(
                target,
                _document(vtodos, zones, instance.get("PRODID")),
                as_todo=True,
                quiet=True,
            )
    except Exception:
        for copy in copies.values():
            _undo(copy, quiet=not keep_original)
        raise
    if keep_original:
        return
    pending = list(moved)
    try:
        while pending:
            delete_components(
                by_uid[pending[-1]], "VTODO", tags[pending[-1]], quiet=True
            )
            pending.pop()
    except WRITE_ERRORS:
        # The server may have deleted it before the timeout.
        for each in pending:
            if _still_there(source, each, todo=True):
                _undo(copies[each], quiet=True)
        raise


def _comparable_etag(value: Any) -> str:
    """Return an etag without the weak marker, for RFC 7232 2.3.2 comparison.

    A proxy may add or drop the W/ between the read and the check.
    """
    return str(value).strip().removeprefix("W/")


def held_etag(resource: Any, expected_etag: str | None) -> str | None:
    """Return the etag a write back has to match, refusing a stale expected one.

    It comes with the read that found the resource, or else from a GET. RFC
    9110 13.1.1 has If-Match compare strongly, and a proxy may weaken a GET's.
    """
    held = resource.props.get(dav.GetEtag.tag)
    if held is None:
        resource.load()
        held = resource.props.get(dav.GetEtag.tag)
    if (
        held is not None
        and expected_etag is not None
        and _comparable_etag(held) != _comparable_etag(expected_etag)
    ):
        raise Refused("etag_conflict")
    for tag in (held, expected_etag):
        if tag is not None and not str(tag).strip().startswith("W/"):
            return str(tag).strip()
    _LOGGER.debug("No strong etag on the stored object; writing without If-Match")
    return None


_CALENDAR_TYPE = 'text/calendar; charset="utf-8"'


def put_resource(
    resource: Any,
    tag: str | None = None,
    new: str | None = None,
    quiet: bool = False,
) -> None:
    """Write a resource's document over the version tagged, or as a new uid.

    caldav's own save sends neither If-Match nor If-None-Match. Quiet asks the
    server to notify no one, as for one half of a move.
    """
    headers = {"Content-Type": _CALENDAR_TYPE, **_quiet(quiet)}
    if tag is not None:
        headers["If-Match"] = tag
    if new is not None:
        headers["If-None-Match"] = "*"
    try:
        response = resource.client.put(str(resource.url), resource.data, headers)
    except AuthorizationError as err:
        # caldav before 3.5 drops the body of a 403.
        if _UNSUPPORTED_KIND in getattr(err, "preconditions", ()):
            raise _not_held(resource) from err
        raise
    if response.status == 412 and new is not None:
        raise Refused("uid_clash", uids=new)
    if response.status == 412:
        raise Refused("etag_conflict")
    if response.status not in (200, 201, 204):
        raise PutError(errmsg(response))


# RFC 4791 5.3.2.1, for a component the collection does not hold.
_UNSUPPORTED_KIND = ns("C", "supported-calendar-component")

_NOT_HELD = {"VEVENT": "no_events", "VTODO": "no_todos", "VJOURNAL": "no_journals"}


def _not_held(resource: Any) -> Refused:
    """Return the refusal for a kind of item the calendar does not hold."""
    kind = next(
        (
            item.name
            for item in resource.icalendar_instance.subcomponents
            if item.name in _NOT_HELD
        ),
        "VEVENT",
    )
    parent = getattr(resource, "parent", None)
    name = display_name(parent) if parent is not None else "CalDAV"
    return Refused(_NOT_HELD[kind], name=name)


def delete_resource(resource: Any, tag: str | None = None, quiet: bool = False) -> None:
    """Delete a resource, only the version tagged.

    caldav takes a 404 for success, which hides a url that missed the object.
    """
    headers = _quiet(quiet) if tag is None else {"If-Match": tag, **_quiet(quiet)}
    response = resource.client.request(str(resource.url), "DELETE", "", headers)
    if response.status == 404:
        raise NotFoundError(errmsg(response))
    if response.status == 412:
        raise Refused("etag_conflict")
    if response.status not in (200, 202, 204):
        raise DeleteError(errmsg(response))


def _quiet(quiet: bool) -> dict[str, str]:
    # RFC 6638 8.1, which sabre also applies to the organizer.
    return {"Schedule-Reply": "F"} if quiet else {}


def stamp(
    component: Any, addresses: list[str] | None = None, personal: bool = False
) -> None:
    """Mark a component as revised now.

    RFC 5545 3.8.7.2: without a METHOD, DTSTAMP is the time of the last
    revision. 3.8.7.4 lets only the organizer move SEQUENCE, and a personal
    change is no new version for the attendees.
    """
    now = datetime.now(tz=UTC)
    changes: list[tuple[str, Any]] = [("DTSTAMP", now), ("LAST-MODIFIED", now)]
    if not personal and not organized_elsewhere(component, addresses):
        changes.append(("SEQUENCE", int(component.get("SEQUENCE", 0)) + 1))
    for key, value in changes:
        if key in component:
            del component[key]
        component.add(key, value)


def organized_elsewhere(component: Any, addresses: list[str] | None) -> bool:
    """Return whether the component names an organizer who is not this account."""
    if not addresses or (organizer := component.get("ORGANIZER")) is None:
        return False
    own = {comparable_address(address) for address in addresses}
    return comparable_address(str(_first(organizer))) not in own


_PERSONAL = frozenset({ATTR_ALARMS})


def only_personal(data: dict[str, Any]) -> bool:
    """Return whether an edit changes only what is the account's own, its alarms."""
    return bool(data) and data.keys() <= _PERSONAL


def move_event(
    source: caldav.Calendar,
    target: caldav.Calendar,
    uid: str,
    keep_original: bool,
    addresses: list[str] | None = None,
) -> None:
    """Copy an event to another calendar, optionally removing the original.

    Nextcloud and Radicale disagree on cross-collection COPY and MOVE. A copy
    tells no one, and an invitation from someone else is only moved.
    """
    event = object_by_uid(source, uid)
    tag = held_etag(event, None)
    instance = ICalCalendar.from_ical(event.data)
    if not (vevents := instance.walk("VEVENT")):
        raise Refused("no_event_in_object")
    if keep_original and any(
        organized_elsewhere(vevent, addresses) for vevent in vevents
    ):
        raise Refused("invitation_copied")
    # RFC 4791 gives one uid one resource, and caldav names it after the uid.
    if _already_there(target, {uid}):
        raise Refused("uid_clash", uids=uid)
    if not keep_original and _moved(event, target, tag):
        return
    copy = _save_new(target, as_written(instance), as_todo=False, quiet=True)
    if keep_original:
        return
    try:
        delete_resource(event, tag, quiet=True)
    except WRITE_ERRORS as err:
        # The server may have deleted it before the timeout.
        if _still_there(source, uid):
            _undo(copy, quiet=True)
            raise
        # SOGo moves an invitation by itself once its uid lands in another calendar.
        if not isinstance(err, NotFoundError):
            raise


def _moved(resource: Any, target: caldav.Calendar, tag: str | None) -> bool:
    """Move a resource by RFC 4918 9.9 MOVE; False where the server will not.

    SOGo reads the DELETE of a copy and delete as a cancellation to every attendee.
    """
    if resource.client is not target.client:
        return False
    name = str(resource.url).rstrip("/").rsplit("/", 1)[-1]
    headers = {"Destination": str(target.url.join(name)), "Overwrite": "F"}
    if tag is not None:
        headers["If-Match"] = tag
    try:
        response = resource.client.request(str(resource.url), "MOVE", "", headers)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("No MOVE between the calendars: %s", err)
        return False
    return response.status in (201, 204)


def _still_there(calendar: caldav.Calendar, uid: str, todo: bool = False) -> bool:
    """Return whether the uid is certainly still on the calendar."""
    try:
        object_by_uid(calendar, uid, todo)
    except NETWORK_ERRORS:
        return False
    return True


def _undo(resource: Any, quiet: bool = False) -> None:
    """Remove a resource this call wrote before failing further on."""
    try:
        delete_resource(resource, quiet=quiet)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not undo a half-finished write: %s", err)


def _save_new(
    calendar: caldav.Calendar,
    document: ICalCalendar,
    as_todo: bool,
    quiet: bool = False,
) -> Any:
    """Write a new resource, removing it again if it landed but the answer did not."""
    try:
        return save_document(calendar, document, as_todo, quiet=quiet)
    except NETWORK_ERRORS:
        head = next(item for item in document.subcomponents if item.name != "VTIMEZONE")
        with suppress(*NETWORK_ERRORS):
            _undo(object_by_uid(calendar, str(_first(head["UID"])), None), quiet)
        raise


def save_document(
    calendar: caldav.Calendar,
    document: ICalCalendar,
    as_todo: bool,
    overwrite: bool = False,
    quiet: bool = False,
) -> Any:
    """Write a whole document to a collection as a resource named after its uid.

    As a new one unless told to overwrite. The document, not its text, which
    caldav would put through vcal.fix.
    """
    head = next(item for item in document.subcomponents if item.name != "VTIMEZONE")
    if not (uid := str(_first(head.get("UID", "")))):
        raise Refused("document_no_uid")
    objclass = caldav.Todo if as_todo else caldav.Event
    stored = objclass(
        calendar.client,
        url=calendar.url.join(_resource_name(uid)),
        data=document,
        parent=calendar,
    )
    put_resource(stored, new=None if overwrite else uid, quiet=quiet)
    return stored


def _resource_name(uid: str) -> str:
    """Return the name caldav gives the resource of a uid.

    Some servers read an encoded slash as a separator, hence the double quoting.
    """
    return quote(uid.replace("/", "%2F")) + ".ics"


def _first(value: Any) -> Any:
    """Return one value where a repeated property left icalendar holding a list."""
    return value[0] if isinstance(value, list) and value else value


def _tzids(component: Any) -> set[str]:
    found: set[str] = set()
    for values in component.values():
        for value in values if isinstance(values, list) else [values]:
            if tzid := getattr(value, "params", {}).get("TZID"):
                found.add(str(tzid))
    # An alarm's TRIGGER may name a zone the event body never mentions.
    for sub in getattr(component, "subcomponents", []):
        found |= _tzids(sub)
    return found


def _timezone(tzid: str, known: dict[str, Any]) -> Any | None:
    """Return the VTIMEZONE for a TZID, generating one the source did not carry."""
    if (zone := known.get(tzid)) is not None:
        return zone
    try:
        # icalendar stops the transitions in 2038 unless told otherwise.
        zone = ICalTimezone.from_tzid(tzid, last_date=date(2100, 1, 1))
        return _one_date_per_line(zone)
    except Exception as err:  # noqa: BLE001
        # An unknown or Windows-style TZID has no definition to generate.
        _LOGGER.debug("No timezone definition for %s: %s", tzid, err)
        return None


def _one_date_per_line(zone: Any) -> Any:
    """Return a generated VTIMEZONE whose transitions survive being read back.

    icalendar writes every transition on one RDATE line, of which vobject
    keeps only the first value; RFC 5545 3.8.5.2 allows a line per date.
    """
    for observance in zone.subcomponents:
        entry = observance.get("RDATE")
        holders = getattr(entry, "dts", None)
        if holders is not None and len(holders) > 1:
            observance["RDATE"] = [vDDDLists([holder.dt]) for holder in holders]
    return zone


# Ours to set, or forbidden on a stored object (RFC 4791 4.1). Everything else
# is carried over, X-CALENDARSERVER-ACCESS above all.
_OUR_PROPERTIES = frozenset({"PRODID", "VERSION", "METHOD"})


def _document(
    components: list[Any],
    zones: dict[str, Any],
    prodid: Any,
    carried: Any = None,
) -> ICalCalendar:
    """Wrap components in a VCALENDAR carrying each needed VTIMEZONE once.

    RFC 5545 allows one definition per TZID.
    """
    document = ICalCalendar()
    document.add("prodid", prodid or "-//ha_caldav//EN")
    document.add("version", "2.0")
    for name, value in (carried or {}).items():
        if str(name).upper() not in _OUR_PROPERTIES:
            document.add(name, value)
    for tzid in sorted({tzid for item in components for tzid in _tzids(item)}):
        if (zone := _timezone(tzid, zones)) is not None:
            document.add_component(zone)
    for component in components:
        document.add_component(as_written(component))
    return document


def as_written(component: Any) -> Any:
    """Return a component whose URI-valued properties go out as they came in.

    icalendar 6 reads a property it does not know as TEXT whatever its VALUE
    says, and escapes the comma of a geo: URI on the way out.
    """
    for name in list(component):
        held = component[name]
        values = held if isinstance(held, list) else [held]
        if not any(_misread(value) for value in values):
            continue
        fixed = [_uri(value) if _misread(value) else value for value in values]
        component[name] = fixed if isinstance(held, list) else fixed[0]
    for sub in component.subcomponents:
        as_written(sub)
    return component


def _misread(value: Any) -> bool:
    params = getattr(value, "params", {})
    return isinstance(value, vText) and str(params.get("VALUE", "")).upper() == "URI"


def _uri(value: Any) -> Any:
    uri = vUri(str(value))
    uri.params = value.params
    return uri


def zoned_document(instance: ICalCalendar) -> ICalCalendar:
    """Return a document defining every TZID its own components reference.

    An edit can introduce a zone the stored object never carried; without the
    definition, lenient servers store a time other clients read as floating.
    """
    zones = {str(zone.get("TZID", "")): zone for zone in instance.walk("VTIMEZONE")}
    parts = [item for item in instance.subcomponents if item.name != "VTIMEZONE"]
    return _document(parts, zones, instance.get("PRODID"), carried=instance)


_IMPORTED = ("VEVENT", "VTODO", "VJOURNAL")


def import_ics(
    calendar: caldav.Calendar, ics: str, held: frozenset[str] | None = None
) -> list[str]:
    """Write every event, to-do and journal entry in a document to the calendar.

    Components sharing a uid are one resource (RFC 4791). A uid already on
    the calendar aborts the whole import. Nobody the document names is invited.
    """
    if not ics.strip():
        raise Refused("document_empty")
    try:
        document = ICalCalendar.from_ical(ics)
    except ValueError as err:
        # icalendar gives up on a line that has no name and value at all.
        lines = [line for line in ics.splitlines() if line.strip() and ":" not in line]
        if not lines:
            raise
        raise Refused("unreadable_lines", lines=", ".join(lines[:5])) from err
    if unreadable := _unreadable(document):
        raise Refused("unreadable_lines", lines=", ".join(unreadable))
    zones = {str(zone.get("TZID", "")): zone for zone in document.walk("VTIMEZONE")}
    groups: dict[str, list[Any]] = {}
    for component in document.walk():
        if component.name not in _IMPORTED:
            continue
        uid = str(_first(component.get("UID", "")))
        if not uid:
            raise Refused("document_no_uid")
        if (recur := component.get("RRULE")) is not None and "DTSTART" in component:
            check_rule(_first(recur), component["DTSTART"].dt)
        groups.setdefault(uid, []).append(component)
    if not groups:
        raise Refused("document_empty")
    for uid, components in groups.items():
        # RFC 5545 3.8.4.4 allows one component without RECURRENCE-ID per uid,
        # and RFC 4791 4.1 one kind of component per resource.
        masters = [item for item in components if "RECURRENCE-ID" not in item]
        if len(masters) > 1 or len({item.name for item in components}) > 1:
            raise Refused("duplicate_uid", uid=uid)
    if held is not None:
        for components in groups.values():
            if (kind := components[0].name) not in held:
                raise Refused(_NOT_HELD[kind], name=display_name(calendar))
    as_todo = {uid: components[0].name == "VTODO" for uid, components in groups.items()}
    if clashing := sorted(_already_there(calendar, set(as_todo))):
        raise Refused("uid_clash", uids=", ".join(clashing[:5]))
    written: list[Any] = []
    try:
        for uid, components in groups.items():
            written.append(
                _save_new(
                    calendar,
                    _document(components, zones, document.get("PRODID")),
                    as_todo[uid],
                    quiet=True,
                )
            )
    except Exception:
        # Objects left behind would be refused as clashes on the retry.
        for stored in written:
            _undo(stored, quiet=True)
        raise
    return list(groups)


def _unreadable(document: ICalCalendar) -> list[str]:
    """Return the properties icalendar could not parse and left out.

    It records them on the component rather than raising.
    """
    return sorted(
        {name for component in document.walk() for name, _ in component.errors}
    )


def _already_there(calendar: caldav.Calendar, wanted: set[str]) -> set[str]:
    """Return which of these uids the calendar holds, as either kind.

    RFC 4791 gives one uid one resource, so an event would overwrite a to-do.
    """
    if len(wanted) > _SCAN_THRESHOLD:
        return _scanned(calendar, wanted)
    pending = set(wanted)
    found: set[str] = set()
    for uid in wanted:
        try:
            _filtered(calendar, uid, None)
        except Exception as err:
            if isinstance(err, NotFoundError):
                pending.discard(uid)
                continue
            if not _uid_search_refused(err):
                raise
            return found | _scanned(calendar, pending)
        pending.discard(uid)
        found.add(uid)
    return found


def _scanned(calendar: caldav.Calendar, wanted: set[str]) -> set[str]:
    """Return which of these uids a full read of the collection turns up."""
    return {uid for _, uid in _scan(calendar, None) if uid in wanted}


def export_ics(calendar: caldav.Calendar, uid: str | None) -> str:
    """Return one object, or the whole calendar, as an iCalendar document."""
    if uid is not None:
        body = str(object_by_uid(calendar, uid, todo=None).data)
        # caldav normalizes what it read to LF; RFC 5545 3.1 wants CRLF.
        return body.replace("\r\n", "\n").replace("\n", "\r\n")
    components: list[Any] = []
    zones: dict[str, Any] = {}
    seen: set[str] = set()
    for item in (
        *calendar.search(event=True),
        *calendar.search(todo=True, include_completed=True),
        *calendar.search(journal=True),
    ):
        # A resource holding both kinds answers both filters.
        url = str(getattr(item, "url", "") or "")
        if url and url in seen:
            continue
        seen.add(url)
        try:
            instance = item.icalendar_instance
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Leaving an object out of the export: %s", err)
            continue
        if unreadable := _unreadable(instance):
            _LOGGER.warning("Exporting %s without its %s lines", url, unreadable)
        subcomponents = instance.subcomponents
        for component in subcomponents:
            if component.name == "VTIMEZONE":
                zones.setdefault(str(component.get("TZID", "")), component)
            else:
                components.append(component)
    return _document(components, zones, None).to_ical().decode("utf-8")


def free_busy(calendar: caldav.Calendar, start: datetime, end: datetime) -> Any:
    """Ask a calendar when it is busy, by RFC 4791 7.10."""
    try:
        return calendar.freebusy_request(start, end)
    except ReportError as err:
        # caldav puts the status line where the url belongs; SOGo answers 501.
        if str(err.url or "").startswith(("405", "501")):
            raise Refused("no_busy_query") from err
        raise


def busy_periods(instance: Any) -> list[dict[str, str]]:
    """Flatten the FREEBUSY lines of a VFREEBUSY into start/end pairs.

    A period's second half is an end or a duration, and icalendar hands back
    a bare value for one period and a list for several.
    """
    periods = []
    for component in instance.walk("VFREEBUSY"):
        entries = component.get("FREEBUSY")
        if entries is None:
            # Radicale answers with a VFREEBUSY per busy span, FBTYPE on it.
            # Without FBTYPE, DTSTART and DTEND are the range asked about.
            if str(component.get("FBTYPE", "")).upper().startswith("BUSY") and (
                span := _span_of(component)
            ):
                periods.append(span)
            continue
        for entry in entries if isinstance(entries, list) else [entries]:
            value = getattr(entry, "dt", None)
            if not isinstance(value, tuple) or len(value) != 2:
                continue
            start, second = value
            end = second if isinstance(second, datetime) else start + second
            periods.append({"start": start.isoformat(), "end": end.isoformat()})
    return periods


def _span_of(component: Any) -> dict[str, str] | None:
    start = getattr(component.get("DTSTART"), "dt", None)
    end = getattr(component.get("DTEND"), "dt", None)
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return None
    return {"start": start.isoformat(), "end": end.isoformat()}


def attendee_free_busy(
    client: caldav.DAVClient,
    start: datetime,
    end: datetime,
    attendees: list[str],
    addresses: list[str],
) -> dict[str, list[dict[str, str]] | None]:
    """Ask the server when other people are busy, by RFC 6638 5.

    None for somebody the server could not answer for, which is not the
    same as free. sabre/dav takes the request only from an organizer spelled
    the way it lists the account's own addresses, a path left relative.
    """
    organizer = next(
        (address for address in addresses if address.lower().startswith("mailto:")),
        urlparse(addresses[0]).path or addresses[0],
    )
    request = ICalCalendar()
    request.add("prodid", "-//ha_caldav//EN")
    request.add("version", "2.0")
    request.add("method", "REQUEST")
    query = ICalFreeBusy()
    query.add("uid", str(uuid4()))
    query.add("dtstamp", datetime.now(tz=UTC))
    query.add("dtstart", start.astimezone(UTC))
    query.add("dtend", end.astimezone(UTC))
    query.add("organizer", vCalAddress(organizer), encode=False)
    for attendee in attendees:
        query.add("attendee", vCalAddress(mailto(attendee)), encode=False)
    request.add_component(query)
    response = client.post(
        _outbox(client), request.to_ical(), {"Content-Type": _CALENDAR_TYPE}
    )
    if response.status != 200 or response.tree is None:
        raise PutError(errmsg(response))
    answered: dict[str, list[dict[str, str]] | None] = {}
    for entry in response.tree.iter(ns("C", "response")):
        # SOGo writes the address into the recipient itself, without an href.
        named = entry.find(ns("C", "recipient"))
        recipient = "" if named is None else "".join(named.itertext()).strip()
        data = entry.findtext(ns("C", "calendar-data"))
        status = entry.findtext(ns("C", "request-status")) or ""
        periods = None
        if data and status.startswith("2"):
            periods = busy_periods(ICalCalendar.from_ical(data))
        answered[comparable_address(recipient)] = periods
    return {
        attendee: answered.get(comparable_address(attendee)) for attendee in attendees
    }


def _outbox(client: caldav.DAVClient) -> str:
    """Return where the account posts scheduling requests, by RFC 6638 2.1.

    Asked for directly: caldav logs an error with a traceback before it
    reports a server that has none.
    """
    principal = client.principal()
    response = principal.get_properties(
        [cdav.ScheduleOutboxURL()], parse_response_xml=False
    )
    for holder in response.tree.iter(cdav.ScheduleOutboxURL.tag):
        if href := holder.findtext(dav.Href.tag):
            return str(principal.url.join(href))
    raise Refused("no_scheduling")


def set_calendar_color(calendar: caldav.Calendar, color: str) -> None:
    """Write the calendar color back to the server."""
    calendar.set_properties([ical.CalendarColor(color)])


def set_calendar_name(calendar: caldav.Calendar, name: str) -> None:
    """Write the display name of a calendar back to the server."""
    calendar.set_properties([dav.DisplayName(name)])


def create_calendar(
    client: caldav.DAVClient, name: str, components: list[str] | None
) -> caldav.Calendar:
    """Create a calendar collection on the account."""
    return client.principal().make_calendar(
        name=name,
        cal_id=str(uuid4()),
        supported_calendar_component_set=components or None,
    )


def delete_calendar(calendar: caldav.Calendar) -> None:
    """Remove a calendar collection from the account."""
    calendar.delete()


def _roll_todo(vtodo: Any) -> bool:
    """Advance a recurring to-do to its next occurrence; False if none remains.

    The series keeps the occurrences it had, less the one completed.
    """
    anchor_key = "DTSTART" if "DTSTART" in vtodo else "DUE"
    if anchor_key not in vtodo:
        return False
    anchor = vtodo[anchor_key].dt
    start = as_datetime(anchor)
    zone = anchor.tzinfo if isinstance(anchor, datetime) else None
    rrule = vtodo["RRULE"]
    try:
        rule = rule_from(rrule, anchor)
        occurrences = _occurrences(vtodo, rule, start)
        following = occurrences.after(start)
        if following is None:
            return False
        if following in rule:
            recur = _counted_down(rrule, rule, start, following)
        else:
            following, recur = _onto_added_date(
                vtodo, anchor, rule, occurrences, following
            )
    except TypeError, ValueError:
        # A rule malformed beyond what rule_from reconciles, or a date that
        # does not compare with the start.
        return False
    if recur is None:
        return False
    delta = _wall_delta(start, following)
    for key in ("DTSTART", "DUE"):
        if key in vtodo:
            replace(vtodo, key.lower(), shifted(vtodo[key].dt, delta, zone))
    if recur is not rrule:
        replace(vtodo, "rrule", recur)
    _drop_dates_through(vtodo, start)
    vtodo["STATUS"] = "NEEDS-ACTION"
    for done in ("COMPLETED", "PERCENT-COMPLETE"):
        vtodo.pop(done, None)
    return True


def _counted_down(
    rrule: Any, rule: Any, start: datetime, following: datetime
) -> Any | None:
    """Return the rule as counted from a later slot of its own, None if none is left."""
    if not (count := rrule.get("COUNT")):
        return rrule
    # COUNT counts the rule's own slots, the excluded ones included.
    passed = sum(1 for slot in rule.between(start, following, inc=True) if slot > start)
    if count[0] - passed < 1:
        return None
    reduced = vRecur(dict(rrule))
    reduced["COUNT"] = [count[0] - passed]
    return reduced


_COMPARED_OCCURRENCES = 50


def _onto_added_date(
    vtodo: Any, anchor: Any, rule: Any, occurrences: rruleset, added: datetime
) -> tuple[datetime, Any | None]:
    """Return where a series goes on from an RDATE its rule lacks, and with what rule.

    RFC 5545 3.8.5.3 leaves a DTSTART off its rule undefined, so the rule
    spells out what it took from the old start, an UNTIL stands in for COUNT,
    and the date stays an RDATE. A period an INTERVAL skips cannot hold the
    start without moving the rule's phase; the series then goes on at the
    next date of its rule, the added date left as an RDATE before it.
    """
    start = as_datetime(anchor)
    rrule = vtodo["RRULE"]
    zone = anchor.tzinfo if isinstance(anchor, datetime) else None
    moved = shifted(anchor, _wall_delta(start, added), zone)
    pinned = framed_rule(_pinned(rrule, rule, start, added, anchor), moved)
    expected = list(occurrences.xafter(start, count=_COMPARED_OCCURRENCES))
    candidate = _occurrences(vtodo, rule_from(pinned, moved), start)
    if list(candidate.xafter(start, count=_COMPARED_OCCURRENCES)) == expected:
        return added, pinned
    regular = next((when for when in occurrences.xafter(start) if when in rule), None)
    if regular is None:
        return added, pinned
    return regular, _counted_down(rrule, rule, start, regular)


_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_DAY_PARTS = ("BYWEEKNO", "BYYEARDAY", "BYMONTHDAY", "BYDAY")
_FINER_THAN = {
    "BYHOUR": ("hour", {"YEARLY", "MONTHLY", "WEEKLY", "DAILY"}),
    "BYMINUTE": ("minute", {"YEARLY", "MONTHLY", "WEEKLY", "DAILY", "HOURLY"}),
    "BYSECOND": (
        "second",
        {"YEARLY", "MONTHLY", "WEEKLY", "DAILY", "HOURLY", "MINUTELY"},
    ),
}


def _pinned(
    rrule: Any, rule: Any, start: datetime, added: datetime, anchor: Any
) -> Any:
    """Return the rule with the parts it took from its start written out.

    RFC 5545 3.3.10 fills what a rule leaves open from DTSTART, as dateutil
    does: the weekday, the day and month together, each part of the time.
    """
    pinned = vRecur(dict(rrule))
    here = added.astimezone(start.tzinfo) if start.tzinfo else added
    freq = str(rrule["FREQ"][0]).upper()
    if not any(part in rrule for part in _DAY_PARTS):
        if freq == "WEEKLY" and here.weekday() != start.weekday():
            pinned["BYDAY"] = [_WEEKDAYS[start.weekday()]]
        elif freq == "MONTHLY" and here.day != start.day:
            pinned["BYMONTHDAY"] = [start.day]
        elif freq == "YEARLY" and (here.month, here.day) != (start.month, start.day):
            pinned["BYMONTHDAY"] = [start.day]
            pinned.setdefault("BYMONTH", [start.month])
    if isinstance(anchor, datetime):
        for part, (name, coarser) in _FINER_THAN.items():
            held = getattr(start, name)
            if part not in rrule and freq in coarser and getattr(here, name) != held:
                pinned[part] = [held]
    if rrule.get("COUNT"):
        del pinned["COUNT"]
        last = deque(rule, maxlen=1)
        pinned["UNTIL"] = [last[0] if last else start]
    return pinned


def _wall_delta(start: datetime, following: datetime) -> timedelta:
    """Return how far the wall clock of the start moves to reach a later moment."""
    if start.tzinfo is None:
        return following - start
    return following.astimezone(start.tzinfo).replace(tzinfo=None) - start.replace(
        tzinfo=None
    )


def _drop_dates_through(vtodo: Any, moment: datetime) -> None:
    """Remove the RDATE values up to and including the occurrence completed."""
    if "RDATE" not in vtodo:
        return
    limit = to_utc(moment)
    entries = vtodo["RDATE"] if isinstance(vtodo["RDATE"], list) else [vtodo["RDATE"]]
    kept = []
    for entry in entries:
        items = holders(entry)
        later = [item.dt for item in items if to_utc(start_of(item.dt)) > limit]
        if len(later) == len(items):
            kept.append(entry)
        elif later:
            rebuilt = vDDDLists(later)
            rebuilt.params = entry.params
            kept.append(rebuilt)
    del vtodo["RDATE"]
    if kept:
        vtodo["RDATE"] = kept if len(kept) > 1 else kept[0]


def _occurrences(vtodo: Any, rule: Any, start: datetime) -> rruleset:
    """Return the rule with the item's own RDATE, EXDATE and EXRULE values applied.

    An EXDATE that is a date beside a timed start excludes that whole day.
    """
    occurrences = rruleset()
    occurrences.rrule(rule)
    added = [
        _like(as_datetime(start_of(value)), start)
        for value in date_values(vtodo, "RDATE")
    ]
    for value in added:
        occurrences.rdate(value)
    for value in date_values(vtodo, "EXDATE"):
        if isinstance(value, datetime):
            occurrences.exdate(_like(value, start))
            continue
        day = _like(as_datetime(value), start)
        for when in [*rule.between(day, day + timedelta(days=1), inc=True), *added]:
            if day <= when < day + timedelta(days=1):
                occurrences.exdate(when)
    for exrule in _exrules(vtodo):
        occurrences.exrule(rule_from(exrule, start))
    return occurrences


def _exrules(vtodo: Any) -> list[Any]:
    held = vtodo.get("EXRULE")
    if held is None:
        return []
    return list(held) if isinstance(held, list) else [held]


def _like(value: datetime, start: datetime) -> datetime:
    """Return a value aware or floating as the start is, so the two compare."""
    if start.tzinfo is None:
        return dt_util.as_local(value).replace(tzinfo=None) if value.tzinfo else value
    return value if value.tzinfo else value.replace(tzinfo=start.tzinfo)
