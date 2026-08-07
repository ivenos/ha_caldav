"""Tests for resolving a uid on servers that refuse to filter on it.

iCloud answers the UID prop-filter REPORT the library builds with 412. Which
exception that becomes depends on the caldav version, so the fallback is driven
here from every shape it has been seen to take.
"""

from unittest.mock import Mock

from caldav.lib.error import NotFoundError, ReportError
from conftest import dav_calendar
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
    calendar = dav_calendar()
    if refusal is None:
        calendar.event_by_uid.return_value = _resource("evt-1")
        calendar.todo_by_uid.return_value = _resource("evt-1")
        calendar.object_by_uid.return_value = _resource("evt-1")
    else:
        calendar.event_by_uid.side_effect = refusal
        calendar.todo_by_uid.side_effect = refusal
        calendar.object_by_uid.side_effect = refusal
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
    # The server answered no; scanning would cost a lot to learn nothing.
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


def test_a_server_that_will_not_filter_is_scanned_once_for_a_whole_import() -> None:
    from custom_components.ha_caldav.api import import_ics

    calendar = _calendar(ReportError("no uid filter"))
    calendar.search.return_value = []

    import_ics(
        calendar,
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VEVENT\r\nUID:a\r\nDTSTAMP:20260101T000000Z\r\n"
        "DTSTART:20260706T090000Z\r\nSUMMARY:A\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:b\r\nDTSTAMP:20260101T000000Z\r\n"
        "DTSTART:20260707T090000Z\r\nSUMMARY:B\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:c\r\nDTSTAMP:20260101T000000Z\r\n"
        "DTSTART:20260708T090000Z\r\nSUMMARY:C\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n",
    )

    # Asking per uid would download the whole collection once per component.
    # Two reads, one per component type, however many uids the document holds.
    assert calendar.search.call_count == 2


def test_a_todo_write_does_not_ask_the_server_for_the_uid_again() -> None:
    """caldav's no_create makes save() look the uid up before writing.

    That is the very request iCloud refuses, and object_by_uid has already
    resolved the resource. Written against a real caldav.Todo, because the
    lookup happens inside save() where a stubbed one cannot show it.
    """
    import caldav

    put: dict = {}
    asked: list[str] = []

    class Client:
        def put(self, url, body, headers=None):
            put["body"] = body if isinstance(body, str) else body.decode("utf-8")
            response = Mock()
            response.status = 204
            response.headers = {}
            return response

        def __getattr__(self, name):
            return Mock()

    class RefusingCalendar(caldav.Calendar):
        def todo_by_uid(self, uid):
            asked.append(uid)
            raise ReportError("412 Precondition Failed")

    client = Client()
    parent = RefusingCalendar(client=client, url="http://dav.test/cal/")
    todo = caldav.Todo(
        client=client, data=VTODO, url="http://dav.test/cal/evt-1.ics", parent=parent
    )
    todo.id = "evt-1"
    calendar = Mock()
    calendar.todo_by_uid.return_value = todo

    update_todo(calendar, "evt-1", {"summary": "renamed"})

    assert "SUMMARY:renamed" in put["body"]
    assert asked == []


def _doc(*entries: tuple[str, str]) -> str:
    """Build an iCalendar document from (uid, component-name) pairs."""
    body = ""
    for uid, name in entries:
        if name == "VTODO":
            body += (
                f"BEGIN:VTODO\r\nUID:{uid}\r\nDTSTAMP:20260101T000000Z\r\n"
                f"DUE:20260706T090000Z\r\nSUMMARY:{uid}\r\nEND:VTODO\r\n"
            )
        else:
            body += (
                f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:20260101T000000Z\r\n"
                f"DTSTART:20260706T090000Z\r\nSUMMARY:{uid}\r\nEND:VEVENT\r\n"
            )
    return (
        f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n{body}END:VCALENDAR\r\n"
    )


def test_an_imported_todo_does_not_overwrite_a_todo_of_the_same_uid() -> None:
    from custom_components.ha_caldav.api import import_ics
    from custom_components.ha_caldav.errors import Refused

    # event_by_uid is restricted to VEVENT, so asking it about a to-do always
    # comes back empty and the clash goes unseen until the PUT lands on it.
    calendar = Mock()
    calendar.event_by_uid.side_effect = NotFoundError("no such event")
    calendar.todo_by_uid.return_value = _resource("task-1")

    with pytest.raises(Refused, match="uid_clash"):
        import_ics(calendar, _doc(("task-1", "VTODO")))

    calendar.save_todo.assert_not_called()


def test_the_uid_whose_lookup_was_refused_is_still_checked_by_the_scan() -> None:
    from custom_components.ha_caldav.api import import_ics
    from custom_components.ha_caldav.errors import Refused

    # The refusal fires on the first uid; dropping it from the scan would let
    # the import PUT straight over a live object.
    calendar = _calendar(ReportError("no uid filter"), [_resource("a")])

    with pytest.raises(Refused, match="uid_clash"):
        import_ics(calendar, _doc(("a", "VEVENT"), ("b", "VEVENT")))

    calendar.save_event.assert_not_called()


def test_a_large_import_reads_the_collection_once_instead_of_asking_per_uid() -> None:
    from custom_components.ha_caldav.api import import_ics

    calendar = _calendar(None)
    calendar.search.return_value = []

    import_ics(calendar, _doc(*((f"u{n}", "VEVENT") for n in range(12))))

    # Two reads, one per component type, rather than one per uid.
    assert calendar.search.call_count == 2
    assert calendar.event_by_uid.call_count == 0


def test_deleting_several_todos_shares_one_scan_when_the_uid_search_is_refused() -> (
    None
):
    from custom_components.ha_caldav.api import delete_todos

    wanted = [_real_resource(VTODO.replace("evt-1", f"t{n}")) for n in range(3)]
    calendar = _calendar(ReportError("no uid filter"), wanted)

    delete_todos(calendar, ["t0", "t1", "t2"], {})

    # One whole-collection read for the selection, not one per item.
    assert calendar.search.call_count == 1
    for resource in wanted:
        resource.delete.assert_called_once()


def test_deleting_a_todo_that_the_scan_does_not_turn_up_is_reported() -> None:
    from custom_components.ha_caldav.api import delete_todos

    calendar = _calendar(ReportError("no uid filter"), [_real_resource(VTODO)])

    with pytest.raises(NotFoundError):
        delete_todos(calendar, ["evt-1", "gone"], {})


def test_deleting_several_todos_forwards_each_etag() -> None:
    from caldav.elements import dav

    from custom_components.ha_caldav.api import delete_todos
    from custom_components.ha_caldav.errors import Refused

    wanted = _real_resource(VTODO)
    wanted.props = {}
    wanted.load.side_effect = lambda: wanted.props.update({dav.GetEtag.tag: '"fresh"'})
    calendar = _calendar(None)
    calendar.todo_by_uid.return_value = wanted

    with pytest.raises(Refused) as refusal:
        delete_todos(calendar, ["evt-1"], {"evt-1": '"stale"'})

    assert refusal.value.key == "etag_conflict"
    wanted.delete.assert_not_called()
