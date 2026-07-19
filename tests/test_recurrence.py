"""Tests for the CalDAV recurring-series write operations."""

from datetime import UTC, date, datetime

from caldav.elements import dav
from icalendar import Calendar as ICalendar
import pytest

from custom_components.ha_caldav.recurrence import (
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

DETAILED_SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:detailed-1
DTSTAMP:20260101T000000Z
DTSTART:20260706T090000Z
DTEND:20260706T100000Z
RRULE:FREQ=WEEKLY
SUMMARY:Standup
DESCRIPTION:Daily sync
LOCATION:Room 1
END:VEVENT
END:VCALENDAR
"""

BYDAY_SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:byday-1
DTSTAMP:20260101T000000Z
DTSTART:20260706T090000Z
DTEND:20260706T100000Z
RRULE:FREQ=WEEKLY;BYDAY=MO
SUMMARY:Standup
END:VEVENT
END:VCALENDAR
"""

ONCE_SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:once-1
DTSTAMP:20260101T000000Z
DTSTART:20260706T090000Z
DTEND:20260706T100000Z
RRULE:FREQ=WEEKLY;COUNT=1
SUMMARY:Standup
END:VEVENT
END:VCALENDAR
"""

TZID_SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:tz-1
DTSTAMP:20260101T000000Z
DTSTART;TZID=Europe/Berlin:20260706T090000
DTEND;TZID=Europe/Berlin:20260706T100000
RRULE:FREQ=WEEKLY
SUMMARY:Standup
END:VEVENT
END:VCALENDAR
"""

FLOATING_SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:float-1
DTSTAMP:20260101T000000Z
DTSTART:20260706T090000
DTEND:20260706T100000
RRULE:FREQ=WEEKLY
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

SERIES_WITH_OVERRIDE_AND_EXDATE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:timed-1
DTSTAMP:20260101T000000Z
DTSTART:20260706T090000Z
DTEND:20260706T100000Z
RRULE:FREQ=WEEKLY
EXDATE:20260720T090000Z
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

# An object that holds only overrides, without the master series.
ORPHAN_OVERRIDES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:timed-1
DTSTAMP:20260101T000000Z
RECURRENCE-ID:20260713T090000Z
DTSTART:20260713T110000Z
DTEND:20260713T120000Z
SUMMARY:Moved one
END:VEVENT
BEGIN:VEVENT
UID:timed-1
DTSTAMP:20260101T000000Z
RECURRENCE-ID:20260720T090000Z
DTSTART:20260720T110000Z
DTEND:20260720T120000Z
SUMMARY:Moved two
END:VEVENT
END:VCALENDAR
"""

FIRST_OCCURRENCE = "2026-07-06 09:00:00+00:00"
SECOND_OCCURRENCE = "2026-07-13 09:00:00+00:00"
THIRD_OCCURRENCE = "2026-07-20 09:00:00+00:00"


class FakeEvent:
    """Stand-in for a caldav.Event backed by a real icalendar object."""

    def __init__(
        self, ics: str, save_error: Exception | None = None, etag: str | None = None
    ) -> None:
        self.data = ics
        self.saved = False
        self.save_kwargs: dict | None = None
        self.deleted = False
        self.props: dict = {}
        self._save_error = save_error
        self._etag = etag
        self._instance = ICalendar.from_ical(ics)

    @property
    def icalendar_instance(self) -> ICalendar:
        return self._instance

    def load(self) -> None:
        if self._etag is not None:
            self.props = {dav.GetEtag.tag: self._etag}

    def save(self, **kwargs) -> None:
        self.saved = True
        self.save_kwargs = kwargs
        if self._save_error is not None:
            raise self._save_error

    def delete(self) -> None:
        self.deleted = True

    def stored(self) -> ICalendar:
        """Return what would have been written back to the server."""
        return ICalendar.from_ical(self.data)


class FakeCalendar:
    """Stand-in for a caldav.Calendar."""

    def __init__(
        self,
        ics: str,
        save_error: Exception | None = None,
        refetch_ics: str | None = None,
        etag: str | None = None,
    ) -> None:
        self.event = FakeEvent(ics, save_error, etag)
        self.created: list[FakeEvent] = []
        self._refetch_ics = refetch_ics
        self._fetches = 0

    def event_by_uid(self, uid: str) -> FakeEvent:
        self._fetches += 1
        # A second lookup models the fresh fetch the split rollback does.
        if self._fetches > 1 and self._refetch_ics is not None:
            return FakeEvent(self._refetch_ics)
        return self.event

    def save_event(self, ics: str) -> FakeEvent:
        created = FakeEvent(ics)
        self.created.append(created)
        return created


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


def _only_vevent(ics: str):
    return ICalendar.from_ical(ics).walk("VEVENT")[0]


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


def test_delete_single_occurrence_empties_series_deletes_resource() -> None:
    calendar = FakeCalendar(ONCE_SERIES)
    delete_event(calendar, "once-1", recurrence_id=FIRST_OCCURRENCE)

    # Excluding the only occurrence leaves nothing, so the resource is removed.
    assert calendar.event.deleted


def test_delete_single_occurrence_on_orphan_object_drops_component() -> None:
    calendar = FakeCalendar(ORPHAN_OVERRIDES)
    delete_event(calendar, "timed-1", recurrence_id=THIRD_OCCURRENCE)

    assert not calendar.event.deleted
    overrides = _overrides(calendar.event.stored())
    assert len(overrides) == 1
    assert overrides[0]["RECURRENCE-ID"].dt == datetime(2026, 7, 13, 9, 0, tzinfo=UTC)


def test_delete_last_orphan_override_removes_resource() -> None:
    calendar = FakeCalendar(SERIES_WITH_OVERRIDE_FIRST)
    # Strip the master so only the single override remains.
    ical = ICalendar.from_ical(SERIES_WITH_OVERRIDE_FIRST)
    for comp in list(ical.subcomponents):
        if comp.name == "VEVENT" and "RECURRENCE-ID" not in comp:
            ical.subcomponents.remove(comp)
    calendar = FakeCalendar(ical.to_ical().decode("utf-8"))
    delete_event(calendar, "timed-1", recurrence_id=SECOND_OCCURRENCE)

    assert calendar.event.deleted


def test_delete_unknown_occurrence_on_orphan_object_raises() -> None:
    calendar = FakeCalendar(ORPHAN_OVERRIDES)
    with pytest.raises(ValueError, match="Occurrence not found"):
        delete_event(calendar, "timed-1", recurrence_id="2026-08-01 09:00:00+00:00")


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


def test_delete_single_occurrence_tzid_series_uses_utc_exdate() -> None:
    calendar = FakeCalendar(TZID_SERIES)
    # 09:00 Europe/Berlin in July is 07:00 UTC.
    delete_event(calendar, "tz-1", recurrence_id="2026-07-13 07:00:00+00:00")

    exdates = _exdates(_master(calendar.event.stored()))
    assert len(exdates) == 1
    assert exdates[0].tzinfo is not None
    assert exdates[0].astimezone(UTC) == datetime(2026, 7, 13, 7, 0, tzinfo=UTC)


def test_delete_single_occurrence_floating_series_stays_floating() -> None:
    calendar = FakeCalendar(FLOATING_SERIES)
    delete_event(calendar, "float-1", recurrence_id="2026-07-13 09:00:00")

    exdates = _exdates(_master(calendar.event.stored()))
    assert exdates == [datetime(2026, 7, 13, 9, 0)]
    assert exdates[0].tzinfo is None


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
    assert calendar.created == []


def test_update_whole_series_without_rrule_keeps_recurrence() -> None:
    calendar = FakeCalendar(TIMED_SERIES)
    update_event(
        calendar,
        "timed-1",
        {
            "summary": "Renamed",
            "dtstart": datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 6, 10, 0, tzinfo=UTC),
        },
    )

    # The frontend never echoes the rule; an absent rrule must not drop it.
    assert _master(calendar.event.stored()).get("RRULE") is not None


def test_update_whole_series_rrule_change_drops_overrides_and_exdate() -> None:
    calendar = FakeCalendar(SERIES_WITH_OVERRIDE_AND_EXDATE)
    update_event(
        calendar,
        "timed-1",
        {
            "summary": "Standup",
            "dtstart": datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 6, 10, 0, tzinfo=UTC),
            "rrule": "FREQ=DAILY",
        },
    )

    stored = calendar.event.stored()
    master = _master(stored)
    assert master["RRULE"]["FREQ"] == ["DAILY"]
    assert _overrides(stored) == []
    assert _exdates(master) == []


def test_update_whole_series_move_start_off_rule_rejected() -> None:
    calendar = FakeCalendar(BYDAY_SERIES)
    with pytest.raises(ValueError, match="does not match the recurrence rule"):
        update_event(
            calendar,
            "byday-1",
            {
                "summary": "Standup",
                "dtstart": datetime(2026, 7, 7, 9, 0, tzinfo=UTC),
                "dtend": datetime(2026, 7, 7, 10, 0, tzinfo=UTC),
            },
        )


def test_update_whole_series_move_start_on_rule_allowed() -> None:
    calendar = FakeCalendar(BYDAY_SERIES)
    update_event(
        calendar,
        "byday-1",
        {
            "summary": "Standup",
            "dtstart": datetime(2026, 7, 13, 9, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 13, 10, 0, tzinfo=UTC),
        },
    )

    assert _master(calendar.event.stored())["DTSTART"].dt == datetime(
        2026, 7, 13, 9, 0, tzinfo=UTC
    )


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
    assert "RRULE" not in overrides[0]
    assert _master(stored)["SUMMARY"] == "Standup"
    assert calendar.created == []


def test_update_single_occurrence_at_series_start_allowed() -> None:
    calendar = FakeCalendar(TIMED_SERIES)
    update_event(
        calendar,
        "timed-1",
        {
            "summary": "Moved",
            "dtstart": datetime(2026, 7, 6, 11, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 6, 12, 0, tzinfo=UTC),
        },
        recurrence_id=FIRST_OCCURRENCE,
    )

    overrides = _overrides(calendar.event.stored())
    assert len(overrides) == 1
    assert overrides[0]["RECURRENCE-ID"].dt == datetime(2026, 7, 6, 9, 0, tzinfo=UTC)


def test_update_single_occurrence_off_rule_rejected() -> None:
    calendar = FakeCalendar(TIMED_SERIES)
    with pytest.raises(ValueError, match="Occurrence not found"):
        update_event(
            calendar,
            "timed-1",
            {
                "summary": "Moved",
                "dtstart": datetime(2026, 7, 9, 11, 0, tzinfo=UTC),
                "dtend": datetime(2026, 7, 9, 12, 0, tzinfo=UTC),
            },
            recurrence_id="2026-07-09 09:00:00+00:00",
        )


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

    rrule = _master(calendar.event.stored())["RRULE"]
    assert rrule["UNTIL"] == [datetime(2026, 7, 13, 8, 59, 59, tzinfo=UTC)]
    assert len(calendar.created) == 1
    tail = _only_vevent(calendar.created[0].data)
    assert tail["SUMMARY"] == "New rhythm"
    assert tail["RRULE"]["FREQ"] == ["WEEKLY"]
    assert str(tail["UID"]) != "timed-1"


def test_update_this_and_future_split_reduces_count() -> None:
    calendar = FakeCalendar(COUNTED_SERIES)
    update_event(
        calendar,
        "counted-1",
        {
            "summary": "New rhythm",
            "dtstart": datetime(2026, 7, 13, 11, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
        },
        recurrence_id=SECOND_OCCURRENCE,
        this_and_future=True,
    )

    # The head keeps the first occurrence, so the tail carries the other nine.
    tail = _only_vevent(calendar.created[0].data)
    assert tail["RRULE"]["COUNT"] == [9]


def test_update_this_and_future_split_preserves_details() -> None:
    calendar = FakeCalendar(DETAILED_SERIES)
    update_event(
        calendar,
        "detailed-1",
        {
            "summary": "Standup",
            "dtstart": datetime(2026, 7, 13, 9, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 13, 10, 0, tzinfo=UTC),
            "description": "Daily sync",
            "location": "Room 1",
        },
        recurrence_id=SECOND_OCCURRENCE,
        this_and_future=True,
    )

    tail = _only_vevent(calendar.created[0].data)
    assert tail["DESCRIPTION"] == "Daily sync"
    assert tail["LOCATION"] == "Room 1"


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
    assert calendar.created == []
    assert _master(calendar.event.stored())["SUMMARY"] == "Renamed"


def test_split_rollback_discards_tail_when_head_stays_uncapped() -> None:
    from requests import Timeout

    # The save fails and a fresh fetch shows the head unchanged, so the
    # orphaned tail must be removed again.
    calendar = FakeCalendar(
        TIMED_SERIES, save_error=Timeout("boom"), refetch_ics=TIMED_SERIES
    )
    with pytest.raises(Timeout):
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

    assert calendar.created[0].deleted


def test_split_rollback_keeps_tail_when_head_capped() -> None:
    from requests import Timeout

    capped = TIMED_SERIES.replace(
        "RRULE:FREQ=WEEKLY", "RRULE:FREQ=WEEKLY;UNTIL=20260713T085959Z"
    )
    # The save timed out but the server committed the cap; deleting the tail
    # would drop every future occurrence, so it is kept.
    calendar = FakeCalendar(
        TIMED_SERIES, save_error=Timeout("boom"), refetch_ics=capped
    )
    with pytest.raises(Timeout):
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

    assert not calendar.created[0].deleted


def test_split_rollback_handles_non_timeout_transport_error() -> None:
    from requests.exceptions import ChunkedEncodingError

    # A transport read error that is neither ConnectionError nor Timeout must
    # still trigger the rollback rather than escape it.
    calendar = FakeCalendar(
        TIMED_SERIES,
        save_error=ChunkedEncodingError("boom"),
        refetch_ics=TIMED_SERIES,
    )
    with pytest.raises(ChunkedEncodingError):
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

    assert calendar.created[0].deleted


# A rule fully excluded by EXDATEs, but with a surviving override for a moved
# occurrence whose slot is one of the excluded ones.
EXCLUDED_RULE_WITH_OVERRIDE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:timed-1
DTSTAMP:20260101T000000Z
DTSTART:20260706T090000Z
DTEND:20260706T100000Z
RRULE:FREQ=WEEKLY;COUNT=3
EXDATE:20260713T090000Z
SUMMARY:Standup
END:VEVENT
BEGIN:VEVENT
UID:timed-1
DTSTAMP:20260101T000000Z
RECURRENCE-ID:20260713T090000Z
DTSTART:20260713T110000Z
DTEND:20260713T120000Z
SUMMARY:Moved meeting
END:VEVENT
END:VCALENDAR
"""


def test_delete_single_occurrence_keeps_surviving_override() -> None:
    calendar = FakeCalendar(EXCLUDED_RULE_WITH_OVERRIDE)
    # Excluding the two remaining rule occurrences empties the master, but the
    # moved-meeting override must not be dropped with the whole resource.
    delete_event(calendar, "timed-1", recurrence_id=FIRST_OCCURRENCE)
    delete_event(calendar, "timed-1", recurrence_id=THIRD_OCCURRENCE)

    assert not calendar.event.deleted
    overrides = _overrides(calendar.event.stored())
    assert [str(o["SUMMARY"]) for o in overrides] == ["Moved meeting"]


def test_update_single_occurrence_all_day_creates_date_override() -> None:
    calendar = FakeCalendar(ALL_DAY_SERIES)
    update_event(
        calendar,
        "allday-1",
        {
            "summary": "Rescheduled",
            "dtstart": date(2026, 7, 8),
            "dtend": date(2026, 7, 9),
        },
        recurrence_id="2026-07-08",
    )

    overrides = _overrides(calendar.event.stored())
    assert len(overrides) == 1
    recurrence_id = overrides[0]["RECURRENCE-ID"].dt
    assert recurrence_id == date(2026, 7, 8)
    assert not isinstance(recurrence_id, datetime)
    assert overrides[0]["SUMMARY"] == "Rescheduled"


def _renamed(dtstart_hour: int = 10) -> dict:
    return {
        "summary": "Renamed",
        "dtstart": datetime(2026, 7, 6, dtstart_hour, 0, tzinfo=UTC),
        "dtend": datetime(2026, 7, 6, dtstart_hour + 1, 0, tzinfo=UTC),
    }


def test_update_conflict_when_etag_changed() -> None:
    calendar = FakeCalendar(TIMED_SERIES, etag='"server-2"')
    with pytest.raises(ValueError, match="changed on the server"):
        update_event(calendar, "timed-1", _renamed(), expected_etag='"user-1"')
    assert not calendar.event.saved


def test_update_proceeds_when_etag_matches() -> None:
    calendar = FakeCalendar(TIMED_SERIES, etag='"same"')
    update_event(calendar, "timed-1", _renamed(), expected_etag='"same"')
    assert calendar.event.saved


def test_update_skips_check_without_expected_etag() -> None:
    calendar = FakeCalendar(TIMED_SERIES, etag='"server-2"')
    update_event(calendar, "timed-1", _renamed())
    assert calendar.event.saved


def test_delete_conflict_when_etag_changed() -> None:
    calendar = FakeCalendar(TIMED_SERIES, etag='"server-2"')
    with pytest.raises(ValueError, match="changed on the server"):
        delete_event(calendar, "timed-1", expected_etag='"user-1"')
    assert not calendar.event.deleted


def test_update_proceeds_when_server_returns_no_etag() -> None:
    # A server that does not report an etag cannot be conflict-checked; the
    # write proceeds rather than being blocked.
    calendar = FakeCalendar(TIMED_SERIES, etag=None)
    update_event(calendar, "timed-1", _renamed(), expected_etag='"stale"')
    assert calendar.event.saved
