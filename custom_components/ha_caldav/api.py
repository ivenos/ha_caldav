"""CalDAV write operations; the recurring-series ones live in :mod:`.recurrence`."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
import logging
from math import isfinite
import re
from typing import Any
from uuid import uuid4

import caldav
from caldav.davclient import requests
from caldav.elements import dav, ical
from caldav.lib import vcal
from caldav.lib.error import NotFoundError
from dateutil.rrule import rruleset
from homeassistant.util import dt as dt_util
from icalendar import (
    Calendar as ICalCalendar,
    Timezone as ICalTimezone,
    vDDDLists,
    vRecur,
)

from .const import EVENT_ATTRIBUTES, SORT_ORDER_PROPERTY
from .errors import NETWORK_ERRORS, Refused, rejected
from .event import (
    apply_extras,
    as_datetime,
    check_rule,
    comparable_address,
    date_values,
    framed_rule,
    hold_sequence,
    replace,
    rule_from,
    shifted,
    start_of,
    to_utc,
)

_LOGGER = logging.getLogger(__name__)

# Past this many uids, one read of the whole collection costs less than a
# request per uid.
_SCAN_THRESHOLD = 8

# Left between to-do positions so an item dropped between two neighbors has
# a whole number to take.
_SORT_GAP = 1024

# The keys apply_extras owns; everything else is a core field for create_ical.
EXTRA_KEYS = frozenset(EVENT_ATTRIBUTES)


def object_by_uid(
    calendar: caldav.Calendar, uid: str, todo: bool | None = False
) -> Any:
    """Return the calendar object carrying this uid.

    iCloud rejects the UID filter outright, so a refused lookup falls back to
    a scan. A todo of None asks for the uid whatever kind it is stored as.
    """
    try:
        return _filtered(calendar, uid, todo)
    except Exception as err:
        if not _uid_search_refused(err):
            raise
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
    try:
        return calendar.event_by_uid(uid)
    except NotFoundError:
        return calendar.todo_by_uid(uid)


def _scan(calendar: caldav.Calendar, todo: bool | None) -> Iterator[tuple[Any, str]]:
    """Yield every object of the collection with the uid it carries.

    Two filtered requests for both kinds: a filter naming only VCALENDAR is
    answered with nothing at all by a good few servers.
    """
    if todo is None:
        found = [
            *calendar.search(event=True),
            *calendar.search(todo=True, include_completed=True),
        ]
    else:
        found = (
            calendar.search(todo=True, include_completed=True)
            if todo
            else calendar.search(event=True)
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
    calendar.save_event(_zoned(instance, vevent))


def create_todo(calendar: caldav.Calendar, data: dict[str, Any]) -> None:
    """Create a new to-do item."""
    status = data.pop("status", None)
    ics = vcal.create_ical(objtype="VTODO", uid=str(uuid4()), **data)
    instance = ICalCalendar.from_ical(ics)
    vtodo = next(iter(instance.walk("VTODO")))
    if status:
        # An item created as done needs the completion properties too.
        _set_status(vtodo, status)
    calendar.save_todo(_zoned(instance, vtodo))


def _zoned(instance: ICalCalendar, component: Any) -> ICalCalendar:
    """Return the document with a definition for every TZID it references.

    The library writes the reference without the definition RFC 5545 wants.
    """
    return _document([component], {}, instance.get("PRODID"))


def update_todo(
    calendar: caldav.Calendar,
    uid: str,
    data: dict[str, Any],
    expected_etag: str | None = None,
) -> None:
    """Apply changed fields to an existing to-do item."""
    todo = object_by_uid(calendar, uid, todo=True)
    check_etag(todo, expected_etag)
    vtodo = collapse_repeated(vtodo_of(todo))
    # Only on the move to done: core sends the status along with every edit.
    settled = str(vtodo.get("STATUS", "")).upper() in _SETTLED
    if (
        data.get("status") == "COMPLETED"
        and not settled
        and "RRULE" in vtodo
        and _roll_todo(vtodo)
    ):
        # The roll owns DUE and DTSTART.
        _set_text(vtodo, data)
        stamp(vtodo)
        _save_todo(todo)
        return
    _set_text(vtodo, data)
    if status := data.get("status"):
        _set_status(vtodo, status)
    # Not through caldav's set_due, which writes onto the first component
    # that is not a timezone, event or not.
    if (due := data.get("due")) is not None:
        # RFC 5545 forbids DURATION alongside DUE.
        replace(vtodo, "duration", None)
        _set_due(vtodo, due)
    else:
        vtodo.pop("DUE", None)
    _check_todo_span(vtodo)
    stamp(vtodo)
    _save_todo(todo)


def _set_due(vtodo: Any, due: Any) -> None:
    """Write a due time in the zone, or the floating form, already stored.

    Core hands every time back in its own zone.
    """
    old = vtodo["DUE"].dt if "DUE" in vtodo else None
    if isinstance(old, datetime) and isinstance(due, datetime):
        if to_utc(old) == to_utc(due):
            return
        if old.tzinfo is None:
            due = dt_util.as_local(due).replace(tzinfo=None)
        elif str(due.tzinfo) != str(old.tzinfo):
            due = due.astimezone(old.tzinfo)
    replace(vtodo, "due", due)


def _check_todo_span(vtodo: Any) -> None:
    """RFC 5545: DUE is later in time than DTSTART, and shares its value type.

    Another client may have set a DTSTART that core never shows.
    """
    if "DTSTART" not in vtodo or "DUE" not in vtodo:
        return
    start, due = vtodo["DTSTART"].dt, vtodo["DUE"].dt
    if isinstance(start, datetime) != isinstance(due, datetime):
        raise Refused("mixed_time_types")
    if to_utc(due) < to_utc(start):
        raise Refused("end_before_start")


def vtodo_of(resource: Any) -> Any:
    """Return the VTODO of a resource.

    caldav's icalendar_component is the first non-timezone component, and a
    resource may hold a VEVENT ahead of the VTODO under one uid.
    """
    for component in resource.icalendar_instance.walk("VTODO"):
        return component
    raise Refused("no_todo_in_object")


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


def _save_todo(todo: Any) -> None:
    """Write a to-do back to the resource it was read from.

    Without no_create, which has caldav ask the server for the uid before
    every write; that is the request iCloud refuses.
    """
    # stamp has moved SEQUENCE already, and caldav bumps the first component
    # that is not a timezone on the way out, event or not.
    hold_sequence(todo.icalendar_component)
    # A due date may reference a zone the stored item never defined.
    todo.icalendar_instance = zoned_document(todo.icalendar_instance)
    # only_this_recurrence recurses without end on an object holding nothing
    # but a detached instance.
    todo.save(obj_type="todo", only_this_recurrence=False)


def _set_text(vtodo: Any, data: dict[str, Any]) -> None:
    vtodo["SUMMARY"] = data.get("summary") or ""
    if description := data.get("description"):
        vtodo["DESCRIPTION"] = description
    else:
        vtodo.pop("DESCRIPTION", None)


# RFC 5545 knows four states where Home Assistant has two.
_SETTLED = frozenset({"COMPLETED", "CANCELLED"})


def _set_status(vtodo: Any, status: str) -> None:
    """Set the status and the completion properties RFC 5545 pairs with it.

    Only when it moves between the two states core has; a rename carries the
    folded value back.
    """
    current = str(vtodo.get("STATUS", "")).upper()
    if current and (current in _SETTLED) == (status in _SETTLED):
        return
    vtodo["STATUS"] = status
    if status == "COMPLETED":
        if "COMPLETED" not in vtodo:
            vtodo.add("COMPLETED", datetime.now(tz=UTC))
        vtodo["PERCENT-COMPLETE"] = 100
        return
    for done in ("COMPLETED", "PERCENT-COMPLETE"):
        vtodo.pop(done, None)


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
    vtodo = vtodo_of(todo)
    vtodo[SORT_ORDER_PROPERTY] = str(position)
    stamp(vtodo)
    _save_todo(todo)


def _sort_position(vtodo: Any) -> float | None:
    """Return the stored sort position, or None where there is no usable one.

    int(inf) raises OverflowError, which is not a ValueError.
    """
    try:
        position = float(str(vtodo[SORT_ORDER_PROPERTY]))
    except KeyError, TypeError, ValueError:
        return None
    return position if isfinite(position) else None


def delete_components(resource: Any, kind: str) -> None:
    """Remove the components of one kind, and the resource once none is left.

    A resource may hold a VEVENT beside a VTODO under one uid, and both
    halves of the collection then list their own.
    """
    instance = resource.icalendar_instance
    kept = [item for item in instance.subcomponents if item.name != kind]
    if not any(item.name != "VTIMEZONE" for item in kept):
        resource.delete()
        return
    _LOGGER.debug("Removing the %s of a resource that holds more than one kind", kind)
    instance.subcomponents = kept
    # A zone only the removed component referenced must not stay behind.
    document = zoned_document(instance)
    # Nothing here revises what stays, and caldav bumps SEQUENCE regardless.
    hold_sequence(
        next(item for item in document.subcomponents if item.name != "VTIMEZONE")
    )
    resource.icalendar_instance = document
    resource.save(increase_seqno=False, only_this_recurrence=False)


def delete_todos(
    calendar: caldav.Calendar, uids: list[str], etags: dict[str, str]
) -> None:
    """Delete several to-do items in one pass, every etag checked first."""
    found = _todos_by_uid(calendar, uids)
    for uid, todo in found.items():
        check_etag(todo, etags.get(uid))
    for todo in found.values():
        delete_components(todo, "VTODO")


def _todos_by_uid(calendar: caldav.Calendar, uids: list[str]) -> dict[str, Any]:
    """Return a resource per uid, read in one go past a few or once refused."""
    found: dict[str, Any] = {}
    if len(uids) <= _SCAN_THRESHOLD:
        for uid in uids:
            try:
                found[uid] = _filtered(calendar, uid, todo=True)
            except Exception as err:
                if not _uid_search_refused(err):
                    raise
                break
        else:
            return found
    scanned = {stored: item for item, stored in _scan(calendar, todo=True)}
    for uid in uids:
        if uid in found:
            continue
        if uid not in scanned:
            raise NotFoundError(f"{uid} not found on server")
        found[uid] = scanned[uid]
    return found


def _comparable_etag(value: Any) -> str:
    """Return an etag without the weak marker, for RFC 7232 2.3.2 comparison.

    A proxy may add or drop the W/ between the read and the check.
    """
    return str(value).strip().removeprefix("W/")


def check_etag(resource: Any, expected_etag: str | None) -> None:
    """Refuse to overwrite a resource that changed since it was last read.

    A read-then-write: caldav sends no If-Match anywhere.
    """
    if expected_etag is None:
        return
    resource.load()
    current = resource.props.get(dav.GetEtag.tag)
    if current is None:
        # A proxy that strips the header, or a server that puts an etag on its
        # REPORT but not on a GET.
        _LOGGER.debug("No etag on the stored object; writing without the check")
        return
    if _comparable_etag(current) != _comparable_etag(expected_etag):
        raise Refused("etag_conflict")


def stamp(component: Any, addresses: list[str] | None = None) -> None:
    """Mark a component as revised now.

    RFC 5545 3.8.7.2: without a METHOD, DTSTAMP is the time of the last
    revision. 3.8.7.4 lets only the organizer move SEQUENCE.
    """
    now = datetime.now(tz=UTC)
    changes: list[tuple[str, Any]] = [("DTSTAMP", now), ("LAST-MODIFIED", now)]
    if not _organized_elsewhere(component, addresses):
        changes.append(("SEQUENCE", int(component.get("SEQUENCE", 0)) + 1))
    for key, value in changes:
        if key in component:
            del component[key]
        component.add(key, value)


def _organized_elsewhere(component: Any, addresses: list[str] | None) -> bool:
    """Return whether the component names an organizer who is not this account."""
    if not addresses or (organizer := component.get("ORGANIZER")) is None:
        return False
    own = {comparable_address(address) for address in addresses}
    return comparable_address(str(_first(organizer))) not in own


def move_event(
    source: caldav.Calendar,
    target: caldav.Calendar,
    uid: str,
    keep_original: bool,
) -> None:
    """Copy an event to another calendar, optionally removing the original.

    Nextcloud and Radicale disagree on cross-collection COPY and MOVE.
    """
    event = object_by_uid(source, uid)
    instance = ICalCalendar.from_ical(event.data)
    if not instance.walk("VEVENT"):
        raise Refused("no_event_in_object")
    # RFC 4791 gives one uid one resource, and caldav names it after the uid.
    if _already_there(target, {uid}):
        raise Refused("uid_clash", uids=uid)
    copy = save_document(target, instance, as_todo=False)
    if keep_original:
        return
    try:
        event.delete()
    except NETWORK_ERRORS:
        # The server may have deleted it before the timeout.
        if _still_there(source, uid):
            _undo(copy)
        raise


def _still_there(calendar: caldav.Calendar, uid: str) -> bool:
    """Return whether the uid is certainly still on the calendar."""
    try:
        object_by_uid(calendar, uid)
    except NETWORK_ERRORS:
        return False
    return True


def _undo(resource: Any) -> None:
    """Remove a resource this call wrote before failing further on."""
    try:
        resource.delete()
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not undo a half-finished write: %s", err)


def save_document(
    calendar: caldav.Calendar, document: ICalCalendar, as_todo: bool
) -> Any:
    """Write a whole document to a collection as one resource.

    The document, not its text: caldav puts a string through vcal.fix, whose
    COMPLETED rule matches inside a DESCRIPTION. only_this_recurrence is read
    off the first non-timezone component, and a detached instance there has
    caldav look the uid up on the target before it exists. Saving the resource
    itself also leaves RELATED-TO alone, unlike Calendar.save_object.
    """
    objclass = caldav.Todo if as_todo else caldav.Event
    stored = objclass(calendar.client, data=document, parent=calendar)
    stored.save(only_this_recurrence=False)
    return stored


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
        return _one_date_per_line(ICalTimezone.from_tzid(tzid))
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
        document.add_component(component)
    return document


def zoned_document(instance: ICalCalendar) -> ICalCalendar:
    """Return a document defining every TZID its own components reference.

    An edit can introduce a zone the stored object never carried; without the
    definition, lenient servers store a time other clients read as floating.
    """
    zones = {str(zone.get("TZID", "")): zone for zone in instance.walk("VTIMEZONE")}
    parts = [item for item in instance.subcomponents if item.name != "VTIMEZONE"]
    return _document(parts, zones, instance.get("PRODID"), carried=instance)


def import_ics(calendar: caldav.Calendar, ics: str) -> list[str]:
    """Write every VEVENT and VTODO in a document to the calendar.

    Components sharing a uid are one resource (RFC 4791). A uid already on
    the calendar aborts the whole import.
    """
    document = ICalCalendar.from_ical(ics)
    if unreadable := _unreadable(document):
        raise Refused("unreadable_lines", lines=", ".join(unreadable))
    zones = {str(zone.get("TZID", "")): zone for zone in document.walk("VTIMEZONE")}
    groups: dict[str, list[Any]] = {}
    for component in document.walk():
        if component.name not in ("VEVENT", "VTODO"):
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
    as_todo = {uid: components[0].name == "VTODO" for uid, components in groups.items()}
    if clashing := sorted(_already_there(calendar, set(as_todo))):
        raise Refused("uid_clash", uids=", ".join(clashing[:5]))
    written: list[Any] = []
    try:
        for uid, components in groups.items():
            written.append(
                save_document(
                    calendar,
                    _document(components, zones, document.get("PRODID")),
                    as_todo[uid],
                )
            )
    except Exception:
        # Objects left behind would be refused as clashes on the retry.
        for stored in written:
            _undo(stored)
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
    for item in calendar.search(event=True) + calendar.search(
        todo=True, include_completed=True
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


def respond_to_invitation(
    calendar: caldav.Calendar, uid: str, partstat: str, addresses: list[str]
) -> None:
    """Set our own participation status on an event we were invited to."""
    event = object_by_uid(calendar, uid)
    instance = event.icalendar_instance
    wanted = {comparable_address(address) for address in addresses}
    changed = False
    for component in instance.walk("VEVENT"):
        attendees = component.get("ATTENDEE")
        if attendees is None:
            continue
        for attendee in attendees if isinstance(attendees, list) else [attendees]:
            if comparable_address(str(attendee)) not in wanted:
                continue
            attendee.params["PARTSTAT"] = partstat
            # An RFC 6638 server relays the reply only once RSVP is off.
            attendee.params["RSVP"] = "FALSE"
            changed = True
    if not changed:
        raise Refused("not_an_attendee")
    # The document, not its text, which caldav would run through vcal.fix.
    event.icalendar_instance = instance
    hold_sequence(event.icalendar_component)
    # only_this_recurrence never terminates on an orphan-override object.
    event.save(increase_seqno=False, only_this_recurrence=False)


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
    """Advance a recurring to-do to its next occurrence; False if none remains."""
    anchor_key = "DTSTART" if "DTSTART" in vtodo else "DUE"
    if anchor_key not in vtodo:
        return False
    anchor = vtodo[anchor_key].dt
    start = as_datetime(anchor)
    rrule = vtodo["RRULE"]
    try:
        rule = rule_from(rrule, anchor)
        following = _occurrences(vtodo, rule, start).after(start)
    except TypeError, ValueError:
        # A rule malformed beyond what rule_from reconciles, or a date that
        # does not compare with the start.
        return False
    if following is None:
        return False
    count = rrule.get("COUNT")
    # COUNT counts the rule's own slots, the excluded ones included.
    passed = sum(1 for slot in rule.between(start, following, inc=True) if slot > start)
    if count and count[0] - passed < 1:
        return False
    delta = following - start
    zone = anchor.tzinfo if isinstance(anchor, datetime) else None
    for key in ("DTSTART", "DUE"):
        if key in vtodo:
            moved = shifted(vtodo[key].dt, delta, zone)
            del vtodo[key]
            vtodo.add(key, moved)
    if count:
        reduced = vRecur(dict(rrule))
        reduced["COUNT"] = [count[0] - passed]
        del vtodo["RRULE"]
        vtodo.add("RRULE", reduced)
    vtodo["STATUS"] = "NEEDS-ACTION"
    for done in ("COMPLETED", "PERCENT-COMPLETE"):
        vtodo.pop(done, None)
    return True


def _occurrences(vtodo: Any, rule: Any, start: datetime) -> rruleset:
    """Return the rule with the item's own RDATE and EXDATE values applied."""
    occurrences = rruleset()
    occurrences.rrule(rule)
    for key, add in (("RDATE", occurrences.rdate), ("EXDATE", occurrences.exdate)):
        for value in date_values(vtodo, key):
            add(_like(as_datetime(start_of(value)), start))
    return occurrences


def _like(value: datetime, start: datetime) -> datetime:
    """Return a value aware or floating as the start is, so the two compare."""
    if start.tzinfo is None:
        return dt_util.as_local(value).replace(tzinfo=None) if value.tzinfo else value
    return value if value.tzinfo else value.replace(tzinfo=start.tzinfo)
