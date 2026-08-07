"""The VEVENT properties Home Assistant's event model has no room for.

Reading happens on vobject components, the shape the coordinator already works
with; writing happens on icalendar components, the shape :mod:`.recurrence`
uses. The two libraries model parameters differently, so the paths stay apart.
"""

from __future__ import annotations

from contextlib import suppress
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from dateutil.rrule import rrulestr
from homeassistant.util import dt as dt_util
from icalendar import Alarm as ICalAlarm, vCalAddress, vText
from icalendar.prop import vRecur

from .const import (
    ATTR_ALARMS,
    ATTR_ATTENDEES,
    ATTR_CATEGORIES,
    ATTR_CLASSIFICATION,
    ATTR_EVENT_STATUS,
    ATTR_ORGANIZER,
    ATTR_PRIORITY,
    ATTR_TRANSPARENCY,
    ATTR_URL,
)
from .errors import Refused

_MAILTO = "mailto:"


def rule_from(recur: Any, dtstart: datetime | date) -> Any:
    """Return the dateutil rule for an RRULE anchored at a start.

    dateutil refuses a rule outright when its UNTIL and the start disagree
    about carrying a zone. Google writes an all-day series as a DATE start with
    a UTC UNTIL, which every client but this one accepts, so the mismatch has
    to be reconciled here or the whole series becomes uneditable.
    """
    # RFC 5545 3.3.10 wants a positive INTERVAL and icalendar takes a zero
    # without complaint. dateutil then re-yields the start forever, so nothing
    # is ever after it: .after() never returns, and it is a thread out of the
    # executor pool spinning until Home Assistant is restarted, not an error
    # anything upstream could catch.
    if (interval := recur.get("INTERVAL")) and int(interval[0]) < 1:
        raise Refused("invalid_rrule", reason=f"INTERVAL={interval[0]}")
    _check_ranges(recur)
    # The value as stored reaches _aligned, not the datetime dateutil is
    # anchored on: converting first turns a DATE start into a naive datetime
    # and makes an all-day series indistinguishable from a floating one, which
    # read the UNTIL beside it in two different frames.
    return rrulestr(_aligned(recur, dtstart), dtstart=as_datetime(dtstart))


# RFC 5545 3.3.10, as ranges of the absolute value; a negative part counts from
# the end of its period and is bounded the same way.
_BY_RANGES = {
    "BYSECOND": (0, 60),
    "BYMINUTE": (0, 59),
    "BYHOUR": (0, 23),
    "BYMONTHDAY": (1, 31),
    "BYYEARDAY": (1, 366),
    "BYWEEKNO": (1, 53),
    "BYMONTH": (1, 12),
    "BYSETPOS": (1, 366),
}


def _check_ranges(recur: Any) -> None:
    """Refuse a rule whose BY parts name something no calendar has.

    Such a rule yields nothing at all, and the guard against a dense rule counts
    the occurrences it yields, so it never fires: dateutil walks to the year
    9999 instead, minute by minute for a sub-daily frequency, holding the
    collection's write lock for seconds on this desktop and far longer on the
    hardware Home Assistant usually runs on.
    """
    for name, (low, high) in _BY_RANGES.items():
        for value in recur.get(name) or ():
            if not low <= abs(int(value)) <= high:
                raise Refused("invalid_rrule", reason=f"{name}={value}")


def _aligned(recur: Any, dtstart: datetime | date) -> str:
    """Return the rule text with its UNTIL in the zone the start uses."""
    values = recur.get("UNTIL")
    text = recur.to_ical().decode("utf-8")
    if not values:
        return text
    until = values[0] if isinstance(values, list) else values
    if not isinstance(until, datetime):
        until = datetime.combine(until, time.min)
    zoned = isinstance(dtstart, datetime) and dtstart.tzinfo is not None
    if (until.tzinfo is not None) == zoned:
        return text
    fixed = vRecur(dict(recur))
    fixed["UNTIL"] = [until_frame(dtstart, until)]
    return fixed.to_ical().decode("utf-8")


def until_frame(dtstart: datetime | date, until: datetime | date) -> datetime | date:
    """Return an UNTIL read in the frame the DTSTART beside it is written in.

    Google writes an aware UNTIL against an all-day start. Read as an instant
    it names a moment, and every zone west of UTC then reads that moment as the
    day before, dropping the last occurrence of the series; read in its own
    wall clock it names the date the server meant, wherever this runs.

    Shared rather than repeated: the same reading is owed by everything that
    asks where a series ends, and having it in one place and not the other is
    what made the shape of an imported series depend on the installation.
    """
    if not isinstance(until, datetime):
        return until
    if isinstance(dtstart, datetime) and dtstart.tzinfo is not None:
        return to_utc(until)
    if until.tzinfo is None:
        return until
    if isinstance(dtstart, datetime):
        # Floating: the series is dated in local terms and to_utc reads it that
        # way, so its end has to be a local wall time too. Read in UTC instead,
        # the rule ends a whole offset away from its own occurrences, and the
        # series keeps or loses its last one depending on where this runs.
        return dt_util.as_local(until).replace(tzinfo=None)
    # A DATE start: the server meant a day, and the wall clock of the value it
    # wrote is what names that day wherever this runs.
    return until.astimezone(UTC).replace(tzinfo=None)


def to_utc(value: datetime | date) -> datetime:
    """Normalize dates and floating times so comparisons never mix types."""
    if not isinstance(value, datetime):
        value = datetime.combine(value, time.min)
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_util.get_default_time_zone())
    return value.astimezone(UTC)


def hold_sequence(component: Any) -> None:
    """Set SEQUENCE one low so caldav's bump lands back on the current value.

    RFC 5546 lets only the organizer move SEQUENCE, and a reply naming a
    version they never issued is one their client is entitled to drop. caldav
    2.1.0 accepts increase_seqno and bumps regardless; a test pins the number
    that reaches the wire, so a version that starts honouring the flag fails
    loudly rather than quietly sending replies one too low.
    """
    try:
        current = int(component.get("SEQUENCE"))
    except TypeError, ValueError:
        # Absent, or written by a server as something that is not a number.
        return
    component["SEQUENCE"] = current - 1


def as_datetime(value: datetime | date) -> datetime:
    """Return a value as a datetime, a plain date at the start of its day."""
    return value if isinstance(value, datetime) else datetime.combine(value, time.min)


def read_extras(vevent: Any) -> dict[str, Any]:
    """Return the extra properties of a vobject VEVENT, omitting the absent ones."""
    extras: dict[str, Any] = {}
    if (url := _text(vevent, "url")) is not None:
        extras[ATTR_URL] = url
    if (status := _text(vevent, "status")) is not None:
        extras[ATTR_EVENT_STATUS] = status.upper()
    if (transp := _text(vevent, "transp")) is not None:
        extras[ATTR_TRANSPARENCY] = transp.upper()
    if (classification := _text(vevent, "class")) is not None:
        extras[ATTR_CLASSIFICATION] = classification.upper()
    if (priority := getattr(vevent, "priority", None)) is not None:
        with suppress(TypeError, ValueError):
            extras[ATTR_PRIORITY] = int(priority.value)
    if categories := _categories(vevent):
        extras[ATTR_CATEGORIES] = categories
    if attendees := _read_attendees(vevent):
        extras[ATTR_ATTENDEES] = attendees
    if (organizer := getattr(vevent, "organizer", None)) is not None:
        extras[ATTR_ORGANIZER] = strip_scheme(str(organizer.value))
    if alarms := _read_alarms(vevent):
        extras[ATTR_ALARMS] = alarms
    return extras


def _text(vevent: Any, name: str) -> str | None:
    if (holder := getattr(vevent, name, None)) is None:
        return None
    value = holder.value
    return str(value) if value else None


def _categories(vevent: Any) -> list[str]:
    """Return every category, flattened over repeated CATEGORIES lines."""
    found: list[str] = []
    for holder in getattr(vevent, "categories_list", []) or []:
        value = holder.value
        # vobject hands back a list for a comma separated line and a bare
        # string for a single value.
        found.extend(value if isinstance(value, list) else [value])
    return [str(item).strip() for item in found if str(item).strip()]


def _read_attendees(vevent: Any) -> list[dict[str, Any]]:
    attendees = []
    for holder in getattr(vevent, "attendee_list", []) or []:
        params = getattr(holder, "params", {}) or {}
        entry: dict[str, Any] = {"email": strip_scheme(str(holder.value))}
        if names := params.get("CN"):
            entry["name"] = names[0]
        if statuses := params.get("PARTSTAT"):
            entry["status"] = statuses[0]
        if roles := params.get("ROLE"):
            entry["role"] = roles[0]
        attendees.append(entry)
    return attendees


def _read_alarms(vevent: Any) -> list[dict[str, Any]]:
    """Return the relative alarms as minutes before the event.

    An absolute trigger has no offset to report and is left out; it would need
    an anchor the attribute shape cannot carry.
    """
    alarms = []
    for valarm in getattr(vevent, "valarm_list", []) or []:
        trigger = getattr(valarm, "trigger", None)
        if trigger is None or not isinstance(trigger.value, timedelta):
            continue
        # Rounded away from zero, so a sub-minute offset still reads as a
        # reminder before the event rather than one minute after it.
        alarm: dict[str, Any] = {
            "minutes_before": -int(trigger.value.total_seconds() // 60)
        }
        related = (getattr(trigger, "params", {}) or {}).get("RELATED")
        if related and str(related[0]).upper() == "END":
            # Written back as-is; reporting it as an offset from the start
            # would move the alarm by the length of the event.
            alarm["related"] = "END"
        if (action := _text(valarm, "action")) is not None:
            alarm["action"] = action.upper()
        if (description := _text(valarm, "description")) is not None:
            alarm["description"] = description
        alarms.append(alarm)
    return alarms


def strip_scheme(value: str) -> str:
    """Return a CAL-ADDRESS without its mailto: prefix.

    RFC 3986 makes the scheme case-insensitive, and clients do write "MailTo:".
    """
    return value[len(_MAILTO) :] if value.lower().startswith(_MAILTO) else value


def comparable_address(value: str) -> str:
    """Return the form in which two CAL-ADDRESS values may be compared.

    Trimmed before the scheme is taken off, or padding a server left in front
    of it would hide the prefix and leave the two forms uncomparable.
    """
    return strip_scheme(value.strip()).lower()


def apply_extras(
    component: Any, data: dict[str, Any], own_address: str | None = None
) -> None:
    """Write the extra properties onto an icalendar component.

    Only keys present in ``data`` are touched: a service call that names none of
    them must leave what the server already holds untouched, and an explicit
    None clears the property.
    """
    if ATTR_URL in data and data[ATTR_URL]:
        data = {**data, ATTR_URL: _one_line(data[ATTR_URL])}
    for key, name in (
        (ATTR_URL, "url"),
        (ATTR_EVENT_STATUS, "status"),
        (ATTR_TRANSPARENCY, "transp"),
        (ATTR_CLASSIFICATION, "class"),
        (ATTR_PRIORITY, "priority"),
    ):
        if key in data:
            replace(component, name, data[key])
    if ATTR_CATEGORIES in data:
        categories = data[ATTR_CATEGORIES]
        replace(component, "categories", categories or None)
    if ATTR_ATTENDEES in data:
        _set_attendees(component, data[ATTR_ATTENDEES] or [])
    if ATTR_ORGANIZER in data:
        _set_organizer(component, data[ATTR_ORGANIZER])
    if ATTR_ALARMS in data:
        _set_alarms(component, data[ATTR_ALARMS] or [])
    _name_an_organizer(component, own_address)


def _name_an_organizer(component: Any, own_address: str | None) -> None:
    """Name the account as organizer of an event that lists attendees.

    RFC 5546 3 requires ORGANIZER wherever ATTENDEE appears. sabre/dav, which
    is Baikal and much else, hands the missing one straight to its scheduling
    plugin when the object is deleted and answers 500, and the event cannot be
    removed at all after that. Nextcloud guards its own copy of that plugin and
    the two servers that do no scheduling never look, which is why only Baikal
    showed it.

    Only where there is none: an organizer the server already holds belongs to
    whoever set it, and a stored event of somebody else's is not ours to claim.
    """
    if not own_address or "ATTENDEE" not in component or "ORGANIZER" in component:
        return
    _set_organizer(component, own_address)


def _one_line(value: Any) -> str:
    """Return text with the line breaks a content line cannot carry removed.

    icalendar asserts on an unescaped one and the failure reaches the user as a
    server error, though nothing about it came from the server. A template in a
    service call is enough to produce one.
    """
    return " ".join(str(value).splitlines()).strip()


def _set_organizer(component: Any, organizer: Any) -> None:
    """Write the organizer, keeping the parameters of the one already there.

    read_extras reports the bare address, so an edit that touches anything else
    names the same organizer again. Rebuilt from the address alone the line
    loses its CN and the SENT-BY that authorises an assistant to act for them.
    """
    held = component.get("ORGANIZER")
    if "ORGANIZER" in component:
        del component["ORGANIZER"]
    if not organizer:
        return
    address = vCalAddress(_mailto(_one_line(organizer)))
    if held is not None and comparable_address(str(held)) == comparable_address(
        str(address)
    ):
        address.params = held.params
    component.add("ORGANIZER", address, encode=False)


def reset_replies(component: Any) -> None:
    """Put the attendees of a split-off series back to NEEDS-ACTION.

    RFC 5546 has an organizer's REQUEST for a new event carry NEEDS-ACTION, and
    a tail is a new object under a uid nobody has seen. Carried over, the
    replies would assert acceptances that were never given for it.
    """
    current = component.get("ATTENDEE")
    if current is None:
        return
    for address in current if isinstance(current, list) else [current]:
        if "PARTSTAT" in address.params:
            address.params["PARTSTAT"] = vText("NEEDS-ACTION")


def _set_attendees(component: Any, attendees: list[Any]) -> None:
    """Rewrite the attendee list, keeping the lines of attendees that stay.

    An attendee that stays on the list keeps their line, so a reply already
    given and the parameters this integration does not model (CUTYPE,
    DELEGATED-FROM, SCHEDULE-STATUS) are not reset by an unrelated edit.
    """
    held = {}
    if (current := component.get("ATTENDEE")) is not None:
        for address in current if isinstance(current, list) else [current]:
            held[comparable_address(str(address))] = address
    if "ATTENDEE" in component:
        del component["ATTENDEE"]
    for attendee in attendees:
        spec = {"email": attendee} if isinstance(attendee, str) else dict(attendee)
        kept = held.get(comparable_address(spec["email"]))
        address = kept or vCalAddress(_mailto(spec["email"]))
        _set_param(address, "CN", spec.get("name"), None)
        _set_param(address, "ROLE", spec.get("role"), "REQ-PARTICIPANT")
        _set_param(address, "PARTSTAT", spec.get("status"), "NEEDS-ACTION")
        rsvp = spec.get("rsvp")
        # Only ever defaulted onto a line being written for the first time. An
        # attendee the server already holds without it has been asked once
        # already, and adding it asks again for a reply they may have given.
        _set_param(
            address,
            "RSVP",
            None if rsvp is None else str(rsvp).upper(),
            None if kept is not None else "TRUE",
        )
        component.add("ATTENDEE", address, encode=False)


def _set_param(address: Any, name: str, value: Any, default: str | None) -> None:
    """Set a parameter, falling back to the default only when none is held."""
    if value is not None:
        address.params[name] = vText(str(value))
    elif default is not None and name not in address.params:
        address.params[name] = vText(default)


def _set_alarms(component: Any, alarms: list[Any]) -> None:
    """Replace the alarms with the given offsets.

    RFC 5545 requires a DESCRIPTION on a DISPLAY alarm and does not permit one
    on an AUDIO alarm, so it is written for the first only. The action itself
    is constrained by the service schema, not here.
    """
    # Only the relative alarms are replaced. An absolute trigger has no offset
    # the attribute shape can carry, so read_extras leaves it out of what the
    # caller saw; clearing it here as well would have every edit that touches
    # alarms delete a reminder nobody was ever shown.
    component.subcomponents = [
        sub
        for sub in component.subcomponents
        if sub.name != "VALARM" or not _is_relative(sub)
    ]
    for alarm in alarms:
        spec = {"minutes_before": alarm} if isinstance(alarm, int) else dict(alarm)
        action = (spec.get("action") or "DISPLAY").upper()
        valarm = ICalAlarm()
        valarm.add("ACTION", action)
        trigger = timedelta(minutes=-int(spec["minutes_before"]))
        valarm.add("TRIGGER", trigger)
        if str(spec.get("related", "")).upper() == "END":
            valarm["TRIGGER"].params["RELATED"] = vText("END")
        if action == "DISPLAY":
            valarm.add("DESCRIPTION", spec.get("description") or "Reminder")
        component.add_component(valarm)


def _is_relative(valarm: Any) -> bool:
    """Return whether an alarm fires at an offset rather than at a fixed time."""
    trigger = valarm.get("TRIGGER")
    return trigger is not None and isinstance(getattr(trigger, "dt", None), timedelta)


def _mailto(address: str) -> str:
    # A CAL-ADDRESS is a URI; anything already carrying a scheme is left alone.
    return address if ":" in address else f"{_MAILTO}{address}"


def replace(component: Any, key: str, value: Any) -> None:
    """Overwrite a property, removing it outright when the value is None.

    icalendar's add() appends rather than replaces, so a plain add on a
    property that is already there produces a second line of it.
    """
    if key in component:
        del component[key]
    if value is not None:
        component.add(key, value)
