"""Tests against a real CalDAV server.

Skipped unless CALDAV_URL points at one. These are the only tests that prove
the server accepts what we write: everything else stops at "we produced valid
iCalendar", which is not the same claim.
"""

from datetime import UTC, date, datetime
import os
from urllib.parse import urlparse

import caldav
import pytest
import pytest_socket

from custom_components.ha_caldav.api import (
    create_event,
    create_todo,
    delete_event,
    delete_todo,
    update_event,
    update_todo,
)

pytestmark = pytest.mark.live

URL = os.environ.get("CALDAV_URL")
USERNAME = os.environ.get("CALDAV_USERNAME", "admin")
PASSWORD = os.environ.get("CALDAV_PASSWORD", "")

RANGE_START = datetime(2026, 7, 1, tzinfo=UTC)
RANGE_END = datetime(2026, 9, 1, tzinfo=UTC)
CALENDAR_NAME = "ha_caldav_live"


def enable_sockets() -> None:
    """Let this test reach the server.

    The Home Assistant harness blocks sockets before every single test and only
    ever allows 127.0.0.1, which would also rule out running these against a
    remote server. This has to be re-applied per test: doing it once per module
    appears to work only because requests keeps reusing the pooled connection,
    and fails as soon as a fresh socket is needed.
    """
    pytest_socket.enable_socket()
    host = urlparse(URL).hostname if URL else None
    pytest_socket.socket_allow_hosts(
        [h for h in (host, "127.0.0.1", "::1") if h], allow_unix_socket=True
    )


@pytest.fixture(scope="module", autouse=True)
def module_sockets():
    """Cover the setup of the module-scoped calendar fixture."""
    enable_sockets()
    return


@pytest.fixture(autouse=True)
def test_sockets(module_sockets):
    """Cover each test, which the harness starts with sockets blocked again."""
    enable_sockets()
    return


@pytest.fixture(scope="module")
def calendar(module_sockets):
    """Provide one calendar for the whole run.

    Nextcloud rate-limits calendar creation ("Too many calendars created"), so
    a calendar per test would fail partway through.
    """
    if not URL:
        pytest.skip("CALDAV_URL is not set")
    principal = caldav.DAVClient(URL, username=USERNAME, password=PASSWORD).principal()
    for existing in principal.calendars():
        if existing.name == CALENDAR_NAME:
            existing.delete()
    created = principal.make_calendar(name=CALENDAR_NAME)
    yield created
    # Teardown runs after the last test, so the block is back on by now.
    enable_sockets()
    created.delete()


@pytest.fixture(autouse=True)
def empty(test_sockets, calendar):
    """Start every test from an empty calendar."""
    for event in calendar.events():
        event.delete()
    for todo in calendar.todos(include_completed=True):
        todo.delete()
    return calendar


def todos(calendar) -> dict:
    """Return the to-do items the server holds, keyed by summary."""
    from custom_components.ha_caldav.coordinator import to_todo

    found = {}
    for resource in calendar.search(todo=True, include_completed=True):
        if hasattr(resource.vobject_instance, "vtodo") and (
            item := to_todo(resource.vobject_instance.vtodo)
        ):
            found[item.summary] = item
    return found


def starts(calendar) -> list:
    """Return the start of every occurrence the server expands for us."""
    found = []
    for item in calendar.search(
        start=RANGE_START, end=RANGE_END, event=True, expand=True
    ):
        value = item.vobject_instance.vevent.dtstart.value
        found.append(
            value
            if isinstance(value, date) and not isinstance(value, datetime)
            else value.astimezone(UTC)
        )
    return sorted(found)


def summaries(calendar) -> dict:
    found = {}
    for item in calendar.search(
        start=RANGE_START, end=RANGE_END, event=True, expand=True
    ):
        vevent = item.vobject_instance.vevent
        value = vevent.dtstart.value
        key = (
            value
            if isinstance(value, date) and not isinstance(value, datetime)
            else value.astimezone(UTC)
        )
        found[key] = vevent.summary.value
    return found


def uid_of(calendar) -> str:
    return str(calendar.events()[0].icalendar_component["UID"])


def series(calendar, rrule: str = "FREQ=WEEKLY;COUNT=4") -> str:
    create_event(
        calendar,
        {
            "summary": "Standup",
            "dtstart": datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 6, 10, 0, tzinfo=UTC),
            "rrule": rrule,
        },
    )
    return uid_of(calendar)


def test_create_update_delete_single_event(calendar) -> None:
    create_event(
        calendar,
        {
            "summary": "Dentist",
            "dtstart": datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 6, 10, 0, tzinfo=UTC),
        },
    )
    assert starts(calendar) == [datetime(2026, 7, 6, 9, 0, tzinfo=UTC)]

    uid = uid_of(calendar)
    update_event(
        calendar,
        uid,
        {
            "summary": "Dentist moved",
            "dtstart": datetime(2026, 7, 6, 14, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 6, 15, 0, tzinfo=UTC),
        },
    )
    assert summaries(calendar) == {
        datetime(2026, 7, 6, 14, 0, tzinfo=UTC): "Dentist moved"
    }

    delete_event(calendar, uid)
    assert starts(calendar) == []


def test_delete_one_occurrence(calendar) -> None:
    uid = series(calendar)
    delete_event(calendar, uid, recurrence_id="2026-07-13 09:00:00+00:00")
    assert starts(calendar) == [
        datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
        datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
        datetime(2026, 7, 27, 9, 0, tzinfo=UTC),
    ]


def test_delete_this_and_future(calendar) -> None:
    uid = series(calendar)
    delete_event(
        calendar, uid, recurrence_id="2026-07-20 09:00:00+00:00", this_and_future=True
    )
    assert starts(calendar) == [
        datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
        datetime(2026, 7, 13, 9, 0, tzinfo=UTC),
    ]


def test_delete_this_and_future_from_the_start_removes_the_series(calendar) -> None:
    uid = series(calendar)
    delete_event(
        calendar, uid, recurrence_id="2026-07-06 09:00:00+00:00", this_and_future=True
    )
    assert starts(calendar) == []


def test_update_one_occurrence(calendar) -> None:
    uid = series(calendar, rrule="FREQ=WEEKLY;COUNT=3")
    update_event(
        calendar,
        uid,
        {
            "summary": "Standup moved",
            "dtstart": datetime(2026, 7, 13, 11, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
        },
        recurrence_id="2026-07-13 09:00:00+00:00",
    )
    assert summaries(calendar) == {
        datetime(2026, 7, 6, 9, 0, tzinfo=UTC): "Standup",
        datetime(2026, 7, 13, 11, 0, tzinfo=UTC): "Standup moved",
        datetime(2026, 7, 20, 9, 0, tzinfo=UTC): "Standup",
    }


def test_update_this_and_future_splits_the_series(calendar) -> None:
    uid = series(calendar)
    update_event(
        calendar,
        uid,
        {
            "summary": "New rhythm",
            "dtstart": datetime(2026, 7, 20, 11, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
            "rrule": "FREQ=WEEKLY;COUNT=2",
        },
        recurrence_id="2026-07-20 09:00:00+00:00",
        this_and_future=True,
    )
    assert summaries(calendar) == {
        datetime(2026, 7, 6, 9, 0, tzinfo=UTC): "Standup",
        datetime(2026, 7, 13, 9, 0, tzinfo=UTC): "Standup",
        datetime(2026, 7, 20, 11, 0, tzinfo=UTC): "New rhythm",
        datetime(2026, 7, 27, 11, 0, tzinfo=UTC): "New rhythm",
    }
    # A second UID has to live in its own resource per RFC 4791.
    assert len(calendar.events()) == 2


def test_all_day_series_delete_one_occurrence(calendar) -> None:
    create_event(
        calendar,
        {
            "summary": "Water plants",
            "dtstart": date(2026, 7, 6),
            "dtend": date(2026, 7, 7),
            "rrule": "FREQ=DAILY;COUNT=5",
        },
    )
    delete_event(calendar, uid_of(calendar), recurrence_id="2026-07-08")
    assert starts(calendar) == [
        date(2026, 7, 6),
        date(2026, 7, 7),
        date(2026, 7, 9),
        date(2026, 7, 10),
    ]


def test_all_day_series_delete_this_and_future(calendar) -> None:
    create_event(
        calendar,
        {
            "summary": "Water plants",
            "dtstart": date(2026, 7, 6),
            "dtend": date(2026, 7, 7),
            "rrule": "FREQ=DAILY;COUNT=5",
        },
    )
    delete_event(
        calendar, uid_of(calendar), recurrence_id="2026-07-09", this_and_future=True
    )
    # UNTIL is inclusive, so the boundary matters: the 8th stays, the 9th goes.
    assert starts(calendar) == [date(2026, 7, 6), date(2026, 7, 7), date(2026, 7, 8)]


def test_recurrence_id_from_the_server_round_trips(calendar) -> None:
    """The ids the server reports must be the ids we can delete with."""
    from custom_components.ha_caldav.coordinator import to_event

    uid = series(calendar, rrule="FREQ=WEEKLY;COUNT=3")
    events = [
        to_event(item.vobject_instance.vevent)
        for item in calendar.search(
            start=RANGE_START, end=RANGE_END, event=True, expand=True
        )
    ]
    second = next(e for e in events if e.start.astimezone(UTC).day == 13)
    assert second.recurrence_id is not None

    delete_event(calendar, uid, recurrence_id=second.recurrence_id)
    assert starts(calendar) == [
        datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
        datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
    ]


def test_create_update_delete_todo(calendar) -> None:
    from homeassistant.components.todo import TodoItemStatus

    create_todo(
        calendar,
        {"summary": "Buy milk", "status": "NEEDS-ACTION", "due": date(2026, 7, 10)},
    )
    stored = todos(calendar)
    assert set(stored) == {"Buy milk"}
    assert stored["Buy milk"].status is TodoItemStatus.NEEDS_ACTION
    assert stored["Buy milk"].due == date(2026, 7, 10)

    uid = stored["Buy milk"].uid
    update_todo(
        calendar,
        uid,
        {
            "summary": "Buy oat milk",
            "status": "COMPLETED",
            "due": date(2026, 7, 11),
            "description": "The barista one",
        },
    )
    stored = todos(calendar)
    assert set(stored) == {"Buy oat milk"}
    item = stored["Buy oat milk"]
    assert item.status is TodoItemStatus.COMPLETED
    assert item.due == date(2026, 7, 11)
    # Description survives alongside due: set_due mutates the same component.
    assert item.description == "The barista one"

    delete_todo(calendar, uid)
    assert todos(calendar) == {}


def test_update_todo_clears_due_and_description(calendar) -> None:
    create_todo(
        calendar,
        {"summary": "Task", "due": date(2026, 7, 10), "description": "notes"},
    )
    uid = todos(calendar)["Task"].uid

    update_todo(calendar, uid, {"summary": "Task"})

    item = todos(calendar)["Task"]
    assert item.due is None
    assert item.description is None


def test_todo_with_due_datetime(calendar) -> None:
    create_todo(
        calendar,
        {"summary": "Call back", "due": datetime(2026, 7, 10, 15, 30, tzinfo=UTC)},
    )
    due = todos(calendar)["Call back"].due
    assert isinstance(due, datetime)
    assert due.astimezone(UTC) == datetime(2026, 7, 10, 15, 30, tzinfo=UTC)
