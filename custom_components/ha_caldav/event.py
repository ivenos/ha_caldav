"""The VEVENT properties Home Assistant's event model has no room for.

Reading happens on vobject components, writing on icalendar ones; the two
libraries model parameters differently, so the paths stay apart.
"""

from __future__ import annotations

from contextlib import suppress
from datetime import UTC, date, datetime, time, timedelta
import re
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

    dateutil refuses a rule whose UNTIL and start disagree about carrying a
    zone, which is how Google writes every all-day series.
    """
    # RFC 5545 3.3.10 wants a positive INTERVAL; on a zero dateutil re-yields
    # the start forever and .after() never returns.
    if (interval := recur.get("INTERVAL")) and int(interval[0]) < 1:
        raise Refused("invalid_rrule", reason=f"INTERVAL={interval[0]}")
    _check_ranges(recur)
    # The value as stored: converted first, a DATE start reads as floating.
    return rrulestr(_aligned(recur, dtstart), dtstart=as_datetime(dtstart))


# RFC 5545 3.3.10, as ranges of the absolute value.
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


def check_rule(recur: Any, dtstart: datetime | date) -> None:
    """Refuse a rule before it is stored rather than after.

    A rule producing nothing has dateutil search to the year 9999 on every
    poll, and an object stored with one cannot be deleted from here.
    """
    if next(iter(rule_from(recur, dtstart)), None) is None:
        raise Refused("invalid_rrule", reason=recur.to_ical().decode("utf-8"))


def _check_ranges(recur: Any) -> None:
    """Refuse a rule whose BY parts name something no calendar has.

    Such a rule yields nothing, so the density guard never fires and dateutil
    walks to the year 9999 minute by minute.
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

    Google writes an aware UNTIL against an all-day start; read as an instant
    it names the day before everywhere west of UTC.
    """
    if not isinstance(until, datetime):
        return until
    if isinstance(dtstart, datetime) and dtstart.tzinfo is not None:
        # RFC 5545 3.3.10: a zoned start takes its UNTIL in UTC.
        return until.replace(tzinfo=UTC) if until.tzinfo is None else to_utc(until)
    if until.tzinfo is None:
        return until
    if isinstance(dtstart, datetime):
        # A floating series is dated in local terms, so its end is too.
        return dt_util.as_local(until).replace(tzinfo=None)
    # A DATE start: the wall clock of the value names the day meant.
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

    caldav 2.1.0 bumps regardless of increase_seqno, and RFC 5546 lets only
    the organizer move SEQUENCE. A test pins the number on the wire.
    """
    try:
        current = int(component.get("SEQUENCE"))
    except TypeError, ValueError:
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
        # A list for a comma separated line, a bare string for a single value.
        found.extend(value if isinstance(value, list) else [value])
    return [str(item).strip() for item in found if str(item).strip()]


# RFC 6868; a ^ in front of anything else is not an escape.
_PARAM_ESCAPES = {"^^": "^", "^n": "\n", "^'": '"'}
_PARAM_ESCAPE = re.compile(r"\^[\^n']")


def _param(params: dict[str, Any], name: str) -> str | None:
    """Return the first value of a vobject parameter, RFC 6868 decoded.

    vobject reads without unescaping while icalendar writes with escaping.
    """
    values = params.get(name)
    if not values:
        return None
    text = str(values[0])
    return _PARAM_ESCAPE.sub(lambda found: _PARAM_ESCAPES[found.group()], text)


def _read_attendees(vevent: Any) -> list[dict[str, Any]]:
    attendees = []
    for holder in getattr(vevent, "attendee_list", []) or []:
        params = getattr(holder, "params", {}) or {}
        entry: dict[str, Any] = {"email": strip_scheme(str(holder.value))}
        if name := _param(params, "CN"):
            entry["name"] = name
        if status := _param(params, "PARTSTAT"):
            entry["status"] = status
        if role := _param(params, "ROLE"):
            entry["role"] = role
        attendees.append(entry)
    return attendees


def _read_alarms(vevent: Any) -> list[dict[str, Any]]:
    """Return the relative alarms as minutes before the event.

    An absolute trigger has no offset to report and is left out.
    """
    alarms = []
    for valarm in getattr(vevent, "valarm_list", []) or []:
        trigger = getattr(valarm, "trigger", None)
        if trigger is None or not isinstance(trigger.value, timedelta):
            continue
        # Rounded away from zero, so a sub-minute offset stays "before".
        alarm: dict[str, Any] = {
            "minutes_before": -int(trigger.value.total_seconds() // 60)
        }
        related = _param(getattr(trigger, "params", {}) or {}, "RELATED")
        if related and related.upper() == "END":
            alarm["related"] = "END"
        if (action := _text(valarm, "action")) is not None:
            alarm["action"] = action.upper()
        if (description := _text(valarm, "description")) is not None:
            alarm["description"] = description
        alarms.append(alarm)
    return alarms


def strip_scheme(value: str) -> str:
    """Return a CAL-ADDRESS without its mailto: prefix, whatever its case."""
    return value[len(_MAILTO) :] if value.lower().startswith(_MAILTO) else value


def comparable_address(value: str) -> str:
    """Return the form in which two CAL-ADDRESS values may be compared."""
    return strip_scheme(value.strip()).lower()


def apply_extras(
    component: Any, data: dict[str, Any], own_address: str | None = None
) -> None:
    """Write the extra properties onto an icalendar component.

    Only keys present in ``data`` are touched; an explicit None clears one.
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

    RFC 5546 3 requires ORGANIZER wherever ATTENDEE appears, and sabre/dav
    answers 500 on deleting an object without one. An organizer already there
    belongs to whoever set it.
    """
    if not own_address or "ATTENDEE" not in component or "ORGANIZER" in component:
        return
    _set_organizer(component, own_address)


def _one_line(value: Any) -> str:
    """Return text without the line breaks icalendar asserts on."""
    return " ".join(str(value).splitlines()).strip()


def _set_organizer(component: Any, organizer: Any) -> None:
    """Write the organizer, keeping the parameters of the one already there.

    read_extras reports the bare address, so an unrelated edit names the same
    organizer again and would lose the CN and SENT-BY.
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

    RFC 5546 has a REQUEST for a new event carry NEEDS-ACTION, and a tail is
    a new object under a uid nobody has answered for.
    """
    current = component.get("ATTENDEE")
    if current is None:
        return
    for address in current if isinstance(current, list) else [current]:
        if "PARTSTAT" in address.params:
            address.params["PARTSTAT"] = vText("NEEDS-ACTION")


def _set_attendees(component: Any, attendees: list[Any]) -> None:
    """Rewrite the attendee list, keeping the lines of attendees that stay.

    A kept line keeps its reply and the parameters this integration does not
    model (CUTYPE, DELEGATED-FROM, SCHEDULE-STATUS).
    """
    held = {}
    if (current := component.get("ATTENDEE")) is not None:
        for address in current if isinstance(current, list) else [current]:
            held[comparable_address(str(address))] = address
    if "ATTENDEE" in component:
        del component["ATTENDEE"]
    for attendee in attendees:
        spec = {"email": attendee} if isinstance(attendee, str) else dict(attendee)
        email = _one_line(spec["email"])
        kept = held.get(comparable_address(email))
        address = kept or vCalAddress(_mailto(email))
        _set_param(address, "CN", spec.get("name"), None)
        _set_param(address, "ROLE", spec.get("role"), "REQ-PARTICIPANT")
        _set_param(address, "PARTSTAT", spec.get("status"), "NEEDS-ACTION")
        rsvp = spec.get("rsvp")
        # Defaulted onto a new line only; a stored attendee was asked already.
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
    """Replace the relative alarms with the given offsets.

    RFC 5545 requires a DESCRIPTION on a DISPLAY alarm and forbids one on
    AUDIO. Absolute triggers stay: read_extras never showed them.
    """
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
    # A CAL-ADDRESS is a URI, and RFC 6638 lets the address set name a
    # principal by its path.
    if ":" in address or address.startswith("/"):
        return address
    return f"{_MAILTO}{address}" if "@" in address else address


def replace(component: Any, key: str, value: Any) -> None:
    """Overwrite a property, removing it outright when the value is None.

    icalendar's add() appends rather than replaces.
    """
    if key in component:
        del component[key]
    if value is not None:
        component.add(key, value)
