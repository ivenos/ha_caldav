"""Tests for completing recurring to-do items."""

from datetime import UTC, date, datetime

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
        self._cal = ICalendar.from_ical(ics)
        self.saved = False

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
        return self.icalendar_component


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
    # 2026-07-07 is a Tuesday; the rule only fires on Mondays, so the anchor is
    # not itself an occurrence. Completing must close, not write COUNT=0.
    calendar = FakeTodoCalendar(
        _vtodo("DUE:20260707T090000Z\r\nRRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=1")
    )
    update_todo(calendar, "t1", {"summary": "Water plants", "status": "COMPLETED"})

    vtodo = calendar.todo.stored()
    assert str(vtodo["STATUS"]) == "COMPLETED"
    assert b"COUNT=0" not in vtodo["RRULE"].to_ical()


def test_completing_with_mismatched_until_closes_instead_of_raising() -> None:
    # Floating anchor with a UTC UNTIL is RFC-noncompliant and makes rrulestr
    # raise; the item must still be completable.
    calendar = FakeTodoCalendar(
        _vtodo("DUE:20260706T090000\r\nRRULE:FREQ=WEEKLY;UNTIL=20260720T090000Z")
    )
    update_todo(calendar, "t1", {"summary": "Water plants", "status": "COMPLETED"})

    assert str(calendar.todo.stored()["STATUS"]) == "COMPLETED"


def test_completing_recurring_todo_with_until_rolls_then_closes() -> None:
    calendar = FakeTodoCalendar(
        _vtodo("DUE:20260706T090000Z\r\nRRULE:FREQ=WEEKLY;UNTIL=20260714T090000Z")
    )
    update_todo(calendar, "t1", COMPLETE)
    vtodo = calendar.todo.stored()
    assert str(vtodo["STATUS"]) == "NEEDS-ACTION"
    assert vtodo["DUE"].dt == datetime(2026, 7, 13, 9, 0, tzinfo=UTC)

    # The next occurrence (07-20) is past UNTIL, so the second completion closes.
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
    # Europe/Berlin leaves DST on 2026-10-25; a weekly 09:00 task rolling from
    # 10-24 to 10-31 must stay 09:00 wall clock and flip +02:00 -> +01:00.
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
