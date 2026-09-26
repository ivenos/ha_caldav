"""Tests against a real CalDAV server, skipped unless CALDAV_URL points at one."""

from datetime import UTC, date, datetime
import os
from urllib.parse import urlparse

import pytest
import pytest_socket

from custom_components.ha_caldav.api import (
    create_event,
    create_todo,
    delete_todos,
    object_by_uid,
    update_todo,
)
from custom_components.ha_caldav.connection import build_client, connection_kwargs
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
    """The Home Assistant harness blocks sockets before every test and allows only
    127.0.0.1; requests reusing a pooled connection hides that."""
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
    """Nextcloud rate-limits calendar creation ("Too many calendars created")."""
    if not URL:
        pytest.skip("CALDAV_URL is not set")
    client = build_client(URL, USERNAME, PASSWORD, connection_kwargs({}))
    principal = client.principal()
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
    for event in calendar.events():
        event.delete()
    for todo in calendar.todos(include_completed=True):
        todo.delete()
    return calendar


def todos(calendar) -> dict:
    from custom_components.ha_caldav.coordinator import to_todo

    found = {}
    for resource in calendar.search(todo=True, include_completed=True):
        if hasattr(resource.vobject_instance, "vtodo") and (
            item := to_todo(resource.vobject_instance.vtodo)
        ):
            found[item.summary] = item
    return found


def _expanded(calendar):
    """Yield every occurrence the way the coordinator reads a window."""
    from custom_components.ha_caldav.coordinator import occurrences

    for item in calendar.search(start=RANGE_START, end=RANGE_END, event=True):
        yield from occurrences(item, RANGE_START, RANGE_END)


def starts(calendar) -> list:
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
    # UNTIL is inclusive: the 8th stays, the 9th goes.
    assert starts(calendar) == [date(2026, 7, 6), date(2026, 7, 7), date(2026, 7, 8)]


def test_recurrence_id_from_the_server_round_trips(calendar) -> None:
    from custom_components.ha_caldav.coordinator import to_event

    uid = series(calendar, rrule="FREQ=WEEKLY;COUNT=3")
    events = [to_event(vevent) for vevent in _expanded(calendar)]
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
    assert item.description == "The barista one"

    delete_todos(calendar, [uid], {})
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
    from caldav.elements import ical

    from custom_components.ha_caldav.color import fetch_collections
    from custom_components.ha_caldav.connection import calendar_key

    client = calendar.client
    try:
        calendar.set_properties([ical.CalendarColor("#00679EFF")])
        # The server hands back the alpha a client wrote.
        assert calendar.get_property(ical.CalendarColor()) == "#00679EFF"

        collections = fetch_collections(client)
    finally:
        # SOGo answers 400 to a calendar color that is not #RRGGBBAA, an empty one
        # included.
        calendar.set_properties([ical.CalendarColor("#FFFFFFFF")])

    assert collections[calendar_key(calendar.url)].color == "#00679e"
    # A depth-1 PROPFIND also reaches subscriptions and trashed calendars, which
    # calendars() leaves out.
    keys = [calendar_key(found.url) for found in client.principal().calendars()]
    assert len(keys) == len(set(keys))


def test_supported_components_come_back_from_the_server(calendar) -> None:
    from custom_components.ha_caldav.capability import fetch_capabilities
    from custom_components.ha_caldav.connection import calendar_key

    capabilities = fetch_capabilities(calendar.client)

    capability = capabilities[calendar_key(calendar.url)]
    assert capability.supports_events
    assert capability.writable


def test_completing_a_todo_stamps_it_on_the_server(calendar) -> None:
    create_todo(calendar, {"summary": "Live done"})
    uid = next(iter(todos(calendar).values())).uid

    update_todo(calendar, uid, {"summary": "Live done", "status": "COMPLETED"})

    component = object_by_uid(calendar, uid, todo=True).icalendar_component
    assert str(component["STATUS"]) == "COMPLETED"
    assert "COMPLETED" in component
    assert int(component["PERCENT-COMPLETE"]) == 100
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
    # Nextcloud parks a deleted object in its trash and refuses its uid while it
    # sits there.
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
    from custom_components.ha_caldav.color import fetch_collections
    from custom_components.ha_caldav.connection import calendar_key

    key = calendar_key(calendar.url)
    before = fetch_collections(calendar.client).get(key)
    try:
        set_calendar_color(calendar, "#00679e")

        assert fetch_collections(calendar.client)[key].color == "#00679e"
    finally:
        enable_sockets()
        if before is not None and before.color is not None:
            set_calendar_color(calendar, before.color)


def test_writing_a_calendar_name_back(calendar) -> None:
    from custom_components.ha_caldav.api import set_calendar_name
    from custom_components.ha_caldav.color import fetch_collections
    from custom_components.ha_caldav.connection import calendar_key

    key = calendar_key(calendar.url)
    assert fetch_collections(calendar.client)[key].name == CALENDAR_NAME
    try:
        set_calendar_name(calendar, "ha_caldav live renamed")

        assert fetch_collections(calendar.client)[key].name == "ha_caldav live renamed"
        listed = {
            calendar_key(found.url): found.name
            for found in calendar.client.principal().calendars()
        }
        assert listed[key] == "ha_caldav live renamed"
    finally:
        enable_sockets()
        set_calendar_name(calendar, CALENDAR_NAME)


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


def test_a_todo_edit_checks_the_etag_the_list_read_off_its_report(calendar) -> None:
    from caldav.elements import dav

    from custom_components.ha_caldav.api import update_todo

    create_todo(calendar, {"summary": "Live etag"})
    uid = next(iter(todos(calendar).values())).uid
    etag = next(
        item.props[dav.GetEtag.tag]
        for item in calendar.search(
            todo=True, include_completed=True, props=[dav.GetEtag()]
        )
        if str(item.vobject_instance.vtodo.uid.value) == uid
    )

    update_todo(calendar, uid, {"summary": "Changed elsewhere"}, expected_etag=etag)

    with pytest.raises(Refused, match="etag_conflict"):
        update_todo(calendar, uid, {"summary": "Mine"}, expected_etag=etag)
    assert "Changed elsewhere" in todos(calendar)


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
    assert len(found) == 1
    assert found[0].icalendar_component.get("SEQUENCE") in (None, 0)


def test_an_unfiltered_search_returns_both_kinds(calendar) -> None:
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


def _raw(uid: str, summary: str = "Raw") -> str:
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//live//EN\r\nBEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nDTSTAMP:20260101T000000Z\r\nDTSTART:20260708T090000Z\r\n"
        f"DTEND:20260708T100000Z\r\nSUMMARY:{summary}\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )


def _put_raw(calendar, name: str, body: str) -> int:
    from caldav.lib.error import AuthorizationError

    try:
        response = calendar.client.put(
            str(calendar.url.join(name)), body, {"Content-Type": "text/calendar"}
        )
    except AuthorizationError:
        return 403
    return response.status


def test_the_server_refuses_a_write_over_a_version_it_moved_past(calendar) -> None:
    from caldav.elements import dav

    from custom_components.ha_caldav.api import put_resource

    create_event(
        calendar,
        {
            "summary": "Meeting",
            "dtstart": datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 6, 10, 0, tzinfo=UTC),
        },
    )
    mine = object_by_uid(calendar, uid_of(calendar))
    mine.load()
    seen = mine.props[dav.GetEtag.tag]
    other = object_by_uid(calendar, uid_of(calendar))
    other.data = other.data.replace("Meeting", "Changed elsewhere")
    put_resource(other)

    mine.data = mine.data.replace("Meeting", "Mine")
    with pytest.raises(Refused, match="etag_conflict"):
        put_resource(mine, seen)
    assert "Changed elsewhere" in summaries(calendar).values()


def test_an_import_leaves_an_object_stored_under_its_resource_name_alone(
    calendar,
) -> None:
    """A uid lookup cannot see an object whose resource is named after another uid."""
    from custom_components.ha_caldav.api import import_ics

    if "/SOGo/" in (URL or ""):
        pytest.skip("SOGo ignores If-None-Match and overwrites")

    assert _put_raw(calendar, "taken.ics", _raw("someone-else")) in (200, 201, 204)

    with pytest.raises(Refused, match="uid_clash"):
        import_ics(calendar, _raw("taken", "Imported"))

    assert "Raw" in summaries(calendar).values()


def test_deleting_from_a_resource_with_an_encoded_slash_never_fakes_success(
    calendar,
) -> None:
    """Xandikos lists such a resource and then answers 404 however it is spelled."""
    from caldav.lib.error import NotFoundError

    if _put_raw(calendar, "a%2Fb-live.ics", _raw("slashed")) not in (200, 201, 204):
        pytest.skip("server does not store a resource name with an encoded slash")

    try:
        delete_event(calendar, "slashed")
    except NotFoundError:
        assert "Raw" in summaries(calendar).values()
    else:
        assert "Raw" not in summaries(calendar).values()
