"""CalDAV write operations for creating, updating and deleting events.

Recurring series are edited in place on the master VEVENT: a single occurrence
becomes an EXDATE or an overriding component carrying a RECURRENCE-ID, and
"this and future" caps the RRULE with UNTIL. Value types follow RFC 5545, which
requires EXDATE and RECURRENCE-ID to match the DTSTART type and UNTIL to be UTC
for timed events.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import caldav
from homeassistant.util import dt as dt_util
from icalendar import Event as ICalEvent, vDDDTypes, vRecur


def create_event(calendar: caldav.Calendar, data: dict[str, Any]) -> None:
    """Create a new event from Home Assistant event fields."""
    calendar.add_event(**data)


def update_event(
    calendar: caldav.Calendar,
    uid: str,
    data: dict[str, Any],
    recurrence_id: str | None = None,
    this_and_future: bool = False,
) -> None:
    """Update a whole series, a single occurrence, or an occurrence onwards."""
    dav_event = calendar.event_by_uid(uid)
    ical = dav_event.icalendar_instance
    master = _master(ical)

    if recurrence_id is None:
        _apply(master, data)
        _save(dav_event, ical, master)
        return

    occurrence = parse_recurrence_id(recurrence_id)

    if not this_and_future:
        target = _override(ical, occurrence) or _new_override(ical, master, occurrence)
        _apply(target, data)
        _save(dav_event, ical, target)
        return

    if _utc(occurrence) <= _utc(master["DTSTART"].dt):
        _apply(master, data)
        _save(dav_event, ical, master)
        return

    # Split: cap the existing series, then store the edited tail as its own
    # resource, since RFC 4791 allows only one UID per calendar object.
    tail = dict(data)
    tail.setdefault("rrule", _rrule_string(master))
    if tail["rrule"] is None:
        del tail["rrule"]

    _cap_series(master, occurrence)
    _drop_overrides(ical, occurrence, from_occurrence=True)
    _save(dav_event, ical, master)
    calendar.add_event(**tail)


def delete_event(
    calendar: caldav.Calendar,
    uid: str,
    recurrence_id: str | None = None,
    this_and_future: bool = False,
) -> None:
    """Delete a whole series, a single occurrence, or an occurrence onwards."""
    dav_event = calendar.event_by_uid(uid)

    if recurrence_id is None:
        dav_event.delete()
        return

    ical = dav_event.icalendar_instance
    master = _master(ical)
    occurrence = parse_recurrence_id(recurrence_id)

    if this_and_future:
        # Capping before the first occurrence would leave an empty series.
        if _utc(occurrence) <= _utc(master["DTSTART"].dt):
            dav_event.delete()
            return
        _cap_series(master, occurrence)
        _drop_overrides(ical, occurrence, from_occurrence=True)
    else:
        _add_exdate(master, occurrence)
        _drop_overrides(ical, occurrence, from_occurrence=False)

    _save(dav_event, ical, master)


def create_todo(calendar: caldav.Calendar, data: dict[str, Any]) -> None:
    """Create a new to-do item."""
    calendar.save_todo(**data)


def update_todo(calendar: caldav.Calendar, uid: str, data: dict[str, Any]) -> None:
    """Apply changed fields to an existing to-do item."""
    todo = calendar.todo_by_uid(uid)
    vtodo = todo.icalendar_component
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


def parse_recurrence_id(value: str) -> datetime | date:
    """Parse the recurrence id Home Assistant echoes back to us.

    Dates are tried first: parse_datetime also accepts a date-only string and
    would turn an all-day occurrence into midnight, losing the value type that
    EXDATE and UNTIL have to match.
    """
    parsed = dt_util.parse_date(value) or dt_util.parse_datetime(value)
    if parsed is None:
        raise ValueError(f"Unable to parse recurrence id: {value}")
    return parsed


def _utc(value: datetime | date) -> datetime:
    """Normalize dates and naive datetimes so comparisons never mix types."""
    if not isinstance(value, datetime):
        value = datetime.combine(value, time.min)
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_util.get_default_time_zone())
    return value.astimezone(UTC)


def _master(ical: Any) -> Any:
    vevents = list(ical.walk("VEVENT"))
    if not vevents:
        raise ValueError("Calendar object contains no event")
    for vevent in vevents:
        if "RECURRENCE-ID" not in vevent:
            return vevent
    return vevents[0]


def _overrides(ical: Any) -> list[Any]:
    return [vevent for vevent in ical.walk("VEVENT") if "RECURRENCE-ID" in vevent]


def _override(ical: Any, occurrence: datetime | date) -> Any | None:
    target = _utc(occurrence)
    for vevent in _overrides(ical):
        if _utc(vevent["RECURRENCE-ID"].dt) == target:
            return vevent
    return None


def _new_override(ical: Any, master: Any, occurrence: datetime | date) -> Any:
    override = ICalEvent()
    override.add("UID", str(master["UID"]))
    override.add("DTSTAMP", datetime.now(UTC))
    override.add("RECURRENCE-ID", vDDDTypes(_match_type(master, occurrence)))
    ical.add_component(override)
    return override


def _drop_overrides(
    ical: Any, occurrence: datetime | date, from_occurrence: bool
) -> None:
    target = _utc(occurrence)
    for vevent in _overrides(ical):
        moment = _utc(vevent["RECURRENCE-ID"].dt)
        if moment >= target if from_occurrence else moment == target:
            ical.subcomponents.remove(vevent)


def _match_type(master: Any, occurrence: datetime | date) -> datetime | date:
    """Return the occurrence in the value type the master DTSTART uses."""
    if isinstance(master["DTSTART"].dt, datetime):
        return _utc(occurrence)
    return occurrence.date() if isinstance(occurrence, datetime) else occurrence


def _add_exdate(master: Any, occurrence: datetime | date) -> None:
    master.add("EXDATE", vDDDTypes(_match_type(master, occurrence)))


def _rrule_string(master: Any) -> str | None:
    rrule = master.get("RRULE")
    return rrule.to_ical().decode("utf-8") if rrule is not None else None


def _cap_series(master: Any, occurrence: datetime | date) -> None:
    rrule = master.get("RRULE")
    if rrule is None:
        raise ValueError("Event is not a recurring series")
    recur = vRecur(dict(rrule))
    recur.pop("COUNT", None)
    recur["UNTIL"] = [_until(master, occurrence)]
    _replace(master, "rrule", recur)


def _until(master: Any, occurrence: datetime | date) -> datetime | date:
    """Return an UNTIL that excludes the occurrence itself."""
    if isinstance(master["DTSTART"].dt, datetime):
        return _utc(occurrence) - timedelta(seconds=1)
    day = occurrence.date() if isinstance(occurrence, datetime) else occurrence
    return day - timedelta(days=1)


def _apply(component: Any, data: dict[str, Any]) -> None:
    _replace(component, "summary", data.get("summary"))
    if "dtstart" in data:
        _replace(component, "dtstart", data["dtstart"])
    if "dtend" in data:
        _replace(component, "duration", None)
        _replace(component, "dtend", data["dtend"])
    _replace(component, "description", data.get("description"))
    _replace(component, "location", data.get("location"))
    if "rrule" in data:
        _replace(
            component,
            "rrule",
            vRecur.from_ical(data["rrule"]) if data["rrule"] else None,
        )


def _save(dav_event: caldav.Event, ical: Any, touched: Any) -> None:
    _replace(touched, "last-modified", datetime.now(UTC))
    _replace(touched, "sequence", int(touched.get("sequence", 0)) + 1)
    dav_event.data = ical.to_ical().decode("utf-8")
    # The data above is already the complete resource. Without these flags,
    # caldav would bump SEQUENCE a second time, and - when an override happens
    # to be the first component - refetch the resource and merge only that
    # component, silently discarding the master edits.
    dav_event.save(increase_seqno=False, only_this_recurrence=False)


def _replace(component: Any, key: str, value: Any) -> None:
    if key in component:
        del component[key]
    if value is not None:
        component.add(key, value)
