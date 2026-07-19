"""CalDAV write operations for events and to-do items.

The recurring-series surgery lives in :mod:`.recurrence`; this module holds the
straightforward create and to-do operations.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any

import caldav
from dateutil.rrule import rrulestr
from icalendar import vRecur


def create_event(calendar: caldav.Calendar, data: dict[str, Any]) -> None:
    """Create a new event from Home Assistant event fields."""
    calendar.add_event(**data)


def create_todo(calendar: caldav.Calendar, data: dict[str, Any]) -> None:
    """Create a new to-do item."""
    calendar.save_todo(**data)


def update_todo(calendar: caldav.Calendar, uid: str, data: dict[str, Any]) -> None:
    """Apply changed fields to an existing to-do item."""
    todo = calendar.todo_by_uid(uid)
    vtodo = todo.icalendar_component
    # Completing a recurring task rolls it to the next occurrence instead of
    # closing the whole series.
    if data.get("status") == "COMPLETED" and "RRULE" in vtodo and _roll_todo(vtodo):
        todo.save(no_create=True, obj_type="todo")
        return
    vtodo["SUMMARY"] = data.get("summary") or ""
    if status := data.get("status"):
        vtodo["STATUS"] = status
    # set_due mutates the same component and drops DURATION, which RFC 5545
    # forbids alongside DUE.
    if (due := data.get("due")) is not None:
        todo.set_due(due)
    else:
        vtodo.pop("DUE", None)
    if description := data.get("description"):
        vtodo["DESCRIPTION"] = description
    else:
        vtodo.pop("DESCRIPTION", None)
    todo.save(no_create=True, obj_type="todo")


def delete_todo(calendar: caldav.Calendar, uid: str) -> None:
    """Delete a to-do item."""
    calendar.todo_by_uid(uid).delete()


def _roll_todo(vtodo: Any) -> bool:
    """Advance a recurring to-do to its next occurrence; False if none remains."""
    anchor_key = "DTSTART" if "DTSTART" in vtodo else "DUE"
    if anchor_key not in vtodo:
        return False
    anchor = vtodo[anchor_key].dt
    start = (
        anchor if isinstance(anchor, datetime) else datetime.combine(anchor, time.min)
    )
    rrule = vtodo["RRULE"]
    try:
        following = rrulestr(rrule.to_ical().decode("utf-8"), dtstart=start).after(
            start
        )
    except ValueError:
        # A malformed rule (e.g. an UNTIL whose zone does not match the anchor)
        # must not make the item impossible to complete.
        return False
    if following is None:
        return False
    count = rrule.get("COUNT")
    if count and count[0] <= 1:
        # One occurrence left; close it rather than writing an invalid COUNT=0.
        return False
    delta = following - start
    for key in ("DTSTART", "DUE"):
        if key in vtodo:
            moved = vtodo[key].dt + delta
            del vtodo[key]
            vtodo.add(key, moved)
    if count:
        reduced = vRecur(dict(rrule))
        reduced["COUNT"] = [count[0] - 1]
        del vtodo["RRULE"]
        vtodo.add("RRULE", reduced)
    vtodo["STATUS"] = "NEEDS-ACTION"
    for done in ("COMPLETED", "PERCENT-COMPLETE"):
        vtodo.pop(done, None)
    return True
