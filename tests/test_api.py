"""Tests for the CalDAV write operations, mainly recurring series handling."""

from datetime import UTC, date, datetime

from icalendar import Calendar as ICalendar
import pytest

from custom_components.ha_caldav.api import (
    delete_event,
    parse_recurrence_id,
    update_event,
)

TIMED_SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:timed-1
DTSTAMP:20260101T000000Z
DTSTART:20260706T090000Z
DTEND:20260706T100000Z
RRULE:FREQ=WEEKLY
SUMMARY:Standup
END:VEVENT
END:VCALENDAR
"""

ALL_DAY_SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:allday-1
DTSTAMP:20260101T000000Z
DTSTART;VALUE=DATE:20260706
DTEND;VALUE=DATE:20260707
RRULE:FREQ=DAILY
SUMMARY:Water plants
END:VEVENT
END:VCALENDAR
"""

COUNTED_SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:counted-1
DTSTAMP:20260101T000000Z
DTSTART:20260706T090000Z
DTEND:20260706T100000Z
RRULE:FREQ=WEEKLY;COUNT=10
SUMMARY:Standup
END:VEVENT
END:VCALENDAR
"""

SERIES_WITH_OVERRIDE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:timed-1
DTSTAMP:20260101T000000Z
DTSTART:20260706T090000Z
DTEND:20260706T100000Z
RRULE:FREQ=WEEKLY
SUMMARY:Standup
END:VEVENT
BEGIN:VEVENT
UID:timed-1
DTSTAMP:20260101T000000Z
RECURRENCE-ID:20260713T090000Z
DTSTART:20260713T110000Z
DTEND:20260713T120000Z
SUMMARY:Standup moved
END:VEVENT
END:VCALENDAR
"""

SINGLE_EVENT = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:single-1
DTSTAMP:20260101T000000Z
DTSTART:20260706T090000Z
DTEND:20260706T100000Z
SUMMARY:Dentist
END:VEVENT
END:VCALENDAR
"""

# Same series as SERIES_WITH_OVERRIDE, but the server happens to store the
# override before the master; RFC 5545 does not promise any order.
SERIES_WITH_OVERRIDE_FIRST = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:timed-1
DTSTAMP:20260101T000000Z
RECURRENCE-ID:20260713T090000Z
DTSTART:20260713T110000Z
DTEND:20260713T120000Z
SUMMARY:Standup moved
END:VEVENT
BEGIN:VEVENT
UID:timed-1
DTSTAMP:20260101T000000Z
DTSTART:20260706T090000Z
DTEND:20260706T100000Z
RRULE:FREQ=WEEKLY
SUMMARY:Standup
END:VEVENT
END:VCALENDAR
"""

SECOND_OCCURRENCE = "2026-07-13 09:00:00+00:00"
FIRST_OCCURRENCE = "2026-07-06 09:00:00+00:00"


class FakeEvent:
    """Stand-in for a caldav.Event backed by a real icalendar object."""

    def __init__(self, ics: str) -> None:
        self.data = ics
        self.saved = False
        self.save_kwargs: dict | None = None
        self.deleted = False
        self._instance = ICalendar.from_ical(ics)

    @property
    def icalendar_instance(self) -> ICalendar:
        return self._instance

    def save(self, **kwargs) -> None:
        self.saved = True
        self.save_kwargs = kwargs

    def delete(self) -> None:
        self.deleted = True

    def stored(self) -> ICalendar:
        """Return what would have been written back to the server."""
        return ICalendar.from_ical(self.data)


class FakeCalendar:
    """Stand-in for a caldav.Calendar."""

    def __init__(self, ics: str) -> None:
        self.event = FakeEvent(ics)
        self.added: list[dict] = []

    def event_by_uid(self, uid: str) -> FakeEvent:
        return self.event

    def add_event(self, **kwargs) -> None:
        self.added.append(kwargs)


def _master(ical: ICalendar):
    return next(c for c in ical.walk("VEVENT") if "RECURRENCE-ID" not in c)


def _overrides(ical: ICalendar) -> list:
    return [c for c in ical.walk("VEVENT") if "RECURRENCE-ID" in c]


def _exdates(component) -> list:
    exdate = component.get("EXDATE")
    if exdate is None:
        return []
    entries = exdate if isinstance(exdate, list) else [exdate]
    return [dt.dt for entry in entries for dt in entry.dts]


def test_parse_recurrence_id_datetime() -> None:
    assert parse_recurrence_id(SECOND_OCCURRENCE) == datetime(
        2026, 7, 13, 9, 0, tzinfo=UTC
    )


def test_parse_recurrence_id_date() -> None:
    assert parse_recurrence_id("2026-07-08") == date(2026, 7, 8)


def test_parse_recurrence_id_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="Unable to parse"):
        parse_recurrence_id("not a date")


def test_delete_single_event() -> None:
    calendar = FakeCalendar(SINGLE_EVENT)
    delete_event(calendar, "single-1")
    assert calendar.event.deleted
    assert not calendar.event.saved


def test_delete_whole_series() -> None:
    calendar = FakeCalendar(TIMED_SERIES)
    delete_event(calendar, "timed-1")
    assert calendar.event.deleted


def test_delete_single_occurrence_adds_exdate() -> None:
    calendar = FakeCalendar(TIMED_SERIES)
    delete_event(calendar, "timed-1", recurrence_id=SECOND_OCCURRENCE)

    assert not calendar.event.deleted
    assert calendar.event.saved
    master = _master(calendar.event.stored())
    assert _exdates(master) == [datetime(2026, 7, 13, 9, 0, tzinfo=UTC)]
    assert master.get("RRULE") is not None


def test_delete_single_occurrence_drops_matching_override() -> None:
    calendar = FakeCalendar(SERIES_WITH_OVERRIDE)
    delete_event(calendar, "timed-1", recurrence_id=SECOND_OCCURRENCE)

    stored = calendar.event.stored()
    assert _overrides(stored) == []
    assert _exdates(_master(stored)) == [datetime(2026, 7, 13, 9, 0, tzinfo=UTC)]


def test_delete_single_occurrence_with_override_stored_first() -> None:
    calendar = FakeCalendar(SERIES_WITH_OVERRIDE_FIRST)
    delete_event(calendar, "timed-1", recurrence_id=SECOND_OCCURRENCE)

    stored = calendar.event.stored()
    assert _overrides(stored) == []
    assert _exdates(_master(stored)) == [datetime(2026, 7, 13, 9, 0, tzinfo=UTC)]


def test_save_writes_the_resource_verbatim() -> None:
    """caldav must neither bump SEQUENCE again nor merge recurrences itself.

    With the default only_this_recurrence=True, caldav refetches the resource
    from the server whenever the first component carries a RECURRENCE-ID and
    keeps only that component's changes, throwing away our master edits.
    """
    calendar = FakeCalendar(SERIES_WITH_OVERRIDE_FIRST)
    delete_event(calendar, "timed-1", recurrence_id=SECOND_OCCURRENCE)

    assert calendar.event.save_kwargs == {
        "increase_seqno": False,
        "only_this_recurrence": False,
    }


def test_delete_this_and_future_caps_series() -> None:
    calendar = FakeCalendar(TIMED_SERIES)
    delete_event(
        calendar, "timed-1", recurrence_id=SECOND_OCCURRENCE, this_and_future=True
    )

    assert not calendar.event.deleted
    rrule = _master(calendar.event.stored())["RRULE"]
    assert rrule["UNTIL"] == [datetime(2026, 7, 13, 8, 59, 59, tzinfo=UTC)]


def test_delete_this_and_future_removes_count() -> None:
    calendar = FakeCalendar(COUNTED_SERIES)
    delete_event(
        calendar, "counted-1", recurrence_id=SECOND_OCCURRENCE, this_and_future=True
    )

    rrule = _master(calendar.event.stored())["RRULE"]
    assert "COUNT" not in rrule
    assert "UNTIL" in rrule


def test_delete_this_and_future_at_series_start_deletes_everything() -> None:
    calendar = FakeCalendar(TIMED_SERIES)
    delete_event(
        calendar, "timed-1", recurrence_id=FIRST_OCCURRENCE, this_and_future=True
    )

    # Capping before the first occurrence would leave an empty series behind.
    assert calendar.event.deleted


def test_delete_this_and_future_all_day_series() -> None:
    calendar = FakeCalendar(ALL_DAY_SERIES)
    delete_event(calendar, "allday-1", recurrence_id="2026-07-08", this_and_future=True)

    # UNTIL is inclusive, so it must land on the day before the occurrence.
    rrule = _master(calendar.event.stored())["RRULE"]
    assert rrule["UNTIL"] == [date(2026, 7, 7)]


def test_delete_single_occurrence_all_day_uses_date_exdate() -> None:
    calendar = FakeCalendar(ALL_DAY_SERIES)
    delete_event(calendar, "allday-1", recurrence_id="2026-07-08")

    exdates = _exdates(_master(calendar.event.stored()))
    assert exdates == [date(2026, 7, 8)]
    assert not isinstance(exdates[0], datetime)


def test_update_whole_series() -> None:
    calendar = FakeCalendar(TIMED_SERIES)
    update_event(
        calendar,
        "timed-1",
        {
            "summary": "Renamed",
            "dtstart": datetime(2026, 7, 6, 10, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 6, 11, 0, tzinfo=UTC),
        },
    )

    master = _master(calendar.event.stored())
    assert master["SUMMARY"] == "Renamed"
    assert master["DTSTART"].dt == datetime(2026, 7, 6, 10, 0, tzinfo=UTC)
    assert int(master["SEQUENCE"]) == 1
    assert calendar.added == []


def test_update_single_occurrence_creates_override() -> None:
    calendar = FakeCalendar(TIMED_SERIES)
    update_event(
        calendar,
        "timed-1",
        {
            "summary": "Moved",
            "dtstart": datetime(2026, 7, 13, 11, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
        },
        recurrence_id=SECOND_OCCURRENCE,
    )

    stored = calendar.event.stored()
    overrides = _overrides(stored)
    assert len(overrides) == 1
    assert overrides[0]["SUMMARY"] == "Moved"
    assert overrides[0]["RECURRENCE-ID"].dt == datetime(2026, 7, 13, 9, 0, tzinfo=UTC)
    # RFC 5545 requires DTSTAMP on every VEVENT; strict servers reject its absence.
    assert "DTSTAMP" in overrides[0]
    # The series itself must stay untouched.
    assert _master(stored)["SUMMARY"] == "Standup"
    assert calendar.added == []


def test_update_single_occurrence_reuses_existing_override() -> None:
    calendar = FakeCalendar(SERIES_WITH_OVERRIDE)
    update_event(
        calendar,
        "timed-1",
        {
            "summary": "Moved again",
            "dtstart": datetime(2026, 7, 13, 13, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 13, 14, 0, tzinfo=UTC),
        },
        recurrence_id=SECOND_OCCURRENCE,
    )

    overrides = _overrides(calendar.event.stored())
    assert len(overrides) == 1
    assert overrides[0]["SUMMARY"] == "Moved again"


def test_update_this_and_future_splits_series() -> None:
    calendar = FakeCalendar(TIMED_SERIES)
    update_event(
        calendar,
        "timed-1",
        {
            "summary": "New rhythm",
            "dtstart": datetime(2026, 7, 13, 11, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
        },
        recurrence_id=SECOND_OCCURRENCE,
        this_and_future=True,
    )

    # Existing series is capped ...
    rrule = _master(calendar.event.stored())["RRULE"]
    assert rrule["UNTIL"] == [datetime(2026, 7, 13, 8, 59, 59, tzinfo=UTC)]
    # ... and the edited tail becomes its own resource, carrying the rule over.
    assert len(calendar.added) == 1
    assert calendar.added[0]["summary"] == "New rhythm"
    assert calendar.added[0]["rrule"] == "FREQ=WEEKLY"


def test_update_this_and_future_at_series_start_updates_master() -> None:
    calendar = FakeCalendar(TIMED_SERIES)
    update_event(
        calendar,
        "timed-1",
        {
            "summary": "Renamed",
            "dtstart": datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 6, 10, 0, tzinfo=UTC),
        },
        recurrence_id=FIRST_OCCURRENCE,
        this_and_future=True,
    )

    # Nothing to split off, so no second resource is created.
    assert calendar.added == []
    assert _master(calendar.event.stored())["SUMMARY"] == "Renamed"
