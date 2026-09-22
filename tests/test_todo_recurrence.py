from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from homeassistant.util import dt as dt_util
from icalendar import Calendar as ICalendar

from custom_components.ha_caldav.api import update_todo


def _vtodo(body: str) -> str:
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//test//EN\r\n"
        "BEGIN:VTODO\r\nUID:t1\r\nDTSTAMP:20260101T000000Z\r\n"
        f"SUMMARY:Water plants\r\n{body}\r\nEND:VTODO\r\nEND:VCALENDAR\r\n"
    )


class FakeTodo:
    def __init__(self, ics: str) -> None:
        self.data = ics
        self._cal = ICalendar.from_ical(ics)
        self.saved = False

    @property
    def icalendar_instance(self):
        return self._cal

    @icalendar_instance.setter
    def icalendar_instance(self, value) -> None:
        # caldav keeps the document handed to it and serializes it only on the way
        # out; a string is put through vcal.fix, which rewrites the object.
        self._cal = value
        self.data = value.to_ical().decode("utf-8")

    @property
    def icalendar_component(self):
        return next(c for c in self._cal.walk() if c.name == "VTODO")

    def set_due(self, due) -> None:
        vtodo = self.icalendar_component
        vtodo.pop("DUE", None)
        vtodo.pop("DURATION", None)
        vtodo.add("DUE", due)

    def save(self, **kwargs) -> None:
        self.saved = True

    def stored(self):
        """Return the to-do out of the document that would reach the server."""
        return next(
            item
            for item in ICalendar.from_ical(self.data).walk()
            if item.name == "VTODO"
        )


class FakeTodoCalendar:
    def __init__(self, ics: str) -> None:
        self.todo = FakeTodo(ics)

    def todo_by_uid(self, uid: str) -> FakeTodo:
        return self.todo


COMPLETE = {
    "summary": "Water plants",
    "status": "COMPLETED",
    "due": datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
}


def test_completing_recurring_todo_rolls_due_forward() -> None:
    calendar = FakeTodoCalendar(_vtodo("DUE:20260706T090000Z\r\nRRULE:FREQ=WEEKLY"))
    update_todo(calendar, "t1", COMPLETE)

    vtodo = calendar.todo.stored()
    assert str(vtodo["STATUS"]) == "NEEDS-ACTION"
    assert vtodo["DUE"].dt == datetime(2026, 7, 13, 9, 0, tzinfo=UTC)


def test_completing_recurring_todo_decrements_count() -> None:
    calendar = FakeTodoCalendar(
        _vtodo("DUE:20260706T090000Z\r\nRRULE:FREQ=WEEKLY;COUNT=3")
    )
    update_todo(calendar, "t1", COMPLETE)

    vtodo = calendar.todo.stored()
    assert vtodo["DUE"].dt == datetime(2026, 7, 13, 9, 0, tzinfo=UTC)
    assert vtodo["RRULE"]["COUNT"] == [2]


def test_completing_last_recurring_todo_closes_it() -> None:
    calendar = FakeTodoCalendar(
        _vtodo("DUE:20260706T090000Z\r\nRRULE:FREQ=WEEKLY;COUNT=1")
    )
    update_todo(calendar, "t1", COMPLETE)

    vtodo = calendar.todo.stored()
    assert str(vtodo["STATUS"]) == "COMPLETED"


def test_completing_recurring_all_day_todo_keeps_date_type() -> None:
    calendar = FakeTodoCalendar(_vtodo("DUE;VALUE=DATE:20260706\r\nRRULE:FREQ=DAILY"))
    update_todo(calendar, "t1", {"summary": "Water plants", "status": "COMPLETED"})

    due = calendar.todo.stored()["DUE"].dt
    assert due == date(2026, 7, 7)
    assert not isinstance(due, datetime)


def test_completing_recurring_todo_shifts_start_and_due_together() -> None:
    calendar = FakeTodoCalendar(
        _vtodo("DTSTART:20260706T090000Z\r\nDUE:20260706T100000Z\r\nRRULE:FREQ=WEEKLY")
    )
    update_todo(calendar, "t1", COMPLETE)

    vtodo = calendar.todo.stored()
    assert vtodo["DTSTART"].dt == datetime(2026, 7, 13, 9, 0, tzinfo=UTC)
    assert vtodo["DUE"].dt == datetime(2026, 7, 13, 10, 0, tzinfo=UTC)


def test_completing_non_recurring_todo_marks_completed() -> None:
    calendar = FakeTodoCalendar(_vtodo("DUE:20260706T090000Z"))
    update_todo(calendar, "t1", COMPLETE)

    assert str(calendar.todo.stored()["STATUS"]) == "COMPLETED"


def test_completing_misaligned_count_one_closes_without_invalid_count() -> None:
    # 2026-07-07 is a Tuesday, so the anchor is not itself an occurrence.
    calendar = FakeTodoCalendar(
        _vtodo("DUE:20260707T090000Z\r\nRRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=1")
    )
    update_todo(calendar, "t1", {"summary": "Water plants", "status": "COMPLETED"})

    vtodo = calendar.todo.stored()
    assert str(vtodo["STATUS"]) == "COMPLETED"
    assert b"COUNT=0" not in vtodo["RRULE"].to_ical()


def test_completing_with_a_mismatched_until_still_rolls_the_item() -> None:
    # Google writes a floating anchor with a UTC UNTIL, which dateutil refuses.
    calendar = FakeTodoCalendar(
        _vtodo("DUE:20260706T090000\r\nRRULE:FREQ=WEEKLY;UNTIL=20260720T090000Z")
    )
    update_todo(calendar, "t1", {"summary": "Water plants", "status": "COMPLETED"})

    vtodo = calendar.todo.stored()
    assert str(vtodo["STATUS"]) == "NEEDS-ACTION"
    assert vtodo["DUE"].dt.day == 13


def test_completing_recurring_todo_with_until_rolls_then_closes() -> None:
    calendar = FakeTodoCalendar(
        _vtodo("DUE:20260706T090000Z\r\nRRULE:FREQ=WEEKLY;UNTIL=20260714T090000Z")
    )
    update_todo(calendar, "t1", COMPLETE)
    vtodo = calendar.todo.stored()
    assert str(vtodo["STATUS"]) == "NEEDS-ACTION"
    assert vtodo["DUE"].dt == datetime(2026, 7, 13, 9, 0, tzinfo=UTC)

    update_todo(
        calendar,
        "t1",
        {
            "summary": "Water plants",
            "status": "COMPLETED",
            "due": datetime(2026, 7, 13, 9, 0, tzinfo=UTC),
        },
    )
    assert str(calendar.todo.stored()["STATUS"]) == "COMPLETED"


def test_completing_recurring_tzid_todo_preserves_wall_clock_across_dst() -> None:
    # Europe/Berlin leaves DST on 2026-10-25.
    calendar = FakeTodoCalendar(
        _vtodo("DUE;TZID=Europe/Berlin:20261024T090000\r\nRRULE:FREQ=WEEKLY")
    )
    update_todo(calendar, "t1", {"summary": "Water plants", "status": "COMPLETED"})

    due = calendar.todo.stored()["DUE"].dt
    assert due.hour == 9
    assert due.utcoffset().total_seconds() == 3600


def test_update_todo_clears_absent_due_and_description() -> None:
    calendar = FakeTodoCalendar(_vtodo("DUE:20260706T090000Z\r\nDESCRIPTION:notes"))
    update_todo(calendar, "t1", {"summary": "Water plants"})

    vtodo = calendar.todo.stored()
    assert "DUE" not in vtodo
    assert "DESCRIPTION" not in vtodo


def test_completing_an_item_that_is_already_done_keeps_one_stamp() -> None:
    calendar = FakeTodoCalendar(
        _vtodo(
            "DUE:20260706T090000Z\r\nSTATUS:COMPLETED\r\n"
            "COMPLETED:20260706T100000Z\r\nPERCENT-COMPLETE:100"
        )
    )

    update_todo(calendar, "t1", {"summary": "Water plants", "status": "COMPLETED"})

    # icalendar's add turns a second write into a list.
    vtodo = calendar.todo.stored()
    assert str(vtodo["STATUS"]) == "COMPLETED"
    assert not isinstance(vtodo["COMPLETED"], list)
    assert vtodo["COMPLETED"].dt == datetime(2026, 7, 6, 10, 0, tzinfo=UTC)


def test_a_recurring_todo_with_both_anchors_rolls_by_the_start() -> None:
    # RFC 5545 anchors the rule on DTSTART.
    calendar = FakeTodoCalendar(
        _vtodo(
            "DTSTART;VALUE=DATE:20260706\r\nDUE;VALUE=DATE:20260707\r\n"
            "RRULE:FREQ=WEEKLY;BYDAY=MO"
        )
    )

    update_todo(calendar, "t1", COMPLETE)

    vtodo = calendar.todo.stored()
    assert vtodo["DTSTART"].dt == date(2026, 7, 13)
    assert vtodo["DUE"].dt == date(2026, 7, 14)


def test_a_due_date_cannot_be_moved_in_front_of_the_start() -> None:
    """RFC 5545 puts DUE after DTSTART, and Home Assistant shows no start for a
    to-do."""
    import pytest

    from custom_components.ha_caldav.errors import Refused

    calendar = FakeTodoCalendar(
        _vtodo("DTSTART:20260710T090000Z\r\nDUE:20260711T090000Z")
    )

    with pytest.raises(Refused, match="end_before_start"):
        update_todo(
            calendar,
            "t1",
            {
                "summary": "Water plants",
                "due": datetime(2026, 7, 5, 9, 0, tzinfo=UTC),
            },
        )

    assert not calendar.todo.saved


DOUBLED = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//test//EN\r\n"
    "BEGIN:VTODO\r\nUID:t1\r\nDTSTAMP:20260101T000000Z\r\nSUMMARY:Water plants\r\n"
    "{}"
    "END:VTODO\r\nEND:VCALENDAR\r\n"
)


def test_a_todo_written_with_two_rules_can_still_be_completed() -> None:
    """RFC 2445 let some properties repeat, and icalendar hands a repeated one back
    as a list."""
    calendar = FakeTodoCalendar(
        DOUBLED.format(
            "DUE:20260706T090000Z\r\nRRULE:FREQ=WEEKLY\r\nRRULE:FREQ=DAILY\r\n"
        )
    )

    update_todo(calendar, "t1", {"summary": "Water plants", "status": "COMPLETED"})

    vtodo = calendar.todo.stored()
    assert str(vtodo["STATUS"]) == "NEEDS-ACTION"
    assert vtodo["DUE"].dt == datetime(2026, 7, 13, 9, 0, tzinfo=UTC)
    assert not isinstance(vtodo["RRULE"], list)


def test_a_todo_written_with_two_starts_can_still_be_dated() -> None:
    calendar = FakeTodoCalendar(
        DOUBLED.format(
            "DTSTART:20260706T090000Z\r\nDTSTART:20260707T090000Z\r\n"
            "DUE:20260710T090000Z\r\n"
        )
    )

    update_todo(
        calendar,
        "t1",
        {"summary": "Water plants", "due": datetime(2026, 7, 11, 9, 0, tzinfo=UTC)},
    )

    vtodo = calendar.todo.stored()
    assert vtodo["DTSTART"].dt == datetime(2026, 7, 6, 9, 0, tzinfo=UTC)
    assert vtodo["DUE"].dt == datetime(2026, 7, 11, 9, 0, tzinfo=UTC)


def test_renaming_a_recurring_todo_that_is_already_done_does_not_roll_it() -> None:
    # Core sends the status along with every edit.
    calendar = FakeTodoCalendar(
        _vtodo("DUE;VALUE=DATE:20260706\r\nRRULE:FREQ=WEEKLY\r\nSTATUS:COMPLETED")
    )

    update_todo(
        calendar,
        "t1",
        {"summary": "Renamed", "status": "COMPLETED", "due": date(2026, 7, 6)},
    )

    stored = calendar.todo.stored()
    assert stored["DUE"].dt == date(2026, 7, 6)
    assert str(stored["STATUS"]) == "COMPLETED"


def test_completing_a_recurring_todo_skips_an_excluded_date() -> None:
    calendar = FakeTodoCalendar(
        _vtodo("DUE:20260706T090000Z\r\nRRULE:FREQ=WEEKLY\r\nEXDATE:20260713T090000Z")
    )

    update_todo(calendar, "t1", COMPLETE)

    assert calendar.todo.stored()["DUE"].dt == datetime(2026, 7, 20, 9, 0, tzinfo=UTC)


def test_completing_a_counted_todo_past_an_excluded_date_uses_up_both() -> None:
    calendar = FakeTodoCalendar(
        _vtodo(
            "DUE:20260706T090000Z\r\nRRULE:FREQ=WEEKLY;COUNT=5\r\n"
            "EXDATE:20260713T090000Z"
        )
    )

    update_todo(calendar, "t1", COMPLETE)

    assert calendar.todo.stored()["RRULE"]["COUNT"] == [3]


def test_completing_a_recurring_todo_takes_an_added_date_first() -> None:
    calendar = FakeTodoCalendar(
        _vtodo("DUE:20260706T090000Z\r\nRRULE:FREQ=WEEKLY\r\nRDATE:20260708T090000Z")
    )

    update_todo(calendar, "t1", COMPLETE)

    assert calendar.todo.stored()["DUE"].dt == datetime(2026, 7, 8, 9, 0, tzinfo=UTC)


def test_a_roll_clears_the_completion_and_marks_the_item_revised() -> None:
    calendar = FakeTodoCalendar(
        _vtodo(
            "DUE:20260706T090000Z\r\nRRULE:FREQ=WEEKLY\r\nSEQUENCE:2\r\n"
            "PERCENT-COMPLETE:50"
        )
    )

    update_todo(calendar, "t1", COMPLETE)

    stored = calendar.todo.stored()
    assert "PERCENT-COMPLETE" not in stored
    assert "COMPLETED" not in stored
    assert stored["DTSTAMP"].dt > datetime(2026, 1, 1, tzinfo=UTC)


def test_a_utc_due_rolls_with_the_wall_clock_of_its_start() -> None:
    zone = ZoneInfo("Europe/Berlin")
    calendar = FakeTodoCalendar(
        _vtodo(
            "DTSTART;TZID=Europe/Berlin:20261019T090000\r\n"
            "DUE:20261019T080000Z\r\nRRULE:FREQ=WEEKLY"
        )
    )

    update_todo(calendar, "t1", {"summary": "Water plants", "status": "COMPLETED"})

    stored = calendar.todo.stored()
    # Berlin leaves summer time on 2026-10-25.
    assert stored["DTSTART"].dt == datetime(2026, 10, 26, 9, 0, tzinfo=zone)
    assert stored["DUE"].dt == datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


def test_an_edit_keeps_a_due_time_in_the_zone_it_was_written_in() -> None:
    new_york = ZoneInfo("America/New_York")
    calendar = FakeTodoCalendar(_vtodo("DUE;TZID=America/New_York:20260706T100000"))

    update_todo(
        calendar,
        "t1",
        {
            "summary": "Renamed",
            "status": "NEEDS-ACTION",
            "due": datetime(2026, 7, 6, 16, 0, tzinfo=ZoneInfo("Europe/Berlin")),
        },
    )

    due = calendar.todo.stored()["DUE"].dt
    assert (due.replace(tzinfo=None), due.tzinfo) == (
        datetime(2026, 7, 6, 10),
        new_york,
    )


def test_an_edit_keeps_a_floating_due_time_floating() -> None:
    calendar = FakeTodoCalendar(_vtodo("DUE:20260706T090000"))

    update_todo(
        calendar,
        "t1",
        {
            "summary": "Renamed",
            "status": "NEEDS-ACTION",
            "due": dt_util.as_local(datetime(2026, 7, 6, 9, 0)),
        },
    )

    assert calendar.todo.stored()["DUE"].dt == datetime(2026, 7, 6, 9, 0)


def test_a_moved_due_time_is_written_in_the_zone_already_stored() -> None:
    new_york = ZoneInfo("America/New_York")
    calendar = FakeTodoCalendar(_vtodo("DUE;TZID=America/New_York:20260706T100000"))

    update_todo(
        calendar,
        "t1",
        {
            "summary": "Renamed",
            "status": "NEEDS-ACTION",
            "due": datetime(2026, 7, 6, 17, 0, tzinfo=ZoneInfo("Europe/Berlin")),
        },
    )

    due = calendar.todo.stored()["DUE"].dt
    assert (due.replace(tzinfo=None), due.tzinfo) == (
        datetime(2026, 7, 6, 11),
        new_york,
    )


def test_a_moved_floating_due_time_stays_floating() -> None:
    calendar = FakeTodoCalendar(_vtodo("DUE:20260706T090000"))

    update_todo(
        calendar,
        "t1",
        {
            "summary": "Renamed",
            "status": "NEEDS-ACTION",
            "due": dt_util.as_local(datetime(2026, 7, 6, 10, 0)),
        },
    )

    assert calendar.todo.stored()["DUE"].dt == datetime(2026, 7, 6, 10, 0)
