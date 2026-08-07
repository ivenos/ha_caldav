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
    def icalendar_instance(self):
        return self._cal

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


def test_completing_with_a_mismatched_until_still_rolls_the_item() -> None:
    # A floating anchor with a UTC UNTIL is what Google writes. dateutil
    # refuses the pair outright, so the zone is reconciled before it is asked
    # and the series keeps rolling instead of quietly closing.
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


def test_completing_an_item_that_is_already_done_keeps_one_stamp() -> None:
    calendar = FakeTodoCalendar(
        _vtodo(
            "DUE:20260706T090000Z\r\nSTATUS:COMPLETED\r\n"
            "COMPLETED:20260706T100000Z\r\nPERCENT-COMPLETE:100"
        )
    )

    update_todo(calendar, "t1", {"summary": "Water plants", "status": "COMPLETED"})

    # icalendar's add turns a second write into a list, and the object would go
    # back with two COMPLETED properties.
    vtodo = calendar.todo.stored()
    assert str(vtodo["STATUS"]) == "COMPLETED"
    assert not isinstance(vtodo["COMPLETED"], list)
    assert vtodo["COMPLETED"].dt == datetime(2026, 7, 6, 10, 0, tzinfo=UTC)


def test_a_recurring_todo_with_both_anchors_rolls_by_the_start() -> None:
    # DTSTART is what the rule is anchored on; measuring the step from DUE
    # instead moves a task with a BYDAY rule backwards.
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
    """RFC 5545 puts DUE after DTSTART.

    Home Assistant shows no start for a to-do, so the DTSTART another client
    set is invisible here and a due date dragged earlier would sail past.
    """
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
