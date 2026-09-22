from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from caldav.elements import dav
from caldav.lib.error import DAVError
from homeassistant.util import dt as dt_util
from icalendar import Calendar as ICalendar
import pytest

from custom_components.ha_caldav.errors import Refused
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

# SERIES_WITH_OVERRIDE with the override first; RFC 5545 promises no order.
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

DATED_SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:dated-1
DTSTAMP:20260101T000000Z
DTSTART:20260706T090000Z
DTEND:20260706T100000Z
RRULE:FREQ=WEEKLY
RDATE:20260709T090000Z
RDATE:20260723T090000Z
EXDATE:20260713T090000Z
EXDATE:20260727T090000Z
SUMMARY:Standup
END:VEVENT
END:VCALENDAR
"""

ALL_DAY_DATED_SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:allday-dated-1
DTSTAMP:20260101T000000Z
DTSTART;VALUE=DATE:20260706
DTEND;VALUE=DATE:20260707
RRULE:FREQ=WEEKLY
RDATE;VALUE=DATE:20260709,20260723
EXDATE;VALUE=DATE:20260713,20260727
SUMMARY:Bins
END:VEVENT
END:VCALENDAR
"""

# RFC 2445 allowed a repeated UID and old clients still write one.
DOUBLED_UID_SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:timed-1
UID:timed-1-copy
DTSTAMP:20260101T000000Z
DTSTART:20260706T090000Z
DTEND:20260706T100000Z
RRULE:FREQ=WEEKLY
SUMMARY:Standup
END:VEVENT
END:VCALENDAR
"""

# Berlin leaves CEST on 2026-10-25.
DST_SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:dst-1
DTSTAMP:20260101T000000Z
DTSTART;TZID=Europe/Berlin:20261011T090000
DTEND;TZID=Europe/Berlin:20261011T100000
RRULE:FREQ=WEEKLY
RDATE:20261024T070000Z
SUMMARY:Standup
END:VEVENT
END:VCALENDAR
"""

FIRST_OCCURRENCE = "2026-07-06 09:00:00+00:00"
SECOND_OCCURRENCE = "2026-07-13 09:00:00+00:00"
THIRD_OCCURRENCE = "2026-07-20 09:00:00+00:00"


class FakeEvent:
    def __init__(
        self,
        ics: str,
        save_error: Exception | None = None,
        etag: str | None = None,
        server_ics: str | None = None,
    ) -> None:
        self.data = ics
        self.saved = False
        self.save_kwargs: dict | None = None
        self.deleted = False
        self.props: dict = {}
        self._save_error = save_error
        self._delete_error: Exception | None = None
        self._etag = etag
        self._instance = ICalendar.from_ical(ics)
        self._server_ics = ics if server_ics is None else server_ics

    @property
    def icalendar_instance(self) -> ICalendar:
        return self._instance

    @icalendar_instance.setter
    def icalendar_instance(self, value: ICalendar) -> None:
        # caldav keeps the document handed to it and serializes it only on the way
        # out; a string is put through vcal.fix, which rewrites the object.
        self._instance = value
        self.data = value.to_ical().decode("utf-8")

    def load(self) -> None:
        # caldav replaces the document before it records the etag.
        self.data = self._server_ics
        self._instance = ICalendar.from_ical(self.data)
        if self._etag is not None:
            self.props = {dav.GetEtag.tag: self._etag}

    def save(self, **kwargs) -> None:
        self.saved = True
        self.save_kwargs = kwargs
        # caldav 2.1.0 bumps the first component's SEQUENCE on the way out whatever
        # increase_seqno says.
        ical = ICalendar.from_ical(self.data)
        component = next(
            (item for item in ical.subcomponents if item.name != "VTIMEZONE"), None
        )
        if component is not None and "SEQUENCE" in component:
            seqno = component.pop("SEQUENCE")
            component.add("SEQUENCE", int(seqno) + 1)
            self.data = ical.to_ical().decode("utf-8")
        if self._save_error is not None:
            raise self._save_error

    def delete(self) -> None:
        if self._delete_error is not None:
            raise self._delete_error
        self.deleted = True

    def stored(self) -> ICalendar:
        return ICalendar.from_ical(self.data)


class FakeCalendar:
    def __init__(
        self,
        ics: str,
        save_error: Exception | None = None,
        refetch_ics: str | None = None,
        etag: str | None = None,
        refetch_error: Exception | None = None,
        server_ics: str | None = None,
        created_delete_error: Exception | None = None,
    ) -> None:
        self.event = FakeEvent(ics, save_error, etag, server_ics)
        self.created: list[FakeEvent] = []
        self._refetch_ics = refetch_ics
        self._refetch_error = refetch_error
        self._created_delete_error = created_delete_error
        self._fetches = 0

    def event_by_uid(self, uid: str) -> FakeEvent:
        self._fetches += 1
        if self._fetches > 1:
            if self._refetch_error is not None:
                raise self._refetch_error
            if self._refetch_ics is not None:
                return FakeEvent(self._refetch_ics)
        return self.event

    def search(self, **kwargs) -> list[FakeEvent]:
        if self._refetch_error is not None:
            raise self._refetch_error
        return [self.event]

    def save_event(self, document: ICalendar) -> FakeEvent:
        # caldav takes the document and serializes it itself; handed a string
        # it would put it through vcal.fix first.
        created = FakeEvent(document.to_ical().decode("utf-8"))
        created._delete_error = self._created_delete_error
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
    with pytest.raises(Refused, match="bad_recurrence_id"):
        parse_recurrence_id("not a date")


def test_delete_single_event() -> None:
    calendar = FakeCalendar(SINGLE_EVENT)
    delete_event(calendar, "single-1")
    assert calendar.event.deleted
    assert not calendar.event.saved


SPRING_SERIES = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
    "BEGIN:VTIMEZONE\r\nTZID:Europe/Berlin\r\n"
    "BEGIN:STANDARD\r\nDTSTART:19701025T030000\r\n"
    "RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=10\r\n"
    "TZOFFSETFROM:+0200\r\nTZOFFSETTO:+0100\r\nEND:STANDARD\r\n"
    "BEGIN:DAYLIGHT\r\nDTSTART:19700329T020000\r\n"
    "RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=3\r\n"
    "TZOFFSETFROM:+0100\r\nTZOFFSETTO:+0200\r\nEND:DAYLIGHT\r\nEND:VTIMEZONE\r\n"
    "BEGIN:VEVENT\r\nUID:dst-1\r\nDTSTAMP:20260101T000000Z\r\n"
    "DTSTART;TZID=Europe/Berlin:20260322T090000\r\n"
    "DTEND;TZID=Europe/Berlin:20260322T100000\r\n"
    "RRULE:FREQ=WEEKLY\r\n"
    "SUMMARY:Standup\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)


def test_an_override_past_a_dst_change_keeps_its_length() -> None:
    """Europe/Berlin changes over on 2026-03-29, so two weeks on the clock is two
    weeks and one hour in UTC."""
    calendar = FakeCalendar(SPRING_SERIES)

    update_event(
        calendar,
        "dst-1",
        {"summary": "Standup, longer"},
        recurrence_id="2026-04-05 09:00:00+02:00",
    )

    stored = calendar.event.stored()
    override = next(v for v in stored.walk("VEVENT") if "RECURRENCE-ID" in v)
    berlin = ZoneInfo("Europe/Berlin")
    assert override["DTSTART"].dt.astimezone(berlin).strftime("%H:%M") == "09:00"
    assert override["DTEND"].dt.astimezone(berlin).strftime("%H:%M") == "10:00"


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
    ical = ICalendar.from_ical(SERIES_WITH_OVERRIDE_FIRST)
    for comp in list(ical.subcomponents):
        if comp.name == "VEVENT" and "RECURRENCE-ID" not in comp:
            ical.subcomponents.remove(comp)
    calendar = FakeCalendar(ical.to_ical().decode("utf-8"))
    delete_event(calendar, "timed-1", recurrence_id=SECOND_OCCURRENCE)

    assert calendar.event.deleted


def test_delete_unknown_occurrence_on_orphan_object_raises() -> None:
    calendar = FakeCalendar(ORPHAN_OVERRIDES)
    with pytest.raises(Refused, match="occurrence_not_found"):
        delete_event(calendar, "timed-1", recurrence_id="2026-08-01 09:00:00+00:00")


def test_save_writes_the_resource_verbatim() -> None:
    """With only_this_recurrence=True, caldav refetches a resource whose first
    component carries a RECURRENCE-ID and keeps only that component's changes."""
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

    assert calendar.event.deleted


def test_delete_this_and_future_all_day_series() -> None:
    calendar = FakeCalendar(ALL_DAY_SERIES)
    delete_event(calendar, "allday-1", recurrence_id="2026-07-08", this_and_future=True)

    # UNTIL is inclusive.
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

    # The frontend never echoes the rule.
    assert _master(calendar.event.stored()).get("RRULE") is not None


def test_a_rule_change_keeps_the_exceptions_the_new_rule_still_produces() -> None:
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
    [override] = _overrides(stored)
    assert override["SUMMARY"] == "Standup moved"
    assert _exdates(master) == [datetime(2026, 7, 20, 9, 0, tzinfo=UTC)]


def test_a_rule_change_drops_the_exceptions_the_new_rule_has_no_slot_for() -> None:
    calendar = FakeCalendar(SERIES_WITH_OVERRIDE_AND_EXDATE)
    update_event(
        calendar,
        "timed-1",
        {"rrule": "FREQ=WEEKLY;INTERVAL=2"},
    )

    stored = calendar.event.stored()
    assert _overrides(stored) == []
    assert _exdates(_master(stored)) == [datetime(2026, 7, 20, 9, 0, tzinfo=UTC)]


def test_extending_a_series_keeps_its_exceptions() -> None:
    calendar = FakeCalendar(
        SERIES_WITH_OVERRIDE_AND_EXDATE.replace(
            "RRULE:FREQ=WEEKLY", "RRULE:FREQ=WEEKLY;UNTIL=20260727T090000Z"
        )
    )
    update_event(
        calendar,
        "timed-1",
        {"rrule": "FREQ=WEEKLY;UNTIL=20261231T090000"},
        recurrence_id=FIRST_OCCURRENCE,
        this_and_future=True,
    )

    stored = calendar.event.stored()
    assert len(_overrides(stored)) == 1
    assert _exdates(_master(stored)) == [datetime(2026, 7, 20, 9, 0, tzinfo=UTC)]


def test_shortening_a_long_series_drops_the_exceptions_past_its_new_end() -> None:
    calendar = FakeCalendar(
        """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:long-1
DTSTAMP:20260101T000000Z
DTSTART:20260706T090000Z
DTEND:20260706T100000Z
RRULE:FREQ=DAILY;UNTIL=20301231T090000Z
EXDATE:20290707T090000Z
SUMMARY:Standup
END:VEVENT
BEGIN:VEVENT
UID:long-1
DTSTAMP:20260101T000000Z
RECURRENCE-ID:20290706T090000Z
DTSTART:20290706T110000Z
DTEND:20290706T120000Z
SUMMARY:Standup moved
END:VEVENT
END:VCALENDAR
"""
    )
    update_event(calendar, "long-1", {"rrule": "FREQ=DAILY;UNTIL=20281231T090000Z"})

    stored = calendar.event.stored()
    assert _overrides(stored) == []
    assert _exdates(_master(stored)) == []


def test_update_whole_series_move_start_off_rule_rejected() -> None:
    calendar = FakeCalendar(BYDAY_SERIES)
    with pytest.raises(Refused, match="rrule_mismatch"):
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
    with pytest.raises(Refused, match="occurrence_not_found"):
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

    tail = _only_vevent(calendar.created[0].data)
    assert tail["RRULE"]["COUNT"] == [9]


FULLY_DETAILED_SERIES = DETAILED_SERIES.replace(
    "LOCATION:Room 1\n",
    "LOCATION:Room 1\nCATEGORIES:Work\nBEGIN:VALARM\nACTION:DISPLAY\n"
    "TRIGGER:-PT10M\nDESCRIPTION:Soon\nEND:VALARM\n",
)


@pytest.mark.parametrize("this_and_future", [False, True])
def test_an_edit_of_part_of_a_series_keeps_what_it_did_not_name(
    this_and_future: bool,
) -> None:
    calendar = FakeCalendar(FULLY_DETAILED_SERIES)
    update_event(
        calendar,
        "detailed-1",
        {"summary": "Renamed"},
        recurrence_id=SECOND_OCCURRENCE,
        this_and_future=this_and_future,
    )

    written = (
        _master(calendar.created[0].stored())
        if this_and_future
        else _overrides(calendar.event.stored())[0]
    )
    assert written["SUMMARY"] == "Renamed"
    assert written["DESCRIPTION"] == "Daily sync"
    assert written["LOCATION"] == "Room 1"
    assert written["CATEGORIES"].cats == ["Work"]
    [alarm] = written.walk("VALARM")
    assert alarm["TRIGGER"].dt == timedelta(minutes=-10)


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

    assert calendar.created == []
    assert _master(calendar.event.stored())["SUMMARY"] == "Renamed"


def test_split_rollback_discards_tail_when_head_stays_uncapped() -> None:
    from caldav.davclient import requests

    Timeout = requests.Timeout

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
    from caldav.davclient import requests

    Timeout = requests.Timeout

    capped = TIMED_SERIES.replace(
        "RRULE:FREQ=WEEKLY", "RRULE:FREQ=WEEKLY;UNTIL=20260713T085959Z"
    )
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
    from caldav.davclient import requests

    ChunkedEncodingError = requests.exceptions.ChunkedEncodingError

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
    with pytest.raises(Refused, match="etag_conflict"):
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
    with pytest.raises(Refused, match="etag_conflict"):
        delete_event(calendar, "timed-1", expected_etag='"user-1"')
    assert not calendar.event.deleted


def test_update_proceeds_when_server_returns_no_etag() -> None:
    calendar = FakeCalendar(TIMED_SERIES, etag=None)
    update_event(calendar, "timed-1", _renamed(), expected_etag='"stale"')
    assert calendar.event.saved


def _rdates(component) -> list:
    rdate = component.get("RDATE")
    if rdate is None:
        return []
    entries = rdate if isinstance(rdate, list) else [rdate]
    return [dt.dt for entry in entries for dt in entry.dts]


def test_split_keeps_each_added_and_excluded_date_on_its_own_side() -> None:
    calendar = FakeCalendar(DATED_SERIES)

    update_event(
        calendar,
        "dated-1",
        {"summary": "Onwards"},
        recurrence_id=THIRD_OCCURRENCE,
        this_and_future=True,
    )

    head = _master(calendar.event.stored())
    tail = _only_vevent(calendar.created[0].data)
    assert _rdates(head) == [datetime(2026, 7, 9, 9, 0, tzinfo=UTC)]
    assert _exdates(head) == [datetime(2026, 7, 13, 9, 0, tzinfo=UTC)]
    assert _rdates(tail) == [datetime(2026, 7, 23, 9, 0, tzinfo=UTC)]
    assert _exdates(tail) == [datetime(2026, 7, 27, 9, 0, tzinfo=UTC)]


def test_delete_this_and_future_drops_the_added_dates_after_the_cut() -> None:
    calendar = FakeCalendar(DATED_SERIES)

    delete_event(
        calendar, "dated-1", recurrence_id=THIRD_OCCURRENCE, this_and_future=True
    )

    head = _master(calendar.event.stored())
    assert _rdates(head) == [datetime(2026, 7, 9, 9, 0, tzinfo=UTC)]
    assert _exdates(head) == [datetime(2026, 7, 13, 9, 0, tzinfo=UTC)]


def test_splitting_at_an_added_date_is_refused() -> None:
    calendar = FakeCalendar(DATED_SERIES)

    with pytest.raises(Refused, match="rdate_split"):
        update_event(
            calendar,
            "dated-1",
            {"summary": "Onwards"},
            recurrence_id="2026-07-09 09:00:00+00:00",
            this_and_future=True,
        )

    assert calendar.created == []


def test_a_moved_split_shifts_the_added_dates_in_wall_clock() -> None:
    calendar = FakeCalendar(DST_SERIES)

    update_event(
        calendar,
        "dst-1",
        {
            "summary": "A day later",
            "dtstart": datetime(2026, 10, 19, 7, 0, tzinfo=UTC),
            "dtend": datetime(2026, 10, 19, 8, 0, tzinfo=UTC),
        },
        recurrence_id="2026-10-18 07:00:00+00:00",
        this_and_future=True,
    )

    tail = _only_vevent(calendar.created[0].data)
    moved = _rdates(tail)[0].astimezone(ZoneInfo("Europe/Berlin"))
    assert moved.date() == date(2026, 10, 25)
    assert moved.hour == 9


def test_splitting_at_an_overridden_occurrence_does_not_leave_it_behind() -> None:
    calendar = FakeCalendar(SERIES_WITH_OVERRIDE)

    update_event(
        calendar,
        "timed-1",
        {"summary": "Onwards"},
        recurrence_id=SECOND_OCCURRENCE,
        this_and_future=True,
    )

    head = ICalendar.from_ical(calendar.event.data)
    tail = _only_vevent(calendar.created[0].data)
    assert _overrides(head) == []
    assert tail["DTSTART"].dt == datetime(2026, 7, 13, 9, 0, tzinfo=UTC)


def test_splitting_twice_does_not_clone_the_tail_again() -> None:
    calendar = FakeCalendar(TIMED_SERIES)
    for _ in range(2):
        update_event(
            calendar,
            "timed-1",
            {"summary": "Onwards"},
            recurrence_id=THIRD_OCCURRENCE,
            this_and_future=True,
        )

    assert len(calendar.created) == 1


def test_split_keeps_the_tail_when_the_head_cannot_be_re_read() -> None:
    from caldav.davclient import requests

    calendar = FakeCalendar(
        TIMED_SERIES,
        save_error=requests.Timeout("boom"),
        refetch_error=requests.ConnectionError("still down"),
    )
    with pytest.raises(requests.Timeout):
        update_event(
            calendar,
            "timed-1",
            {"summary": "New rhythm"},
            recurrence_id=SECOND_OCCURRENCE,
            this_and_future=True,
        )

    assert not calendar.created[0].deleted


def test_renaming_a_zoned_series_leaves_its_anchor_alone() -> None:
    calendar = FakeCalendar(TZID_SERIES)

    update_event(calendar, "tz-1", {"summary": "Renamed"})

    master = _master(calendar.event.stored())
    assert master["DTSTART"].params.get("TZID") == "Europe/Berlin"
    assert master["DTSTART"].dt.hour == 9


def test_renaming_a_floating_series_leaves_it_floating() -> None:
    calendar = FakeCalendar(FLOATING_SERIES)

    update_event(calendar, "float-1", {"summary": "Renamed"})

    master = _master(calendar.event.stored())
    assert master["DTSTART"].dt.tzinfo is None
    assert master["DTSTART"].dt == datetime(2026, 7, 6, 9, 0)


def test_the_same_instant_in_another_zone_does_not_re_anchor() -> None:
    calendar = FakeCalendar(TZID_SERIES)

    # 09:00 Europe/Berlin in July is 07:00 UTC.
    update_event(
        calendar,
        "tz-1",
        {"dtstart": datetime(2026, 7, 6, 7, 0, tzinfo=UTC)},
    )

    assert _master(calendar.event.stored())["DTSTART"].params.get("TZID") == (
        "Europe/Berlin"
    )


@pytest.mark.parametrize(
    ("series", "uid", "recurrence_id", "expected"),
    [
        (TIMED_SERIES, "timed-1", THIRD_OCCURRENCE, datetime(2026, 7, 20, 9, 0)),
        (
            FLOATING_SERIES,
            "float-1",
            "2026-07-20 09:00:00",
            datetime(2026, 7, 20, 9, 0),
        ),
        (TZID_SERIES, "tz-1", "2026-07-20 07:00:00+00:00", datetime(2026, 7, 20, 9, 0)),
    ],
)
def test_a_new_override_sits_on_its_own_occurrence(
    series, uid, recurrence_id, expected
) -> None:
    calendar = FakeCalendar(series)

    update_event(
        calendar, uid, {"summary": "Renamed once"}, recurrence_id=recurrence_id
    )

    override = _overrides(calendar.event.stored())[0]
    assert override["DTSTART"].dt.replace(tzinfo=None) == expected
    assert override["DTEND"].dt.replace(tzinfo=None) == expected.replace(hour=10)


def test_a_new_all_day_override_keeps_its_length() -> None:
    calendar = FakeCalendar(ALL_DAY_SERIES)

    update_event(
        calendar, "allday-1", {"summary": "Renamed once"}, recurrence_id="2026-07-20"
    )

    override = _overrides(calendar.event.stored())[0]
    assert override["DTSTART"].dt == date(2026, 7, 20)
    assert override["DTEND"].dt == date(2026, 7, 21)


def test_a_named_start_still_wins_over_the_occurrence() -> None:
    calendar = FakeCalendar(TIMED_SERIES)

    update_event(
        calendar,
        "timed-1",
        {
            "summary": "Moved",
            "dtstart": datetime(2026, 7, 20, 14, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 20, 15, 30, tzinfo=UTC),
        },
        recurrence_id=THIRD_OCCURRENCE,
    )

    override = _overrides(calendar.event.stored())[0]
    assert override["DTSTART"].dt == datetime(2026, 7, 20, 14, 0, tzinfo=UTC)
    assert override["DTEND"].dt == datetime(2026, 7, 20, 15, 30, tzinfo=UTC)


def test_splitting_without_a_start_anchors_the_tail_on_the_occurrence() -> None:
    calendar = FakeCalendar(TIMED_SERIES)

    update_event(
        calendar,
        "timed-1",
        {"summary": "Onwards"},
        recurrence_id=THIRD_OCCURRENCE,
        this_and_future=True,
    )

    tail = _only_vevent(calendar.created[0].data)
    assert tail["DTSTART"].dt == datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
    assert tail["DTEND"].dt == datetime(2026, 7, 20, 10, 0, tzinfo=UTC)


def test_an_rrule_is_never_written_onto_a_single_occurrence() -> None:
    calendar = FakeCalendar(TIMED_SERIES)

    update_event(
        calendar,
        "timed-1",
        {"summary": "Renamed once", "rrule": "FREQ=DAILY"},
        recurrence_id=THIRD_OCCURRENCE,
    )

    assert "RRULE" not in _overrides(calendar.event.stored())[0]


def test_naming_one_side_of_a_timed_event_cannot_make_it_half_all_day() -> None:
    calendar = FakeCalendar(SINGLE_EVENT)

    with pytest.raises(Refused, match="mixed_time_types"):
        update_event(calendar, "single-1", {"dtstart": date(2026, 7, 8)})

    assert not calendar.event.saved


def test_an_end_before_its_start_is_refused() -> None:
    calendar = FakeCalendar(SINGLE_EVENT)

    with pytest.raises(Refused, match="end_before_start"):
        update_event(
            calendar, "single-1", {"dtend": datetime(2026, 7, 6, 8, 0, tzinfo=UTC)}
        )

    assert not calendar.event.saved


def test_an_absent_field_is_left_alone_and_an_explicit_none_clears_it() -> None:
    calendar = FakeCalendar(DETAILED_SERIES)

    update_event(calendar, "detailed-1", {"summary": "Renamed", "location": None})

    master = _master(calendar.event.stored())
    assert master["SUMMARY"] == "Renamed"
    assert "LOCATION" not in master
    assert master["DESCRIPTION"] == "Daily sync"


def test_a_series_cannot_be_switched_between_all_day_and_timed() -> None:
    calendar = FakeCalendar(TIMED_SERIES)

    with pytest.raises(Refused, match="allday_timed_switch"):
        update_event(
            calendar,
            "timed-1",
            {"dtstart": date(2026, 7, 6), "dtend": date(2026, 7, 7)},
        )

    assert not calendar.event.saved


def test_a_single_event_can_be_switched_to_all_day() -> None:
    calendar = FakeCalendar(SINGLE_EVENT)

    update_event(
        calendar, "single-1", {"dtstart": date(2026, 7, 6), "dtend": date(2026, 7, 7)}
    )

    assert _only_vevent(calendar.event.data)["DTSTART"].dt == date(2026, 7, 6)


def test_renaming_a_whole_series_keeps_its_overrides_and_exdates() -> None:
    calendar = FakeCalendar(SERIES_WITH_OVERRIDE_AND_EXDATE)

    update_event(
        calendar,
        "timed-1",
        {
            "summary": "Daily sync",
            "dtstart": datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 6, 10, 0, tzinfo=UTC),
            "rrule": "FREQ=WEEKLY",
        },
    )

    stored = calendar.event.stored()
    master = _master(stored)
    assert master["SUMMARY"] == "Daily sync"
    assert [str(item["RECURRENCE-ID"].dt) for item in _overrides(stored)] == [
        "2026-07-13 09:00:00+00:00"
    ]
    assert _exdates(master) == [datetime(2026, 7, 20, 9, 0, tzinfo=UTC)]


BOUNDED_SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:timed-1
DTSTAMP:20260101T000000Z
DTSTART:20260706T090000Z
DTEND:20260706T100000Z
RRULE:FREQ=WEEKLY;UNTIL=20260803T090000Z
SUMMARY:Standup
END:VEVENT
END:VCALENDAR
"""

RDATE_ONLY_SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:dated-1
DTSTAMP:20260101T000000Z
DTSTART:20260706T090000Z
DTEND:20260706T100000Z
RDATE:20260709T090000Z
RDATE:20260716T090000Z
SUMMARY:Standup
END:VEVENT
END:VCALENDAR
"""


def _occurrences(component) -> set:
    return {component["DTSTART"].dt} | {
        value.astimezone(UTC) for value in _rdates(component)
    }


def test_moving_a_split_later_carries_until_with_it() -> None:
    calendar = FakeCalendar(BOUNDED_SERIES)

    update_event(
        calendar,
        "timed-1",
        {
            "summary": "Standup",
            "dtstart": datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 14, 10, 0, tzinfo=UTC),
        },
        recurrence_id=SECOND_OCCURRENCE,
        this_and_future=True,
    )

    tail = _only_vevent(calendar.created[0].data)
    assert tail["RRULE"]["UNTIL"] == [datetime(2026, 8, 4, 9, 0, tzinfo=UTC)]


def test_a_series_made_only_of_rdates_can_be_split() -> None:
    calendar = FakeCalendar(RDATE_ONLY_SERIES)

    update_event(
        calendar,
        "dated-1",
        {
            "summary": "Later half",
            "dtstart": datetime(2026, 7, 16, 9, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 16, 10, 0, tzinfo=UTC),
        },
        recurrence_id="2026-07-16 09:00:00+00:00",
        this_and_future=True,
    )

    head = _master(calendar.event.stored())
    assert _rdates(head) == [datetime(2026, 7, 9, 9, 0, tzinfo=UTC)]
    tail = _only_vevent(calendar.created[0].data)
    assert tail["SUMMARY"] == "Later half"


def test_the_occurrence_a_series_is_cut_at_belongs_to_the_tail() -> None:
    calendar = FakeCalendar(RDATE_ONLY_SERIES)

    update_event(
        calendar,
        "dated-1",
        {
            "summary": "Later half",
            "dtstart": datetime(2026, 7, 9, 9, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 9, 10, 0, tzinfo=UTC),
        },
        recurrence_id="2026-07-09 09:00:00+00:00",
        this_and_future=True,
    )

    # RFC 5545 counts an RDATE repeating DTSTART as one occurrence.
    head = _master(calendar.event.stored())
    tail = _only_vevent(calendar.created[0].data)
    assert _occurrences(head) == {datetime(2026, 7, 6, 9, 0, tzinfo=UTC)}
    assert _occurrences(tail) == {
        datetime(2026, 7, 9, 9, 0, tzinfo=UTC),
        datetime(2026, 7, 16, 9, 0, tzinfo=UTC),
    }


def test_two_splits_of_one_series_do_not_collide_on_the_tail() -> None:
    def split(calendar, occurrence: str, start: datetime) -> str:
        update_event(
            calendar,
            "timed-1",
            {
                "summary": "Tail",
                "dtstart": start,
                "dtend": start + timedelta(hours=1),
            },
            recurrence_id=occurrence,
            this_and_future=True,
        )
        return str(_only_vevent(calendar.created[-1].data)["UID"])

    second = split(
        FakeCalendar(TIMED_SERIES),
        SECOND_OCCURRENCE,
        datetime(2026, 7, 13, 9, 0, tzinfo=UTC),
    )
    third = split(
        FakeCalendar(TIMED_SERIES),
        THIRD_OCCURRENCE,
        datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
    )

    # RFC 4791 allows one UID per object.
    assert second != third
    assert second.startswith("timed-1-")


GOOGLE_ALL_DAY_SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:google-1
DTSTAMP:20260101T000000Z
DTSTART;VALUE=DATE:20260706
DTEND;VALUE=DATE:20260707
RRULE:FREQ=WEEKLY;UNTIL=20260831T215959Z
SUMMARY:Standup
END:VEVENT
END:VCALENDAR
"""


def test_deleting_from_an_all_day_series_bounded_in_utc() -> None:
    # Google writes a DATE start with a UTC UNTIL, and dateutil refuses the pair.
    calendar = FakeCalendar(GOOGLE_ALL_DAY_SERIES)

    delete_event(calendar, "google-1", "2026-07-13")

    assert _exdates(_master(calendar.event.stored())) == [date(2026, 7, 13)]


def test_updating_one_occurrence_of_a_series_bounded_in_utc() -> None:
    calendar = FakeCalendar(GOOGLE_ALL_DAY_SERIES)

    update_event(calendar, "google-1", {"summary": "Moved"}, recurrence_id="2026-07-13")

    overrides = _overrides(calendar.event.stored())
    assert str(overrides[0]["SUMMARY"]) == "Moved"


def test_splitting_a_series_bounded_in_utc() -> None:
    calendar = FakeCalendar(GOOGLE_ALL_DAY_SERIES)

    update_event(
        calendar,
        "google-1",
        {"summary": "Later"},
        recurrence_id="2026-07-20",
        this_and_future=True,
    )

    assert calendar.created


EXCLUDED_SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:excluded-1
DTSTAMP:20260101T000000Z
DTSTART:20260706T090000Z
DTEND:20260706T100000Z
RRULE:FREQ=WEEKLY
EXDATE:20260713T090000Z
SUMMARY:Standup
END:VEVENT
END:VCALENDAR
"""

DATED_MASTER = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:dated-2
DTSTAMP:20260101T000000Z
DTSTART:20260706T090000Z
DTEND:20260706T100000Z
RRULE:FREQ=WEEKLY
RDATE:20260709T090000Z
EXDATE:20260727T090000Z
SEQUENCE:8
SUMMARY:Standup
END:VEVENT
END:VCALENDAR
"""

ORPHAN_ONLY_OBJECT = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:orphan-1
DTSTAMP:20260101T000000Z
RECURRENCE-ID:20260713T090000Z
DTSTART:20260713T110000Z
DTEND:20260713T120000Z
SUMMARY:Moved once
END:VEVENT
BEGIN:VEVENT
UID:orphan-1
DTSTAMP:20260101T000000Z
RECURRENCE-ID:20260720T090000Z
DTSTART:20260720T110000Z
DTEND:20260720T120000Z
SUMMARY:Moved twice
END:VEVENT
END:VCALENDAR
"""


def test_an_excluded_occurrence_cannot_be_updated() -> None:
    # An override for an EXDATEd slot is hidden by that same EXDATE.
    calendar = FakeCalendar(EXCLUDED_SERIES)

    with pytest.raises(Refused, match="occurrence_not_found"):
        update_event(
            calendar,
            "excluded-1",
            {"summary": "Resurrected"},
            recurrence_id="2026-07-13 09:00:00+00:00",
        )


def test_a_new_override_does_not_inherit_the_dates_of_its_series() -> None:
    calendar = FakeCalendar(DATED_MASTER)

    update_event(
        calendar, "dated-2", {"summary": "Moved"}, recurrence_id=SECOND_OCCURRENCE
    )

    override = _overrides(calendar.event.stored())[0]
    assert "RRULE" not in override
    assert "RDATE" not in override
    assert "EXDATE" not in override


def test_a_new_override_starts_its_own_sequence() -> None:
    calendar = FakeCalendar(DATED_MASTER)

    update_event(
        calendar, "dated-2", {"summary": "Moved"}, recurrence_id=SECOND_OCCURRENCE
    )

    override = _overrides(calendar.event.stored())[0]
    assert int(override["SEQUENCE"]) == 1


def test_an_orphan_object_refuses_an_occurrence_it_does_not_hold() -> None:
    calendar = FakeCalendar(ORPHAN_ONLY_OBJECT)

    with pytest.raises(Refused, match="occurrence_not_found"):
        update_event(
            calendar,
            "orphan-1",
            {"summary": "Again"},
            recurrence_id="2026-07-13 11:00:00+00:00",
        )

    assert not calendar.event.saved


def test_this_and_future_on_an_orphan_object_is_refused() -> None:
    calendar = FakeCalendar(ORPHAN_ONLY_OBJECT)

    with pytest.raises(Refused, match="not_recurring"):
        update_event(
            calendar,
            "orphan-1",
            {"summary": "Later"},
            recurrence_id="2026-07-20 09:00:00+00:00",
            this_and_future=True,
        )


def test_editing_a_series_on_an_orphan_object_does_not_empty_it() -> None:
    lone = ICalendar.from_ical(ORPHAN_ONLY_OBJECT)
    for component in lone.walk("VEVENT")[1:]:
        lone.subcomponents.remove(component)
    calendar = FakeCalendar(lone.to_ical().decode("utf-8"))

    update_event(
        calendar,
        "orphan-1",
        {
            "summary": "Renamed",
            "dtstart": datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 13, 13, 0, tzinfo=UTC),
        },
    )

    remaining = calendar.event.stored().walk("VEVENT")
    assert len(remaining) == 1
    assert str(remaining[0]["SUMMARY"]) == "Renamed"


BOUNDED_WITH_LATER_RDATE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:bounded-2
DTSTAMP:20260101T000000Z
DTSTART:20260706T090000Z
DTEND:20260706T100000Z
RRULE:FREQ=WEEKLY;UNTIL=20260720T090000Z
RDATE:20260810T090000Z
SUMMARY:Standup
END:VEVENT
END:VCALENDAR
"""

MULTI_VALUE_RDATE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:multi-1
DTSTAMP:20260101T000000Z
DTSTART:20260706T090000Z
DTEND:20260706T100000Z
RRULE:FREQ=WEEKLY;COUNT=2
RDATE:20260709T090000Z,20260723T090000Z
SUMMARY:Standup
END:VEVENT
END:VCALENDAR
"""

RDATE_HEAD_SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:rdate-only-1
DTSTAMP:20260101T000000Z
DTSTART:20260706T090000Z
DTEND:20260706T100000Z
RDATE:20260709T090000Z
SUMMARY:Standup
END:VEVENT
END:VCALENDAR
"""

RELATED_SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:related-1
DTSTAMP:20260101T000000Z
DTSTART:20260706T090000Z
DTEND:20260706T100000Z
RRULE:FREQ=WEEKLY
RELATED-TO:parent-uid
SUMMARY:Standup
END:VEVENT
END:VCALENDAR
"""

DURATION_SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:duration-1
DTSTAMP:20260101T000000Z
DTSTART:20260706T090000Z
DURATION:PT1H
RRULE:FREQ=WEEKLY
SUMMARY:Standup
END:VEVENT
END:VCALENDAR
"""


def test_a_cut_past_the_rules_own_end_leaves_until_alone() -> None:
    calendar = FakeCalendar(BOUNDED_WITH_LATER_RDATE)

    delete_event(
        calendar,
        "bounded-2",
        recurrence_id="2026-08-10 09:00:00+00:00",
        this_and_future=True,
    )

    rrule = _master(calendar.event.stored())["RRULE"]
    assert rrule["UNTIL"][0] == datetime(2026, 7, 20, 9, 0, tzinfo=UTC)


ENDS_ON_CUT_SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:ends-1
DTSTAMP:20260101T000000Z
DTSTART:20260706T090000Z
DTEND:20260706T100000Z
RRULE:FREQ=WEEKLY;UNTIL=20260720T090000Z
SUMMARY:Standup
END:VEVENT
END:VCALENDAR
"""


def test_a_cut_on_the_series_own_last_occurrence_still_removes_it() -> None:
    calendar = FakeCalendar(ENDS_ON_CUT_SERIES)

    delete_event(
        calendar, "ends-1", recurrence_id=THIRD_OCCURRENCE, this_and_future=True
    )

    rrule = _master(calendar.event.stored())["RRULE"]
    assert rrule["UNTIL"][0] < datetime(2026, 7, 20, 9, 0, tzinfo=UTC)


def test_a_split_divides_a_multi_value_date_line() -> None:
    calendar = FakeCalendar(MULTI_VALUE_RDATE)

    update_event(
        calendar,
        "multi-1",
        {"summary": "Later"},
        recurrence_id="2026-07-23 09:00:00+00:00",
        this_and_future=True,
    )

    head = {v.astimezone(UTC) for v in _rdates(_master(calendar.event.stored()))}
    tail = {v.astimezone(UTC) for v in _rdates(_master(calendar.created[0].stored()))}
    assert head == {datetime(2026, 7, 9, 9, 0, tzinfo=UTC)}
    assert datetime(2026, 7, 9, 9, 0, tzinfo=UTC) not in tail


def test_an_all_day_series_gets_a_date_exdate_from_a_timed_recurrence_id() -> None:
    # RFC 5545 makes EXDATE share DTSTART's value type, and strict servers reject
    # the whole object otherwise.
    calendar = FakeCalendar(ALL_DAY_SERIES)

    delete_event(calendar, "allday-1", recurrence_id="2026-07-08 00:00:00+00:00")

    assert _exdates(_master(calendar.event.stored())) == [date(2026, 7, 8)]


def test_a_split_off_tail_drops_related_to() -> None:
    # caldav follows RELATED-TO and would write into the objects it names.
    calendar = FakeCalendar(RELATED_SERIES)

    update_event(
        calendar,
        "related-1",
        {"summary": "Later"},
        recurrence_id=THIRD_OCCURRENCE,
        this_and_future=True,
    )

    assert "RELATED-TO" not in _master(calendar.created[0].stored())


def test_an_rdate_only_series_cannot_be_switched_to_all_day() -> None:
    calendar = FakeCalendar(RDATE_HEAD_SERIES)

    with pytest.raises(Refused, match="allday_timed_switch"):
        update_event(
            calendar,
            "rdate-only-1",
            {
                "summary": "Standup",
                "dtstart": date(2026, 7, 6),
                "dtend": date(2026, 7, 7),
            },
        )


def test_the_head_of_an_rdate_only_series_can_be_edited() -> None:
    # RFC 5545 counts DTSTART itself as the first occurrence.
    calendar = FakeCalendar(RDATE_HEAD_SERIES)

    update_event(
        calendar, "rdate-only-1", {"summary": "Moved"}, recurrence_id=FIRST_OCCURRENCE
    )

    assert str(_overrides(calendar.event.stored())[0]["SUMMARY"]) == "Moved"


HOURLY_SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:hourly-1
DTSTAMP:20260101T000000Z
DTSTART:20260706T090000Z
DTEND:20260706T093000Z
RRULE:FREQ=HOURLY;INTERVAL=4
SUMMARY:Check in
END:VEVENT
END:VCALENDAR
"""


def test_two_splits_on_one_day_do_not_share_a_tail_uid() -> None:
    first = FakeCalendar(HOURLY_SERIES)
    update_event(
        first,
        "hourly-1",
        {"summary": "A"},
        recurrence_id="2026-07-06 13:00:00+00:00",
        this_and_future=True,
    )
    second = FakeCalendar(HOURLY_SERIES)
    update_event(
        second,
        "hourly-1",
        {"summary": "B"},
        recurrence_id="2026-07-06 17:00:00+00:00",
        this_and_future=True,
    )

    assert str(_master(first.created[0].stored())["UID"]) != str(
        _master(second.created[0].stored())["UID"]
    )


def test_writing_an_end_removes_a_duration_the_server_had() -> None:
    # RFC 5545 forbids DTEND and DURATION together.
    calendar = FakeCalendar(DURATION_SERIES)

    update_event(
        calendar,
        "duration-1",
        {
            "summary": "Standup",
            "dtstart": datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 6, 11, 0, tzinfo=UTC),
        },
    )

    stored = _master(calendar.event.stored())
    assert "DURATION" not in stored
    assert "DTEND" in stored


NIGHT_JOB_SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:dst-1
DTSTAMP:20260101T000000Z
DTSTART;TZID=Europe/Berlin:20260301T023000
DTEND;TZID=Europe/Berlin:20260301T033000
RRULE:FREQ=WEEKLY
SUMMARY:Nightly job
END:VEVENT
END:VCALENDAR
"""


@pytest.mark.parametrize(
    ("moment", "recurrence_id"),
    [
        ("spring gap", "2026-03-29T03:30:00+02:00"),
        ("autumn fold", "2026-10-25T02:30:00+02:00"),
        ("plain week", "2026-03-22T02:30:00+01:00"),
    ],
)
def test_an_occurrence_on_a_dst_boundary_can_still_be_edited(
    moment: str, recurrence_id: str
) -> None:
    """PEP 495 makes a gap or fold compare unequal across zones, instants aside."""
    calendar = FakeCalendar(NIGHT_JOB_SERIES)

    update_event(calendar, "dst-1", {"summary": "Renamed"}, recurrence_id=recurrence_id)

    overrides = _overrides(calendar.event.stored())
    assert [str(item["SUMMARY"]) for item in overrides] == ["Renamed"], moment


def test_a_dst_boundary_occurrence_can_start_a_this_and_future_split() -> None:
    calendar = FakeCalendar(NIGHT_JOB_SERIES)

    update_event(
        calendar,
        "dst-1",
        {"summary": "Renamed"},
        recurrence_id="2026-03-29T03:30:00+02:00",
        this_and_future=True,
    )

    assert len(calendar.created) == 1
    tail = _master(calendar.created[0].stored())
    assert str(tail["SUMMARY"]) == "Renamed"


@pytest.mark.parametrize("hour", [7, 11])
def test_a_start_named_on_its_own_takes_the_end_with_it(hour: int) -> None:
    calendar = FakeCalendar(TIMED_SERIES)

    update_event(
        calendar,
        "timed-1",
        {"dtstart": datetime(2026, 7, 6, hour, 0, tzinfo=UTC)},
    )

    master = _master(calendar.event.stored())
    assert master["DTSTART"].dt == datetime(2026, 7, 6, hour, 0, tzinfo=UTC)
    assert master["DTEND"].dt == datetime(2026, 7, 6, hour + 1, 0, tzinfo=UTC)


def test_a_start_named_on_its_own_moves_one_occurrence_whole() -> None:
    calendar = FakeCalendar(TIMED_SERIES)

    update_event(
        calendar,
        "timed-1",
        {"dtstart": datetime(2026, 7, 13, 11, 0, tzinfo=UTC)},
        recurrence_id=SECOND_OCCURRENCE,
    )

    override = _overrides(calendar.event.stored())[0]
    assert override["DTSTART"].dt == datetime(2026, 7, 13, 11, 0, tzinfo=UTC)
    assert override["DTEND"].dt == datetime(2026, 7, 13, 12, 0, tzinfo=UTC)


def test_a_rule_is_refused_on_an_object_that_holds_only_detached_instances() -> None:
    calendar = FakeCalendar(ORPHAN_OVERRIDES)

    with pytest.raises(Refused) as refusal:
        update_event(calendar, "timed-1", {"rrule": "FREQ=DAILY;COUNT=3"})

    assert refusal.value.key == "not_recurring"
    assert not calendar.event.saved


def test_editing_one_occurrence_leaves_the_series_version_alone() -> None:
    """RFC 5546: a SEQUENCE bump on the master announces a new whole series."""
    calendar = FakeCalendar(SERIES_WITH_OVERRIDE)
    before = int(_master(calendar.event.stored()).get("SEQUENCE", 0))

    update_event(
        calendar, "timed-1", {"summary": "Renamed"}, recurrence_id=SECOND_OCCURRENCE
    )

    stored = calendar.event.stored()
    assert int(_master(stored).get("SEQUENCE", 0)) == before
    assert int(_overrides(stored)[0]["SEQUENCE"]) == 1


def test_a_series_edit_moves_its_version_on_by_one() -> None:
    calendar = FakeCalendar(TIMED_SERIES)

    update_event(calendar, "timed-1", {"summary": "Renamed"})

    assert int(_master(calendar.event.stored())["SEQUENCE"]) == 1


TODO_AHEAD_OF_EVENT = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VTODO
UID:mixed-1
DTSTAMP:20260101T000000Z
SUMMARY:File the contract
SEQUENCE:5
END:VTODO
BEGIN:VEVENT
UID:mixed-1
DTSTAMP:20260101T000000Z
DTSTART:20260706T090000Z
DTEND:20260706T100000Z
SUMMARY:Contract review
SEQUENCE:2
END:VEVENT
END:VCALENDAR
"""


def test_an_edit_moves_the_version_of_the_component_it_touched() -> None:
    """caldav bumps the SEQUENCE of the first component that is not a timezone, and
    a VTODO may sit ahead of the VEVENT under the same uid."""
    calendar = FakeCalendar(TODO_AHEAD_OF_EVENT)

    update_event(calendar, "mixed-1", {"summary": "Renamed"})

    stored = calendar.event.stored()
    vevent = next(item for item in stored.walk("VEVENT"))
    vtodo = next(item for item in stored.walk("VTODO"))
    assert int(vevent["SEQUENCE"]) == 3
    assert int(vtodo["SEQUENCE"]) == 5


TAIL_OVERRIDE_SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:o-1
DTSTAMP:20260101T000000Z
DTSTART:20260706T090000Z
DTEND:20260706T100000Z
RRULE:FREQ=WEEKLY;COUNT=8
SUMMARY:Standup
END:VEVENT
BEGIN:VEVENT
UID:o-1
DTSTAMP:20260101T000000Z
RECURRENCE-ID:20260803T090000Z
DTSTART:20260803T140000Z
DTEND:20260803T150000Z
SUMMARY:Standup, moved
END:VEVENT
END:VCALENDAR
"""


def test_a_split_carries_an_exception_that_falls_in_the_tail() -> None:
    calendar = FakeCalendar(TAIL_OVERRIDE_SERIES)

    update_event(
        calendar,
        "o-1",
        {"summary": "Onwards"},
        recurrence_id="2026-07-20T09:00:00+00:00",
        this_and_future=True,
    )

    assert _overrides(ICalendar.from_ical(calendar.event.data)) == []
    tail = ICalendar.from_ical(calendar.created[0].data)
    moved = _overrides(tail)
    assert [str(item["SUMMARY"]) for item in moved] == ["Standup, moved"]
    assert str(moved[0]["UID"]) == str(_master(tail)["UID"])
    assert moved[0]["DTSTART"].dt == datetime(2026, 8, 3, 14, 0, tzinfo=UTC)


def test_a_moved_split_takes_its_carried_exception_along() -> None:
    calendar = FakeCalendar(TAIL_OVERRIDE_SERIES)

    update_event(
        calendar,
        "o-1",
        {
            "dtstart": datetime(2026, 7, 20, 11, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        },
        recurrence_id="2026-07-20T09:00:00+00:00",
        this_and_future=True,
    )

    moved = _overrides(ICalendar.from_ical(calendar.created[0].data))[0]
    assert moved["RECURRENCE-ID"].dt == datetime(2026, 8, 3, 11, 0, tzinfo=UTC)
    assert moved["DTSTART"].dt == datetime(2026, 8, 3, 16, 0, tzinfo=UTC)


def test_a_split_that_replaces_the_rule_drops_the_exceptions() -> None:
    calendar = FakeCalendar(TAIL_OVERRIDE_SERIES)

    update_event(
        calendar,
        "o-1",
        {"rrule": "FREQ=DAILY;COUNT=4"},
        recurrence_id="2026-07-20T09:00:00+00:00",
        this_and_future=True,
    )

    assert _overrides(ICalendar.from_ical(calendar.created[0].data)) == []


@pytest.fixture
def berlin():
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(ZoneInfo("Europe/Berlin"))
    yield
    dt_util.set_default_time_zone(previous)


def test_a_floating_series_is_written_in_the_local_wall_clock(berlin) -> None:
    """A value without a zone means local time, not UTC."""
    calendar = FakeCalendar(FLOATING_SERIES)

    update_event(
        calendar,
        "float-1",
        {
            "dtstart": datetime(2026, 7, 6, 15, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 6, 16, 0, tzinfo=UTC),
        },
    )

    master = _master(calendar.event.stored())
    assert master["DTSTART"].dt == datetime(2026, 7, 6, 17, 0)
    assert master["DTSTART"].dt.tzinfo is None


def test_an_occurrence_of_a_floating_series_is_found_by_its_local_time(
    berlin,
) -> None:
    calendar = FakeCalendar(FLOATING_SERIES)

    delete_event(calendar, "float-1", recurrence_id="2026-07-13T09:00:00")

    assert _exdates(_master(calendar.event.stored())) == [datetime(2026, 7, 13, 9, 0)]


def test_an_all_day_override_keeps_the_date_value_type(berlin) -> None:
    """A RECURRENCE-ID that gains a time no longer names a slot of the series."""
    calendar = FakeCalendar(ALL_DAY_SERIES)

    update_event(
        calendar, "allday-1", {"summary": "Renamed"}, recurrence_id="2026-07-08"
    )

    override = _overrides(calendar.event.stored())[0]
    assert override["RECURRENCE-ID"].dt == date(2026, 7, 8)


def test_a_zero_length_event_is_allowed(berlin) -> None:
    """RFC 5545 permits DTEND == DTSTART; servers store them."""
    calendar = FakeCalendar(TIMED_SERIES)
    moment = datetime(2026, 7, 6, 9, 0, tzinfo=UTC)

    update_event(calendar, "timed-1", {"dtstart": moment, "dtend": moment})

    master = _master(calendar.event.stored())
    assert master["DTEND"].dt == master["DTSTART"].dt


def test_a_floating_occurrence_is_matched_against_a_zoned_recurrence_id(
    berlin,
) -> None:
    """The frontend sends an aware id; a floating series means local time."""
    calendar = FakeCalendar(FLOATING_SERIES)

    delete_event(calendar, "float-1", recurrence_id="2026-07-13T07:00:00+00:00")

    assert _exdates(_master(calendar.event.stored())) == [datetime(2026, 7, 13, 9, 0)]


ZONED_FLOATING_UNTIL = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:until-1
DTSTAMP:20260101T000000Z
DTSTART;TZID=Europe/Berlin:20260706T090000
DTEND;TZID=Europe/Berlin:20260706T100000
RRULE:FREQ=WEEKLY;UNTIL=20260720T080000
SUMMARY:Standup
END:VEVENT
END:VCALENDAR
"""


def test_an_until_without_a_zone_is_read_in_utc(berlin) -> None:
    """RFC 5545 3.3.10 gives a zoned start a UTC UNTIL, and the expansion the
    panel is filled from reads one written without a zone that way."""
    calendar = FakeCalendar(ZONED_FLOATING_UNTIL)

    update_event(
        calendar,
        "until-1",
        {"summary": "Onwards"},
        recurrence_id="2026-07-20T09:00:00+02:00",
        this_and_future=True,
    )

    assert str(_master(calendar.created[0].stored())["SUMMARY"]) == "Onwards"


def test_an_occurrence_before_a_floating_until_stays_editable(berlin) -> None:
    calendar = FakeCalendar(ZONED_FLOATING_UNTIL)

    update_event(
        calendar,
        "until-1",
        {"summary": "Renamed"},
        recurrence_id="2026-07-20T09:00:00+02:00",
    )

    override = next(
        v for v in calendar.event.stored().walk("VEVENT") if "RECURRENCE-ID" in v
    )
    assert str(override["SUMMARY"]) == "Renamed"


def test_a_split_tail_puts_the_master_before_the_exceptions_it_carries() -> None:
    """caldav reads the first component that is not a timezone, and finding a
    RECURRENCE-ID there sends it looking up a uid the tail is only creating."""
    later_override_first = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:timed-1
DTSTAMP:20260101T000000Z
RECURRENCE-ID:20260720T090000Z
DTSTART:20260720T110000Z
DTEND:20260720T120000Z
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
    calendar = FakeCalendar(later_override_first)

    update_event(
        calendar,
        "timed-1",
        {"summary": "Renamed"},
        recurrence_id=SECOND_OCCURRENCE,
        this_and_future=True,
    )

    tail = calendar.created[0].stored()
    events = [sub for sub in tail.subcomponents if sub.name == "VEVENT"]
    assert "RECURRENCE-ID" not in events[0]
    assert any("RECURRENCE-ID" in event for event in events[1:])


def test_editing_a_whole_object_of_detached_instances_is_refused() -> None:
    calendar = FakeCalendar(ORPHAN_OVERRIDES)

    with pytest.raises(Refused, match="not_recurring"):
        update_event(
            calendar,
            "timed-1",
            {"dtstart": datetime(2026, 7, 13, 12, 0, tzinfo=UTC)},
        )

    assert not calendar.event.saved


def test_an_absolute_alarm_survives_an_edit_that_sets_the_relative_ones() -> None:
    from custom_components.ha_caldav.event import _set_alarms

    event = ICalendar.from_ical(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VEVENT\r\nUID:u\r\nDTSTAMP:20260101T000000Z\r\n"
        "DTSTART:20260706T090000Z\r\n"
        "BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:rel\r\n"
        "TRIGGER:-PT15M\r\nEND:VALARM\r\n"
        "BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:abs\r\n"
        "TRIGGER;VALUE=DATE-TIME:20260706T080000Z\r\nEND:VALARM\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    vevent = next(iter(event.walk("VEVENT")))

    _set_alarms(vevent, [30])

    kept = [sub for sub in vevent.subcomponents if sub.name == "VALARM"]
    assert [str(sub["DESCRIPTION"]) for sub in kept] == ["abs", "Reminder"]


def test_an_exception_covering_later_occurrences_is_refused() -> None:
    """RFC 5545 RANGE=THISANDFUTURE stands for its occurrence and every later one."""
    ranged = SERIES_WITH_OVERRIDE_FIRST.replace(
        "RECURRENCE-ID:20260713T090000Z",
        "RECURRENCE-ID;RANGE=THISANDFUTURE:20260713T090000Z",
    )
    calendar = FakeCalendar(ranged)

    with pytest.raises(Refused, match="ranged_override"):
        update_event(
            calendar, "timed-1", {"summary": "Renamed"}, recurrence_id=THIRD_OCCURRENCE
        )

    assert not calendar.event.saved


def test_moving_a_whole_series_carries_its_extra_and_canceled_dates() -> None:
    series = TIMED_SERIES.replace(
        "RRULE:FREQ=WEEKLY",
        "RRULE:FREQ=WEEKLY\nRDATE:20260709T090000Z\nEXDATE:20260720T090000Z",
    )
    calendar = FakeCalendar(series)

    update_event(
        calendar,
        "timed-1",
        {
            "dtstart": datetime(2026, 7, 6, 9, 30, tzinfo=UTC),
            "dtend": datetime(2026, 7, 6, 10, 30, tzinfo=UTC),
        },
    )

    master = _master(calendar.event.stored())
    assert master["RDATE"].dts[0].dt == datetime(2026, 7, 9, 9, 30, tzinfo=UTC)
    assert master["EXDATE"].dts[0].dt == datetime(2026, 7, 20, 9, 30, tzinfo=UTC)


def test_splitting_at_a_moved_occurrence_leaves_the_rest_of_the_series_put() -> None:
    """The frontend names the start the occurrence actually has."""
    calendar = FakeCalendar(SERIES_WITH_OVERRIDE_FIRST)

    update_event(
        calendar,
        "timed-1",
        {
            "summary": "Renamed",
            "dtstart": datetime(2026, 7, 13, 11, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
        },
        recurrence_id=SECOND_OCCURRENCE,
        this_and_future=True,
    )

    tail = calendar.created[0].stored()
    master = _master(tail)
    assert master["DTSTART"].dt == datetime(2026, 7, 13, 9, 0, tzinfo=UTC)
    assert str(master["SUMMARY"]) == "Renamed"

    override = next(c for c in tail.walk("VEVENT") if "RECURRENCE-ID" in c)
    assert override["RECURRENCE-ID"].dt == datetime(2026, 7, 13, 9, 0, tzinfo=UTC)
    assert override["DTSTART"].dt == datetime(2026, 7, 13, 11, 0, tzinfo=UTC)


def test_moving_a_moved_occurrence_onwards_shifts_by_what_the_user_changed() -> None:
    calendar = FakeCalendar(SERIES_WITH_OVERRIDE_FIRST)

    update_event(
        calendar,
        "timed-1",
        {
            "dtstart": datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 13, 13, 0, tzinfo=UTC),
        },
        recurrence_id=SECOND_OCCURRENCE,
        this_and_future=True,
    )

    tail = calendar.created[0].stored()
    assert _master(tail)["DTSTART"].dt == datetime(2026, 7, 13, 10, 0, tzinfo=UTC)

    override = next(c for c in tail.walk("VEVENT") if "RECURRENCE-ID" in c)
    assert override["RECURRENCE-ID"].dt == datetime(2026, 7, 13, 10, 0, tzinfo=UTC)
    assert override["DTSTART"].dt == datetime(2026, 7, 13, 12, 0, tzinfo=UTC)


def test_an_all_day_occurrence_named_as_an_instant_finds_its_own_override() -> None:
    with_override = ALL_DAY_SERIES.replace(
        "END:VCALENDAR",
        "BEGIN:VEVENT\nUID:allday-1\nDTSTAMP:20260101T000000Z\n"
        "RECURRENCE-ID;VALUE=DATE:20260708\n"
        "DTSTART;VALUE=DATE:20260708\nDTEND;VALUE=DATE:20260709\n"
        "SUMMARY:Moved one\nEND:VEVENT\nEND:VCALENDAR",
    )
    calendar = FakeCalendar(with_override)

    update_event(
        calendar,
        "allday-1",
        {"summary": "Renamed"},
        recurrence_id="2026-07-08 00:00:00+00:00",
    )

    stored = calendar.event.stored()
    overrides = [c for c in stored.walk("VEVENT") if "RECURRENCE-ID" in c]
    assert len(overrides) == 1
    assert str(overrides[0]["SUMMARY"]) == "Renamed"


def test_an_all_day_occurrence_named_as_an_instant_respects_an_exdate() -> None:
    excluded = ALL_DAY_SERIES.replace(
        "RRULE:FREQ=DAILY", "RRULE:FREQ=DAILY\nEXDATE;VALUE=DATE:20260710"
    )
    calendar = FakeCalendar(excluded)

    with pytest.raises(Refused, match="occurrence_not_found"):
        update_event(
            calendar,
            "allday-1",
            {"summary": "Renamed"},
            recurrence_id="2026-07-10 00:00:00+00:00",
        )


def test_a_split_off_series_asks_its_attendees_again() -> None:
    """RFC 5546: an organizer's REQUEST for a new event carries NEEDS-ACTION."""
    invited = TIMED_SERIES.replace(
        "SUMMARY:Standup",
        "ATTENDEE;PARTSTAT=ACCEPTED:mailto:iven@example.com\nSUMMARY:Standup",
    )
    calendar = FakeCalendar(invited)

    update_event(
        calendar,
        "timed-1",
        {"summary": "Renamed"},
        recurrence_id=SECOND_OCCURRENCE,
        this_and_future=True,
    )

    tail = _master(calendar.created[0].stored())
    attendee = tail["ATTENDEE"]
    attendee = attendee[0] if isinstance(attendee, list) else attendee
    assert str(attendee.params["PARTSTAT"]) == "NEEDS-ACTION"

    head = _master(calendar.event.stored())
    kept = head["ATTENDEE"]
    kept = kept[0] if isinstance(kept, list) else kept
    assert str(kept.params["PARTSTAT"]) == "ACCEPTED"


def test_a_whole_series_nudge_carries_its_exceptions_along() -> None:
    calendar = FakeCalendar(SERIES_WITH_OVERRIDE)

    update_event(
        calendar,
        "timed-1",
        {
            "summary": "Standup",
            "dtstart": datetime(2026, 7, 6, 9, 30, tzinfo=UTC),
            "dtend": datetime(2026, 7, 6, 10, 30, tzinfo=UTC),
        },
    )

    stored = calendar.event.stored()
    override = next(v for v in stored.walk("VEVENT") if "RECURRENCE-ID" in v)
    assert override["RECURRENCE-ID"].dt == datetime(2026, 7, 13, 9, 30, tzinfo=UTC)
    assert override["DTSTART"].dt == datetime(2026, 7, 13, 11, 30, tzinfo=UTC)


GOOGLE_MIDNIGHT_SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:midnight-1
DTSTAMP:20260101T000000Z
DTSTART;VALUE=DATE:20260706
DTEND;VALUE=DATE:20260707
RRULE:FREQ=DAILY;UNTIL=20260710T000000Z
SUMMARY:Water plants
END:VEVENT
END:VCALENDAR
"""


@pytest.fixture
def new_york():
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(ZoneInfo("America/New_York"))
    yield
    dt_util.set_default_time_zone(previous)


def test_the_last_day_of_an_all_day_series_can_be_split_off(new_york) -> None:
    """Google writes an aware UNTIL against a DATE start."""
    calendar = FakeCalendar(GOOGLE_MIDNIGHT_SERIES)

    update_event(
        calendar,
        "midnight-1",
        {"summary": "Renamed"},
        recurrence_id="2026-07-10",
        this_and_future=True,
    )

    assert calendar.created


def test_the_last_day_of_an_all_day_series_can_be_deleted(new_york) -> None:
    calendar = FakeCalendar(GOOGLE_MIDNIGHT_SERIES)

    delete_event(calendar, "midnight-1", "2026-07-10", this_and_future=True)

    master = _master(calendar.event.stored())
    assert str(master["RRULE"].to_ical().decode()) == "FREQ=DAILY;UNTIL=20260709"


def test_a_whole_series_edit_is_refused_over_a_ranged_exception() -> None:
    ranged = SERIES_WITH_OVERRIDE_FIRST.replace(
        "RECURRENCE-ID:20260713T090000Z",
        "RECURRENCE-ID;RANGE=THISANDFUTURE:20260713T090000Z",
    )
    calendar = FakeCalendar(ranged)

    with pytest.raises(Refused, match="ranged_override"):
        update_event(calendar, "timed-1", {"rrule": "FREQ=WEEKLY;INTERVAL=2"})

    assert not calendar.event.saved


def test_renaming_a_series_with_a_ranged_exception_is_still_allowed() -> None:
    ranged = SERIES_WITH_OVERRIDE_FIRST.replace(
        "RECURRENCE-ID:20260713T090000Z",
        "RECURRENCE-ID;RANGE=THISANDFUTURE:20260713T090000Z",
    )
    calendar = FakeCalendar(ranged)

    update_event(calendar, "timed-1", {"summary": "Renamed"})

    assert calendar.event.saved


def test_an_event_stored_without_a_start_is_refused_by_name() -> None:
    """RFC 5545 leaves DTSTART out once an object carries a METHOD."""
    startless = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nMETHOD:REQUEST\r\n"
        "BEGIN:VEVENT\r\nUID:no-start\r\nDTSTAMP:20260101T000000Z\r\n"
        "DTEND:20260706T100000Z\r\nSUMMARY:Invitation\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    calendar = FakeCalendar(startless)

    with pytest.raises(Refused, match="no_start_in_object"):
        update_event(calendar, "no-start", {"summary": "Renamed"})


def test_turning_an_all_day_event_into_a_timed_one_carries_its_timezone() -> None:
    """RFC 5545 wants the definition alongside the reference: strict servers refuse
    a TZID without one, lenient ones store a time others read as floating."""
    single = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VEVENT\r\nUID:allday-single\r\nDTSTAMP:20260101T000000Z\r\n"
        "DTSTART;VALUE=DATE:20260706\r\nDTEND;VALUE=DATE:20260707\r\n"
        "SUMMARY:Holiday\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    calendar = FakeCalendar(single)
    berlin = ZoneInfo("Europe/Berlin")

    update_event(
        calendar,
        "allday-single",
        {
            "summary": "Now timed",
            "dtstart": datetime(2026, 7, 6, 9, 0, tzinfo=berlin),
            "dtend": datetime(2026, 7, 6, 17, 0, tzinfo=berlin),
        },
    )

    stored = calendar.event.stored()
    referenced = {
        str(component[key].params["TZID"])
        for component in stored.walk("VEVENT")
        for key in ("DTSTART", "DTEND")
        if key in component and "TZID" in component[key].params
    }
    defined = {str(zone["TZID"]) for zone in stored.walk("VTIMEZONE")}
    assert referenced
    assert referenced <= defined


def test_moving_a_series_onto_a_gap_keeps_its_exceptions_attached() -> None:
    """A wall time in a spring-forward gap comes back an hour later from a trip
    through UTC."""
    ny = ZoneInfo("America/New_York")
    series = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VEVENT\r\nUID:gap-1\r\nDTSTAMP:20260101T000000Z\r\n"
        "DTSTART;TZID=America/New_York:20260305T023000\r\n"
        "DTEND;TZID=America/New_York:20260305T024500\r\n"
        "RRULE:FREQ=DAILY\r\nSUMMARY:Backup\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:gap-1\r\nDTSTAMP:20260101T000000Z\r\n"
        "RECURRENCE-ID;TZID=America/New_York:20260310T023000\r\n"
        "DTSTART;TZID=America/New_York:20260310T043000\r\n"
        "DTEND;TZID=America/New_York:20260310T044500\r\n"
        "SUMMARY:Backup, later\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    calendar = FakeCalendar(series)

    # 2026-03-08 02:30 in New York does not exist: the clocks go forward.
    update_event(
        calendar,
        "gap-1",
        {
            "summary": "Backup",
            "dtstart": datetime(2026, 3, 8, 2, 30, tzinfo=ny),
            "dtend": datetime(2026, 3, 8, 2, 45, tzinfo=ny),
        },
    )

    stored = calendar.event.stored()
    master = _master(stored)
    override = next(v for v in stored.walk("VEVENT") if "RECURRENCE-ID" in v)
    from custom_components.ha_caldav.recurrence import _on_rule

    assert _on_rule(master, override["RECURRENCE-ID"].dt)


def test_a_series_nudge_keeps_its_excluded_dates_on_the_clock() -> None:
    ny = ZoneInfo("America/New_York")
    series = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VEVENT\r\nUID:gap-2\r\nDTSTAMP:20260101T000000Z\r\n"
        "DTSTART;TZID=America/New_York:20260305T023000\r\n"
        "DTEND;TZID=America/New_York:20260305T024500\r\n"
        "RRULE:FREQ=DAILY\r\n"
        "EXDATE;TZID=America/New_York:20260312T023000\r\n"
        "SUMMARY:Backup\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    calendar = FakeCalendar(series)

    update_event(
        calendar,
        "gap-2",
        {
            "summary": "Backup",
            "dtstart": datetime(2026, 3, 8, 2, 30, tzinfo=ny),
            "dtend": datetime(2026, 3, 8, 2, 45, tzinfo=ny),
        },
    )

    master = _master(calendar.event.stored())
    excluded = _exdates(master)[0]
    assert excluded.astimezone(ny).strftime("%Y-%m-%d %H:%M") == "2026-03-15 02:30"


def test_splitting_at_an_hour_the_zone_skips_does_not_retime_the_tail() -> None:
    """The frontend resolves an occurrence to an instant, which converted back into
    the series zone lands an hour past a slot in a DST gap."""
    series = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VEVENT\r\nUID:yearly-1\r\nDTSTAMP:20260101T000000Z\r\n"
        "DTSTART;TZID=Europe/Berlin:20260325T023000\r\n"
        "DTEND;TZID=Europe/Berlin:20260325T024500\r\n"
        "RRULE:FREQ=YEARLY\r\nSUMMARY:Yearly\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    calendar = FakeCalendar(series)

    # Berlin springs forward on 2040-03-25, so 02:30 does not exist that year.
    update_event(
        calendar,
        "yearly-1",
        {"summary": "Renamed"},
        recurrence_id="2040-03-25 02:30:00+01:00",
        this_and_future=True,
    )

    tail = _only_vevent(calendar.created[0].data)
    assert tail["DTSTART"].dt.strftime("%Y-%m-%d %H:%M") == "2040-03-25 02:30"


DENSE = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
    "BEGIN:VEVENT\r\nUID:dense-1\r\nDTSTAMP:20260101T000000Z\r\n"
    "DTSTART:20260101T000000Z\r\nDTEND:20260101T000030Z\r\n"
    "RRULE:FREQ=SECONDLY\r\nSUMMARY:Tick\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)


@pytest.mark.parametrize(
    "call",
    [
        lambda cal: update_event(
            cal, "dense-1", {"summary": "x"}, recurrence_id="2027-01-01 00:00:00+00:00"
        ),
        lambda cal: update_event(
            cal,
            "dense-1",
            {"summary": "x"},
            recurrence_id="2027-01-01 00:00:00+00:00",
            this_and_future=True,
        ),
    ],
)
def test_a_rule_too_dense_to_walk_is_refused_rather_than_walked(call) -> None:
    """FREQ=SECONDLY is legal, and a slot a year out is thirty million steps away."""
    calendar = FakeCalendar(DENSE)

    with pytest.raises(Refused, match="rrule_too_dense"):
        call(calendar)

    assert not calendar.event.saved


@pytest.mark.parametrize(
    ("ics", "this_and_future"),
    [
        (DENSE.replace("0000Z\r\nDTEND", "0000\r\nDTEND"), False),
        (DENSE.replace("FREQ=SECONDLY", "FREQ=SECONDLY;COUNT=99999999"), True),
    ],
    ids=["floating", "counted"],
)
def test_a_dense_rule_is_refused_on_every_walk_it_needs(
    ics: str, this_and_future: bool
) -> None:
    calendar = FakeCalendar(ics)

    with pytest.raises(Refused, match="rrule_too_dense"):
        update_event(
            calendar,
            "dense-1",
            {"summary": "x"},
            recurrence_id="2027-01-01 00:00:00+00:00",
            this_and_future=this_and_future,
        )

    assert not calendar.event.saved


@pytest.mark.parametrize("this_and_future", [False, True])
def test_deleting_from_a_dense_series_does_not_walk_it(this_and_future: bool) -> None:
    calendar = FakeCalendar(DENSE)

    delete_event(
        calendar,
        "dense-1",
        recurrence_id="2027-01-01 00:00:00+00:00",
        this_and_future=this_and_future,
    )

    assert calendar.event.saved


def test_a_floating_series_takes_an_id_naming_an_hour_the_local_zone_skips(
    new_york,
) -> None:
    """The frontend echoes an aware id for a floating series, and as_local moves an
    hour the local zone skips forward."""
    floating = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VEVENT\r\nUID:float-1\r\nDTSTAMP:20260101T000000Z\r\n"
        "DTSTART:20260305T023000\r\nDTEND:20260305T024500\r\n"
        "RRULE:FREQ=DAILY\r\nSUMMARY:Nightly\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    calendar = FakeCalendar(floating)

    # 2026-03-08 02:30 does not exist in New York.
    delete_event(calendar, "float-1", recurrence_id="2026-03-08 02:30:00-05:00")

    master = _master(calendar.event.stored())
    assert _exdates(master)[0].strftime("%Y-%m-%d %H:%M") == "2026-03-08 02:30"


@pytest.mark.parametrize(
    "zone", ["UTC", "America/New_York", "Asia/Kolkata", "Pacific/Kiritimati"]
)
def test_the_uid_of_a_split_all_day_series_does_not_move_with_the_zone(
    zone: str,
) -> None:
    calendar = FakeCalendar(ALL_DAY_SERIES)
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(ZoneInfo(zone))
    try:
        update_event(
            calendar,
            "allday-1",
            {"summary": "Later"},
            recurrence_id="2026-07-09",
            this_and_future=True,
        )
    finally:
        dt_util.set_default_time_zone(previous)

    tail = _only_vevent(calendar.created[0].data)
    assert str(tail["UID"]) == "allday-1-20260709T000000"


def test_a_split_tail_carries_the_uid_of_the_instant_it_starts_at() -> None:
    calendar = FakeCalendar(TIMED_SERIES)

    update_event(
        calendar,
        "timed-1",
        {"summary": "Later"},
        recurrence_id=SECOND_OCCURRENCE,
        this_and_future=True,
    )

    tail = _only_vevent(calendar.created[0].data)
    assert str(tail["UID"]) == "timed-1-20260713T090000Z"


def test_a_tail_whose_rule_does_not_produce_its_own_start_is_refused() -> None:
    mondays = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VEVENT\r\nUID:m-1\r\nDTSTAMP:20260101T000000Z\r\n"
        "DTSTART:20260706T090000Z\r\nDTEND:20260706T100000Z\r\n"
        "RRULE:FREQ=WEEKLY;BYDAY=MO\r\n"
        "SUMMARY:Standup\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    calendar = FakeCalendar(mondays)

    with pytest.raises(Refused, match="rrule_mismatch"):
        update_event(
            calendar,
            "m-1",
            {
                "summary": "Later",
                "dtstart": datetime(2026, 7, 15, 9, 0, tzinfo=UTC),
                "dtend": datetime(2026, 7, 15, 10, 0, tzinfo=UTC),
            },
            recurrence_id="2026-07-13 09:00:00+00:00",
            this_and_future=True,
        )


@pytest.mark.parametrize(
    "instant",
    [
        # The hour the clocks skip, and both halves of the hour they repeat.
        datetime(2026, 3, 8, 7, 30, tzinfo=UTC),
        datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
        datetime(2026, 11, 1, 6, 30, tzinfo=UTC),
    ],
)
def test_stripping_the_zone_off_a_local_time_stays_an_exact_inverse(
    new_york, instant: datetime
) -> None:
    """PEP 495 puts fold on the value, not on the zone, so it survives the strip."""
    from custom_components.ha_caldav.event import to_utc

    walled = dt_util.as_local(instant).replace(tzinfo=None)

    assert to_utc(walled) == instant


ALL_DAY_WITH_EXTRAS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:allday-2
DTSTAMP:20260101T000000Z
DTSTART;VALUE=DATE:20260706
DTEND;VALUE=DATE:20260707
RRULE:FREQ=WEEKLY
EXDATE;VALUE=DATE:20260720
SUMMARY:Water plants
END:VEVENT
BEGIN:VEVENT
UID:allday-2
DTSTAMP:20260101T000000Z
RECURRENCE-ID;VALUE=DATE:20260713
DTSTART;VALUE=DATE:20260713
DTEND;VALUE=DATE:20260714
SUMMARY:Water the big one
END:VEVENT
END:VCALENDAR
"""

TIMED_WITH_EXTRAS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:timed-2
DTSTAMP:20260101T000000Z
DTSTART:20260706T090000Z
DTEND:20260706T100000Z
RRULE:FREQ=WEEKLY
RDATE:20260709T090000Z
EXDATE:20260720T090000Z
SUMMARY:Standup
END:VEVENT
BEGIN:VEVENT
UID:timed-2
DTSTAMP:20260101T000000Z
RECURRENCE-ID:20260713T090000Z
DTSTART:20260713T110000Z
DTEND:20260713T120000Z
SUMMARY:Moved one
END:VEVENT
END:VCALENDAR
"""

ORPHANS_LATE_FIRST = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:orphan-2
DTSTAMP:20260101T000000Z
RECURRENCE-ID:20260727T090000Z
DTSTART:20260727T110000Z
DTEND:20260727T120000Z
SUMMARY:Third
END:VEVENT
BEGIN:VEVENT
UID:orphan-2
DTSTAMP:20260101T000000Z
RECURRENCE-ID:20260713T090000Z
DTSTART:20260713T110000Z
DTEND:20260713T120000Z
SUMMARY:First
END:VEVENT
END:VCALENDAR
"""


def test_moving_an_all_day_series_shifts_its_dates_by_the_days_asked_for(
    berlin,
) -> None:
    calendar = FakeCalendar(ALL_DAY_WITH_EXTRAS)

    update_event(
        calendar,
        "allday-2",
        {"dtstart": date(2026, 7, 13), "dtend": date(2026, 7, 14)},
    )

    stored = calendar.event.stored()
    master = _master(stored)
    override = next(v for v in stored.walk("VEVENT") if "RECURRENCE-ID" in v)
    assert master["DTSTART"].dt == date(2026, 7, 13)
    assert master["EXDATE"].dts[0].dt == date(2026, 7, 27)
    assert override["RECURRENCE-ID"].dt == date(2026, 7, 20)


def test_this_and_future_at_the_first_occurrence_keeps_what_the_series_carries(
    berlin,
) -> None:
    calendar = FakeCalendar(TIMED_WITH_EXTRAS)

    update_event(
        calendar,
        "timed-2",
        {"summary": "Renamed"},
        recurrence_id="2026-07-06 09:00:00+00:00",
        this_and_future=True,
    )

    stored = calendar.event.stored()
    master = _master(stored)
    assert str(master["SUMMARY"]) == "Renamed"
    assert master["RDATE"].dts[0].dt == datetime(2026, 7, 9, 9, 0, tzinfo=UTC)
    assert master["EXDATE"].dts[0].dt == datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
    assert [v for v in stored.walk("VEVENT") if "RECURRENCE-ID" in v]


def test_this_and_future_at_the_first_occurrence_moves_the_dates_with_it() -> None:
    calendar = FakeCalendar(TIMED_WITH_EXTRAS)

    update_event(
        calendar,
        "timed-2",
        {
            "dtstart": datetime(2026, 7, 6, 10, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 6, 11, 0, tzinfo=UTC),
        },
        recurrence_id="2026-07-06 09:00:00+00:00",
        this_and_future=True,
    )

    master = _master(calendar.event.stored())
    assert master["RDATE"].dts[0].dt == datetime(2026, 7, 9, 10, 0, tzinfo=UTC)
    assert master["EXDATE"].dts[0].dt == datetime(2026, 7, 20, 10, 0, tzinfo=UTC)


@pytest.mark.parametrize("swapped", [False, True])
def test_deleting_from_an_occurrence_onwards_needs_a_series_head(swapped) -> None:
    lines = ORPHANS_LATE_FIRST.splitlines(keepends=True)
    body = (
        "".join(lines[:3] + lines[11:19] + lines[3:11] + lines[19:])
        if swapped
        else ORPHANS_LATE_FIRST
    )
    calendar = FakeCalendar(body)

    with pytest.raises(Refused) as refusal:
        delete_event(
            calendar,
            "orphan-2",
            recurrence_id="2026-07-20 09:00:00+00:00",
            this_and_future=True,
        )

    assert refusal.value.key == "not_recurring"
    assert not calendar.event.deleted


def test_a_saved_edit_carries_a_fresh_dtstamp() -> None:
    """RFC 5545 3.8.7.2: with no METHOD on the object, DTSTAMP is when the
    information was last revised."""
    calendar = FakeCalendar(TIMED_SERIES)

    update_event(calendar, "timed-1", {"summary": "Renamed"})

    master = _master(calendar.event.stored())
    assert master["DTSTAMP"].dt > datetime(2026, 1, 1, tzinfo=UTC)
    assert master["DTSTAMP"].dt == master["LAST-MODIFIED"].dt


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=DAILY;INTERVAL=0",
        "FREQ=DAILY;BYMONTHDAY=99",
        "FREQ=MONTHLY;BYMONTHDAY=30;BYMONTH=2",
    ],
)
def test_a_rule_nothing_can_expand_is_refused_before_it_is_stored(rule) -> None:
    calendar = FakeCalendar(TIMED_SERIES)

    with pytest.raises(Refused) as refusal:
        update_event(calendar, "timed-1", {"rrule": rule})

    assert refusal.value.key == "invalid_rrule"
    assert not calendar.event.saved


@pytest.mark.parametrize(
    ("half", "kept"),
    [("head", "20260709"), ("tail", "20260723")],
)
def test_a_split_keeps_the_parameters_of_the_dates_it_rebuilds(half, kept) -> None:
    """VALUE=DATE and TZID live in the parameters of a date line."""
    calendar = FakeCalendar(ALL_DAY_DATED_SERIES)

    update_event(
        calendar,
        "allday-dated-1",
        {"summary": "Bins moved"},
        recurrence_id="2026-07-20",
        this_and_future=True,
    )

    body = calendar.event.data if half == "head" else calendar.created[0].data
    assert f"RDATE;VALUE=DATE:{kept}" in body
    assert "\r\nRDATE:" not in body


def test_moving_a_dated_series_keeps_the_parameters_of_the_shifted_dates() -> None:
    calendar = FakeCalendar(ALL_DAY_DATED_SERIES)

    update_event(
        calendar,
        "allday-dated-1",
        {"dtstart": date(2026, 7, 7), "dtend": date(2026, 7, 8)},
    )

    body = calendar.event.data
    assert "RDATE;VALUE=DATE:20260710,20260724" in body
    assert "EXDATE;VALUE=DATE:20260714,20260728" in body


def test_moving_a_zoned_series_keeps_it_anchored_to_its_own_zone() -> None:
    calendar = FakeCalendar(TZID_SERIES)

    update_event(
        calendar,
        "tz-1",
        {
            "dtstart": datetime(2026, 7, 6, 11, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 6, 12, 0, tzinfo=UTC),
        },
    )

    assert "DTSTART;TZID=Europe/Berlin:20260706T130000" in calendar.event.data


def test_a_split_of_an_object_with_a_repeated_uid_names_the_tail_once() -> None:
    """icalendar hands a repeated property back as a list."""
    calendar = FakeCalendar(DOUBLED_UID_SERIES)

    update_event(
        calendar,
        "timed-1",
        {"summary": "Standup moved"},
        recurrence_id=SECOND_OCCURRENCE,
        this_and_future=True,
    )

    tail = _only_vevent(calendar.created[0].data)
    assert str(tail["UID"]) == "timed-1-20260713T090000Z"


def test_an_empty_rule_removes_the_recurrence() -> None:
    """Expanding strips RRULE from what the frontend echoes back."""
    calendar = FakeCalendar(TIMED_SERIES)

    update_event(calendar, "timed-1", {"summary": "Once", "rrule": ""})

    assert "RRULE" not in _master(calendar.event.stored())


def test_a_repeated_occurrence_delete_adds_one_exdate() -> None:
    """Home Assistant retries a failed delete."""
    calendar = FakeCalendar(TIMED_SERIES)
    delete_event(calendar, "timed-1", recurrence_id=SECOND_OCCURRENCE)
    again = FakeCalendar(calendar.event.data)

    delete_event(again, "timed-1", recurrence_id=SECOND_OCCURRENCE)

    assert again.event.data.count("EXDATE") == 1


def test_the_conflict_check_reads_the_document_the_write_is_built_on() -> None:
    """caldav's load() replaces the document before it records the etag."""
    fresh = DETAILED_SERIES.replace("LOCATION:Room 1", "LOCATION:Room 5")
    calendar = FakeCalendar(DETAILED_SERIES, etag='"unchanged"', server_ics=fresh)

    update_event(
        calendar, "detailed-1", {"summary": "Renamed"}, expected_etag='"unchanged"'
    )

    assert "LOCATION:Room 5" in calendar.event.data


def test_a_split_that_cannot_take_its_tail_back_still_reports_the_real_failure(
    caplog,
) -> None:
    calendar = FakeCalendar(
        TIMED_SERIES,
        save_error=DAVError("head write refused"),
        refetch_ics=TIMED_SERIES,
        created_delete_error=DAVError("and the tail will not go either"),
    )

    with pytest.raises(DAVError, match="head write refused"):
        update_event(
            calendar,
            "timed-1",
            {"summary": "From here on"},
            recurrence_id=SECOND_OCCURRENCE,
            this_and_future=True,
        )

    assert "duplicate events" in caplog.text


def test_a_series_moved_with_its_rule_respelled_keeps_its_exceptions() -> None:
    # The frontend re-emits the rule of a moved series in its own spelling.
    calendar = FakeCalendar(SERIES_WITH_OVERRIDE_AND_EXDATE)

    update_event(
        calendar,
        "timed-1",
        {**_renamed(10), "summary": "Standup", "rrule": "FREQ=WEEKLY;BYDAY=MO"},
        recurrence_id=FIRST_OCCURRENCE,
        this_and_future=True,
    )

    stored = calendar.event.stored()
    assert _exdates(_master(stored)) == [datetime(2026, 7, 20, 10, 0, tzinfo=UTC)]
    [override] = _overrides(stored)
    assert override["RECURRENCE-ID"].dt == datetime(2026, 7, 13, 10, 0, tzinfo=UTC)
    assert override["SUMMARY"] == "Standup moved"


def test_a_tail_with_the_rule_respelled_keeps_the_exceptions_past_the_cut() -> None:
    calendar = FakeCalendar(SERIES_WITH_OVERRIDE_AND_EXDATE)

    update_event(
        calendar,
        "timed-1",
        {
            "summary": "Standup",
            "dtstart": datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 13, 13, 0, tzinfo=UTC),
            "rrule": "FREQ=WEEKLY;BYDAY=MO",
        },
        recurrence_id=SECOND_OCCURRENCE,
        this_and_future=True,
    )

    tail = calendar.created[0].stored()
    assert _master(tail)["RRULE"].to_ical() == b"FREQ=WEEKLY;BYDAY=MO"
    assert _exdates(_master(tail)) == [datetime(2026, 7, 20, 10, 0, tzinfo=UTC)]
    assert len(_overrides(tail)) == 1


def test_a_tail_echoing_the_count_of_the_whole_series_takes_what_is_left() -> None:
    calendar = FakeCalendar(COUNTED_SERIES)

    update_event(
        calendar,
        "counted-1",
        {
            "summary": "Standup",
            "dtstart": datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 20, 11, 0, tzinfo=UTC),
            "rrule": "FREQ=WEEKLY;COUNT=10;BYDAY=MO",
        },
        recurrence_id=THIRD_OCCURRENCE,
        this_and_future=True,
    )

    assert _master(calendar.created[0].stored())["RRULE"]["COUNT"] == [8]


def test_an_empty_rule_from_an_occurrence_on_ends_the_series_there() -> None:
    calendar = FakeCalendar(TIMED_SERIES)

    update_event(
        calendar,
        "timed-1",
        {"summary": "Last one", "rrule": ""},
        recurrence_id=THIRD_OCCURRENCE,
        this_and_future=True,
    )

    assert "UNTIL" in _master(calendar.event.stored())["RRULE"]
    tail = _master(calendar.created[0].stored())
    assert "RRULE" not in tail
    assert tail["DTSTART"].dt == datetime(2026, 7, 20, 9, 0, tzinfo=UTC)


FIRST_MOVED_SERIES = TIMED_SERIES.replace(
    "END:VEVENT\n",
    "END:VEVENT\nBEGIN:VEVENT\nUID:timed-1\nDTSTAMP:20260101T000000Z\n"
    "RECURRENCE-ID:20260706T090000Z\nDTSTART:20260706T110000Z\n"
    "DTEND:20260706T120000Z\nSUMMARY:Standup moved\nEND:VEVENT\n",
)


def test_an_edit_from_a_moved_first_occurrence_leaves_the_series_on_its_slots() -> None:
    calendar = FakeCalendar(FIRST_MOVED_SERIES)

    update_event(
        calendar,
        "timed-1",
        {
            "summary": "Renamed",
            "dtstart": datetime(2026, 7, 6, 11, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 6, 12, 0, tzinfo=UTC),
        },
        recurrence_id=FIRST_OCCURRENCE,
        this_and_future=True,
    )

    stored = calendar.event.stored()
    assert _master(stored)["DTSTART"].dt == datetime(2026, 7, 6, 9, 0, tzinfo=UTC)
    assert _master(stored)["SUMMARY"] == "Renamed"
    [moved] = _overrides(stored)
    assert moved["DTSTART"].dt == datetime(2026, 7, 6, 11, 0, tzinfo=UTC)
    assert moved["SUMMARY"] == "Renamed"


def test_a_split_at_a_moved_occurrence_carries_the_edit_to_it() -> None:
    calendar = FakeCalendar(SERIES_WITH_OVERRIDE)

    update_event(
        calendar,
        "timed-1",
        {
            "summary": "Renamed",
            "location": "Room 9",
            "dtstart": datetime(2026, 7, 13, 11, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
        },
        recurrence_id=SECOND_OCCURRENCE,
        this_and_future=True,
    )

    [moved] = _overrides(calendar.created[0].stored())
    assert moved["SUMMARY"] == "Renamed"
    assert moved["LOCATION"] == "Room 9"
    assert moved["DTSTART"].dt == datetime(2026, 7, 13, 11, 0, tzinfo=UTC)


EXCLUDED_START_SERIES = TIMED_SERIES.replace(
    "RRULE:FREQ=WEEKLY\n", "RRULE:FREQ=WEEKLY\nEXDATE:20260706T090000Z\n"
)


def test_a_split_leaving_no_occurrence_before_it_removes_the_head() -> None:
    # Nextcloud refuses to store an object without a single instance.
    calendar = FakeCalendar(EXCLUDED_START_SERIES)

    update_event(
        calendar,
        "timed-1",
        {"summary": "Renamed"},
        recurrence_id=SECOND_OCCURRENCE,
        this_and_future=True,
    )

    assert calendar.event.deleted
    assert not calendar.event.saved
    assert len(calendar.created) == 1


def test_deleting_from_the_first_occurrence_left_removes_the_whole_object() -> None:
    calendar = FakeCalendar(EXCLUDED_START_SERIES)

    delete_event(calendar, "timed-1", SECOND_OCCURRENCE, this_and_future=True)

    assert calendar.event.deleted
    assert not calendar.event.saved


def test_an_occurrence_of_an_event_that_does_not_recur_is_refused() -> None:
    calendar = FakeCalendar(SINGLE_EVENT)

    with pytest.raises(Refused, match="not_recurring"):
        update_event(
            calendar, "single-1", {"summary": "x"}, recurrence_id=FIRST_OCCURRENCE
        )

    assert not calendar.event.saved


def test_a_new_organizer_for_the_series_reaches_its_exceptions() -> None:
    calendar = FakeCalendar(SERIES_WITH_OVERRIDE)

    update_event(calendar, "timed-1", {"organizer": "new@example.com"})

    for vevent in calendar.event.stored().walk("VEVENT"):
        assert str(vevent["ORGANIZER"]) == "mailto:new@example.com"


INVITED_SERIES = TIMED_SERIES.replace(
    "SUMMARY:Standup\n",
    "SUMMARY:Standup\nSEQUENCE:4\nORGANIZER:mailto:boss@example.com\n"
    "ATTENDEE:mailto:me@example.com\n",
)


def test_an_edit_to_someone_elses_event_leaves_its_sequence_alone() -> None:
    calendar = FakeCalendar(INVITED_SERIES)

    update_event(
        calendar,
        "timed-1",
        {"alarms": [15]},
        addresses=["mailto:me@example.com", "/principals/me/"],
    )

    assert int(_master(calendar.event.stored())["SEQUENCE"]) == 4


def test_an_edit_to_ones_own_event_moves_its_sequence() -> None:
    calendar = FakeCalendar(INVITED_SERIES.replace("boss@", "me@"))

    update_event(
        calendar, "timed-1", {"alarms": [15]}, addresses=["mailto:me@example.com"]
    )

    assert int(_master(calendar.event.stored())["SEQUENCE"]) == 5


def test_a_moved_series_keeps_the_occurrence_its_end_named() -> None:
    calendar = FakeCalendar(
        TIMED_SERIES.replace("FREQ=WEEKLY", "FREQ=WEEKLY;UNTIL=20260720T090000Z")
    )

    update_event(calendar, "timed-1", _renamed(10))

    rule = _master(calendar.event.stored())["RRULE"]
    assert rule["UNTIL"] == [datetime(2026, 7, 20, 10, 0, tzinfo=UTC)]


def test_a_rule_given_with_a_floating_end_is_stored_with_it_in_utc() -> None:
    # The frontend writes UTC digits without the Z.
    calendar = FakeCalendar(TZID_SERIES)
    master = _master(calendar.event.stored())

    update_event(
        calendar,
        master["UID"],
        {"rrule": "FREQ=WEEKLY;UNTIL=20261229T090000"},
    )

    rule = _master(calendar.event.stored())["RRULE"]
    assert rule["UNTIL"] == [datetime(2026, 12, 29, 9, 0, tzinfo=UTC)]


@pytest.mark.parametrize("this_and_future", [False, True])
def test_a_delete_is_refused_over_a_ranged_exception(this_and_future: bool) -> None:
    ranged = SERIES_WITH_OVERRIDE_FIRST.replace(
        "RECURRENCE-ID:20260713T090000Z",
        "RECURRENCE-ID;RANGE=THISANDFUTURE:20260713T090000Z",
    )
    calendar = FakeCalendar(ranged)

    with pytest.raises(Refused, match="ranged_override"):
        delete_event(
            calendar, "timed-1", THIRD_OCCURRENCE, this_and_future=this_and_future
        )

    assert not calendar.event.saved
    assert not calendar.event.deleted


def test_moving_a_series_shifts_every_line_of_its_excluded_dates() -> None:
    # Apple and Thunderbird write one EXDATE per line.
    calendar = FakeCalendar(
        TIMED_SERIES.replace(
            "RRULE:FREQ=WEEKLY\n",
            "RRULE:FREQ=WEEKLY\nEXDATE:20260713T090000Z\nEXDATE:20260720T090000Z\n",
        )
    )

    update_event(calendar, "timed-1", _renamed(10))

    assert _exdates(_master(calendar.event.stored())) == [
        datetime(2026, 7, 13, 10, 0, tzinfo=UTC),
        datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
    ]


THIRD_MOVED_SERIES = FIRST_MOVED_SERIES.replace(
    "RECURRENCE-ID:20260706T090000Z\nDTSTART:20260706T110000Z\nDTEND:20260706T120000Z",
    "RECURRENCE-ID:20260720T090000Z\nDTSTART:20260720T110000Z\nDTEND:20260720T120000Z",
)


def test_ending_a_series_at_a_moved_first_occurrence_keeps_its_time() -> None:
    calendar = FakeCalendar(FIRST_MOVED_SERIES)

    update_event(
        calendar,
        "timed-1",
        {
            "summary": "Standup moved",
            "dtstart": datetime(2026, 7, 6, 11, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 6, 12, 0, tzinfo=UTC),
            "rrule": "",
        },
        recurrence_id=FIRST_OCCURRENCE,
        this_and_future=True,
    )

    stored = calendar.event.stored()
    assert _overrides(stored) == []
    master = _master(stored)
    assert "RRULE" not in master
    assert master["DTSTART"].dt == datetime(2026, 7, 6, 11, 0, tzinfo=UTC)


def test_ending_a_series_from_a_moved_occurrence_on_keeps_its_time() -> None:
    calendar = FakeCalendar(THIRD_MOVED_SERIES)

    update_event(
        calendar,
        "timed-1",
        {
            "summary": "Standup moved",
            "dtstart": datetime(2026, 7, 20, 11, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
            "rrule": "",
        },
        recurrence_id=THIRD_OCCURRENCE,
        this_and_future=True,
    )

    tail = calendar.created[0].stored()
    assert _overrides(tail) == []
    assert _master(tail)["DTSTART"].dt == datetime(2026, 7, 20, 11, 0, tzinfo=UTC)


def test_a_new_rule_from_a_moved_occurrence_on_keeps_that_occurrence_moved() -> None:
    calendar = FakeCalendar(THIRD_MOVED_SERIES)

    update_event(
        calendar,
        "timed-1",
        {
            "summary": "Standup moved",
            "dtstart": datetime(2026, 7, 20, 11, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
            "rrule": "FREQ=DAILY",
        },
        recurrence_id=THIRD_OCCURRENCE,
        this_and_future=True,
    )

    tail = calendar.created[0].stored()
    assert _master(tail)["RRULE"].to_ical() == b"FREQ=DAILY"
    [override] = _overrides(tail)
    assert override["DTSTART"].dt == datetime(2026, 7, 20, 11, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("rule", "occurrence", "moved", "respelled"),
    [
        (
            "FREQ=WEEKLY;BYDAY=MO",
            THIRD_OCCURRENCE,
            datetime(2026, 7, 21, 9, 0, tzinfo=UTC),
            "FREQ=WEEKLY;BYDAY=TU",
        ),
        (
            "FREQ=MONTHLY;BYMONTHDAY=6",
            "2026-09-06 09:00:00+00:00",
            datetime(2026, 9, 7, 9, 0, tzinfo=UTC),
            "FREQ=MONTHLY;BYMONTHDAY=7",
        ),
    ],
)
def test_moving_a_tail_to_another_day_takes_the_rule_as_respelled(
    rule: str, occurrence: str, moved: datetime, respelled: str
) -> None:
    calendar = FakeCalendar(TIMED_SERIES.replace("FREQ=WEEKLY", rule))

    update_event(
        calendar,
        "timed-1",
        {
            "summary": "Standup",
            "dtstart": moved,
            "dtend": moved + timedelta(hours=1),
            "rrule": respelled,
        },
        recurrence_id=occurrence,
        this_and_future=True,
    )

    tail = _master(calendar.created[0].stored())
    assert tail["DTSTART"].dt == moved
    assert tail["RRULE"].to_ical() == respelled.encode()


def test_a_new_rule_for_a_tail_takes_what_is_left_of_an_echoed_count() -> None:
    calendar = FakeCalendar(COUNTED_SERIES)

    update_event(
        calendar,
        "counted-1",
        {
            "summary": "Standup",
            "dtstart": datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
            "rrule": "FREQ=WEEKLY;INTERVAL=2;COUNT=10",
        },
        recurrence_id=THIRD_OCCURRENCE,
        this_and_future=True,
    )

    rule = _master(calendar.created[0].stored())["RRULE"]
    assert rule["INTERVAL"] == [2]
    assert rule["COUNT"] == [8]


def test_a_tail_leaves_a_to_do_sharing_the_resource_and_the_method_behind() -> None:
    calendar = FakeCalendar(
        TIMED_SERIES.replace("VERSION:2.0\n", "VERSION:2.0\nMETHOD:REQUEST\n").replace(
            "END:VCALENDAR",
            "BEGIN:VTODO\nUID:timed-1\nDTSTAMP:20260101T000000Z\n"
            "SUMMARY:Prepare\nEND:VTODO\nEND:VCALENDAR",
        )
    )

    update_event(
        calendar,
        "timed-1",
        {"summary": "Tail"},
        recurrence_id=THIRD_OCCURRENCE,
        this_and_future=True,
    )

    tail = calendar.created[0].stored()
    assert tail.walk("VTODO") == []
    assert "METHOD" not in tail


def test_a_zoned_series_ending_on_a_date_splits_at_its_last_occurrence(
    berlin,
) -> None:
    calendar = FakeCalendar(
        TZID_SERIES.replace("T090000", "T003000")
        .replace("T100000", "T013000")
        .replace("RRULE:FREQ=WEEKLY", "RRULE:FREQ=WEEKLY;UNTIL=20260720")
    )

    update_event(
        calendar,
        "tz-1",
        {"summary": "Tail"},
        recurrence_id="2026-07-20 00:30:00+02:00",
        this_and_future=True,
    )

    assert len(calendar.created) == 1
    assert _master(calendar.event.stored())["RRULE"]["UNTIL"] == [
        datetime(2026, 7, 19, 22, 29, 59, tzinfo=UTC)
    ]


def test_an_occurrence_decades_into_a_daily_series_can_be_edited() -> None:
    calendar = FakeCalendar(
        TIMED_SERIES.replace("20260706T", "19980101T").replace(
            "FREQ=WEEKLY", "FREQ=DAILY"
        )
    )

    update_event(
        calendar,
        "timed-1",
        {"summary": "Once"},
        recurrence_id="2026-09-22 09:00:00+00:00",
    )

    [override] = _overrides(calendar.event.stored())
    assert override["SUMMARY"] == "Once"


def test_deleting_a_detached_instance_leaves_the_others_unrevised() -> None:
    calendar = FakeCalendar(
        """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:orphans-1
DTSTAMP:20260101T000000Z
SEQUENCE:3
RECURRENCE-ID:20260713T090000Z
DTSTART:20260713T110000Z
DTEND:20260713T120000Z
SUMMARY:One
END:VEVENT
BEGIN:VEVENT
UID:orphans-1
DTSTAMP:20260101T000000Z
SEQUENCE:3
RECURRENCE-ID:20260720T090000Z
DTSTART:20260720T110000Z
DTEND:20260720T120000Z
SUMMARY:Two
END:VEVENT
END:VCALENDAR
"""
    )

    delete_event(calendar, "orphans-1", SECOND_OCCURRENCE)

    [kept] = calendar.event.stored().walk("VEVENT")
    assert kept["SUMMARY"] == "Two"
    assert kept["SEQUENCE"] == 3
    assert kept["DTSTAMP"].dt == datetime(2026, 1, 1, tzinfo=UTC)


def test_moving_a_series_revises_the_exceptions_it_moves() -> None:
    calendar = FakeCalendar(SERIES_WITH_OVERRIDE_AND_EXDATE)

    update_event(calendar, "timed-1", _renamed(10))

    [override] = _overrides(calendar.event.stored())
    assert override["RECURRENCE-ID"].dt == datetime(2026, 7, 13, 10, 0, tzinfo=UTC)
    assert override["DTSTAMP"].dt > datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize("frequency", ["SECONDLY", "MINUTELY"])
def test_a_rule_denser_than_hourly_is_refused(frequency: str) -> None:
    calendar = FakeCalendar(TIMED_SERIES)

    with pytest.raises(Refused, match="rrule_too_dense"):
        update_event(calendar, "timed-1", {"rrule": f"FREQ={frequency}"})

    assert not calendar.event.saved
