"""The VTODO properties and the subtask relation core's to-do item has no room for.

Reading happens on vobject components, writing on icalendar ones, as in
:mod:`.event`.
"""

from __future__ import annotations

from contextlib import suppress
from datetime import UTC, date, datetime
from typing import Any

from homeassistant.util import dt as dt_util
from icalendar import vText
from icalendar.prop import vRecur

from .const import (
    ATTR_ALARMS,
    ATTR_ATTENDEES,
    ATTR_CATEGORIES,
    ATTR_CLASSIFICATION,
    ATTR_ORGANIZER,
    ATTR_PARENT_UID,
    ATTR_PERCENT_COMPLETE,
    ATTR_PRIORITY,
    ATTR_URL,
    TODO_STATUSES,
)
from .errors import Refused
from .event import apply_extras, check_rule, framed_rule, read_extras, replace, to_utc

# RFC 5545 knows four states where Home Assistant has two.
SETTLED = frozenset({"COMPLETED", "CANCELLED"})

# What a to-do shares with an event; STATUS has its own vocabulary and TRANSP
# is not a VTODO property.
_SHARED = (
    ATTR_URL,
    ATTR_CLASSIFICATION,
    ATTR_PRIORITY,
    ATTR_CATEGORIES,
    ATTR_ATTENDEES,
    ATTR_ORGANIZER,
    ATTR_ALARMS,
)


def status_of(vtodo: Any) -> str:
    """Return the RFC 5545 status of a vobject VTODO, one without as not started."""
    holder = getattr(vtodo, "status", None)
    status = str(getattr(holder, "value", "") or "").upper()
    return status if status in TODO_STATUSES else "NEEDS-ACTION"


def parent_of(vtodo: Any) -> str | None:
    """Return the uid of the to-do a vobject VTODO is a subtask of."""
    for holder in vtodo.contents.get("related-to", []):
        kinds = (getattr(holder, "params", {}) or {}).get("RELTYPE") or ["PARENT"]
        if str(kinds[0]).upper() == "PARENT" and (uid := str(holder.value).strip()):
            return uid
    return None


def read_todo_extras(vtodo: Any) -> dict[str, Any]:
    """Return what a vobject VTODO holds beyond core's item, omitting the absent."""
    extras: dict[str, Any] = {}
    if (parent := parent_of(vtodo)) is not None:
        extras[ATTR_PARENT_UID] = parent
    start = getattr(getattr(vtodo, "dtstart", None), "value", None)
    if isinstance(start, datetime):
        extras["start"] = dt_util.as_local(start).isoformat()
    elif isinstance(start, date):
        extras["start"] = start.isoformat()
    if (percent := getattr(vtodo, "percent_complete", None)) is not None:
        with suppress(TypeError, ValueError):
            extras[ATTR_PERCENT_COMPLETE] = int(percent.value)
    for name in ("location", "rrule"):
        if (holder := getattr(vtodo, name, None)) is not None and holder.value:
            extras[name] = str(holder.value)
    shared = read_extras(vtodo)
    extras.update({key: shared[key] for key in _SHARED if key in shared})
    return extras


def _relations(vtodo: Any) -> list[Any]:
    held = vtodo.get("RELATED-TO")
    if held is None:
        return []
    return list(held) if isinstance(held, list) else [held]


def _names_parent(relation: Any) -> bool:
    # RFC 5545 3.2.15: a relation without a RELTYPE is to the parent.
    return str(relation.params.get("RELTYPE", "PARENT")).upper() == "PARENT"


def parent_uid(vtodo: Any) -> str | None:
    """Return the uid of the to-do an icalendar VTODO is a subtask of."""
    for relation in _relations(vtodo):
        if _names_parent(relation) and (uid := str(relation).strip()):
            return uid
    return None


def set_parent(vtodo: Any, uid: str | None) -> None:
    """Put a to-do under another one, or under none.

    Sibling and child relations stay as they are.
    """
    relations = _relations(vtodo)
    held = next((item for item in relations if _names_parent(item)), None)
    vtodo.pop("RELATED-TO", None)
    if uid:
        relation = vText(uid)
        if held is not None:
            relation.params = held.params
        vtodo.add("RELATED-TO", relation, encode=False)
    for relation in relations:
        if not _names_parent(relation):
            vtodo.add("RELATED-TO", relation, encode=False)


def closed(vtodo: Any) -> bool:
    """Return whether an icalendar VTODO is completed or canceled."""
    return str(vtodo.get("STATUS", "")).upper() in SETTLED


def status_asked(data: dict[str, Any]) -> str | None:
    """Return the status a field set asks for, the one a percentage implies included."""
    if status := data.get("status"):
        return str(status)
    if ATTR_PERCENT_COMPLETE not in data:
        return None
    percent = data[ATTR_PERCENT_COMPLETE] or 0
    if percent >= 100:
        return "COMPLETED"
    return "IN-PROCESS" if percent else "NEEDS-ACTION"


def set_status(vtodo: Any, status: str, folded: bool = False) -> None:
    """Set the status and the completion properties RFC 5545 pairs with it.

    Folded is core's view of it: a status on the side the stored one is on
    is the stored one read back, as every rename carries it.
    """
    current = str(vtodo.get("STATUS", "NEEDS-ACTION")).upper()
    if folded and (current in SETTLED) == (status in SETTLED):
        return
    vtodo["STATUS"] = status
    if status == "COMPLETED":
        if "COMPLETED" not in vtodo:
            vtodo.add("COMPLETED", datetime.now(tz=UTC))
        vtodo["PERCENT-COMPLETE"] = 100
        return
    vtodo.pop("COMPLETED", None)
    if _percent(vtodo) == 100:
        vtodo.pop("PERCENT-COMPLETE", None)


def _percent(vtodo: Any) -> int | None:
    try:
        return int(vtodo["PERCENT-COMPLETE"])
    except KeyError, TypeError, ValueError:
        return None


def apply_fields(
    vtodo: Any,
    data: dict[str, Any],
    own_address: str | None = None,
    folded: bool = False,
) -> None:
    """Write the named fields onto an icalendar VTODO.

    Only keys present in ``data`` are touched; an explicit None clears one.
    """
    if "summary" in data:
        vtodo["SUMMARY"] = data["summary"] or ""
    if "description" in data:
        if data["description"]:
            vtodo["DESCRIPTION"] = data["description"]
        else:
            vtodo.pop("DESCRIPTION", None)
    if "location" in data:
        replace(vtodo, "location", data["location"] or None)
    if "start" in data:
        _set_moment(vtodo, "DTSTART", data["start"])
    if "due" in data:
        if data["due"] is not None:
            # RFC 5545 forbids DURATION alongside DUE.
            replace(vtodo, "duration", None)
        # Not through caldav's set_due, which writes onto the first component
        # that is not a timezone, event or not.
        _set_moment(vtodo, "DUE", data["due"])
    if "start" in data or "due" in data:
        _check_span(vtodo)
    if "rrule" in data:
        _set_rule(vtodo, data["rrule"])
    if ATTR_PERCENT_COMPLETE in data:
        replace(vtodo, "percent-complete", data[ATTR_PERCENT_COMPLETE] or None)
    if status := status_asked(data):
        set_status(vtodo, status, folded)
    if ATTR_PARENT_UID in data:
        set_parent(vtodo, data[ATTR_PARENT_UID])
    extras = {key: data[key] for key in _SHARED if key in data}
    if extras.get(ATTR_ALARMS):
        extras[ATTR_ALARMS] = _anchored(vtodo, extras[ATTR_ALARMS])
    if extras:
        apply_extras(vtodo, extras, own_address)


def _set_moment(vtodo: Any, name: str, value: Any) -> None:
    """Write a time in the zone, or the floating form, already stored.

    Core hands every time back in its own zone.
    """
    if value is None:
        vtodo.pop(name, None)
        return
    old = vtodo[name].dt if name in vtodo else None
    if isinstance(old, datetime) and isinstance(value, datetime):
        if to_utc(old) == to_utc(value):
            return
        if old.tzinfo is None:
            value = dt_util.as_local(value).replace(tzinfo=None)
        elif str(value.tzinfo) != str(old.tzinfo):
            value = value.astimezone(old.tzinfo)
    replace(vtodo, name, value)


def _check_span(vtodo: Any) -> None:
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


def _set_rule(vtodo: Any, rrule: str | None) -> None:
    """Write a recurrence rule, an empty one removing it.

    RFC 5545 3.8.5.3 counts from DTSTART; a to-do without one is counted
    from its DUE by every client that repeats it.
    """
    if not rrule:
        vtodo.pop("RRULE", None)
        return
    anchor = next((vtodo[key].dt for key in ("DTSTART", "DUE") if key in vtodo), None)
    if anchor is None:
        raise Refused("rrule_needs_date")
    recur = framed_rule(vRecur.from_ical(rrule), anchor)
    check_rule(recur, anchor)
    replace(vtodo, "rrule", recur)


def _anchored(vtodo: Any, alarms: list[Any]) -> list[Any]:
    """Return the alarms counting from a date the to-do has.

    RFC 5545 3.8.6.3: an offset counts from DTSTART, and from DUE only with
    RELATED=END.
    """
    if "DTSTART" in vtodo:
        return alarms
    if "DUE" not in vtodo:
        raise Refused("alarm_needs_date")
    specs = [
        {"minutes_before": alarm} if isinstance(alarm, int) else dict(alarm)
        for alarm in alarms
    ]
    return [{**spec, "related": spec.get("related") or "END"} for spec in specs]


class Tree:
    """How the to-dos of one list nest, by uid."""

    def __init__(self, parents: dict[str, str | None]) -> None:
        """Build the tree from the parent each uid names, if any."""
        self.parents = parents
        self._children: dict[str, list[str]] = {}
        for uid, parent in parents.items():
            if parent is not None:
                self._children.setdefault(parent, []).append(uid)

    def children(self, uid: str) -> list[str]:
        """Return the direct subtasks of an item."""
        return self._children.get(uid, [])

    def descendants(self, uid: str) -> list[str]:
        """Return everything below an item, each parent ahead of its subtasks.

        Another client may have stored a cycle, which is walked once.
        """
        found: list[str] = []
        seen = {uid}
        pending = [uid]
        while pending:
            for child in self.children(pending.pop(0)):
                if child not in seen:
                    seen.add(child)
                    found.append(child)
                    pending.append(child)
        return found

    def ancestors(self, uid: str) -> list[str]:
        """Return the parents above an item that are on the list, nearest first."""
        found: list[str] = []
        current = self.parents.get(uid)
        while current in self.parents and current != uid and current not in found:
            found.append(current)
            current = self.parents.get(current)
        return found
