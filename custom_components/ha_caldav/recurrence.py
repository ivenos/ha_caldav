"""Write operations for CalDAV events.

CalDAV has no occurrence-level call: the whole series lives in one calendar
object that is rewritten by hand. RFC 5545 requires EXDATE, RECURRENCE-ID and
UNTIL to follow DTSTART, in both value type and zone; a floating DTSTART keeps
them floating, a zoned one puts them in UTC.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
import logging
from typing import Any

import caldav
from dateutil.rrule import rruleset
from homeassistant.util import dt as dt_util
from icalendar import (
    Calendar as ICalCalendar,
    Event as ICalEvent,
    vDDDLists,
    vDDDTypes,
    vRecur,
)

from .api import (
    check_etag,
    collapse_repeated,
    delete_components,
    object_by_uid,
    stamp,
    zoned_document,
)
from .errors import NETWORK_ERRORS, WRITE_ERRORS, Refused
from .event import (
    apply_extras,
    as_datetime,
    check_rule,
    hold_sequence,
    replace,
    reset_replies,
    rule_from,
    to_utc,
    until_frame,
)

_LOGGER = logging.getLogger(__name__)

_MAX_OCCURRENCES = 10_000


def update_event(
    calendar: caldav.Calendar,
    uid: str,
    data: dict[str, Any],
    recurrence_id: str | None = None,
    this_and_future: bool = False,
    expected_etag: str | None = None,
    own_address: str | None = None,
) -> None:
    """Update a whole series, a single occurrence, or an occurrence onwards."""
    dav_event = object_by_uid(calendar, uid)
    check_etag(dav_event, expected_etag)
    ical = _collapsed(dav_event.icalendar_instance)
    master = _master(ical)

    if recurrence_id is None:
        if data.get("rrule") and "RECURRENCE-ID" in master:
            # No series head here; a rule on a detached instance is inert.
            raise Refused("not_recurring")
        _update_series(dav_event, ical, master, data, own_address)
        return

    _check_no_ranged_override(ical)
    # Aligned once: to_utc reads a bare date as local midnight and an aware
    # id as its own instant.
    occurrence = _align(master, parse_recurrence_id(recurrence_id))

    if this_and_future and "RECURRENCE-ID" in master:
        raise Refused("not_recurring")

    if not this_and_future:
        target = _override(ical, occurrence)
        if target is None:
            if not _is_occurrence(master, occurrence):
                raise Refused("occurrence_not_found", recurrence_id=recurrence_id)
            target = _new_override(ical, master, occurrence)
        _apply(target, _without_rrule(data), own_address)
        _save(dav_event, ical, target)
        return

    if to_utc(occurrence) <= to_utc(master["DTSTART"].dt):
        # From the first occurrence onwards is the whole series.
        _update_series(dav_event, ical, master, data, own_address)
        return

    if "RRULE" not in master and "RDATE" not in master:
        raise Refused("not_recurring")

    # A retry finds the head already capped and must not clone the tail again.
    if not _has_occurrences_from(master, occurrence):
        return

    # Splitting at an off-rule RDATE would re-anchor and reschedule the series.
    if (
        master.get("RRULE") is not None
        and not _rule_replaced(master, data)
        and not _ends_before(master, occurrence)
        and not _on_rule(master, occurrence)
    ):
        raise Refused("rdate_split")

    # RFC 4791 allows one UID per object; a derived UID lets a retry overwrite
    # the tail instead of duplicating it.
    tail = calendar.save_event(
        _tail_document(ical, master, data, occurrence, own_address)
    )
    try:
        _cap_series(master, occurrence)
        _drop_overrides(ical, master, occurrence, from_occurrence=True)
        _save(dav_event, ical, master)
    except WRITE_ERRORS:
        # The server may have committed the cap before the timeout.
        if _head_uncapped(calendar, uid, occurrence):
            _discard(tail)
        else:
            _LOGGER.warning(
                "Keeping the split-off series; the head state could not be"
                " verified after a failed update"
            )
        raise


def _update_series(
    dav_event: caldav.Event,
    ical: Any,
    master: Any,
    data: dict[str, Any],
    own_address: str | None,
) -> None:
    """Apply an edit to every occurrence of a series and save it."""
    # As written, not as an instant: to_utc reads a bare date as local midnight.
    old_start = master["DTSTART"].dt
    old_rule = _rule_string(master)
    _apply(master, data, own_address)
    start_moved = to_utc(master["DTSTART"].dt) != to_utc(old_start)
    rule_changed = _rule_string(master) != old_rule
    if start_moved and not rule_changed and not _self_anchored(master):
        raise Refused("rrule_mismatch")
    if rule_changed or start_moved:
        if "RECURRENCE-ID" in master and len(ical.walk("VEVENT")) > 1:
            # No series head: the other components are detached instances.
            raise Refused("not_recurring")
        _check_no_ranged_override(ical)
        if rule_changed:
            # The slots the exceptions named need not exist under the new rule.
            _drop_overrides(
                ical, master, old_start, from_occurrence=False, all_overrides=True
            )
            _clear_dates(master)
        elif delta := _wall(master, master["DTSTART"].dt) - _wall(master, old_start):
            # The rule stands and the series moved, so the exceptions, extra
            # and cancelled dates move with it.
            dtstart = master["DTSTART"].dt
            zone = dtstart.tzinfo if isinstance(dtstart, datetime) else None
            for override in _overrides(ical):
                if override is not master:
                    _shift_override(override, delta, zone)
            _shift_dates(master, delta, zone)
    _save(dav_event, ical, master)


def delete_event(
    calendar: caldav.Calendar,
    uid: str,
    recurrence_id: str | None = None,
    this_and_future: bool = False,
    expected_etag: str | None = None,
) -> None:
    """Delete a whole series, a single occurrence, or an occurrence onwards."""
    dav_event = object_by_uid(calendar, uid)
    check_etag(dav_event, expected_etag)

    if recurrence_id is None:
        delete_components(dav_event, "VEVENT")
        return

    ical = _collapsed(dav_event.icalendar_instance)
    master = _master(ical)
    _check_no_ranged_override(ical)
    occurrence = _align(master, parse_recurrence_id(recurrence_id))

    if this_and_future:
        if "RECURRENCE-ID" in master and len(ical.walk("VEVENT")) > 1:
            # No series head, so which instance stands in for it is the
            # order the server listed them in.
            raise Refused("not_recurring")
        # Capping before the first occurrence would leave an empty series.
        if to_utc(occurrence) <= to_utc(master["DTSTART"].dt):
            delete_components(dav_event, "VEVENT")
            return
        _cap_series(master, occurrence)
        _drop_overrides(ical, master, occurrence, from_occurrence=True)
    elif "RECURRENCE-ID" in master:
        target = _override(ical, occurrence)
        if target is None:
            raise Refused("occurrence_not_found", recurrence_id=recurrence_id)
        remaining = [v for v in _overrides(ical) if v is not target]
        if not remaining:
            delete_components(dav_event, "VEVENT")
            return
        ical.subcomponents.remove(target)
        _save(dav_event, ical, remaining[0])
        return
    else:
        if "RRULE" not in master and "RDATE" not in master:
            raise Refused("not_recurring")
        _add_exdate(master, occurrence)
        _drop_overrides(ical, master, occurrence, from_occurrence=False)
        if _series_empty(master) and not _overrides(ical):
            delete_components(dav_event, "VEVENT")
            return

    _save(dav_event, ical, master)


def parse_recurrence_id(value: str) -> datetime | date:
    """Parse the recurrence id Home Assistant echoes back.

    Dates first: parse_datetime accepts a date-only string too and would lose
    the value type EXDATE and UNTIL have to match.
    """
    parsed = dt_util.parse_date(value) or dt_util.parse_datetime(value)
    if parsed is None:
        raise Refused("bad_recurrence_id", value=str(value))
    return parsed


def _collapsed(ical: Any) -> Any:
    """Return the object with each repeated single-value property reduced."""
    for component in ical.walk("VEVENT"):
        collapse_repeated(component)
    return ical


def _master(ical: Any) -> Any:
    vevents = list(ical.walk("VEVENT"))
    if not vevents:
        raise Refused("no_event_in_object")
    for vevent in vevents:
        if "RECURRENCE-ID" not in vevent:
            return _dated(vevent)
    return _dated(vevents[0])


def _dated(vevent: Any) -> Any:
    """Return the component, refusing one stored without a start.

    RFC 5545 leaves DTSTART out once an object carries a METHOD.
    """
    if "DTSTART" not in vevent:
        raise Refused("no_start_in_object")
    return vevent


def _rule_string(master: Any) -> str | None:
    rrule = master.get("RRULE")
    return rrule.to_ical().decode("utf-8") if rrule is not None else None


def _overrides(ical: Any) -> list[Any]:
    return [vevent for vevent in ical.walk("VEVENT") if "RECURRENCE-ID" in vevent]


def _check_no_ranged_override(ical: Any) -> None:
    """Refuse an object whose exception covers everything from a day onwards.

    RFC 5545 RANGE=THISANDFUTURE is read nowhere here, so such an override
    would be handled as a single day. Apple Calendar and Outlook write it.
    """
    for override in _overrides(ical):
        params = getattr(override["RECURRENCE-ID"], "params", None) or {}
        if str(params.get("RANGE", "")).upper() == "THISANDFUTURE":
            raise Refused("ranged_override")


def _override(ical: Any, occurrence: datetime | date) -> Any | None:
    target = to_utc(occurrence)
    for vevent in _overrides(ical):
        if to_utc(vevent["RECURRENCE-ID"].dt) == target:
            return vevent
    return None


def _new_override(ical: Any, master: Any, occurrence: datetime | date) -> Any:
    override = ICalEvent.from_ical(master.to_ical())
    # RECURRENCE-ID too: the master may itself be a detached instance, and
    # RFC 5545 allows a component exactly one.
    for key in ("RRULE", "RDATE", "EXDATE", "RECURRENCE-ID"):
        if key in override:
            del override[key]
    replace(override, "dtstamp", dt_util.utcnow())
    replace(override, "sequence", None)
    override.add("RECURRENCE-ID", vDDDTypes(_align(master, occurrence)))
    _anchor_at(override, master, occurrence)
    ical.add_component(override)
    return override


def _anchor_at(component: Any, master: Any, occurrence: datetime | date) -> None:
    """Move a component cloned from the master onto the occurrence."""
    aligned = _align(master, occurrence)
    dtstart = master["DTSTART"].dt
    delta = _wall(master, aligned) - _wall(master, dtstart)
    zone = dtstart.tzinfo if isinstance(dtstart, datetime) else None
    _set_time(component, "dtstart", _rule_wall(master, occurrence) or aligned)
    if "DTEND" in component:
        _set_time(component, "dtend", _shifted(master["DTEND"].dt, delta, zone))


def _rule_wall(master: Any, occurrence: datetime | date) -> datetime | None:
    """Return the occurrence as the rule itself dates it, or None if not on it.

    An occurrence in a DST gap arrives resolved to an instant, which converted
    back lands an hour past the slot; the rule still holds the wall clock.
    """
    recur = master.get("RRULE")
    dtstart = master["DTSTART"].dt
    if recur is None or not isinstance(dtstart, datetime):
        return None
    target = to_utc(occurrence)
    for count, moment in enumerate(rule_from(recur, dtstart)):
        if count > _MAX_OCCURRENCES:
            raise Refused("rrule_too_dense")
        if to_utc(moment) >= target:
            return moment if to_utc(moment) == target else None
    return None


def _drop_overrides(
    ical: Any,
    master: Any,
    occurrence: datetime | date,
    from_occurrence: bool,
    all_overrides: bool = False,
) -> None:
    """Remove overrides, but never the component _master() returned."""
    target = to_utc(occurrence)
    for vevent in _overrides(ical):
        if vevent is master:
            continue
        moment = to_utc(vevent["RECURRENCE-ID"].dt)
        if all_overrides or (moment >= target if from_occurrence else moment == target):
            ical.subcomponents.remove(vevent)


def _align(master: Any, occurrence: datetime | date) -> datetime | date:
    """Return the occurrence in the value type and zone the master DTSTART uses."""
    dtstart = master["DTSTART"].dt
    if not isinstance(dtstart, datetime):
        return occurrence.date() if isinstance(occurrence, datetime) else occurrence
    if not isinstance(occurrence, datetime):
        occurrence = datetime.combine(occurrence, time.min)
    if dtstart.tzinfo is None:
        # A floating series is dated in local terms; the frontend echoes an
        # aware id back.
        if occurrence.tzinfo is None:
            return occurrence
        # An instant has two wall clocks in a DST gap or fold, and only the
        # rule knows which one it produced.
        return _rule_wall(master, occurrence) or dt_util.as_local(occurrence).replace(
            tzinfo=None
        )
    return to_utc(occurrence)


def _add_exdate(master: Any, occurrence: datetime | date) -> None:
    """Cancel an occurrence, once. A retry must not append a second line."""
    aligned = _align(master, occurrence)
    target = to_utc(aligned)
    if any(to_utc(value) == target for value in _date_values(master, "EXDATE")):
        return
    master.add("EXDATE", vDDDTypes(aligned))


def _is_occurrence(master: Any, occurrence: datetime | date) -> bool:
    target = to_utc(occurrence)
    # An excluded slot is not one; an override there would stay hidden.
    if any(to_utc(value) == target for value in _date_values(master, "EXDATE")):
        return False
    if target == to_utc(master["DTSTART"].dt):
        return True
    if master.get("RRULE") is not None and _on_rule(master, occurrence):
        return True
    return any(
        to_utc(_start_of(value)) == target for value in _date_values(master, "RDATE")
    )


def _series_empty(master: Any) -> bool:
    recur = master.get("RRULE")
    if recur is not None and not recur.get("COUNT") and not recur.get("UNTIL"):
        return False
    exdates = [to_utc(value) for value in _date_values(master, "EXDATE")]
    remaining = rruleset()
    remaining.rdate(to_utc(master["DTSTART"].dt))
    for value in _date_values(master, "RDATE"):
        remaining.rdate(to_utc(_start_of(value)))
    if recur is not None:
        rule = rule_from(recur, master["DTSTART"].dt)
        for count, moment in enumerate(rule):
            if count > len(exdates):
                return False
            if count > _MAX_OCCURRENCES:
                raise Refused("rrule_too_dense")
            remaining.rdate(to_utc(moment))
    for value in exdates:
        remaining.exdate(value)
    return next(iter(remaining), None) is None


def _tail_document(
    ical: Any,
    master: Any,
    data: dict[str, Any],
    occurrence: datetime | date,
    own_address: str | None = None,
) -> ICalCalendar:
    """Return the tail of a split as a document.

    The document, not its text, which caldav would run through vcal.fix.
    """
    tail = ICalCalendar.from_ical(ical.to_ical())
    vevent = _master(tail)
    target = to_utc(_align(master, occurrence))
    # The frontend names the exception's start when the split point carries
    # one, while the tail master is anchored on the rule slot.
    split = _override(tail, occurrence)
    baseline = split["DTSTART"].dt if split is not None else occurrence
    carried = []
    for override in _overrides(tail):
        # Exceptions past the cut belong to the tail, unless the rule is
        # being replaced outright.
        stale = to_utc(override["RECURRENCE-ID"].dt) < target
        if stale or _rule_replaced(master, data):
            tail.subcomponents.remove(override)
        else:
            carried.append(override)
    # Master first: caldav reads the first non-timezone component, and a
    # RECURRENCE-ID there sends it looking for a uid that does not exist yet.
    zones = [sub for sub in tail.subcomponents if sub.name == "VTIMEZONE"]
    rest = [
        sub
        for sub in tail.subcomponents
        if sub.name != "VTIMEZONE" and sub is not vevent
    ]
    tail.subcomponents = [*zones, vevent, *rest]
    uid = _tail_uid(str(master["UID"]), occurrence)
    for component in (vevent, *carried):
        replace(component, "uid", uid)
        replace(component, "dtstamp", dt_util.utcnow())
        replace(component, "sequence", None)
        # A kept RELATED-TO would make save_event write into other objects.
        replace(component, "related-to", None)
    # The carried exceptions were answered for; the series under them is new.
    reset_replies(vevent)
    _anchor_at(vevent, master, occurrence)
    _apply(
        vevent,
        _without_rrule(_rebased(master, data, occurrence, baseline)),
        own_address,
    )
    if _rule_replaced(master, data):
        replace(vevent, "rrule", vRecur.from_ical(data["rrule"]))
        check_rule(vevent["RRULE"], vevent["DTSTART"].dt)
        _clear_dates(vevent)
        return tail
    replace(vevent, "rrule", _tail_rrule(master, occurrence))
    if vevent.get("RRULE") is not None and not _self_anchored(vevent):
        raise Refused("rrule_mismatch")
    _keep_dates(vevent, "RDATE", occurrence, before=False)
    _keep_dates(vevent, "EXDATE", occurrence, before=False)
    # A wall-clock delta keeps the shift stable across DST.
    if delta := _wall(master, data.get("dtstart", baseline)) - _wall(master, baseline):
        dtstart = master["DTSTART"].dt
        zone = dtstart.tzinfo if isinstance(dtstart, datetime) else None
        _shift_dates(vevent, delta, zone)
        if (recur := vevent.get("RRULE")) is not None and (until := recur.get("UNTIL")):
            recur["UNTIL"] = [_shifted(until[0], delta, zone)]
        for override in carried:
            _shift_override(override, delta, zone)
    return tail


def _rebased(
    master: Any,
    data: dict[str, Any],
    occurrence: datetime | date,
    baseline: datetime | date,
) -> dict[str, Any]:
    """Return the span moved off the exception's own start onto its rule slot."""
    if baseline == occurrence or "dtstart" not in data:
        return data
    back = _wall(master, occurrence) - _wall(master, baseline)
    dtstart = master["DTSTART"].dt
    zone = dtstart.tzinfo if isinstance(dtstart, datetime) else None
    moved = dict(data)
    for key in ("dtstart", "dtend"):
        if moved.get(key) is not None:
            moved[key] = _shifted(moved[key], back, zone)
    return moved


def _shift_override(override: Any, delta: timedelta, zone: Any) -> None:
    """Move an exception with its series, RECURRENCE-ID included."""
    for key in ("RECURRENCE-ID", "DTSTART", "DTEND"):
        if key in override:
            shifted = _shifted(override[key].dt, delta, zone)
            params = override[key].params
            del override[key]
            override.add(key, shifted)
            override[key].params = params


def _tail_uid(uid: str, occurrence: datetime | date) -> str:
    """Return the uid of the series split off at an occurrence.

    Off the value as written, so a retry derives the same uid in every zone.
    """
    if isinstance(occurrence, datetime) and occurrence.tzinfo is not None:
        stamp = occurrence.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    else:
        stamp = as_datetime(occurrence).strftime("%Y%m%dT%H%M%S")
    return f"{uid}-{stamp}"


def _tail_rrule(master: Any, occurrence: datetime | date) -> vRecur | None:
    """Return the master's rule with COUNT reduced by the capped head."""
    rrule = master.get("RRULE")
    if rrule is None or _ends_before(master, occurrence):
        return None
    recur = vRecur(dict(rrule))
    if count := recur.get("COUNT"):
        recur["COUNT"] = [count[0] - _occurrences_before(master, occurrence)]
    return recur


def _occurrences_before(master: Any, occurrence: datetime | date) -> int:
    target = as_datetime(_align(master, occurrence))
    rule = rule_from(master["RRULE"], master["DTSTART"].dt)
    count = 0
    for moment in rule:
        if moment >= target:
            break
        count += 1
        if count > _MAX_OCCURRENCES:
            raise Refused("rrule_too_dense")
    return count


def _rule_replaced(master: Any, data: dict[str, Any]) -> bool:
    supplied = data.get("rrule")
    if not supplied:
        return False
    return (
        "RRULE" not in master
        or vRecur.from_ical(supplied).to_ical() != master["RRULE"].to_ical()
    )


def _self_anchored(master: Any) -> bool:
    """Return whether DTSTART is itself an occurrence of the rule."""
    rrule = master.get("RRULE")
    if rrule is None:
        return True
    start = as_datetime(master["DTSTART"].dt)
    return next(iter(rule_from(rrule, master["DTSTART"].dt)), None) == start


def _wall(master: Any, value: datetime | date) -> datetime:
    """Return the value as wall-clock time in the DTSTART anchor."""
    dtstart = master["DTSTART"].dt
    if not isinstance(dtstart, datetime):
        aligned = _align(master, value)
        return datetime.combine(aligned, time.min)
    zone = dtstart.tzinfo or dt_util.get_default_time_zone()
    if isinstance(value, datetime) and str(value.tzinfo) == str(zone):
        # Straight off the value: a spring-forward hour comes back an hour
        # later from a trip through UTC.
        return value.replace(tzinfo=None)
    return to_utc(value).astimezone(zone).replace(tzinfo=None)


def _on_rule(master: Any, occurrence: datetime | date) -> bool:
    # Both sides as instants: PEP 495 makes a wall clock in a DST gap or fold
    # compare unequal to the same instant in another zone.
    target = to_utc(_align(master, occurrence))
    rule = rule_from(master["RRULE"], master["DTSTART"].dt)
    for count, moment in enumerate(rule):
        moment = to_utc(moment)
        if moment >= target:
            return moment == target
        if count > _MAX_OCCURRENCES:
            raise Refused("rrule_too_dense")
    return False


def _ends_before(master: Any, occurrence: datetime | date) -> bool:
    """Return whether the rule's own occurrences all lie before the cutoff."""
    recur = master["RRULE"]
    if until := recur.get("UNTIL"):
        moment = until_frame(master["DTSTART"].dt, until[0])
        return to_utc(moment) < to_utc(_align(master, occurrence))
    if count := recur.get("COUNT"):
        return _occurrences_before(master, occurrence) >= count[0]
    return False


def _has_occurrences_from(master: Any, occurrence: datetime | date) -> bool:
    if master.get("RRULE") is not None and not _ends_before(master, occurrence):
        return True
    return _has_dates_from(master, "RDATE", occurrence)


def _dts(entry: Any) -> list[Any]:
    """Return the value holders of an EXDATE/RDATE entry.

    A parsed line is a vDDDLists exposing .dts; a single value added at
    runtime is a bare vDDDTypes.
    """
    return entry.dts if hasattr(entry, "dts") else [entry]


def _date_values(component: Any, key: str) -> list[Any]:
    if key not in component:
        return []
    entries = component[key]
    if not isinstance(entries, list):
        entries = [entries]
    return [item.dt for entry in entries for item in _dts(entry)]


def _has_dates_from(component: Any, key: str, occurrence: datetime | date) -> bool:
    target = to_utc(occurrence)
    return any(
        to_utc(_start_of(value)) >= target for value in _date_values(component, key)
    )


def _head_uncapped(
    calendar: caldav.Calendar, uid: str, occurrence: datetime | date
) -> bool:
    try:
        ical = object_by_uid(calendar, uid).icalendar_instance
    except NETWORK_ERRORS:
        return False
    return _has_occurrences_from(_master(ical), occurrence)


def _discard(tail: Any) -> None:
    try:
        tail.delete()
    except NETWORK_ERRORS as err:
        _LOGGER.warning(
            "The split-off series could not be removed after a failed update"
            " and may show duplicate events until the update is retried: %s",
            err,
        )


def _cap_series(master: Any, occurrence: datetime | date) -> None:
    rrule = master.get("RRULE")
    if rrule is None and "RDATE" not in master:
        raise Refused("not_recurring")
    # An UNTIL on a rule already ending before the cutoff would add occurrences.
    if rrule is not None and not _ends_before(master, occurrence):
        recur = vRecur(dict(rrule))
        recur.pop("COUNT", None)
        recur["UNTIL"] = [_until(master, occurrence)]
        replace(master, "rrule", recur)
    _keep_dates(master, "RDATE", occurrence, before=True)
    _keep_dates(master, "EXDATE", occurrence, before=True)


def _shift_dates(component: Any, delta: timedelta, zone: Any) -> None:
    for key in ("RDATE", "EXDATE"):
        if key not in component:
            continue
        entries = component[key]
        if not isinstance(entries, list):
            entries = [entries]
        shifted = []
        for entry in entries:
            rebuilt = vDDDLists(
                [_shifted(item.dt, delta, zone) for item in _dts(entry)]
            )
            rebuilt.params = entry.params
            shifted.append(rebuilt)
        del component[key]
        component[key] = shifted if len(shifted) > 1 else shifted[0]


def _shifted(value: Any, delta: timedelta, zone: Any) -> Any:
    """Shift in the series' wall clock, keeping the value's representation."""
    if isinstance(value, tuple):
        end = (
            _shifted(value[1], delta, zone)
            if isinstance(value[1], datetime)
            else value[1]
        )
        return (_shifted(value[0], delta, zone), end)
    if not isinstance(value, datetime) or value.tzinfo is None:
        return value + delta
    anchor = zone or dt_util.get_default_time_zone()
    wall = value.astimezone(anchor).replace(tzinfo=None) + delta
    return wall.replace(tzinfo=anchor).astimezone(value.tzinfo)


def _clear_dates(component: Any) -> None:
    for key in ("EXDATE", "RDATE"):
        if key in component:
            del component[key]


def _keep_dates(
    component: Any, key: str, occurrence: datetime | date, before: bool
) -> None:
    """Keep the RDATE or EXDATE values on one side of the occurrence.

    Lines are filtered one by one: merged, every value would share one TZID.
    """
    if key not in component:
        return
    entries = component[key]
    if not isinstance(entries, list):
        entries = [entries]
    target = to_utc(occurrence)
    kept_entries = []
    for entry in entries:
        items = _dts(entry)
        kept = [
            item.dt for item in items if (to_utc(_start_of(item.dt)) < target) == before
        ]
        if len(kept) == len(items):
            kept_entries.append(entry)
        elif kept:
            rebuilt = vDDDLists(kept)
            rebuilt.params = entry.params
            kept_entries.append(rebuilt)
    del component[key]
    if kept_entries:
        component[key] = kept_entries if len(kept_entries) > 1 else kept_entries[0]


def _start_of(value: Any) -> datetime | date:
    """Return the start of an RDATE value, which may be a period."""
    return value[0] if isinstance(value, tuple) else value


def _until(master: Any, occurrence: datetime | date) -> datetime | date:
    aligned = _align(master, occurrence)
    if isinstance(aligned, datetime):
        return aligned - timedelta(seconds=1)
    return aligned - timedelta(days=1)


def _apply(
    component: Any, data: dict[str, Any], own_address: str | None = None
) -> None:
    """Write the named fields onto a component, keyed on presence throughout.

    An absent key means "leave it" and a present None "clear it". The rule
    takes an empty string for that, since None is what an absent one looks
    like upstream.
    """
    if "summary" in data:
        replace(component, "summary", data["summary"])
    if "dtstart" in data:
        if "dtend" not in data:
            _shift_end(component, data["dtstart"])
        _set_time(component, "dtstart", data["dtstart"])
    if "dtend" in data:
        # RFC 5545 forbids DURATION alongside DTEND.
        replace(component, "duration", None)
        _set_time(component, "dtend", data["dtend"])
    if "description" in data:
        replace(component, "description", data["description"])
    if "location" in data:
        replace(component, "location", data["location"])
    if "rrule" in data:
        rrule = data["rrule"]
        replace(component, "rrule", vRecur.from_ical(rrule) if rrule else None)
        if rrule and "DTSTART" in component:
            check_rule(component["RRULE"], component["DTSTART"].dt)
    apply_extras(component, data, own_address)
    _check_span(component)


def _shift_end(component: Any, dtstart: datetime | date) -> None:
    """Move DTEND with DTSTART, so a start named on its own moves the event."""
    if "DTEND" not in component or "DTSTART" not in component:
        return
    old = component["DTSTART"].dt
    if isinstance(old, datetime) != isinstance(dtstart, datetime):
        return
    delta = _wall(component, dtstart) - _wall(component, old)
    zone = old.tzinfo if isinstance(old, datetime) else None
    _set_time(component, "dtend", _shifted(component["DTEND"].dt, delta, zone))


def _without_rrule(data: dict[str, Any]) -> dict[str, Any]:
    """Return the fields with any recurrence rule left out.

    A rule belongs to the series, never to one occurrence or the tail.
    """
    return {key: value for key, value in data.items() if key != "rrule"}


def _check_span(component: Any) -> None:
    """RFC 5545: DTEND shares DTSTART's value type and never precedes it."""
    if "DTSTART" not in component or "DTEND" not in component:
        return
    start, end = component["DTSTART"].dt, component["DTEND"].dt
    if isinstance(start, datetime) != isinstance(end, datetime):
        raise Refused("mixed_time_types")
    if to_utc(end) < to_utc(start):
        raise Refused("end_before_start")


def _set_time(component: Any, key: str, value: Any) -> None:
    """Write a time in the anchor the component already uses.

    Written as received, a summary-only edit would re-anchor a UTC, TZID or
    floating series and shift its occurrences across DST.
    """
    old = component[key].dt if key in component else None
    if (
        old is not None
        and isinstance(old, datetime) != isinstance(value, datetime)
        and any(k in component for k in ("RRULE", "RDATE", "RECURRENCE-ID"))
    ):
        raise Refused("allday_timed_switch")
    if (
        isinstance(old, datetime)
        and isinstance(value, datetime)
        and to_utc(old) == to_utc(value)
    ):
        return
    anchor = (
        old
        if old is not None
        else (component["DTSTART"].dt if "DTSTART" in component else None)
    )
    if isinstance(anchor, datetime) and isinstance(value, datetime):
        if anchor.tzinfo is None:
            value = dt_util.as_local(value).replace(tzinfo=None)
        elif str(value.tzinfo) != str(anchor.tzinfo):
            # A value already in the series zone keeps its wall clock, which
            # a trip through the instant would move out of a DST gap.
            value = value.astimezone(anchor.tzinfo)
    replace(component, key, value)


def _save(dav_event: caldav.Event, ical: Any, touched: Any) -> None:
    stamp(touched)
    # caldav bumps SEQUENCE regardless of increase_seqno, on the first
    # component that is not a timezone, which may be a VTODO sharing the
    # resource.
    hold_sequence(
        next((item for item in ical.subcomponents if item.name != "VTIMEZONE"), touched)
    )
    # An edit can introduce a TZID the object never defined. The document,
    # not its text, which caldav would run through vcal.fix.
    dav_event.icalendar_instance = zoned_document(ical)
    # only_this_recurrence would splice back only the first component.
    dav_event.save(increase_seqno=False, only_this_recurrence=False)
