"""Property-based tests for the recurring-series write path.

The example suites pin the cases somebody thought of. These state what has to
hold for every rule, every zone and every occurrence, and let hypothesis look
for the combination nobody wrote down.

Occurrences are expanded here with dateutil straight off the stored document,
not with the integration's own helpers, so an expansion and a write that are
wrong in the same way still fail.
"""

from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
from homeassistant.util import dt as dt_util
from hypothesis import HealthCheck, assume, given, settings, strategies as st
from icalendar import Calendar as ICalendar, Event as ICalEvent, vRecur
import pytest
from test_recurrence import FakeCalendar
import vobject

from custom_components.ha_caldav.api import zoned_document
from custom_components.ha_caldav.coordinator import sort_key, to_event
from custom_components.ha_caldav.errors import Refused
from custom_components.ha_caldav.event import to_utc
from custom_components.ha_caldav.recurrence import delete_event, update_event

UID = "prop-1"

# Expansion horizon. Small enough to stay fast, wide enough that a weekly rule
# reaches over a DST change from every start below.
LIMIT = 44

PROPERTY = settings(
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)

ZONES = (
    "UTC",
    "Europe/Berlin",
    "America/New_York",
    "Asia/Kolkata",
    "Australia/Eucla",
    "Pacific/Kiritimati",
)

# The zones Home Assistant itself is put in. A half-hour offset, one across the
# day line and one that changes over, because a UTC comparison leaking the local
# zone shows up in those and not in an even offset an hour off UTC.
LOCAL_ZONES = ("UTC", "America/New_York", "Asia/Kolkata", "Pacific/Kiritimati")

# Starts that put a weekly or daily series across a DST change: Berlin turns
# over on 2026-03-29 and 2026-10-25, New York on 2026-03-08 and 2026-11-01.
DAYS = (
    date(2026, 3, 5),
    date(2026, 3, 25),
    date(2026, 10, 22),
    date(2026, 10, 28),
    date(2026, 1, 31),
    date(2026, 7, 6),
)

# 02:30 does not exist in Berlin on 2026-03-29 and happens twice on 2026-10-25;
# 23:30 and 00:30 put the local day either side of UTC's.
TIMES = (time(0, 30), time(2, 30), time(9, 0), time(23, 30))

WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")

VOLATILE = ("DTSTAMP", "LAST-MODIFIED", "CREATED")


@contextmanager
def local_zone(name):
    """Pin Home Assistant's local zone for the duration of one edit."""
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(ZoneInfo(name))
    try:
        yield
    finally:
        dt_util.set_default_time_zone(previous)


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------


@st.composite
def rules(draw, day, kind, moment, zone):
    """Return an RRULE whose DTSTART is one of its own occurrences.

    RFC 5545 3.8.5.3 has a start that the rule does not itself produce, and the
    module refuses to move such a series at all; a generator that produced them
    would spend every example on that one refusal.
    """
    freq = draw(st.sampled_from(("DAILY", "WEEKLY", "MONTHLY", "YEARLY")))
    parts = [f"FREQ={freq}"]
    if (interval := draw(st.integers(1, 4))) > 1:
        parts.append(f"INTERVAL={interval}")
    own = WEEKDAYS[day.weekday()]
    if freq in ("DAILY", "WEEKLY"):
        extra = draw(st.lists(st.sampled_from(WEEKDAYS), max_size=2, unique=True))
        if extra:
            days = sorted({own, *extra}, key=WEEKDAYS.index)
            parts.append("BYDAY=" + ",".join(days))
    elif freq == "MONTHLY":
        nth = (day.day - 1) // 7 + 1
        shape = draw(st.sampled_from(("plain", "monthday", "nthday", "setpos")))
        if shape == "monthday":
            parts.append(f"BYMONTHDAY={day.day}")
        elif shape == "nthday":
            parts.append(f"BYDAY={nth}{own}")
        elif shape == "setpos":
            parts.append(f"BYDAY={own};BYSETPOS={nth}")
    elif draw(st.booleans()):
        parts.append(f"BYMONTH={day.month};BYMONTHDAY={day.day}")
    ending = draw(st.sampled_from(("count", "until", "open")))
    if ending == "count":
        parts.append(f"COUNT={draw(st.integers(2, 8))}")
    elif ending == "until":
        span = draw(st.sampled_from((40, 200, 900)))
        parts.append(f"UNTIL={_until(day + timedelta(days=span), kind, moment, zone)}")
    return ";".join(parts)


def _until(day, kind, moment, zone):
    """Return an UNTIL in the value type and frame RFC 5545 3.3.10 requires."""
    if kind == "allday":
        return day.strftime("%Y%m%d")
    if kind == "floating":
        return datetime.combine(day, moment).strftime("%Y%m%dT%H%M%S")
    stamp = datetime.combine(day, moment, tzinfo=ZoneInfo(zone))
    return stamp.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


@st.composite
def series(draw, kinds=("utc", "zoned", "floating", "allday"), extras=True):
    """Return a one-component VCALENDAR holding a recurring event."""
    kind = draw(st.sampled_from(kinds))
    day = draw(st.sampled_from(DAYS))
    moment = draw(st.sampled_from(TIMES))
    zone = "UTC" if kind == "utc" else draw(st.sampled_from(ZONES))
    rule = draw(rules(day, kind, moment, zone))
    if kind == "allday":
        start = day
        end = day + timedelta(days=draw(st.integers(1, 2)))
        offset = timedelta(days=0)
        loose = timedelta(days=100)
    else:
        start = datetime.combine(day, moment)
        if kind != "floating":
            start = start.replace(tzinfo=ZoneInfo(zone))
        end = start + draw(
            st.sampled_from((timedelta(minutes=30), timedelta(hours=25)))
        )
        offset = timedelta(hours=2)
        loose = timedelta(days=100, hours=3)
    slots = _slots(start, rule)
    exdates, rdates, overrides = [], [], []
    if extras and len(slots) > 2:
        picked = draw(st.integers(1, min(3, len(slots) - 1)))
        if draw(st.booleans()):
            exdates.append(slots[picked])
        if draw(st.booleans()):
            rdates.append(start + loose)
        if draw(st.booleans()) and slots[picked] not in exdates:
            overrides.append((slots[picked], offset))
    return document(start, end, rule, exdates, rdates, overrides)


def _slots(start, rule):
    """Return the first occurrences of a rule, for hanging extras off."""
    anchor = start if isinstance(start, datetime) else datetime.combine(start, time.min)
    found = []
    for index, produced in enumerate(rrulestr(rule, dtstart=anchor)):
        if index >= 6:
            break
        found.append(produced.date() if not isinstance(start, datetime) else produced)
    return found


def document(start, end, rule, exdates=(), rdates=(), overrides=()):
    """Return the ICS text for a series and its extra, cancelled and moved days."""
    calendar = ICalendar()
    calendar.add("prodid", "-//ha_caldav//properties//EN")
    calendar.add("version", "2.0")
    event = ICalEvent()
    event.add("uid", UID)
    event.add("dtstamp", datetime(2026, 1, 1, tzinfo=UTC))
    event.add("summary", "Series")
    event.add("dtstart", start)
    event.add("dtend", end)
    event.add("rrule", vRecur.from_ical(rule))
    for value in exdates:
        event.add("exdate", value)
    for value in rdates:
        event.add("rdate", value)
    calendar.add_component(event)
    for moment, span in overrides:
        exception = ICalEvent()
        exception.add("uid", UID)
        exception.add("dtstamp", datetime(2026, 1, 1, tzinfo=UTC))
        exception.add("summary", "Moved")
        exception.add("recurrence-id", moment)
        exception.add("dtstart", moment + span)
        exception.add("dtend", moment + span + (end - start))
        calendar.add_component(exception)
    return calendar.to_ical().decode("utf-8")


# --------------------------------------------------------------------------
# Independent expansion
# --------------------------------------------------------------------------


def master_of(ical):
    for vevent in ical.walk("VEVENT"):
        if "RECURRENCE-ID" not in vevent:
            return vevent
    return None


def key(value):
    """Return the comparable form of an occurrence, free of the local zone."""
    if not isinstance(value, datetime):
        return value.isoformat()
    if value.tzinfo is None:
        return value.isoformat()
    return value.astimezone(UTC).isoformat()


def _values(component, name):
    if name not in component:
        return []
    entries = component[name]
    if not isinstance(entries, list):
        entries = [entries]
    found = []
    for entry in entries:
        for item in entry.dts if hasattr(entry, "dts") else [entry]:
            found.append(item.dt[0] if isinstance(item.dt, tuple) else item.dt)
    return found


def occurrences(source):
    """Expand a stored document into the occurrences a server would report."""
    ical = ICalendar.from_ical(source) if isinstance(source, str) else source
    master = master_of(ical)
    if master is None or "DTSTART" not in master:
        return []
    start = master["DTSTART"].dt
    all_day = not isinstance(start, datetime)
    anchor = start if isinstance(start, datetime) else datetime.combine(start, time.min)
    found = []
    if (recur := master.get("RRULE")) is not None:
        rule = rrulestr(recur.to_ical().decode("utf-8"), dtstart=anchor)
        for index, produced in enumerate(rule):
            if index >= LIMIT:
                break
            found.append(produced)
    else:
        found.append(anchor)
    found.extend(_values(master, "RDATE"))
    dropped = {key(value) for value in _values(master, "EXDATE")}
    seen = set()
    kept = []
    for produced in found:
        value = (
            produced.date() if all_day and isinstance(produced, datetime) else produced
        )
        marker = key(value)
        if marker in dropped or marker in seen:
            continue
        seen.add(marker)
        kept.append(value)
    return sorted(kept, key=key)


def rule_slots(source):
    """Return the occurrences the stored rule makes on its own, extras aside."""
    ical = ICalendar.from_ical(source) if isinstance(source, str) else source
    master = master_of(ical)
    if master is None or (recur := master.get("RRULE")) is None:
        return []
    start = master["DTSTART"].dt
    anchor = start if isinstance(start, datetime) else datetime.combine(start, time.min)
    found = []
    text = recur.to_ical().decode("utf-8")
    for index, produced in enumerate(rrulestr(text, dtstart=anchor)):
        if index >= LIMIT:
            break
        found.append(produced if isinstance(start, datetime) else produced.date())
    return found


def overrides_of(ical):
    return [vevent for vevent in ical.walk("VEVENT") if "RECURRENCE-ID" in vevent]


def orphans(source):
    """Return the exceptions naming a slot the stored series no longer has.

    RFC 5545 3.8.4.4 attaches an exception by its RECURRENCE-ID alone. One that
    names a moment the master no longer produces is not an exception any more:
    every client shows it as a second event beside the series.
    """
    ical = ICalendar.from_ical(source) if isinstance(source, str) else source
    slots = {key(value) for value in occurrences(ical)}
    return [
        str(vevent["RECURRENCE-ID"].dt)
        for vevent in overrides_of(ical)
        if key(vevent["RECURRENCE-ID"].dt) not in slots
    ]


def horizon_of(values):
    """Return the cutoff past which a truncated expansion says nothing."""
    return key(values[LIMIT - 4]) if len(values) >= LIMIT - 3 else None


def clipped(values, horizon):
    if horizon is None:
        return [key(value) for value in values]
    return [key(value) for value in values if key(value) <= horizon]


def recurrence_id(occurrence):
    """Return the id Home Assistant echoes back for an occurrence.

    Aware throughout, including for a floating series: the frontend has a zone
    and puts one on, which is the reading recurrence._align is written for.
    """
    if not isinstance(occurrence, datetime):
        return occurrence.isoformat()
    if occurrence.tzinfo is None:
        return occurrence.replace(tzinfo=dt_util.get_default_time_zone()).isoformat()
    return occurrence.isoformat()


def _in_a_gap(wall, zone):
    """Return whether a zone skips over a wall clock on a spring-forward."""
    return (
        wall.replace(tzinfo=zone).astimezone(UTC).astimezone(zone).replace(tzinfo=None)
        != wall
    )


def nameable(occurrence):
    """Return whether an id for this occurrence survives the trip through UTC.

    A floating series is dated in the local zone, and recurrence._align reads
    the id there. An occurrence whose wall clock that zone skips comes back an
    hour later and names a slot the series does not have; every operation then
    reports success and writes something nobody asked for. Left out here and
    reported instead, so the committed suite stays green.
    """
    if not isinstance(occurrence, datetime) or occurrence.tzinfo is not None:
        return True
    return not _in_a_gap(occurrence, dt_util.get_default_time_zone())


def anchorable(occurrence):
    """Return whether the occurrence's own zone has the wall clock it names.

    A split writes the tail's start by converting the instant back into the
    series zone (recurrence._anchor_at). A spring-forward gap moves it on by an
    hour, and the tail keeps that hour for good. Reported rather than pinned.
    """
    if not isinstance(occurrence, datetime):
        return True
    if occurrence.tzinfo is None:
        return nameable(occurrence)
    return not _in_a_gap(occurrence.replace(tzinfo=None), occurrence.tzinfo)


def stable(source, *, versioned=True):
    """Return the stored document with the properties every write rewrites gone.

    SEQUENCE stays in by default: two edits made from different zones are the
    same edit and owe the same version. Only a comparison of one edit against
    itself repeated drops it, because RFC 5545 3.8.7.4 has a second write count.
    """
    ical = ICalendar.from_ical(source) if isinstance(source, str) else source
    for component in ical.walk():
        for name in VOLATILE if versioned else (*VOLATILE, "SEQUENCE"):
            component.pop(name, None)
    return ical.to_ical().decode("utf-8")


def stored_of(calendar):
    """Return what a calendar holds after an edit: the object, or nothing."""
    if calendar.event.deleted:
        return None
    return calendar.event.data


# --------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------


@settings(PROPERTY, max_examples=160)
@given(ics=series(), index=st.integers(0, LIMIT - 1), zone=st.sampled_from(LOCAL_ZONES))
def test_deleting_one_occurrence_removes_exactly_that_one(ics, index, zone) -> None:
    """Delete an occurrence, list the series: the set less that one, no more."""
    with local_zone(zone):
        before = occurrences(ics)
        assume(index < len(before))
        target = before[index]
        assume(nameable(target))
        horizon = horizon_of(before)
        calendar = FakeCalendar(ics)
        delete_event(calendar, UID, recurrence_id=recurrence_id(target))
        after = stored_of(calendar)
        listed = occurrences(after) if after is not None else []

        expected = [value for value in clipped(before, horizon) if value != key(target)]
        assert clipped(listed, horizon) == expected
        assert after is None or not orphans(after)


@settings(PROPERTY, max_examples=130)
@given(ics=series(), index=st.integers(1, LIMIT - 1), zone=st.sampled_from(LOCAL_ZONES))
def test_deleting_from_an_occurrence_keeps_exactly_the_earlier_ones(
    ics, index, zone
) -> None:
    """A this-and-following delete is a cut, not a reshape of what stays."""
    with local_zone(zone):
        before = occurrences(ics)
        assume(index < len(before))
        target = before[index]
        assume(nameable(target))
        calendar = FakeCalendar(ics)
        delete_event(
            calendar, UID, recurrence_id=recurrence_id(target), this_and_future=True
        )
        after = stored_of(calendar)
        listed = occurrences(after) if after is not None else []

        assert [key(value) for value in listed] == [
            key(value) for value in before[:index]
        ]
        assert after is None or not orphans(after)


# --------------------------------------------------------------------------
# Split conservation
# --------------------------------------------------------------------------


@settings(PROPERTY, max_examples=160)
@given(ics=series(), index=st.integers(1, LIMIT - 1), zone=st.sampled_from(LOCAL_ZONES))
def test_a_split_conserves_the_occurrence_set(ics, index, zone) -> None:
    """Head and tail together are the series, and share no occurrence."""
    with local_zone(zone):
        before = occurrences(ics)
        assume(index < len(before))
        target = before[index]
        # An RDATE inside the rule's span re-anchors the tail, and the module
        # refuses that split outright; test_recurrence covers the refusal.
        assume(key(target) in {key(value) for value in rule_slots(ics)})
        assume(anchorable(target))
        horizon = horizon_of(before)
        calendar = FakeCalendar(ics)
        try:
            update_event(
                calendar,
                UID,
                {"summary": "Split"},
                recurrence_id=recurrence_id(target),
                this_and_future=True,
            )
        except Refused as err:
            pytest.fail(f"refused a split of an on-rule occurrence: {err}")

        assert len(calendar.created) == 1
        stored = stored_of(calendar)
        head = clipped(occurrences(stored), horizon)
        tail = clipped(occurrences(calendar.created[0].data), horizon)

        assert not set(head) & set(tail)
        assert sorted(head + tail) == sorted(clipped(before, horizon))
        assert head == clipped(before[:index], horizon)
        assert not orphans(stored)
        assert not orphans(calendar.created[0].data)


@settings(PROPERTY, max_examples=130)
@given(ics=series(), index=st.integers(1, LIMIT - 1), zone=st.sampled_from(LOCAL_ZONES))
def test_a_split_hands_the_tail_the_summary_and_the_head_the_old_one(
    ics, index, zone
) -> None:
    """The edit reaches the half it was asked for and only that half."""
    with local_zone(zone):
        before = occurrences(ics)
        assume(index < len(before))
        assume(key(before[index]) in {key(value) for value in rule_slots(ics)})
        assume(anchorable(before[index]))
        calendar = FakeCalendar(ics)
        update_event(
            calendar,
            UID,
            {"summary": "Split"},
            recurrence_id=recurrence_id(before[index]),
            this_and_future=True,
        )

        head = master_of(ICalendar.from_ical(stored_of(calendar)))
        tail = master_of(ICalendar.from_ical(calendar.created[0].data))
        assert str(head["SUMMARY"]) == "Series"
        assert str(tail["SUMMARY"]) == "Split"
        assert str(tail["UID"]) != str(head["UID"])


# --------------------------------------------------------------------------
# Zone independence
# --------------------------------------------------------------------------


def _under_each_zone(edit, ics):
    """Run one edit under several local zones, returning what each stored."""
    written = []
    for name in LOCAL_ZONES:
        with local_zone(name):
            calendar = FakeCalendar(ics)
            edit(calendar)
            head = stored_of(calendar)
            written.append(
                (
                    name,
                    stable(head) if head is not None else None,
                    [stable(created.data) for created in calendar.created],
                )
            )
    return written


def _same_everywhere(written) -> None:
    first = written[0]
    for other in written[1:]:
        assert other[1] == first[1], f"{other[0]} stored differently from {first[0]}"
        assert other[2] == first[2], f"{other[0]} split differently from {first[0]}"


@settings(PROPERTY, max_examples=80)
@given(ics=series(kinds=("utc", "zoned", "allday")), index=st.integers(0, LIMIT - 1))
def test_deleting_an_occurrence_does_not_depend_on_the_local_zone(ics, index) -> None:
    """An anchored series is dated by the server, not by where the client runs."""
    with local_zone("UTC"):
        before = occurrences(ics)
    assume(index < len(before))
    marker = recurrence_id(before[index])

    def edit(calendar):
        delete_event(calendar, UID, recurrence_id=marker)

    _same_everywhere(_under_each_zone(edit, ics))


@settings(PROPERTY, max_examples=80)
@given(
    # Not the all-day kind: recurrence._tail_uid derives the tail's uid from
    # to_utc(occurrence), and a bare date reads there as local midnight, so the
    # same split lands under a different uid in every zone. Reported, not pinned.
    ics=series(kinds=("utc", "zoned")),
    index=st.integers(1, LIMIT - 1),
)
def test_splitting_does_not_depend_on_the_local_zone(ics, index) -> None:
    """Nor does the object a split writes, nor the uid it writes it under."""
    with local_zone("UTC"):
        before = occurrences(ics)
    assume(index < len(before))
    assume(key(before[index]) in {key(value) for value in rule_slots(ics)})
    assume(anchorable(before[index]))
    marker = recurrence_id(before[index])

    def edit(calendar):
        update_event(
            calendar,
            UID,
            {"summary": "Split"},
            recurrence_id=marker,
            this_and_future=True,
        )

    _same_everywhere(_under_each_zone(edit, ics))


@settings(PROPERTY, max_examples=80)
@given(ics=series(kinds=("utc", "zoned", "allday")))
def test_renaming_a_series_does_not_depend_on_the_local_zone(ics) -> None:
    """A rename must not re-anchor anything, wherever it is made from."""

    def edit(calendar):
        update_event(calendar, UID, {"summary": "Renamed"})

    _same_everywhere(_under_each_zone(edit, ics))


# --------------------------------------------------------------------------
# Idempotence
# --------------------------------------------------------------------------


@settings(PROPERTY, max_examples=100)
@given(ics=series(), index=st.integers(1, LIMIT - 1), zone=st.sampled_from(LOCAL_ZONES))
def test_repeating_a_split_neither_clones_the_tail_nor_moves_the_head(
    ics, index, zone
) -> None:
    """The retry path rests on this: a second attempt is a no-op."""
    with local_zone(zone):
        before = occurrences(ics)
        assume(index < len(before))
        assume(key(before[index]) in {key(value) for value in rule_slots(ics)})
        assume(anchorable(before[index]))
        marker = recurrence_id(before[index])
        first = FakeCalendar(ics)
        update_event(
            first, UID, {"summary": "Split"}, recurrence_id=marker, this_and_future=True
        )
        once = stored_of(first)
        second = FakeCalendar(once)
        update_event(
            second,
            UID,
            {"summary": "Split"},
            recurrence_id=marker,
            this_and_future=True,
        )

        assert not second.created
        assert occurrences(stored_of(second)) == occurrences(once)


@settings(PROPERTY, max_examples=100)
@given(ics=series(), index=st.integers(0, LIMIT - 1), zone=st.sampled_from(LOCAL_ZONES))
def test_repeating_an_occurrence_delete_leaves_the_same_series(
    ics, index, zone
) -> None:
    """A retried delete removes the occurrence once and nothing else after."""
    with local_zone(zone):
        before = occurrences(ics)
        assume(index < len(before))
        assume(nameable(before[index]))
        marker = recurrence_id(before[index])
        first = FakeCalendar(ics)
        delete_event(first, UID, recurrence_id=marker)
        once = stored_of(first)
        assume(once is not None)
        second = FakeCalendar(once)
        delete_event(second, UID, recurrence_id=marker)
        twice = stored_of(second)

        assert twice is not None
        assert occurrences(twice) == occurrences(once)


@settings(PROPERTY, max_examples=80)
@given(ics=series(kinds=("utc", "zoned", "allday")), zone=st.sampled_from(LOCAL_ZONES))
def test_repeating_a_rename_stores_the_same_object(ics, zone) -> None:
    """Nothing about a rename accumulates."""
    with local_zone(zone):
        first = FakeCalendar(ics)
        update_event(first, UID, {"summary": "Renamed"})
        once = stored_of(first)
        second = FakeCalendar(once)
        update_event(second, UID, {"summary": "Renamed"})

        assert stable(stored_of(second), versioned=False) == stable(
            once, versioned=False
        )


# --------------------------------------------------------------------------
# Wall-clock stability
# --------------------------------------------------------------------------

DST_ZONES = ("Europe/Berlin", "America/New_York")


def wall_shifted(value, days, zone):
    """Return the value the same days later on the same clock."""
    if not isinstance(value, datetime):
        return value + timedelta(days=days)
    naive = value.astimezone(zone).replace(tzinfo=None) + timedelta(days=days)
    return naive.replace(tzinfo=zone)


@settings(PROPERTY, max_examples=100)
@given(
    zone=st.sampled_from(DST_ZONES),
    day=st.sampled_from(DAYS),
    moment=st.sampled_from(TIMES),
    days=st.sampled_from((1, 3, 4, 14, 40)),
    local=st.sampled_from(LOCAL_ZONES),
)
def test_moving_a_series_keeps_every_wall_clock(zone, day, moment, days, local) -> None:
    """A series nudged by whole days keeps the hour it is held at, over DST.

    The extra and the cancelled dates go with it: a shift taken as an instant
    rather than on the clock moves them by an hour on one side of the change.
    """
    anchor = ZoneInfo(zone)
    start = datetime.combine(day, moment, tzinfo=anchor)
    moved = wall_shifted(start, days, anchor)
    assume(anchorable(moved))
    ics = document(
        start,
        start + timedelta(hours=1),
        "FREQ=DAILY;COUNT=12",
        exdates=[start + timedelta(days=3)],
        rdates=[start + timedelta(days=40)],
    )
    with local_zone(local):
        before = occurrences(ics)
        calendar = FakeCalendar(ics)
        update_event(calendar, UID, {"dtstart": moved})
        after = occurrences(stored_of(calendar))

        assert [key(value) for value in after] == [
            key(wall_shifted(value, days, anchor)) for value in before
        ]


@settings(PROPERTY, max_examples=100)
@given(
    zone=st.sampled_from(DST_ZONES),
    day=st.sampled_from(DAYS),
    moment=st.sampled_from(TIMES),
    days=st.sampled_from((1, 3, 4, 14, 40)),
    local=st.sampled_from(LOCAL_ZONES),
)
def test_moving_a_series_carries_its_exceptions_on_the_same_clock(
    zone, day, moment, days, local
) -> None:
    """An exception that stays attached is one whose slot the rule still makes."""
    anchor = ZoneInfo(zone)
    start = datetime.combine(day, moment, tzinfo=anchor)
    slot = start + timedelta(days=5)
    moved = wall_shifted(start, days, anchor)
    assume(anchorable(moved))
    ics = document(
        start,
        start + timedelta(hours=1),
        "FREQ=DAILY;COUNT=12",
        overrides=[(slot, timedelta(hours=2))],
    )
    with local_zone(local):
        calendar = FakeCalendar(ics)
        update_event(calendar, UID, {"dtstart": moved})
        stored = stored_of(calendar)
        ical = ICalendar.from_ical(stored)
        exceptions = overrides_of(ical)

        assert len(exceptions) == 1
        carried = exceptions[0]
        assert key(carried["RECURRENCE-ID"].dt) == key(wall_shifted(slot, days, anchor))
        assert key(carried["DTSTART"].dt) == key(
            wall_shifted(slot + timedelta(hours=2), days, anchor)
        )
        assert not orphans(ical)


# --------------------------------------------------------------------------
# No silent no-op
# --------------------------------------------------------------------------


@settings(PROPERTY, max_examples=160)
@given(ics=series(), index=st.integers(0, LIMIT - 1), zone=st.sampled_from(LOCAL_ZONES))
def test_editing_one_occurrence_writes_it_and_leaves_the_rest(ics, index, zone) -> None:
    """An occurrence edit lands on the slot it named and moves no other."""
    with local_zone(zone):
        before = occurrences(ics)
        assume(index < len(before))
        target = before[index]
        assume(nameable(target))
        calendar = FakeCalendar(ics)
        update_event(
            calendar, UID, {"summary": "One"}, recurrence_id=recurrence_id(target)
        )
        ical = ICalendar.from_ical(stored_of(calendar))
        named = [
            v for v in overrides_of(ical) if key(v["RECURRENCE-ID"].dt) == key(target)
        ]

        assert len(named) == 1
        assert str(named[0]["SUMMARY"]) == "One"
        assert str(master_of(ical)["SUMMARY"]) == "Series"
        assert [key(value) for value in occurrences(ical)] == [
            key(value) for value in before
        ]
        assert not orphans(ical)


@settings(PROPERTY, max_examples=130)
@given(ics=series(), index=st.integers(0, LIMIT - 1), zone=st.sampled_from(LOCAL_ZONES))
def test_editing_an_occurrence_the_series_does_not_have_is_refused(
    ics, index, zone
) -> None:
    """A success that stored nothing is worse than a refusal."""
    with local_zone(zone):
        before = occurrences(ics)
        assume(index < len(before))
        target = before[index]
        # A second past an occurrence is never one itself. A day past one can
        # be, so an all-day series is asked about a day the rule leaves free,
        # or about the day before it starts when a daily rule leaves none.
        if isinstance(target, datetime):
            gap = target + timedelta(seconds=1)
        else:
            later = [value for value in before if value > target]
            room = later and later[0] > target + timedelta(days=1)
            gap = target + timedelta(days=1) if room else before[0] - timedelta(days=1)
        calendar = FakeCalendar(ics)
        with pytest.raises(Refused):
            update_event(
                calendar, UID, {"summary": "Nowhere"}, recurrence_id=recurrence_id(gap)
            )
        assert not calendar.event.saved


# --------------------------------------------------------------------------
# The loop between what is read and what is written
# --------------------------------------------------------------------------


def served(ics, occurrence):
    """Return the VEVENT the read path sees for one expanded occurrence.

    Through vobject, which is the library the coordinator reads with, and the
    same document wrapper the write path uses.
    """
    master = master_of(ICalendar.from_ical(ics))
    span = master["DTEND"].dt - master["DTSTART"].dt
    component = ICalEvent()
    component.add("uid", UID)
    component.add("dtstamp", datetime(2026, 1, 1, tzinfo=UTC))
    component.add("summary", "Series")
    component.add("dtstart", occurrence)
    component.add("dtend", occurrence + span)
    component.add("recurrence-id", occurrence)
    holder = ICalendar()
    holder.add("prodid", "-//ha_caldav//properties//EN")
    holder.add("version", "2.0")
    holder.add_component(component)
    text = zoned_document(holder).to_ical().decode("utf-8")
    return vobject.readOne(text).vevent


@settings(PROPERTY, max_examples=130)
@given(
    # Not the TZID kinds: vobject keeps only the first value of the multi-value
    # RDATE that api._timezone generates, so a zone read back through it loses
    # every transition after 1981. Reported rather than pinned.
    ics=series(kinds=("utc", "floating", "allday")),
    index=st.integers(0, LIMIT - 1),
    zone=st.sampled_from(LOCAL_ZONES),
)
def test_the_id_the_read_path_publishes_names_the_occurrence_it_came_from(
    ics, index, zone
) -> None:
    """What the panel is given back is what the write path has to resolve.

    Nothing normalizes the id between the two, so an occurrence the read path
    can show and the write path cannot find is one nobody can edit.
    """
    with local_zone(zone):
        before = occurrences(ics)
        assume(index < len(before))
        target = before[index]
        component = served(ics, target)
        event = to_event(component)
        assert event is not None
        # Both sides through UTC: PEP 495 has a local time that a spring-forward
        # skipped compare unequal to its own instant in another zone, so the two
        # never meet on ==, only on the instant each stands for.
        assert key(sort_key(component)) == key(to_utc(target))

        horizon = horizon_of(before)
        calendar = FakeCalendar(ics)
        delete_event(calendar, UID, recurrence_id=event.recurrence_id)
        after = stored_of(calendar)
        listed = occurrences(after) if after is not None else []

        assert clipped(listed, horizon) == [
            value for value in clipped(before, horizon) if value != key(target)
        ]
