"""The VEVENT properties Home Assistant's event model has no room for.

Reading happens on vobject components, writing on icalendar ones; the two
libraries model parameters differently, so the paths stay apart.
"""

from __future__ import annotations

from base64 import b64encode
from contextlib import suppress
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
import re
from typing import Any

from dateutil.rrule import rrulestr
from homeassistant.util import dt as dt_util
from icalendar import Alarm as ICalAlarm, vCalAddress, vText, vUri
from icalendar.prop import vRecur

from .const import (
    ALARM_ACTIONS,
    ATTR_ALARMS,
    ATTR_ATTACHMENTS,
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


_TOO_DENSE = frozenset({"SECONDLY", "MINUTELY"})


def check_expandable(recur: Any, dtstart: datetime | date) -> None:
    """Refuse a rule whose expansion would never finish or flood the window.

    Each poll window is expanded in memory, a week of a minutely rule to 10,080.
    """
    if str((recur.get("FREQ") or [""])[0]).upper() in _TOO_DENSE:
        raise Refused("rrule_too_dense")
    rule_from(recur, dtstart)


def check_rule(recur: Any, dtstart: datetime | date) -> None:
    """Refuse a rule before it is stored rather than after.

    A rule producing nothing has dateutil search to the year 9999 on every
    poll, and an object stored with one cannot be deleted from here.
    """
    check_expandable(recur, dtstart)
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


def framed_rule(recur: Any, dtstart: datetime | date) -> Any:
    """Return the rule with its UNTIL in the form RFC 5545 3.3.10 ties to DTSTART.

    The Home Assistant editor writes the UNTIL of a zoned series as UTC digits
    without the Z. A DATE beside a timed start ends with the last second of it.
    """
    values = recur.get("UNTIL")
    if not values:
        return recur
    until = values[0] if isinstance(values, list) else values
    if isinstance(dtstart, datetime) and not isinstance(until, datetime):
        until = datetime.combine(until, time(23, 59, 59), tzinfo=dtstart.tzinfo)
    framed = until_frame(dtstart, until)
    if not isinstance(dtstart, datetime) and isinstance(framed, datetime):
        framed = framed.date()
    fixed = vRecur(dict(recur))
    fixed["UNTIL"] = [framed]
    return fixed


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


def as_datetime(value: datetime | date) -> datetime:
    """Return a value as a datetime, a plain date at the start of its day."""
    return value if isinstance(value, datetime) else datetime.combine(value, time.min)


def shifted(value: Any, delta: timedelta, zone: Any) -> Any:
    """Shift in the series' wall clock, keeping the value's representation."""
    if isinstance(value, tuple):
        end = (
            shifted(value[1], delta, zone)
            if isinstance(value[1], datetime)
            else value[1]
        )
        return (shifted(value[0], delta, zone), end)
    if not isinstance(value, datetime) or value.tzinfo is None:
        return value + delta
    anchor = zone or dt_util.get_default_time_zone()
    wall = value.astimezone(anchor).replace(tzinfo=None) + delta
    return wall.replace(tzinfo=anchor).astimezone(value.tzinfo)


def holders(entry: Any) -> list[Any]:
    """Return the value holders of an EXDATE/RDATE entry.

    A parsed line is a vDDDLists exposing .dts; a single value added at
    runtime is a bare vDDDTypes.
    """
    return entry.dts if hasattr(entry, "dts") else [entry]


def date_values(component: Any, key: str) -> list[Any]:
    """Return every value of an EXDATE or RDATE property, over all its lines."""
    if key not in component:
        return []
    entries = component[key]
    if not isinstance(entries, list):
        entries = [entries]
    return [item.dt for entry in entries for item in holders(entry)]


def start_of(value: Any) -> datetime | date:
    """Return the start of an RDATE value, which may be a period."""
    return value[0] if isinstance(value, tuple) else value


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
    if attachments := _read_attachments(vevent):
        extras[ATTR_ATTACHMENTS] = attachments
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
    """Return the alarms, an offset as minutes before and a fixed time as it is."""
    found = (_read_alarm(valarm) for valarm in getattr(vevent, "valarm_list", []) or [])
    return [alarm for alarm in found if alarm is not None]


def _read_alarm(valarm: Any) -> dict[str, Any] | None:
    trigger = getattr(valarm, "trigger", None)
    alarm: dict[str, Any] = {}
    if trigger is not None and isinstance(trigger.value, timedelta):
        # Rounded away from zero, so a sub-minute offset stays "before".
        alarm["minutes_before"] = -int(trigger.value.total_seconds() // 60)
        related = _param(getattr(trigger, "params", {}) or {}, "RELATED")
        if related and related.upper() == "END":
            alarm["related"] = "END"
    elif trigger is not None and isinstance(trigger.value, datetime):
        alarm["at"] = dt_util.as_local(trigger.value).isoformat()
    else:
        return None
    if (action := _text(valarm, "action")) is not None:
        alarm["action"] = action.upper()
    if (description := _text(valarm, "description")) is not None:
        alarm["description"] = description
    if alarm.get("action") == "EMAIL":
        if (summary := _text(valarm, "summary")) is not None:
            alarm["summary"] = summary
        alarm["attendees"] = [
            strip_scheme(str(holder.value))
            for holder in getattr(valarm, "attendee_list", []) or []
        ]
    return alarm


_MAX_REPEATS = 100


def alarm_moments(
    component: Any, start: Any, end: Any
) -> list[tuple[datetime, dict[str, Any]]]:
    """Return when each alarm of a vobject component fires, with the alarm.

    RFC 5545 3.8.6.3 counts an offset from the start, or from the end with
    RELATED=END, and 3.8.6.2 repeats an alarm REPEAT more times, DURATION apart.
    """
    moments = []
    for valarm in getattr(component, "valarm_list", []) or []:
        if (alarm := _read_alarm(valarm)) is None:
            continue
        trigger = valarm.trigger.value
        if isinstance(trigger, timedelta):
            ends = alarm.get("related") == "END"
            # Clients put an offset on a to-do that has only one of the two.
            base = (end if ends else start) or start or end
            if base is None:
                continue
            first = to_utc(base) + trigger
        else:
            first = to_utc(trigger)
        gap = getattr(getattr(valarm, "duration", None), "value", None)
        repeats = 0
        if isinstance(gap, timedelta):
            with suppress(TypeError, ValueError):
                repeats = int(valarm.repeat.value)
        for count in range(min(max(repeats, 0), _MAX_REPEATS) + 1):
            moments.append((first + count * gap if count else first, alarm))
    return moments


# RFC 8607 names the file FILENAME; Thunderbird and Evolution have their own.
_FILE_NAMES = ("FILENAME", "X-FILENAME", "X-LABEL")


def _read_attachments(vevent: Any) -> list[dict[str, Any]]:
    """Return the attachments, a link by its url and an embedded file by its size."""
    attachments = []
    for holder in getattr(vevent, "attach_list", []) or []:
        params = getattr(holder, "params", {}) or {}
        value = str(holder.value)
        entry: dict[str, Any] = {}
        if str(_param(params, "VALUE") or "").upper() == "BINARY":
            entry["size"] = len(value) * 3 // 4 - value.count("=")
        else:
            entry["url"] = value
        if name := next(filter(None, (_param(params, key) for key in _FILE_NAMES)), ""):
            entry["name"] = name
        if media_type := _param(params, "FMTTYPE"):
            entry["media_type"] = media_type
        attachments.append(entry)
    return attachments


def awaits_reply(vevent: Any, addresses: list[str]) -> bool:
    """Return whether a vobject VEVENT is an invitation the account has not answered.

    RFC 5545 3.2.12: an attendee without a PARTSTAT is at NEEDS-ACTION.
    """
    own = {comparable_address(address) for address in addresses}
    organizer = getattr(vevent, "organizer", None)
    if organizer is not None and comparable_address(str(organizer.value)) in own:
        return False
    for holder in getattr(vevent, "attendee_list", []) or []:
        if comparable_address(str(holder.value)) in own:
            status = _param(getattr(holder, "params", {}) or {}, "PARTSTAT")
            return (status or "NEEDS-ACTION").upper() == "NEEDS-ACTION"
    return False


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
        _set_alarms(component, data[ATTR_ALARMS] or [], own_address)
    if ATTR_ATTACHMENTS in data:
        _set_attachments(component, data[ATTR_ATTACHMENTS] or [])
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
    address = vCalAddress(mailto(_one_line(organizer)))
    if held is not None and comparable_address(str(held)) == comparable_address(
        str(address)
    ):
        address.params = held.params
    component.add("ORGANIZER", address, encode=False)


def reset_replies(component: Any) -> None:
    """Put the attendees of a split-off series back to NEEDS-ACTION.

    RFC 5546 has a REQUEST for a new event carry NEEDS-ACTION, and a tail is
    a new object under a uid nobody has answered for but its organizer.
    """
    current = component.get("ATTENDEE")
    if current is None:
        return
    organizer = component.get("ORGANIZER")
    if isinstance(organizer, list):
        organizer = organizer[0] if organizer else None
    own = None if organizer is None else comparable_address(str(organizer))
    for address in current if isinstance(current, list) else [current]:
        if comparable_address(str(address)) == own:
            continue
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
        address = kept or vCalAddress(mailto(email))
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


def _set_alarms(
    component: Any, alarms: list[Any], own_address: str | None = None
) -> None:
    """Replace the alarms with the given ones, keeping those that stay.

    A kept alarm keeps what this integration does not model, the UID and the
    ACKNOWLEDGED of RFC 9074 among it. An alarm with another action stays too.
    """
    held: dict[tuple[Any, ...], list[Any]] = {}
    others = []
    for sub in component.subcomponents:
        if sub.name == "VALARM" and (key := _alarm_key(sub)) is not None:
            held.setdefault(key, []).append(sub)
        else:
            others.append(sub)
    wanted = []
    for alarm in alarms:
        spec = {"minutes_before": alarm} if isinstance(alarm, int) else dict(alarm)
        action = (spec.get("action") or "DISPLAY").upper()
        if (at := spec.get("at")) is not None:
            trigger: Any = dt_util.as_utc(at)
            related = False
        else:
            trigger = timedelta(minutes=-int(spec["minutes_before"]))
            related = str(spec.get("related", "")).upper() == "END"
        kept = held.get((action, trigger, related))
        valarm = kept.pop(0) if kept else _new_alarm(action, trigger, related)
        _describe_alarm(valarm, action, spec, component, own_address)
        wanted.append(valarm)
    component.subcomponents = [*others, *wanted]


def _alarm_key(valarm: Any) -> tuple[Any, ...] | None:
    """Return what tells one alarm from another, None for one never written here."""
    action = str(valarm.get("ACTION", "")).upper()
    trigger = getattr(valarm.get("TRIGGER"), "dt", None)
    if action not in ALARM_ACTIONS or trigger is None:
        return None
    if isinstance(trigger, timedelta):
        related = str(valarm["TRIGGER"].params.get("RELATED", "")).upper() == "END"
        return (action, trigger, related)
    return (action, to_utc(trigger), False)


def _new_alarm(action: str, trigger: Any, related: bool) -> Any:
    valarm = ICalAlarm()
    valarm.add("ACTION", action)
    valarm.add("TRIGGER", trigger)
    if related:
        valarm["TRIGGER"].params["RELATED"] = vText("END")
    if isinstance(trigger, datetime):
        # RFC 5545 3.8.6.3: a TRIGGER is a DURATION unless it says otherwise.
        valarm["TRIGGER"].params["VALUE"] = vText("DATE-TIME")
    return valarm


def _describe_alarm(
    valarm: Any,
    action: str,
    spec: dict[str, Any],
    component: Any,
    own_address: str | None,
) -> None:
    """Write the texts and recipients RFC 5545 3.6.6 asks of each action.

    DISPLAY needs a DESCRIPTION and AUDIO must not have one. EMAIL needs a
    DESCRIPTION, a SUMMARY and somebody to send it to.
    """
    if action == "AUDIO":
        return
    if spec.get("description") or "DESCRIPTION" not in valarm:
        replace(valarm, "description", spec.get("description") or "Reminder")
    if action != "EMAIL":
        return
    if spec.get("summary") or "SUMMARY" not in valarm:
        summary = spec.get("summary") or str(component.get("SUMMARY", "")) or "Reminder"
        replace(valarm, "summary", summary)
    recipients = spec.get("attendees") or (
        [] if "ATTENDEE" in valarm else [own_address] if own_address else []
    )
    if recipients:
        valarm.pop("ATTENDEE", None)
        for recipient in recipients:
            address = vCalAddress(mailto(_one_line(recipient)))
            valarm.add("ATTENDEE", address, encode=False)
    if "ATTENDEE" not in valarm:
        raise Refused("email_alarm_needs_recipient")


def _set_attachments(component: Any, attachments: list[Any]) -> None:
    """Rewrite the attachments, keeping the lines of those that stay.

    A link is known by its url and an embedded file by its name, which is how
    reading reports one back.
    """
    held = component.get("ATTACH")
    stored = [] if held is None else held if isinstance(held, list) else [held]
    component.pop("ATTACH", None)
    for attachment in attachments:
        spec = {"url": attachment} if isinstance(attachment, str) else dict(attachment)
        if path := spec.get("path"):
            line = _embedded(path)
            spec.setdefault("name", Path(path).name)
        elif url := spec.get("url"):
            url = _one_line(url)
            line = next(
                (item for item in stored if str(item) == url and not _binary(item)),
                None,
            ) or vUri(url)
        else:
            line = next(
                (
                    item
                    for item in stored
                    if _binary(item) and _file_name(item) == spec.get("name")
                ),
                None,
            )
            if line is None:
                raise Refused("attachment_not_found", name=str(spec.get("name")))
        if spec.get("name") and _file_name(line) != spec["name"]:
            line.params["FILENAME"] = vText(spec["name"])
        if spec.get("media_type"):
            line.params["FMTTYPE"] = vText(spec["media_type"])
        component.add("ATTACH", line, encode=False)


def _binary(line: Any) -> bool:
    return str(line.params.get("VALUE", "")).upper() == "BINARY"


def _file_name(line: Any) -> str | None:
    return next(
        (str(line.params[key]) for key in _FILE_NAMES if key in line.params), None
    )


def _embedded(path: str) -> Any:
    """Return a file as the base64 line RFC 5545 3.8.1.1 embeds it in."""
    try:
        content = Path(path).read_bytes()
    except OSError as err:
        raise Refused("attachment_unreadable", path=path) from err
    line = vUri(b64encode(content).decode("ascii"))
    line.params["ENCODING"] = vText("BASE64")
    line.params["VALUE"] = vText("BINARY")
    return line


def mailto(address: str) -> str:
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
