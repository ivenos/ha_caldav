"""Tests for resolving a uid on servers that refuse to filter on it.

iCloud answers the UID prop-filter REPORT the library builds with 412. Which
exception that becomes depends on the caldav version, so the fallback is driven
here from every shape it has been seen to take.
"""

from unittest.mock import Mock

from caldav.lib.error import NotFoundError, ReportError
import pytest

from custom_components.ha_caldav.api import delete_todo, object_by_uid, update_todo
from custom_components.ha_caldav.recurrence import delete_event, update_event

# caldav 2.1.0 turns the 412 into this, through its own broken retry; 2.2 and
# later dropped that retry and let the ReportError through.
REFUSALS = [
    TypeError("Calendar.search() got multiple values for argument 'sort_keys'"),
    ReportError("412 Precondition Failed"),
    AssertionError("weird xml"),
]


def _resource(uid: str) -> Mock:
    item = Mock()
    item.icalendar_component = {"UID": uid}
    return item


def _calendar(refusal: Exception | None, found: list[Mock] | None = None) -> Mock:
    calendar = Mock()
    if refusal is None:
        calendar.event_by_uid.return_value = _resource("evt-1")
        calendar.todo_by_uid.return_value = _resource("evt-1")
    else:
        calendar.event_by_uid.side_effect = refusal
        calendar.todo_by_uid.side_effect = refusal
    calendar.search.return_value = found or []
    return calendar


@pytest.mark.parametrize("refusal", REFUSALS)
@pytest.mark.parametrize("todo", [False, True])
def test_refused_uid_search_falls_back_to_a_scan(refusal, todo) -> None:
    wanted = _resource("evt-1")
    calendar = _calendar(refusal, [_resource("other"), wanted])

    assert object_by_uid(calendar, "evt-1", todo=todo) is wanted


@pytest.mark.parametrize("todo", [False, True])
def test_the_scan_covers_the_whole_calendar(todo) -> None:
    # No date window: an event far outside the one on screen still has to be
    # reachable, and the server is the wrong place to narrow it down.
    calendar = _calendar(ReportError("412"), [_resource("evt-1")])

    object_by_uid(calendar, "evt-1", todo=todo)

    kwargs = calendar.search.call_args.kwargs
    assert "start" not in kwargs and "end" not in kwargs
    assert kwargs == (
        {"todo": True, "include_completed": True} if todo else {"event": True}
    )


@pytest.mark.parametrize("todo", [False, True])
def test_a_working_server_is_never_scanned(todo) -> None:
    calendar = _calendar(None)

    assert object_by_uid(calendar, "evt-1", todo=todo) is not None

    calendar.search.assert_not_called()


@pytest.mark.parametrize("todo", [False, True])
def test_a_genuinely_missing_object_is_not_scanned_for(todo) -> None:
    # The server answered and said no. Reading the whole calendar to confirm
    # would cost a lot to learn nothing.
    calendar = _calendar(NotFoundError("gone"))

    with pytest.raises(NotFoundError):
        object_by_uid(calendar, "evt-1", todo=todo)

    calendar.search.assert_not_called()


@pytest.mark.parametrize("todo", [False, True])
def test_a_scan_that_finds_nothing_raises_not_found(todo) -> None:
    calendar = _calendar(ReportError("412"), [_resource("other")])

    with pytest.raises(NotFoundError):
        object_by_uid(calendar, "evt-1", todo=todo)


def test_a_resource_without_a_component_is_skipped() -> None:
    empty = Mock()
    empty.icalendar_component = None
    wanted = _resource("evt-1")
    calendar = _calendar(ReportError("412"), [empty, wanted])

    assert object_by_uid(calendar, "evt-1") is wanted


VEVENT = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:evt-1\r\nDTSTAMP:20260101T000000Z\r\n"
    "DTSTART:20260706T090000Z\r\nDTEND:20260706T100000Z\r\nSUMMARY:x\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)
VTODO = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//test//EN\r\n"
    "BEGIN:VTODO\r\nUID:evt-1\r\nDTSTAMP:20260101T000000Z\r\n"
    "DUE:20260706T090000Z\r\nSUMMARY:x\r\nEND:VTODO\r\nEND:VCALENDAR\r\n"
)


def _real_resource(raw: str) -> Mock:
    """A search result close enough to the real thing to be written back."""
    from icalendar import Calendar as ICalCalendar

    item = Mock()
    ical = ICalCalendar.from_ical(raw)
    item.icalendar_instance = ical
    item.icalendar_component = next(
        component for component in ical.walk() if component.name in ("VEVENT", "VTODO")
    )
    return item


def test_event_delete_survives_a_refused_uid_search() -> None:
    wanted = _real_resource(VEVENT)
    calendar = _calendar(ReportError("412"), [wanted])

    delete_event(calendar, "evt-1")

    wanted.delete.assert_called_once()


def test_event_update_survives_a_refused_uid_search() -> None:
    from datetime import UTC, datetime

    wanted = _real_resource(VEVENT)
    calendar = _calendar(ReportError("412"), [wanted])

    update_event(
        calendar,
        "evt-1",
        {
            "summary": "moved",
            "dtstart": datetime(2026, 7, 7, 9, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 7, 10, 0, tzinfo=UTC),
        },
    )

    wanted.save.assert_called_once()


def test_todo_update_survives_a_refused_uid_search() -> None:
    wanted = _real_resource(VTODO)
    calendar = _calendar(ReportError("412"), [wanted])

    update_todo(calendar, "evt-1", {"summary": "moved"})

    assert str(wanted.icalendar_component["SUMMARY"]) == "moved"
    wanted.save.assert_called_once()


def test_todo_delete_survives_a_refused_uid_search() -> None:
    wanted = _real_resource(VTODO)
    calendar = _calendar(ReportError("412"), [wanted])

    delete_todo(calendar, "evt-1")

    wanted.delete.assert_called_once()
