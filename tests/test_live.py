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
    delete_todo,
    object_by_uid,
    update_todo,
)
from custom_components.ha_caldav.errors import Refused
from custom_components.ha_caldav.recurrence import delete_event, update_event

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


def _expanded(calendar):
    """Yield every occurrence the way the poll reads them.

    Same call and same reader as coordinator._fetch_events, so these tests
    cover the unsplit path the integration actually uses.
    """
    from custom_components.ha_caldav.coordinator import components_of

    for item in calendar.search(
        start=RANGE_START,
        end=RANGE_END,
        event=True,
        expand=True,
        split_expanded=False,
    ):
        yield from components_of(item, "vevent")


def starts(calendar) -> list:
    """Return the start of every occurrence the server expands for us."""
    found = []
    for vevent in _expanded(calendar):
        value = vevent.dtstart.value
        found.append(
            value
            if isinstance(value, date) and not isinstance(value, datetime)
            else value.astimezone(UTC)
        )
    return sorted(found)


def summaries(calendar) -> dict:
    found = {}
    for vevent in _expanded(calendar):
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


def etag_of(calendar, uid: str) -> str:
    """Return the etag the server currently reports for a resource."""
    from caldav.elements import dav

    for item in calendar.search(
        event=True,
        start=RANGE_START,
        end=RANGE_END,
        expand=False,
        props=[dav.GetEtag()],
    ):
        if str(item.vobject_instance.vevent.uid.value) == uid:
            return item.props[dav.GetEtag.tag]
    raise AssertionError(f"no etag for {uid}")


def test_update_refuses_stale_write(calendar) -> None:
    create_event(
        calendar,
        {
            "summary": "Meeting",
            "dtstart": datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 6, 10, 0, tzinfo=UTC),
        },
    )
    uid = uid_of(calendar)
    seen = etag_of(calendar, uid)

    # Someone edits the same event directly on the server.
    other = calendar.event_by_uid(uid)
    other.data = other.data.replace("Meeting", "Changed on the server")
    other.save()
    assert etag_of(calendar, uid) != seen

    my_edit = {
        "summary": "My edit",
        "dtstart": datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
        "dtend": datetime(2026, 7, 6, 10, 0, tzinfo=UTC),
    }
    with pytest.raises(Refused, match="etag_conflict"):
        update_event(calendar, uid, my_edit, expected_etag=seen)
    assert "Changed on the server" in summaries(calendar).values()

    update_event(calendar, uid, my_edit, expected_etag=etag_of(calendar, uid))
    assert "My edit" in summaries(calendar).values()


def test_completing_recurring_todo_rolls_forward(calendar) -> None:
    from homeassistant.components.todo import TodoItemStatus

    calendar.save_todo(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//test//EN\r\n"
        "BEGIN:VTODO\r\nUID:rt1\r\nDTSTAMP:20260101T000000Z\r\n"
        "DUE:20260706T090000Z\r\nRRULE:FREQ=WEEKLY;COUNT=3\r\n"
        "SUMMARY:Water\r\nEND:VTODO\r\nEND:VCALENDAR\r\n"
    )
    update_todo(calendar, "rt1", {"summary": "Water", "status": "COMPLETED"})

    item = todos(calendar)["Water"]
    assert item.status == TodoItemStatus.NEEDS_ACTION
    assert item.due.astimezone(UTC) == datetime(2026, 7, 13, 9, 0, tzinfo=UTC)


def test_calendar_color_round_trips(calendar) -> None:
    """The color the server holds must come back keyed by the calendar's path."""
    from caldav.elements import ical

    from custom_components.ha_caldav.color import fetch_colors
    from custom_components.ha_caldav.connection import calendar_key

    client = calendar.client
    try:
        calendar.set_properties([ical.CalendarColor("#00679EFF")])
        # Alpha is what a client may well have written, and the server hands it
        # straight back; turning it into #rrggbb is on us.
        assert calendar.get_property(ical.CalendarColor()) == "#00679EFF"

        colors = fetch_colors(client)
    finally:
        # The calendar is shared with every test in this module. Cleared by
        # writing a color rather than an empty value: SOGo validates the
        # property and answers 400 for anything that is not #RRGGBBAA, and no
        # path in the integration writes an empty one — the service field is
        # required and normalized.
        calendar.set_properties([ical.CalendarColor("#FFFFFFFF")])

    assert colors[calendar_key(calendar.url)] == "#00679e"
    # Two calendars reducing to one key would hand one of them the other's
    # color. Spare keys are fine: a depth-1 PROPFIND also reaches subscriptions
    # and trashed calendars, which are not calendars() results.
    keys = [calendar_key(found.url) for found in client.principal().calendars()]
    assert len(keys) == len(set(keys))


def test_supported_components_come_back_from_the_server(calendar) -> None:
    from custom_components.ha_caldav.capability import fetch_capabilities
    from custom_components.ha_caldav.connection import calendar_key

    capabilities = fetch_capabilities(calendar.client)

    capability = capabilities[calendar_key(calendar.url)]
    assert capability.supports_events
    # Our own calendar has to come back writable, whatever the server calls it.
    assert capability.writable


def test_completing_a_todo_stamps_it_on_the_server(calendar) -> None:
    create_todo(calendar, {"summary": "Live done"})
    uid = next(iter(todos(calendar).values())).uid

    update_todo(calendar, uid, {"summary": "Live done", "status": "COMPLETED"})

    component = object_by_uid(calendar, uid, todo=True).icalendar_component
    assert str(component["STATUS"]) == "COMPLETED"
    assert "COMPLETED" in component
    assert int(component["PERCENT-COMPLETE"]) == 100
    # And the read path has to surface it again.
    assert todos(calendar)["Live done"].completed is not None


def test_reopening_a_todo_clears_the_stamp_on_the_server(calendar) -> None:
    create_todo(calendar, {"summary": "Live reopen"})
    uid = next(iter(todos(calendar).values())).uid
    update_todo(calendar, uid, {"summary": "Live reopen", "status": "COMPLETED"})

    update_todo(calendar, uid, {"summary": "Live reopen", "status": "NEEDS-ACTION"})

    component = object_by_uid(calendar, uid, todo=True).icalendar_component
    assert "COMPLETED" not in component
    assert "PERCENT-COMPLETE" not in component


def test_sort_order_survives_a_round_trip(calendar) -> None:
    from custom_components.ha_caldav.api import reorder_todos

    for summary in ("Live a", "Live b", "Live c"):
        create_todo(calendar, {"summary": summary})
    by_summary = todos(calendar)
    order = [by_summary[name].uid for name in ("Live c", "Live a", "Live b")]

    reorder_todos(calendar, order)

    from custom_components.ha_caldav.coordinator import sort_order

    positions = {}
    for resource in calendar.search(todo=True, include_completed=True):
        vtodo = resource.vobject_instance.vtodo
        positions[str(vtodo.summary.value)] = sort_order(vtodo)
    assert positions["Live c"] < positions["Live a"] < positions["Live b"]


def test_an_event_with_alarms_and_attendees_is_accepted(calendar) -> None:
    from custom_components.ha_caldav.event import read_extras

    create_event(
        calendar,
        {
            "summary": "Live extras",
            "dtstart": datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 6, 10, 0, tzinfo=UTC),
            "url": "https://meet.example.com/live",
            "categories": ["work"],
            "priority": 2,
            "alarms": [15],
            "attendees": [{"email": "ann@example.com", "name": "Ann"}],
        },
    )

    found = calendar.search(start=RANGE_START, end=RANGE_END, event=True)
    extras = read_extras(found[0].vobject_instance.vevent)
    assert extras["url"] == "https://meet.example.com/live"
    assert extras["categories"] == ["work"]
    assert extras["priority"] == 2
    assert extras["alarms"][0]["minutes_before"] == 15
    assert extras["attendees"][0]["email"] == "ann@example.com"


def test_export_and_import_round_trip(calendar) -> None:
    from custom_components.ha_caldav.api import export_ics, import_ics

    create_event(
        calendar,
        {
            "summary": "Live export",
            "dtstart": datetime(2026, 7, 7, 9, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 7, 10, 0, tzinfo=UTC),
        },
    )
    document = export_ics(calendar, None)
    # Re-imported under a new uid rather than after deleting the original:
    # Nextcloud parks a deleted object in its trash and refuses the uid again
    # while it sits there, which is its own rule and not the round trip.
    restored = document.replace("UID:", "UID:restored-", 1)

    import_ics(calendar, restored)

    found = calendar.search(start=RANGE_START, end=RANGE_END, event=True)
    summaries = sorted(
        str(item.vobject_instance.vevent.summary.value) for item in found
    )
    assert summaries == ["Live export", "Live export"]
    assert any(
        str(item.vobject_instance.vevent.uid.value).startswith("restored-")
        for item in found
    )


def test_moving_an_event_between_calendars(calendar) -> None:
    from custom_components.ha_caldav.api import move_event

    principal = calendar.client.principal()
    target_name = f"{CALENDAR_NAME}_target"
    for existing in principal.calendars():
        if existing.name == target_name:
            existing.delete()
    target = principal.make_calendar(name=target_name)
    try:
        create_event(
            calendar,
            {
                "summary": "Live move",
                "dtstart": datetime(2026, 7, 8, 9, 0, tzinfo=UTC),
                "dtend": datetime(2026, 7, 8, 10, 0, tzinfo=UTC),
            },
        )
        uid = str(calendar.events()[0].icalendar_component["UID"])

        move_event(calendar, target, uid, keep_original=False)

        assert calendar.events() == []
        moved = target.search(start=RANGE_START, end=RANGE_END, event=True)
        assert str(moved[0].vobject_instance.vevent.summary.value) == "Live move"
    finally:
        enable_sockets()
        target.delete()


def test_writing_a_calendar_color_back(calendar) -> None:
    from custom_components.ha_caldav.api import set_calendar_color
    from custom_components.ha_caldav.color import fetch_colors
    from custom_components.ha_caldav.connection import calendar_key

    before = fetch_colors(calendar.client).get(calendar_key(calendar.url))
    try:
        set_calendar_color(calendar, "#00679e")

        assert fetch_colors(calendar.client)[calendar_key(calendar.url)] == "#00679e"
    finally:
        # The calendar is shared with every test in this module.
        enable_sockets()
        if before is not None:
            set_calendar_color(calendar, before)


def test_responding_to_an_invitation_sets_our_partstat(calendar) -> None:
    from custom_components.ha_caldav.api import respond_to_invitation
    from custom_components.ha_caldav.capability import fetch_address_set

    addresses = fetch_address_set(calendar.client)
    if not addresses:
        pytest.skip("server does not do CalDAV scheduling")
    create_event(
        calendar,
        {
            "summary": "Live invite",
            "dtstart": datetime(2026, 7, 9, 9, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 9, 10, 0, tzinfo=UTC),
            "attendees": [addresses[0]],
        },
    )
    uid = str(calendar.events()[0].icalendar_component["UID"])

    respond_to_invitation(calendar, uid, "ACCEPTED", addresses)

    component = calendar.event_by_uid(uid).icalendar_component
    attendees = component.get("ATTENDEE")
    attendee = attendees[0] if isinstance(attendees, list) else attendees
    assert attendee.params["PARTSTAT"] == "ACCEPTED"


def test_import_refuses_to_overwrite_what_is_already_there(calendar) -> None:
    from custom_components.ha_caldav.api import export_ics, import_ics

    create_event(
        calendar,
        {
            "summary": "Live clash",
            "dtstart": datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 10, 10, 0, tzinfo=UTC),
        },
    )
    document = export_ics(calendar, None)

    with pytest.raises(Refused, match="uid_clash"):
        import_ics(calendar, document)

    found = calendar.search(start=RANGE_START, end=RANGE_END, event=True)
    assert len(found) == 1


def test_a_stale_todo_edit_is_refused_by_the_server_state(calendar) -> None:
    from caldav.elements import dav

    from custom_components.ha_caldav.api import update_todo

    create_todo(calendar, {"summary": "Live etag"})
    uid = next(iter(todos(calendar).values())).uid
    resource = object_by_uid(calendar, uid, todo=True)
    resource.load()
    etag = resource.props.get(dav.GetEtag.tag)
    assert etag is not None

    # Somebody else edits it in between.
    update_todo(calendar, uid, {"summary": "Changed elsewhere"})

    with pytest.raises(Refused, match="etag_conflict"):
        update_todo(calendar, uid, {"summary": "Mine"}, expected_etag=etag)
    assert todos(calendar)["Changed elsewhere"] is not None


def test_creating_an_event_with_extras_is_a_single_write(calendar) -> None:
    create_event(
        calendar,
        {
            "summary": "Live single",
            "dtstart": datetime(2026, 7, 11, 9, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 11, 10, 0, tzinfo=UTC),
            "alarms": [10],
        },
    )

    found = calendar.search(start=RANGE_START, end=RANGE_END, event=True)
    # A two-pass write would leave a second object behind on a retry.
    assert len(found) == 1
    assert found[0].icalendar_component.get("SEQUENCE") in (None, 0)


def test_an_unfiltered_search_returns_both_kinds(calendar) -> None:
    """The uid-clash check reads the collection once for events and to-dos.

    A server that answered only one kind without a component filter would make
    that check quietly find nothing.
    """
    from custom_components.ha_caldav.api import _scan

    create_event(
        calendar,
        {
            "summary": "Live both event",
            "dtstart": datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 6, 10, 0, tzinfo=UTC),
        },
    )
    create_todo(calendar, {"summary": "Live both todo"})

    summaries = {
        str(item.icalendar_component.get("SUMMARY"))
        for item, _uid in _scan(calendar, None)
    }

    assert {"Live both event", "Live both todo"} <= summaries


def test_moving_one_item_leaves_the_others_untouched(calendar) -> None:
    """Renumbering the whole list is a PUT per item on every drag."""
    from custom_components.ha_caldav.api import reorder_todos

    for summary in ("Live one", "Live two", "Live three"):
        create_todo(calendar, {"summary": summary})
    by_summary = todos(calendar)
    order = [by_summary[name].uid for name in ("Live one", "Live two", "Live three")]
    reorder_todos(calendar, order)

    moved = [order[2], order[0], order[1]]
    reorder_todos(calendar, moved)

    from custom_components.ha_caldav.coordinator import sort_order

    positions = {}
    for resource in calendar.search(todo=True, include_completed=True):
        vtodo = resource.vobject_instance.vtodo
        positions[str(vtodo.summary.value)] = sort_order(vtodo)
    assert positions["Live three"] < positions["Live one"] < positions["Live two"]
