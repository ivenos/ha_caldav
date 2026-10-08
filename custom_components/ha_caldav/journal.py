"""Journal entries, which Home Assistant has no platform for and actions reach."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import caldav
from icalendar import Calendar as ICalCalendar, Journal as ICalJournal

from .api import (
    delete_components,
    held_etag,
    put_resource,
    save_document,
    stamp,
    zoned_document,
)
from .const import (
    ATTR_ATTACHMENTS,
    ATTR_CATEGORIES,
    ATTR_CLASSIFICATION,
    ATTR_EVENT_STATUS,
    ATTR_URL,
)
from .coordinator import get_attr_value, master_of, to_local
from .errors import Refused
from .event import apply_extras, read_extras, replace

# What a journal entry shares with an event; STATUS has its own vocabulary.
_SHARED = (
    ATTR_URL,
    ATTR_EVENT_STATUS,
    ATTR_CLASSIFICATION,
    ATTR_CATEGORIES,
    ATTR_ATTACHMENTS,
)


def create_journal(calendar: caldav.Calendar, data: dict[str, Any]) -> None:
    """Create a new journal entry."""
    entry = ICalJournal()
    entry.add("uid", str(uuid4()))
    entry.add("dtstamp", datetime.now(tz=UTC))
    _apply(entry, data)
    document = ICalCalendar()
    document.add_component(entry)
    save_document(calendar, zoned_document(document), as_todo=False)


def update_journal(calendar: caldav.Calendar, uid: str, data: dict[str, Any]) -> None:
    """Apply changed fields to an existing journal entry."""
    resource = calendar.journal_by_uid(uid)
    tag = held_etag(resource, None)
    found = resource.icalendar_instance.walk("VJOURNAL")
    if not found:
        raise Refused("no_journal_in_object")
    entry = next((item for item in found if "RECURRENCE-ID" not in item), found[0])
    _apply(entry, data)
    stamp(entry)
    resource.icalendar_instance = zoned_document(resource.icalendar_instance)
    put_resource(resource, tag)


def delete_journal(calendar: caldav.Calendar, uid: str) -> None:
    """Delete a journal entry."""
    resource = calendar.journal_by_uid(uid)
    delete_components(resource, "VJOURNAL", held_etag(resource, None))


def _apply(entry: Any, data: dict[str, Any]) -> None:
    """Write the named fields; an explicit None clears one."""
    if "summary" in data:
        replace(entry, "summary", data["summary"])
    if "description" in data:
        replace(entry, "description", data["description"] or None)
    if "start" in data:
        replace(entry, "dtstart", data["start"])
    apply_extras(entry, {key: data[key] for key in _SHARED if key in data})


def read_journals(
    calendar: caldav.Calendar, start: datetime | None, end: datetime | None
) -> list[dict[str, Any]]:
    """Return the journal entries of a calendar, the dated ones first. Blocking.

    A window leaves out the entries without a date, as RFC 4791 9.9 has it.
    """
    window = {key: value for key, value in (("start", start), ("end", end)) if value}
    found = []
    for item in calendar.search(journal=True, **window):
        entry = master_of(item, "vjournal")
        if entry is None or (uid := get_attr_value(entry, "uid")) is None:
            continue
        journal: dict[str, Any] = {
            "uid": uid,
            "summary": get_attr_value(entry, "summary") or "",
        }
        moment = get_attr_value(entry, "dtstart")
        if isinstance(moment, date):
            journal["start"] = to_local(moment).isoformat()
        if description := get_attr_value(entry, "description"):
            journal["description"] = description
        extras = read_extras(entry)
        journal.update({key: extras[key] for key in _SHARED if key in extras})
        found.append(journal)
    return sorted(
        found, key=lambda journal: ("start" not in journal, journal.get("start", ""))
    )
