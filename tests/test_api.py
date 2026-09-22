from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import Mock, patch
from uuid import UUID
from zoneinfo import ZoneInfo

from caldav.calendarobjectresource import Event as CaldavEvent
from caldav.davclient import requests
from caldav.elements import ical
from caldav.lib.error import AuthorizationError, NotFoundError, PutError, ReportError
from conftest import dav_calendar
from icalendar import Calendar as ICalCalendar
import pytest
from test_todo_recurrence import FakeTodoCalendar

from custom_components.ha_caldav.api import (
    _SORT_GAP,
    create_calendar,
    create_event,
    create_todo,
    delete_calendar,
    delete_todos,
    export_ics,
    import_ics,
    move_event,
    reorder_todos,
    respond_to_invitation,
    set_calendar_color,
    update_todo,
)
from custom_components.ha_caldav.const import EVENT_ATTRIBUTES, SORT_ORDER_PROPERTY
from custom_components.ha_caldav.errors import Refused
from custom_components.ha_caldav.event import apply_extras
from custom_components.ha_caldav.recurrence import delete_event

EVENT = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:uid-1\r\nDTSTAMP:20260101T000000Z\r\n"
    "DTSTART;TZID=Europe/Berlin:20260706T090000\r\n"
    "DTEND;TZID=Europe/Berlin:20260706T100000\r\nSUMMARY:Standup\r\n"
    "RELATED-TO:parent-uid\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)


def _missing_calendar() -> Mock:
    calendar = dav_calendar()
    calendar.event_by_uid.side_effect = NotFoundError("nope")
    calendar.todo_by_uid.side_effect = NotFoundError("nope")
    calendar.search.return_value = []
    return calendar


def _written(calendar: Mock) -> list[str]:
    return calendar.client.bodies


def _component(body: str, name: str = "VEVENT"):
    return next(iter(ICalCalendar.from_ical(body).walk(name)))


def test_create_event_sends_one_complete_document() -> None:
    calendar = Mock()

    create_event(
        calendar,
        {
            "summary": "Standup",
            "dtstart": datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 6, 10, 0, tzinfo=UTC),
            "alarms": [15],
            "url": "https://meet.example.com/x",
        },
    )

    assert calendar.save_event.call_count == 1
    assert calendar.add_event.call_count == 0
    body = calendar.save_event.call_args.args[0].to_ical().decode("utf-8")
    assert "TRIGGER:-PT15M" in body
    assert "https://meet.example.com/x" in body


def test_create_todo_marked_done_carries_the_completion_properties() -> None:
    calendar = Mock()

    create_todo(calendar, {"summary": "Already done", "status": "COMPLETED"})

    body = calendar.save_todo.call_args.args[0].to_ical().decode("utf-8")
    component = _component(body, "VTODO")
    assert str(component["STATUS"]) == "COMPLETED"
    assert int(component["PERCENT-COMPLETE"]) == 100
    assert "COMPLETED" in component


def test_import_refuses_a_document_that_would_overwrite() -> None:
    calendar = dav_calendar()

    with pytest.raises(Refused, match="uid_clash"):
        import_ics(calendar, EVENT)

    assert calendar.client.puts == []


def test_import_keeps_relations_and_carries_one_timezone() -> None:
    calendar = _missing_calendar()

    assert import_ics(calendar, EVENT) == ["uid-1"]

    body = _written(calendar)[0]
    # caldav's Calendar.save_object follows RELATED-TO and rewrites what it names.
    assert "RELATED-TO:parent-uid" in body
    assert body.count("BEGIN:VTIMEZONE") == 1
    assert "TZID=Europe/Berlin" in body


def test_import_generates_a_timezone_the_document_left_out() -> None:
    calendar = _missing_calendar()
    without_zone = EVENT.replace("RELATED-TO:parent-uid\r\n", "")

    import_ics(calendar, without_zone)

    # RFC 5545 requires the referenced TZID to be defined in the same object.
    assert "BEGIN:VTIMEZONE" in _written(calendar)[0]


def test_import_groups_a_series_into_one_resource() -> None:
    calendar = _missing_calendar()
    series = EVENT.replace(
        "END:VEVENT\r\nEND:VCALENDAR",
        "END:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:uid-1\r\nDTSTAMP:20260101T000000Z\r\n"
        "RECURRENCE-ID;TZID=Europe/Berlin:20260713T090000\r\n"
        "DTSTART;TZID=Europe/Berlin:20260713T110000\r\n"
        "DTEND;TZID=Europe/Berlin:20260713T120000\r\nSUMMARY:Moved\r\n"
        "END:VEVENT\r\nEND:VCALENDAR",
    )

    import_ics(calendar, series)

    # RFC 4791 allows one uid per calendar object resource.
    assert len(_written(calendar)) == 1
    assert _written(calendar)[0].count("BEGIN:VEVENT") == 2


def test_import_stores_a_todo_as_a_todo() -> None:
    calendar = _missing_calendar()
    document = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
        "BEGIN:VTODO\r\nUID:t-1\r\nDTSTAMP:20260101T000000Z\r\nSUMMARY:Task\r\n"
        "END:VTODO\r\nEND:VCALENDAR\r\n"
    )

    import_ics(calendar, document)

    body = _written(calendar)[0]
    assert "BEGIN:VTODO" in body
    assert "BEGIN:VEVENT" not in body


ORPHAN_OVERRIDE = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
    "BEGIN:VEVENT\r\nUID:uid-1\r\nDTSTAMP:20260101T000000Z\r\n"
    "RECURRENCE-ID:20260713T090000Z\r\nDTSTART:20260713T110000Z\r\n"
    "DTEND:20260713T120000Z\r\nSUMMARY:Moved\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)


def test_import_stores_an_object_whose_first_component_is_an_exception() -> None:
    """caldav reads its recurrence handling off the first non-timezone component,
    and on a RECURRENCE-ID there looks up a uid the target does not hold yet."""
    calendar = _missing_calendar()

    assert import_ics(calendar, ORPHAN_OVERRIDE) == ["uid-1"]
    assert "RECURRENCE-ID" in _written(calendar)[0]


def test_move_carries_an_object_whose_first_component_is_an_exception() -> None:
    event = Mock(data=ORPHAN_OVERRIDE)
    source, target = Mock(), dav_calendar()
    source.event_by_uid.return_value = event
    target.event_by_uid.side_effect = NotFoundError("uid-1 not found on server")
    target.todo_by_uid.side_effect = NotFoundError("uid-1 not found on server")

    move_event(source, target, "uid-1", keep_original=False)

    assert "RECURRENCE-ID" in _written(target)[0]
    event.delete.assert_called_once()


@pytest.mark.parametrize(
    ("document", "key"),
    [
        (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nEND:VCALENDAR\r\n",
            "document_empty",
        ),
        (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nBEGIN:VEVENT\r\n"
            "DTSTAMP:20260101T000000Z\r\nSUMMARY:x\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
            "document_no_uid",
        ),
    ],
)
def test_import_refuses_a_document_it_cannot_address(document, key) -> None:
    with pytest.raises(Refused, match=key):
        import_ics(_missing_calendar(), document)


def test_export_of_one_object_returns_it_verbatim() -> None:
    """caldav normalizes the line endings it read, which RFC 5545 3.1 has as CRLF."""
    calendar = dav_calendar()
    calendar.event_by_uid.return_value = CaldavEvent(calendar.client, data=EVENT)

    exported = export_ics(calendar, "uid-1")

    assert exported == EVENT
    assert exported.count("\n") == exported.count("\r\n")


def test_export_of_one_object_finds_a_todo_too() -> None:
    body = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VTODO\r\nUID:t-1\r\nDTSTAMP:20260101T000000Z\r\nSUMMARY:Task\r\n"
        "END:VTODO\r\nEND:VCALENDAR\r\n"
    )
    calendar = Mock()
    calendar.event_by_uid.side_effect = NotFoundError("not an event")
    calendar.todo_by_uid.return_value = Mock(data=body)

    assert export_ics(calendar, "t-1") == body


def _export_item(uid: str) -> Mock:
    return Mock(
        url=f"https://dav.test/cal/{uid}.ics",
        icalendar_instance=ICalCalendar.from_ical(EVENT.replace("uid-1", uid)),
    )


def test_export_of_the_calendar_carries_each_timezone_once() -> None:
    items = [_export_item("a"), _export_item("b")]
    calendar = Mock()
    calendar.search.side_effect = lambda **kwargs: items if kwargs.get("event") else []

    document = export_ics(calendar, None)

    assert document.count("BEGIN:VEVENT") == 2
    # RFC 5545 allows one definition per TZID in a VCALENDAR.
    assert document.count("BEGIN:VTIMEZONE") == 1


def test_export_writes_a_resource_both_searches_match_only_once() -> None:
    both = Mock(
        url="https://dav.test/cal/mixed.ics",
        icalendar_instance=ICalCalendar.from_ical(
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
            "BEGIN:VEVENT\r\nUID:u\r\nDTSTAMP:20260101T000000Z\r\n"
            "DTSTART:20260706T090000Z\r\nSUMMARY:meeting\r\nEND:VEVENT\r\n"
            "BEGIN:VTODO\r\nUID:u\r\nDTSTAMP:20260101T000000Z\r\n"
            "SUMMARY:slides\r\nEND:VTODO\r\nEND:VCALENDAR\r\n"
        ),
    )
    calendar = Mock()
    calendar.search.return_value = [both]

    document = export_ics(calendar, None)

    assert document.count("BEGIN:VEVENT") == 1
    assert document.count("BEGIN:VTODO") == 1


def _move_pair() -> tuple[Mock, Mock, Mock]:
    event = Mock(data=EVENT)
    source, target = Mock(), dav_calendar()
    source.event_by_uid.return_value = event
    target.event_by_uid.side_effect = NotFoundError("uid-1 not found on server")
    target.todo_by_uid.side_effect = NotFoundError("uid-1 not found on server")
    return event, source, target


def test_move_writes_to_the_target_and_removes_the_source() -> None:
    event, source, target = _move_pair()

    move_event(source, target, "uid-1", keep_original=False)

    assert "RELATED-TO:parent-uid" in _written(target)[0]
    event.delete.assert_called_once()


def test_move_can_keep_the_original() -> None:
    event, source, target = _move_pair()

    move_event(source, target, "uid-1", keep_original=True)

    assert len(_written(target)) == 1
    event.delete.assert_not_called()


def test_moving_onto_the_calendar_the_event_is_already_on_is_refused() -> None:
    """caldav names the resource after the uid."""
    event = Mock(data=EVENT)
    calendar = dav_calendar()
    calendar.event_by_uid.return_value = event

    with pytest.raises(Refused, match="uid_clash"):
        move_event(calendar, calendar, "uid-1", keep_original=False)

    assert calendar.client.puts == []
    event.delete.assert_not_called()


def test_moving_onto_a_calendar_that_holds_the_uid_is_refused() -> None:
    event = Mock(data=EVENT)
    source, target = Mock(), dav_calendar()
    source.event_by_uid.return_value = event
    target.event_by_uid.return_value = Mock()

    with pytest.raises(Refused, match="uid_clash"):
        move_event(source, target, "uid-1", keep_original=False)

    assert _written(target) == []
    event.delete.assert_not_called()


class _Resource:
    """A resource whose text follows the document written onto it, as caldav's does."""

    def __init__(self, ics: str) -> None:
        self.data = ics
        self._instance = ICalCalendar.from_ical(ics)
        self.save = Mock(side_effect=self._bump)
        self.delete = Mock()

    def _bump(self, **kwargs: Any) -> None:
        """caldav 2.1.0 bumps the SEQUENCE of the first non-timezone component on the
        way out, whatever increase_seqno says."""
        if (component := self.icalendar_component) is not None and (
            "SEQUENCE" in component
        ):
            seqno = component.pop("SEQUENCE")
            component.add("SEQUENCE", int(seqno) + 1)
        self.data = self._instance.to_ical().decode("utf-8")

    @property
    def icalendar_instance(self) -> ICalCalendar:
        return self._instance

    @icalendar_instance.setter
    def icalendar_instance(self, value: ICalCalendar) -> None:
        self._instance = value
        self.data = value.to_ical().decode("utf-8")

    @property
    def icalendar_component(self):
        return next(
            (sub for sub in self._instance.subcomponents if sub.name != "VTIMEZONE"),
            None,
        )


def _invitation(attendee: str) -> _Resource:
    body = EVENT.replace("SUMMARY:Standup", f"SUMMARY:Standup\r\nATTENDEE:{attendee}")
    return _Resource(body)


@pytest.mark.parametrize(
    "attendee",
    ["mailto:iven@example.com", "MailTo:iven@example.com", "iven@example.com"],
)
def test_responding_matches_the_address_however_it_is_spelled(attendee) -> None:
    calendar = Mock()
    event = _invitation(attendee)
    calendar.event_by_uid.return_value = event

    respond_to_invitation(calendar, "uid-1", "ACCEPTED", ["mailto:iven@example.com"])

    assert "PARTSTAT=ACCEPTED" in event.data
    # With only_this_recurrence, caldav recurses on an orphan-override object.
    assert event.save.call_args.kwargs["only_this_recurrence"] is False


def test_responding_to_an_event_we_are_not_on_is_refused() -> None:
    calendar = Mock()
    calendar.event_by_uid.return_value = _invitation("mailto:someone@example.com")

    with pytest.raises(Refused, match="not_an_attendee"):
        respond_to_invitation(calendar, "uid-1", "ACCEPTED", ["iven@example.com"])


def test_setting_a_color_proppatches_the_collection() -> None:
    calendar = Mock()

    set_calendar_color(calendar, "#00679e")

    props = calendar.set_properties.call_args.args[0]
    assert isinstance(props[0], ical.CalendarColor)
    assert props[0].value == "#00679e"


def test_creating_a_calendar_passes_the_component_set() -> None:
    client = Mock()

    create_calendar(client, "Holidays", ["VEVENT"])

    make = client.principal.return_value.make_calendar
    assert make.call_args.kwargs["name"] == "Holidays"
    assert make.call_args.kwargs["supported_calendar_component_set"] == ["VEVENT"]


def test_creating_a_calendar_without_a_component_set_lets_the_server_decide() -> None:
    client = Mock()

    create_calendar(client, "Holidays", [])

    assert (
        client.principal.return_value.make_calendar.call_args.kwargs[
            "supported_calendar_component_set"
        ]
        is None
    )


def test_deleting_a_calendar_removes_the_collection() -> None:
    calendar = Mock()

    delete_calendar(calendar)

    calendar.delete.assert_called_once()


def _todo(body: str) -> Mock:
    instance = ICalCalendar.from_ical(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        f"BEGIN:VTODO\r\nUID:uid-1\r\nDTSTAMP:20260101T000000Z\r\n{body}\r\n"
        "END:VTODO\r\nEND:VCALENDAR\r\n"
    )
    todo = Mock()
    # A caldav resource carries both, the component a subcomponent of the instance.
    todo.icalendar_instance = instance
    todo.icalendar_component = next(iter(instance.walk("VTODO")))

    def save(**_kwargs: object) -> None:
        # caldav 2.1.0 bumps SEQUENCE on the way out whatever increase_seqno says,
        # and only when the property is already there.
        component = todo.icalendar_component
        if "SEQUENCE" in component:
            seqno = component.pop("SEQUENCE")
            component.add("SEQUENCE", int(seqno) + 1)

    todo.save.side_effect = save
    return todo


def test_a_stale_todo_edit_is_refused() -> None:
    calendar = Mock()
    todo = _todo("SUMMARY:Buy milk")
    todo.props = {"{DAV:}getetag": '"newer"'}
    calendar.todo_by_uid.return_value = todo

    with pytest.raises(Refused, match="etag_conflict"):
        update_todo(calendar, "uid-1", {"summary": "Buy milk"}, expected_etag='"older"')

    todo.save.assert_not_called()


def test_a_todo_edit_records_the_change() -> None:
    calendar = Mock()
    todo = _todo("SUMMARY:Buy milk\r\nSEQUENCE:2")
    calendar.todo_by_uid.return_value = todo

    update_todo(calendar, "uid-1", {"summary": "Buy oat milk"})

    component = todo.icalendar_component
    assert int(component["SEQUENCE"]) == 3
    assert "LAST-MODIFIED" in component


def _ordered_todo(uid: str, body: str) -> Mock:
    todo = _todo(body)
    todo.icalendar_component["UID"] = uid
    return todo


def test_reorder_compares_positions_numerically() -> None:
    calendar = Mock()
    todos = {
        "a": _ordered_todo("a", "SUMMARY:a\r\nX-APPLE-SORT-ORDER:00"),
        "b": _ordered_todo("b", "SUMMARY:b\r\nX-APPLE-SORT-ORDER:9"),
    }
    calendar.search.return_value = list(todos.values())

    reorder_todos(calendar, ["a", "b"])

    todos["a"].save.assert_not_called()
    todos["b"].save.assert_not_called()


def test_reorder_writes_only_the_item_that_moved() -> None:
    calendar = Mock()
    todos = {
        letter: _ordered_todo(letter, f"SUMMARY:{letter}\r\nX-APPLE-SORT-ORDER:{n}")
        for n, letter in enumerate("abcde")
    }
    calendar.search.return_value = list(todos.values())

    reorder_todos(calendar, ["e", "a", "b", "c", "d"])

    for letter in "abcd":
        todos[letter].save.assert_not_called()
    todos["e"].save.assert_called_once()
    assert _order(todos) == ["e", "a", "b", "c", "d"]


def test_reorder_numbers_a_list_that_was_never_ordered() -> None:
    calendar = Mock()
    todos = {letter: _ordered_todo(letter, f"SUMMARY:{letter}") for letter in "abc"}
    calendar.search.return_value = list(todos.values())

    reorder_todos(calendar, ["c", "a", "b"])

    assert _order(todos) == ["c", "a", "b"]
    positions = [
        int(str(todo.icalendar_component["X-APPLE-SORT-ORDER"]))
        for todo in todos.values()
    ]
    assert min(abs(a - b) for a in positions for b in positions if a != b) >= _SORT_GAP


def test_reorder_renumbers_when_the_neighbors_leave_no_room() -> None:
    calendar = Mock()
    todos = {
        letter: _ordered_todo(letter, f"SUMMARY:{letter}\r\nX-APPLE-SORT-ORDER:{n}")
        for n, letter in enumerate("abc")
    }
    calendar.search.return_value = list(todos.values())

    reorder_todos(calendar, ["a", "c", "b"])

    assert _order(todos) == ["a", "c", "b"]


@pytest.mark.parametrize("target", [2, 5, 9])
def test_reorder_ends_up_in_the_requested_order(target: int) -> None:
    letters = list("abcdefghij")
    calendar = Mock()
    todos = {
        letter: _ordered_todo(letter, f"SUMMARY:{letter}\r\nX-APPLE-SORT-ORDER:{n}")
        for n, letter in enumerate(letters)
    }
    calendar.search.return_value = list(todos.values())
    wanted = letters[:]
    wanted.insert(target, wanted.pop(0))

    reorder_todos(calendar, wanted)

    assert _order(todos) == wanted


def _order(todos: dict[str, Mock]) -> list[str]:
    return sorted(
        todos,
        key=lambda uid: float(
            str(todos[uid].icalendar_component["X-APPLE-SORT-ORDER"])
        ),
    )


@pytest.mark.parametrize("written", ["nonsense", "", "3.5.1"])
def test_reorder_survives_a_sort_order_the_server_wrote_by_hand(written: str) -> None:
    calendar = Mock()
    todo = _ordered_todo("a", f"SUMMARY:a\r\nX-APPLE-SORT-ORDER:{written}")
    calendar.search.return_value = [todo]

    reorder_todos(calendar, ["a"])

    assert str(todo.icalendar_component["X-APPLE-SORT-ORDER"]) == "0"


def test_reorder_reads_the_collection_once_for_the_whole_ordering() -> None:
    # Home Assistant sends the entire ordering for a single drag.
    calendar = Mock()
    calendar.search.return_value = [
        _ordered_todo(str(n), f"SUMMARY:{n}\r\nX-APPLE-SORT-ORDER:{99 - n}")
        for n in range(20)
    ]

    reorder_todos(calendar, [str(n) for n in range(20)])

    assert calendar.search.call_count == 1
    assert calendar.todo_by_uid.call_count == 0


def test_reorder_skips_an_item_that_is_no_longer_there() -> None:
    calendar = Mock()
    todos = {
        "a": _ordered_todo("a", "SUMMARY:a\r\nX-APPLE-SORT-ORDER:5"),
        "b": _ordered_todo("b", "SUMMARY:b\r\nX-APPLE-SORT-ORDER:6"),
    }
    calendar.search.return_value = list(todos.values())

    reorder_todos(calendar, ["gone", "b", "a"])

    assert _order(todos) == ["b", "a"]


def test_a_missing_item_is_reported_as_user_error_not_a_crash() -> None:
    from homeassistant.exceptions import ServiceValidationError

    from custom_components.ha_caldav.errors import as_reported

    reported = as_reported(NotFoundError("gone"), "update")

    assert isinstance(reported, ServiceValidationError)
    assert reported.translation_key == "not_found"


def test_a_library_valueerror_keeps_its_own_wording() -> None:
    from homeassistant.exceptions import ServiceValidationError

    from custom_components.ha_caldav.errors import as_reported

    reported = as_reported(ValueError("Already on this calendar: a"), "import_ics")

    assert isinstance(reported, ServiceValidationError)
    assert reported.translation_placeholders["reason"].startswith("Already on")


def test_a_server_error_names_only_the_action() -> None:
    from caldav.lib.error import DAVError

    from custom_components.ha_caldav.errors import as_reported

    reported = as_reported(
        DAVError("500 at 'https://cloud.example.com/dav/iven/'"), "create"
    )

    assert reported.translation_key == "server_error"
    assert "cloud.example.com" not in str(reported.translation_placeholders)


def test_a_todo_update_writes_the_due_date_and_the_description() -> None:
    instance = ICalCalendar.from_ical(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VTODO\r\nUID:uid-1\r\nDTSTAMP:20260101T000000Z\r\nSUMMARY:Task\r\n"
        "END:VTODO\r\nEND:VCALENDAR\r\n"
    )
    todo = Mock()
    todo.icalendar_instance = instance
    todo.icalendar_component = next(iter(instance.walk("VTODO")))
    calendar = Mock()
    calendar.todo_by_uid.return_value = todo

    update_todo(
        calendar,
        "uid-1",
        {"summary": "Task", "due": date(2026, 7, 10), "description": "Two liters"},
    )

    vtodo = next(iter(todo.icalendar_instance.walk("VTODO")))
    assert vtodo["DUE"].dt == date(2026, 7, 10)
    assert str(vtodo["DESCRIPTION"]) == "Two liters"


def test_the_invitation_reply_clears_rsvp() -> None:
    calendar = Mock()
    event = _invitation("mailto:iven@example.com")
    calendar.event_by_uid.return_value = event

    respond_to_invitation(calendar, "uid-1", "ACCEPTED", ["mailto:iven@example.com"])

    # RFC 6638: a server relaying the reply looks at RSVP, and an answered
    # attendee that still asks for one gets asked again.
    assert "RSVP=FALSE" in event.data


def test_a_refusal_of_ours_reaches_the_user_by_its_own_key() -> None:
    from custom_components.ha_caldav.errors import Refused, as_reported

    reported = as_reported(Refused("etag_conflict"), "update")

    assert reported.translation_key == "etag_conflict"


def test_a_library_error_that_is_also_a_valueerror_keeps_the_url_out() -> None:
    # caldav binds requests to niquests where that is importable, so only its own
    # alias names the exceptions it raises.
    from caldav.davclient import requests

    from custom_components.ha_caldav.errors import as_reported

    # Several of these exceptions are RequestException and ValueError at once,
    # and their message names the collection, hence the account.
    reported = as_reported(
        requests.exceptions.InvalidURL(
            "https://cloud.example.com/dav/calendars/iven/therapy/ is bad"
        ),
        "update",
    )

    assert reported.translation_key == "server_error"
    assert "iven" not in str(reported.translation_placeholders)


INVITE_WITH_SEQUENCE = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\nMETHOD:REQUEST\r\n"
    "BEGIN:VEVENT\r\nUID:inv-1\r\nDTSTAMP:20260101T000000Z\r\n"
    "DTSTART:20260706T090000Z\r\nDTEND:20260706T100000Z\r\nSEQUENCE:3\r\n"
    "SUMMARY:Review\r\nORGANIZER:mailto:boss@example.com\r\n"
    "ATTENDEE;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:iven@example.com\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)


def test_a_reply_keeps_the_sequence_the_organizer_issued() -> None:
    """RFC 5546: only the organizer moves SEQUENCE. caldav 2.1.0 accepts
    increase_seqno and bumps regardless."""
    import caldav

    put: dict = {}

    class Client:
        def put(self, url, body, headers=None):
            put["body"] = body if isinstance(body, str) else body.decode("utf-8")
            response = Mock()
            response.status = 204
            response.headers = {}
            return response

        def __getattr__(self, name):
            return Mock()

    client = Client()
    parent = caldav.Calendar(client=client, url="http://dav.test/cal/")
    event = caldav.Event(
        client=client,
        data=INVITE_WITH_SEQUENCE,
        url="http://dav.test/cal/inv-1.ics",
        parent=parent,
    )
    event.id = "inv-1"
    calendar = Mock()
    calendar.event_by_uid.return_value = event

    respond_to_invitation(calendar, "inv-1", "ACCEPTED", ["iven@example.com"])

    assert "SEQUENCE:3" in put["body"]
    assert "PARTSTAT=ACCEPTED" in put["body"]


def test_a_created_event_defines_the_timezone_it_references() -> None:
    """RFC 5545 requires the TZID to be defined in the same object."""
    calendar = Mock()
    zone = ZoneInfo("Europe/Berlin")

    create_event(
        calendar,
        {
            "summary": "Standup",
            "dtstart": datetime(2026, 7, 6, 9, 0, tzinfo=zone),
            "dtend": datetime(2026, 7, 6, 10, 0, tzinfo=zone),
        },
    )

    document = calendar.save_event.call_args.args[0]
    assert "TZID=Europe/Berlin" in document.to_ical().decode("utf-8")
    assert [str(item["TZID"]) for item in document.walk("VTIMEZONE")] == [
        "Europe/Berlin"
    ]


def test_a_created_todo_defines_the_timezone_it_references() -> None:
    calendar = Mock()
    zone = ZoneInfo("Europe/Berlin")

    create_todo(
        calendar,
        {"summary": "Pay rent", "due": datetime(2026, 7, 6, 9, 0, tzinfo=zone)},
    )

    document = calendar.save_todo.call_args.args[0]
    assert [str(item["TZID"]) for item in document.walk("VTIMEZONE")] == [
        "Europe/Berlin"
    ]


def test_import_refuses_a_uid_the_server_holds_as_the_other_kind() -> None:
    """RFC 4791 gives one uid one resource."""
    calendar = _missing_calendar()
    calendar.todo_by_uid.side_effect = None
    calendar.todo_by_uid.return_value = Mock()

    with pytest.raises(Refused, match="uid_clash"):
        import_ics(calendar, EVENT)

    assert calendar.client.puts == []


def test_a_large_import_sees_a_clash_of_either_kind() -> None:
    calendar = _missing_calendar()
    calendar.event_by_uid.side_effect = ReportError("no uid filter")
    stored = Mock()
    stored.icalendar_component = {"UID": "u3"}
    calendar.search.return_value = [stored]
    document = "".join(
        f"BEGIN:VEVENT\r\nUID:u{n}\r\nDTSTAMP:20260101T000000Z\r\n"
        f"DTSTART:20260706T090000Z\r\nSUMMARY:{n}\r\nEND:VEVENT\r\n"
        for n in range(12)
    )

    with pytest.raises(Refused, match="uid_clash"):
        import_ics(
            calendar,
            f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n{document}"
            "END:VCALENDAR\r\n",
        )

    # A good few servers answer a query without a component filter with nothing.
    assert [call.kwargs for call in calendar.search.call_args_list] == [
        {"event": True},
        {"todo": True, "include_completed": True},
    ]


def test_a_created_event_writes_no_property_named_after_an_attribute() -> None:
    calendar = Mock()

    create_event(
        calendar,
        {
            "summary": "Standup",
            "dtstart": datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 6, 10, 0, tzinfo=UTC),
            "attendees": [{"email": "a@example.com"}],
            "categories": ["work"],
            "alarms": [15],
        },
    )

    written = calendar.save_event.call_args.args[0].to_ical().decode("utf-8")
    # Not every extra is an iCalendar property, and caldav's create_ical writes
    # whatever it is handed verbatim, with a Python repr for a value.
    real = {
        "URL",
        "STATUS",
        "TRANSP",
        "CLASS",
        "PRIORITY",
        "CATEGORIES",
        "ATTENDEE",
        "ORGANIZER",
        "VALARM",
    }
    for attribute in EVENT_ATTRIBUTES:
        if attribute.upper() not in real:
            assert attribute.upper() not in written.upper()
    assert "{" not in written
    assert "ATTENDEE" in written


def test_renaming_a_recurring_todo_does_not_roll_it_forward() -> None:
    calendar = Mock()
    todo = _todo(
        "SUMMARY:Water plants\r\nDTSTART:20260706T090000Z\r\n"
        "DUE:20260706T100000Z\r\nRRULE:FREQ=WEEKLY;COUNT=4\r\nSTATUS:NEEDS-ACTION"
    )

    with patch("custom_components.ha_caldav.api.object_by_uid", return_value=todo):
        update_todo(calendar, "uid-1", {"summary": "Water the plants"})

    component = todo.icalendar_component
    assert component["DTSTART"].dt == datetime(2026, 7, 6, 9, 0, tzinfo=UTC)
    assert str(component["RRULE"].to_ical().decode()) == "FREQ=WEEKLY;COUNT=4"
    assert str(component["STATUS"]) == "NEEDS-ACTION"


def test_a_due_date_of_another_value_type_than_the_start_is_refused() -> None:
    calendar = Mock()
    todo = _todo("SUMMARY:x\r\nDTSTART;VALUE=DATE:20260706\r\nDUE;VALUE=DATE:20260707")

    with (
        patch("custom_components.ha_caldav.api.object_by_uid", return_value=todo),
        pytest.raises(Refused) as refusal,
    ):
        update_todo(
            calendar,
            "uid-1",
            {"summary": "x", "due": datetime(2026, 7, 7, 9, 0, tzinfo=UTC)},
        )

    assert refusal.value.key == "mixed_time_types"


def test_a_floating_start_is_compared_with_a_zoned_due_in_local_time() -> None:
    from homeassistant.util import dt as dt_util

    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(ZoneInfo("Europe/Berlin"))
    try:
        calendar = Mock()
        todo = _todo("SUMMARY:x\r\nDTSTART:20260706T100000")
        component = todo.icalendar_component

        with patch("custom_components.ha_caldav.api.object_by_uid", return_value=todo):
            update_todo(
                calendar,
                "uid-1",
                {"summary": "x", "due": datetime(2026, 7, 6, 8, 0, tzinfo=UTC)},
            )
    finally:
        dt_util.set_default_time_zone(previous)

    assert "DUE" in component


def test_a_todo_edit_refreshes_the_dtstamp() -> None:
    """RFC 5545 3.8.7.2: without a METHOD, DTSTAMP is when the object was revised."""
    todo = _todo("SUMMARY:Buy milk")
    calendar = Mock()
    calendar.todo_by_uid.return_value = todo

    update_todo(calendar, "uid-1", {"summary": "Buy oat milk"})

    assert todo.icalendar_component["DTSTAMP"].dt > datetime(2026, 1, 2, tzinfo=UTC)


def test_a_bulk_delete_checks_every_etag_before_deleting_anything() -> None:
    from custom_components.ha_caldav.api import delete_todos

    items = {}
    for uid, tag in (("a", '"a1"'), ("b", '"b1"'), ("c", '"c1"')):
        item = Mock()
        item.props = {"{DAV:}getetag": tag}
        items[uid] = item
    calendar = Mock()
    calendar.todo_by_uid.side_effect = items.__getitem__

    with pytest.raises(Refused, match="etag_conflict"):
        delete_todos(
            calendar,
            ["a", "b", "c"],
            {"a": '"a1"', "b": '"moved"', "c": '"c1"'},
        )

    for item in items.values():
        item.delete.assert_not_called()


def test_a_zone_named_only_by_an_alarm_is_still_collected() -> None:
    from custom_components.ha_caldav.api import _tzids

    document = ICalCalendar.from_ical(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VEVENT\r\nUID:uid-1\r\nDTSTAMP:20260101T000000Z\r\n"
        "DTSTART:20260706T090000Z\r\nSUMMARY:A\r\n"
        "BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:R\r\n"
        "TRIGGER;VALUE=DATE-TIME;TZID=Europe/Berlin:20260706T080000\r\n"
        "END:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )

    assert "Europe/Berlin" in _tzids(next(iter(document.walk("VEVENT"))))


def test_renaming_an_in_process_todo_leaves_its_progress_alone() -> None:
    """Home Assistant has two states for CalDAV's four and echoes the folded one
    back on any edit."""
    todo = _todo("SUMMARY:Paint\r\nSTATUS:IN-PROCESS\r\nPERCENT-COMPLETE:60")
    calendar = Mock()
    calendar.todo_by_uid.return_value = todo

    update_todo(
        calendar, "uid-1", {"summary": "Paint hallway", "status": "NEEDS-ACTION"}
    )

    component = todo.icalendar_component
    assert str(component["STATUS"]) == "IN-PROCESS"
    assert int(component["PERCENT-COMPLETE"]) == 60


def test_ticking_a_canceled_todo_does_not_call_it_done() -> None:
    todo = _todo("SUMMARY:Dropped\r\nSTATUS:CANCELLED")
    calendar = Mock()
    calendar.todo_by_uid.return_value = todo

    update_todo(calendar, "uid-1", {"summary": "Dropped", "status": "COMPLETED"})

    assert str(todo.icalendar_component["STATUS"]) == "CANCELLED"


def test_completing_an_outstanding_todo_still_records_it() -> None:
    todo = _todo("SUMMARY:Paint\r\nSTATUS:NEEDS-ACTION")
    calendar = Mock()
    calendar.todo_by_uid.return_value = todo

    update_todo(calendar, "uid-1", {"summary": "Paint", "status": "COMPLETED"})

    component = todo.icalendar_component
    assert str(component["STATUS"]) == "COMPLETED"
    assert int(component["PERCENT-COMPLETE"]) == 100
    assert "COMPLETED" in component


def test_a_failed_import_takes_back_what_it_already_wrote() -> None:
    calendar = _missing_calendar()
    calendar.client.fail_from = 1
    document = "".join(
        f"BEGIN:VEVENT\r\nUID:u{n}\r\nDTSTAMP:20260101T000000Z\r\n"
        f"DTSTART:20260706T090000Z\r\nSUMMARY:{n}\r\nEND:VEVENT\r\n"
        for n in range(2)
    )

    with pytest.raises(PutError):
        import_ics(
            calendar,
            f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n{document}"
            "END:VCALENDAR\r\n",
        )

    assert calendar.client.deletes == [f"{calendar.url}u0.ics"]
    # caldav answers a refusal by reserializing through vobject and putting the
    # same resource a second time.
    assert [url for url, _ in calendar.client.puts] == [
        f"{calendar.url}u0.ics",
        f"{calendar.url}u1.ics",
        f"{calendar.url}u1.ics",
    ]


def test_an_import_says_so_when_it_cannot_take_back_what_it_wrote() -> None:
    calendar = _missing_calendar()
    calendar.client.fail_from = 2
    calendar.client.delete_status = 423
    document = "".join(
        f"BEGIN:VEVENT\r\nUID:u{n}\r\nDTSTAMP:20260101T000000Z\r\n"
        f"DTSTART:20260706T090000Z\r\nSUMMARY:{n}\r\nEND:VEVENT\r\n"
        for n in range(3)
    )

    with pytest.raises(PutError):
        import_ics(
            calendar,
            f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n{document}"
            "END:VCALENDAR\r\n",
        )

    assert calendar.client.deletes == [f"{calendar.url}u0.ics", f"{calendar.url}u1.ics"]


def test_a_todo_reorder_renumbers_when_two_items_share_a_position() -> None:
    items = [
        _ordered_todo("a", f"SUMMARY:A\r\n{SORT_ORDER_PROPERTY}:5"),
        _ordered_todo("b", f"SUMMARY:B\r\n{SORT_ORDER_PROPERTY}:5"),
    ]
    calendar = Mock()
    calendar.search.return_value = items

    reorder_todos(calendar, ["b", "a"])

    assert [item.save.call_count for item in items] == [1, 1]


def test_a_scan_reads_the_uid_of_an_object_icalendar_refuses() -> None:
    """icalendar raises on an object with PRIORITY:high, which vobject on the read
    path parses."""
    from custom_components.ha_caldav.api import _scan

    broken = Mock()
    type(broken).icalendar_component = property(
        lambda _self: (_ for _ in ()).throw(ValueError("Expected int, got: high"))
    )
    broken.data = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:sore-thumb\r\n"
        "PRIORITY:high\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    calendar = Mock()
    calendar.search.return_value = [broken]

    assert [uid for _, uid in _scan(calendar, todo=False)] == ["sore-thumb"]


def test_ticking_a_todo_does_not_write_into_the_event_beside_it() -> None:
    """caldav's icalendar_component is the first subcomponent that is not a
    timezone, and a resource may hold a VEVENT ahead of the VTODO."""
    instance = ICalCalendar.from_ical(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VEVENT\r\nUID:u\r\nDTSTAMP:20260101T000000Z\r\n"
        "DTSTART:20260706T090000Z\r\nSUMMARY:Board meeting\r\nEND:VEVENT\r\n"
        "BEGIN:VTODO\r\nUID:u\r\nDTSTAMP:20260101T000000Z\r\n"
        "SUMMARY:Prepare slides\r\nSTATUS:NEEDS-ACTION\r\nEND:VTODO\r\n"
        "END:VCALENDAR\r\n"
    )
    todo = Mock()
    todo.icalendar_instance = instance
    todo.icalendar_component = next(iter(instance.walk("VEVENT")))
    calendar = Mock()
    calendar.todo_by_uid.return_value = todo

    update_todo(calendar, "u", {"summary": "Prepare slides", "status": "COMPLETED"})

    vevent = next(iter(instance.walk("VEVENT")))
    vtodo = next(iter(instance.walk("VTODO")))
    assert str(vevent["SUMMARY"]) == "Board meeting"
    assert "COMPLETED" not in vevent
    assert str(vtodo["STATUS"]) == "COMPLETED"


def test_a_recurring_todo_with_a_zero_interval_can_still_be_completed() -> None:
    """RFC 5545 wants a positive INTERVAL, but icalendar takes a zero, and dateutil
    then re-yields the start forever."""
    instance = ICalCalendar.from_ical(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VTODO\r\nUID:t\r\nDTSTAMP:20260101T000000Z\r\n"
        "DTSTART:20260101T100000Z\r\nSUMMARY:Water plants\r\n"
        "RRULE:FREQ=DAILY;INTERVAL=0\r\nEND:VTODO\r\nEND:VCALENDAR\r\n"
    )
    todo = Mock()
    todo.icalendar_instance = instance
    todo.icalendar_component = next(iter(instance.walk("VTODO")))
    calendar = Mock()
    calendar.todo_by_uid.return_value = todo

    update_todo(calendar, "t", {"summary": "Water plants", "status": "COMPLETED"})

    assert str(next(iter(instance.walk("VTODO")))["STATUS"]) == "COMPLETED"


def test_a_todo_is_saved_without_the_single_recurrence_path() -> None:
    """At caldav's default, a to-do holding only a detached instance has it refetch
    and splice back the first component until the stack runs out."""
    todo = _todo("SUMMARY:Task")
    calendar = Mock()
    calendar.todo_by_uid.return_value = todo

    update_todo(calendar, "uid-1", {"summary": "Task"})

    assert todo.save.call_args.kwargs["only_this_recurrence"] is False


def test_the_export_leaves_out_an_object_it_cannot_parse() -> None:
    broken = Mock(url="https://dav.test/cal/broken.ics")
    type(broken).icalendar_instance = property(
        lambda _self: (_ for _ in ()).throw(ValueError("Expected int, got: high"))
    )
    calendar = Mock()
    calendar.search.side_effect = lambda **kwargs: (
        [broken, _export_item("good")] if kwargs.get("event") else []
    )

    document = export_ics(calendar, None)

    assert document.count("BEGIN:VEVENT") == 1
    assert "good" in document


def test_a_folded_uid_is_read_whole() -> None:
    """RFC 5545 3.1 breaks a line past 75 octets and continues it with a space, and
    the uid Outlook and Exchange write is 112 characters."""
    from custom_components.ha_caldav.api import _raw_uid

    uid = "040000008200E00074C5B7101A82E00800000000B0C1D2E3F4A5DB01" + "AF1E2D3C" * 7
    item = Mock(
        data=(
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n"
            f"UID:{uid[:60]}\r\n {uid[60:]}\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n"
        )
    )

    assert _raw_uid(item) == uid


def test_the_sequence_hold_lands_where_caldav_bumps() -> None:
    """caldav raises the SEQUENCE of the first component that is not a timezone,
    whichever it is."""
    instance = ICalCalendar.from_ical(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VEVENT\r\nUID:u\r\nDTSTAMP:20260101T000000Z\r\n"
        "DTSTART:20260706T090000Z\r\nSUMMARY:Board meeting\r\nSEQUENCE:4\r\n"
        "END:VEVENT\r\n"
        "BEGIN:VTODO\r\nUID:u\r\nDTSTAMP:20260101T000000Z\r\n"
        "SUMMARY:Prepare slides\r\nSTATUS:NEEDS-ACTION\r\nSEQUENCE:7\r\nEND:VTODO\r\n"
        "END:VCALENDAR\r\n"
    )
    todo = Mock()
    todo.icalendar_instance = instance
    todo.icalendar_component = next(iter(instance.walk("VEVENT")))

    def save(**_kwargs: object) -> None:
        component = todo.icalendar_component
        if "SEQUENCE" in component:
            component.add("SEQUENCE", int(component.pop("SEQUENCE")) + 1)

    todo.save.side_effect = save
    calendar = Mock()
    calendar.todo_by_uid.return_value = todo

    update_todo(calendar, "u", {"summary": "Prepare slides", "status": "COMPLETED"})

    assert int(next(iter(instance.walk("VEVENT")))["SEQUENCE"]) == 4
    assert int(next(iter(instance.walk("VTODO")))["SEQUENCE"]) == 8


def test_a_rewrite_keeps_the_properties_the_server_put_on_the_document() -> None:
    """X-CALENDARSERVER-ACCESS is how Apple marks an event confidential, and it
    sits at the VCALENDAR level."""
    from custom_components.ha_caldav.api import zoned_document

    document = ICalCalendar.from_ical(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Apple//EN\r\n"
        "CALSCALE:GREGORIAN\r\nX-CALENDARSERVER-ACCESS:CONFIDENTIAL\r\n"
        "METHOD:REQUEST\r\n"
        "BEGIN:VEVENT\r\nUID:a\r\nDTSTAMP:20260101T000000Z\r\n"
        "DTSTART:20260706T090000Z\r\nSUMMARY:S\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )

    rebuilt = zoned_document(document)

    assert str(rebuilt["X-CALENDARSERVER-ACCESS"]) == "CONFIDENTIAL"
    assert str(rebuilt["CALSCALE"]) == "GREGORIAN"
    # RFC 4791 4.1: no METHOD on a calendar object resource.
    assert rebuilt.get("METHOD") is None


def test_a_generated_timezone_survives_being_read_back() -> None:
    """icalendar writes one RDATE per observance carrying every transition, and
    vobject keeps only the first value of such a line."""
    from icalendar import Event as ICalEvent
    import vobject

    from custom_components.ha_caldav.api import _document

    berlin = ZoneInfo("Europe/Berlin")
    vevent = ICalEvent()
    vevent.add("uid", "tz-1")
    vevent.add("dtstamp", datetime(2026, 1, 1, tzinfo=UTC))
    vevent.add("dtstart", datetime(2026, 6, 15, 9, 0, tzinfo=berlin))
    vevent.add("summary", "Summer")

    body = _document([vevent], {}, None).to_ical().decode("utf-8")

    read = vobject.readOne(body).vevent.dtstart.value
    assert read.astimezone(UTC) == datetime(2026, 6, 15, 7, 0, tzinfo=UTC)


def test_moving_a_resource_that_holds_no_event_is_refused() -> None:
    body = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VTODO\r\nUID:uid-1\r\nDTSTAMP:20260101T000000Z\r\n"
        "SUMMARY:Task\r\nEND:VTODO\r\nEND:VCALENDAR\r\n"
    )
    event = Mock(data=body)
    source, target = Mock(), dav_calendar()
    source.event_by_uid.return_value = event

    with pytest.raises(Refused, match="no_event_in_object"):
        move_event(source, target, "uid-1", keep_original=False)

    assert _written(target) == []
    event.delete.assert_not_called()


@pytest.mark.parametrize(
    ("stored", "expected"),
    [('W/"abc"', '"abc"'), ('"abc"', 'W/"abc"'), ('W/"abc"', 'W/"abc"')],
)
def test_the_weak_marker_does_not_make_an_etag_a_conflict(stored, expected) -> None:
    """RFC 7232 2.3.2 wants weak comparison here, and a proxy may add or drop the
    W/ between two reads."""
    from caldav.elements import dav

    from custom_components.ha_caldav.api import check_etag

    resource = Mock()
    resource.props = {dav.GetEtag.tag: stored}

    check_etag(resource, expected)


def test_a_rule_naming_a_day_no_month_has_is_refused() -> None:
    """A rule yielding nothing has dateutil walk to the year 9999, minute by minute
    for a sub-daily frequency."""
    instance = ICalCalendar.from_ical(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VTODO\r\nUID:t\r\nDTSTAMP:20260101T000000Z\r\n"
        "DTSTART:20260101T100000Z\r\nSUMMARY:Water plants\r\n"
        "RRULE:FREQ=MINUTELY;BYMONTHDAY=32\r\nEND:VTODO\r\nEND:VCALENDAR\r\n"
    )
    todo = Mock()
    todo.icalendar_instance = instance
    todo.icalendar_component = next(iter(instance.walk("VTODO")))
    calendar = Mock()
    calendar.todo_by_uid.return_value = todo

    update_todo(calendar, "t", {"summary": "Water plants", "status": "COMPLETED"})

    assert str(next(iter(instance.walk("VTODO")))["STATUS"]) == "COMPLETED"


EVENT_BESIDE_TODO = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:uid-1\r\nDTSTAMP:20260101T000000Z\r\n"
    "DTSTART:20260706T090000Z\r\nDURATION:PT1H\r\nSUMMARY:Meeting\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VTODO\r\nUID:uid-1\r\nDTSTAMP:20260101T000000Z\r\n"
    "SUMMARY:Task\r\nDUE;VALUE=DATE:20260701\r\nEND:VTODO\r\nEND:VCALENDAR\r\n"
)


def test_a_due_date_lands_on_the_todo_and_not_on_the_event_beside_it() -> None:
    """caldav's set_due writes onto the first component that is not a timezone."""
    instance = ICalCalendar.from_ical(EVENT_BESIDE_TODO)
    todo = Mock()
    todo.icalendar_instance = instance
    todo.icalendar_component = next(iter(instance.walk("VEVENT")))
    calendar = Mock()
    calendar.todo_by_uid.return_value = todo

    update_todo(calendar, "uid-1", {"summary": "Task", "due": date(2026, 7, 10)})

    vevent = next(iter(todo.icalendar_instance.walk("VEVENT")))
    vtodo = next(iter(todo.icalendar_instance.walk("VTODO")))
    assert vtodo["DUE"].dt == date(2026, 7, 10)
    assert "DUE" not in vevent
    assert str(vevent["DURATION"].to_ical().decode()) == "PT1H"


def test_a_zoned_due_date_carries_its_timezone_definition() -> None:
    """RFC 5545 wants the definition beside the reference: strict servers refuse
    a TZID without one, lenient ones store a time others read as floating."""
    instance = ICalCalendar.from_ical(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VTODO\r\nUID:uid-1\r\nDTSTAMP:20260101T000000Z\r\nSUMMARY:Task\r\n"
        "END:VTODO\r\nEND:VCALENDAR\r\n"
    )
    todo = _Resource(instance.to_ical().decode("utf-8"))
    calendar = Mock()
    calendar.todo_by_uid.return_value = todo

    update_todo(
        calendar,
        "uid-1",
        {
            "summary": "Task",
            "due": datetime(2026, 7, 10, 17, 0, tzinfo=ZoneInfo("Europe/Berlin")),
        },
    )

    assert "TZID=Europe/Berlin" in todo.data
    assert "BEGIN:VTIMEZONE" in todo.data


def test_a_uid_written_twice_is_still_found_by_the_clash_check() -> None:
    """RFC 2445 let a client repeat UID, and the list icalendar hands back for that
    stringifies to its own repr."""
    stored = CaldavEvent(
        client=None,
        url="https://dav.test/cal/legacy-1.ics",
        data=(
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
            "BEGIN:VEVENT\r\nUID:legacy-1\r\nUID:legacy-1\r\n"
            "DTSTAMP:20260101T000000Z\r\nDTSTART:20260706T090000Z\r\n"
            "SUMMARY:Stored\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        ),
    )
    calendar = dav_calendar()
    calendar.event_by_uid.side_effect = ReportError("no uid filter here")
    calendar.search.return_value = [stored]

    with pytest.raises(Refused) as refusal:
        import_ics(
            calendar,
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
            "BEGIN:VEVENT\r\nUID:legacy-1\r\nDTSTAMP:20260101T000000Z\r\n"
            "DTSTART:20260801T090000Z\r\nSUMMARY:Incoming\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n",
        )

    assert refusal.value.key == "uid_clash"
    assert _written(calendar) == []


def test_importing_a_rule_nothing_can_expand_writes_nothing() -> None:
    calendar = _missing_calendar()

    with pytest.raises(Refused) as refusal:
        import_ics(
            calendar,
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
            "BEGIN:VEVENT\r\nUID:boom-1\r\nDTSTAMP:20260101T000000Z\r\n"
            "DTSTART:20260706T090000Z\r\nRRULE:FREQ=DAILY;INTERVAL=0\r\n"
            "SUMMARY:Boom\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
        )

    assert refusal.value.key == "invalid_rrule"
    assert _written(calendar) == []


def test_creating_an_event_with_a_rule_nothing_can_expand_is_refused() -> None:
    calendar = _missing_calendar()

    with pytest.raises(Refused) as refusal:
        create_event(
            calendar,
            {
                "summary": "Boom",
                "dtstart": datetime(2026, 7, 6, 9, tzinfo=UTC),
                "dtend": datetime(2026, 7, 6, 10, tzinfo=UTC),
                "rrule": "FREQ=DAILY;INTERVAL=0",
            },
        )

    assert refusal.value.key == "invalid_rrule"
    assert _written(calendar) == []


def test_a_move_keeps_the_original_when_the_copy_will_not_land() -> None:
    source, target = dav_calendar(), _missing_calendar()
    target.client.fail_from = 0
    stored = CaldavEvent(source.client, data=EVENT, url=f"{source.url}uid-1.ics")
    source.event_by_uid.return_value = stored

    with pytest.raises(PutError):
        move_event(source, target, "uid-1", keep_original=False)

    assert source.client.deletes == []


def test_an_edit_meant_for_a_todo_refuses_a_resource_holding_none() -> None:
    calendar = Mock()
    calendar.todo_by_uid.return_value = Mock(
        icalendar_instance=ICalCalendar.from_ical(EVENT)
    )

    with pytest.raises(Refused, match="no_todo_in_object"):
        update_todo(calendar, "uid-1", {"summary": "Ticked"})


def test_a_drag_over_a_position_no_float_can_hold_still_lands() -> None:
    """A value past what a float holds parses as inf, and int(inf) raises
    OverflowError, which is not a ValueError."""
    todos = {
        "a": _ordered_todo("a", f"SUMMARY:a\r\n{SORT_ORDER_PROPERTY}:0"),
        "b": _ordered_todo("b", f"SUMMARY:b\r\n{SORT_ORDER_PROPERTY}:1024"),
        "c": _ordered_todo("c", f"SUMMARY:c\r\n{SORT_ORDER_PROPERTY}:{'9' * 400}"),
    }
    calendar = Mock()
    calendar.search.return_value = list(todos.values())

    reorder_todos(calendar, ["b", "a", "c"])

    assert all(todo.save.called for todo in todos.values())


def test_an_object_no_read_can_reach_is_skipped_by_a_scan_without_failing() -> None:
    unreadable = Mock(url="https://dav.test/cal/broken.ics")
    type(unreadable).icalendar_component = property(
        lambda self: (_ for _ in ()).throw(ValueError("not iCalendar"))
    )
    type(unreadable).data = property(
        lambda self: (_ for _ in ()).throw(OSError("gone from under us"))
    )
    calendar = Mock()
    calendar.event_by_uid.side_effect = ReportError("no uid filter")
    calendar.search.return_value = [unreadable]

    with pytest.raises(NotFoundError):
        export_ics(calendar, "uid-1")


# Text a caldav rewrite rule matches; RFC 5545 escapes none of it.
_TRAP = "Deal COMPLETED:20260101 - archive the file"


def test_an_import_puts_the_text_it_was_given_and_not_a_rewrite_of_it() -> None:
    """caldav puts every string assigned to a resource through vcal.fix, whose
    COMPLETED rule is not anchored to the start of a line."""
    calendar = _missing_calendar()

    import_ics(
        calendar,
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nBEGIN:VEVENT\r\n"
        "UID:u1\r\nDTSTAMP:20260101T000000Z\r\nDTSTART:20260706T090000Z\r\n"
        f"SUMMARY:Contract review\r\nDESCRIPTION:{_TRAP}\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n",
    )

    body = _written(calendar)[0]
    assert str(_component(body)["DESCRIPTION"]) == _TRAP


def test_a_move_carries_the_text_of_the_event_across_unaltered() -> None:
    source, target = _missing_calendar(), _missing_calendar()
    source.event_by_uid.side_effect = None
    source.event_by_uid.return_value = Mock(
        data=EVENT.replace("SUMMARY:Standup", f"SUMMARY:Standup\r\nDESCRIPTION:{_TRAP}")
    )

    move_event(source, target, "uid-1", keep_original=True)

    assert str(_component(_written(target)[0])["DESCRIPTION"]) == _TRAP


MIXED_RESOURCE = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//other-client//EN\r\n"
    "BEGIN:VEVENT\r\nUID:mixed-1\r\nDTSTAMP:20260101T000000Z\r\n"
    "DTSTART:20260706T090000Z\r\nDTEND:20260706T100000Z\r\n"
    "SUMMARY:Contract review\r\nSEQUENCE:4\r\nEND:VEVENT\r\n"
    "BEGIN:VTODO\r\nUID:mixed-1\r\nDTSTAMP:20260101T000000Z\r\n"
    "SUMMARY:File the contract\r\nEND:VTODO\r\nEND:VCALENDAR\r\n"
)


def test_ticking_off_a_todo_leaves_the_event_sharing_its_resource() -> None:
    """RFC 4791 gives a resource one component type, but one holding both under a
    single uid does occur."""
    calendar = Mock()
    resource = _Resource(MIXED_RESOURCE)
    calendar.todo_by_uid.return_value = resource

    delete_todos(calendar, ["mixed-1"], {})

    resource.delete.assert_not_called()
    document = ICalCalendar.from_ical(resource.data)
    assert not document.walk("VTODO")
    assert str(_component(resource.data)["SUMMARY"]) == "Contract review"
    # caldav moves a SEQUENCE on the way out whatever it is told.
    assert int(_component(resource.data)["SEQUENCE"]) == 4


def test_deleting_the_event_leaves_the_todo_sharing_its_resource() -> None:
    calendar = Mock()
    resource = _Resource(MIXED_RESOURCE)
    calendar.event_by_uid.return_value = resource

    delete_event(calendar, "mixed-1")

    resource.delete.assert_not_called()
    document = ICalCalendar.from_ical(resource.data)
    assert not document.walk("VEVENT")
    assert str(next(iter(document.walk("VTODO")))["SUMMARY"]) == "File the contract"


def test_deleting_the_last_component_removes_the_resource_itself() -> None:
    calendar = Mock()
    resource = _Resource(EVENT)
    calendar.event_by_uid.return_value = resource

    delete_event(calendar, "uid-1")

    resource.delete.assert_called_once()
    resource.save.assert_not_called()


def test_clearing_a_selection_says_which_item_was_gone_rather_than_scanning() -> None:
    calendar = Mock()
    calendar.todo_by_uid.side_effect = NotFoundError("gone")

    with pytest.raises(NotFoundError):
        delete_todos(calendar, ["todo-1", "todo-2"], {})

    calendar.search.assert_not_called()


def test_a_recurring_todo_with_no_date_at_all_is_simply_closed() -> None:
    """RFC 5545 leaves both DTSTART and DUE optional, and a rule has nothing to
    step from without one."""
    calendar = FakeTodoCalendar(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nBEGIN:VTODO\r\n"
        "UID:t1\r\nDTSTAMP:20260101T000000Z\r\nSUMMARY:Water plants\r\n"
        "RRULE:FREQ=WEEKLY\r\nEND:VTODO\r\nEND:VCALENDAR\r\n"
    )

    update_todo(calendar, "t1", {"summary": "Water plants", "status": "COMPLETED"})

    assert str(calendar.todo.stored()["STATUS"]) == "COMPLETED"


def _created_uid(save: Mock, kind: str = "VEVENT") -> UUID:
    return UUID(str(_component(save.call_args.args[0].to_ical().decode(), kind)["UID"]))


def test_a_new_event_and_todo_carry_a_random_uid() -> None:
    calendar = Mock()

    create_event(
        calendar,
        {
            "summary": "Standup",
            "dtstart": datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
            "dtend": datetime(2026, 7, 6, 10, 0, tzinfo=UTC),
        },
    )
    create_todo(calendar, {"summary": "Task"})

    assert _created_uid(calendar.save_event).version == 4
    assert _created_uid(calendar.save_todo, "VTODO").version == 4


def test_a_new_calendar_gets_a_random_collection_name() -> None:
    client = Mock()

    create_calendar(client, "Holidays", None)

    cal_id = client.principal.return_value.make_calendar.call_args.kwargs["cal_id"]
    assert UUID(cal_id).version == 4


def test_a_new_series_gets_its_end_in_utc() -> None:
    # The frontend writes UTC digits without the Z.
    calendar = Mock()
    berlin = ZoneInfo("Europe/Berlin")

    create_event(
        calendar,
        {
            "summary": "Standup",
            "dtstart": datetime(2026, 12, 1, 10, 0, tzinfo=berlin),
            "dtend": datetime(2026, 12, 1, 11, 0, tzinfo=berlin),
            "rrule": "FREQ=WEEKLY;UNTIL=20261229T090000",
        },
    )

    body = calendar.save_event.call_args.args[0].to_ical().decode()
    assert "UNTIL=20261229T090000Z" in body


def test_the_clash_check_asks_for_each_kind_rather_than_for_any() -> None:
    calendar = _missing_calendar()

    import_ics(calendar, EVENT)

    calendar.object_by_uid.assert_not_called()
    calendar.event_by_uid.assert_called_once_with("uid-1")
    calendar.todo_by_uid.assert_called_once_with("uid-1")


@pytest.mark.parametrize(
    "error",
    [requests.Timeout("slow"), AuthorizationError(reason="Unauthorized")],
    ids=["timeout", "unauthorized"],
)
def test_a_lookup_that_got_no_answer_is_not_answered_with_a_scan(error) -> None:
    calendar = _missing_calendar()
    calendar.event_by_uid.side_effect = error

    with pytest.raises(type(error)):
        import_ics(calendar, EVENT)

    calendar.search.assert_not_called()
    assert _written(calendar) == []


def test_an_import_with_two_series_under_one_uid_is_refused() -> None:
    series = (
        "BEGIN:VEVENT\r\nUID:uid-1\r\nDTSTAMP:20260101T000000Z\r\n"
        "DTSTART:20260706T090000Z\r\nEND:VEVENT\r\n"
    )
    doubled = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        f"{series}{series}END:VCALENDAR\r\n"
    )

    with pytest.raises(Refused, match="duplicate_uid"):
        import_ics(_missing_calendar(), doubled)


def test_setting_alarms_keeps_the_email_ones_it_cannot_write() -> None:
    component = _component(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nBEGIN:VEVENT\r\n"
        "UID:e-1\r\nDTSTAMP:20260101T000000Z\r\nDTSTART:20260706T090000Z\r\n"
        "BEGIN:VALARM\r\nACTION:EMAIL\r\nTRIGGER:-P1D\r\nSUMMARY:x\r\n"
        "DESCRIPTION:Mail me\r\nATTENDEE:mailto:me@example.com\r\nEND:VALARM\r\n"
        "BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT5M\r\nDESCRIPTION:x\r\n"
        "END:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )

    apply_extras(component, {"alarms": [10]})

    actions = sorted(str(alarm["ACTION"]) for alarm in component.walk("VALARM"))
    assert actions == ["DISPLAY", "EMAIL"]


def test_a_move_whose_delete_fails_takes_its_copy_back() -> None:
    event, source, target = _move_pair()
    event.delete.side_effect = requests.Timeout("slow")

    with pytest.raises(requests.Timeout):
        move_event(source, target, "uid-1", keep_original=False)

    assert target.client.deletes == [f"{target.url}uid-1.ics"]


def test_a_move_whose_delete_landed_before_the_timeout_keeps_its_copy() -> None:
    event, source, target = _move_pair()
    event.delete.side_effect = requests.Timeout("slow")
    source.event_by_uid.side_effect = [event, NotFoundError("uid-1 is gone")]

    with pytest.raises(requests.Timeout):
        move_event(source, target, "uid-1", keep_original=False)

    assert target.client.deletes == []


UNREADABLE_RULE = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nBEGIN:VEVENT\r\n"
    "UID:uid-1\r\nDTSTAMP:20260101T000000Z\r\nDTSTART:20260706T090000Z\r\n"
    "RRULE:FREQ=WEEKLY;UNTIL=2026-12-31\r\nSUMMARY:Standup\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)


def test_an_import_refuses_a_line_it_cannot_read() -> None:
    calendar = _missing_calendar()

    with pytest.raises(Refused, match="unreadable_lines") as raised:
        import_ics(calendar, UNREADABLE_RULE)

    assert raised.value.placeholders == {"lines": "RRULE"}
    assert _written(calendar) == []


def test_an_import_refuses_an_event_and_a_todo_under_one_uid() -> None:
    calendar = _missing_calendar()
    document = EVENT.replace(
        "END:VCALENDAR",
        "BEGIN:VTODO\r\nUID:uid-1\r\nDTSTAMP:20260101T000000Z\r\n"
        "SUMMARY:Slides\r\nEND:VTODO\r\nEND:VCALENDAR",
    )

    with pytest.raises(Refused, match="duplicate_uid"):
        import_ics(calendar, document)

    assert _written(calendar) == []


def test_the_export_names_the_lines_it_could_not_carry(caplog) -> None:
    item = Mock(url="https://dav.test/cal/uid-1.ics")
    item.icalendar_instance = ICalCalendar.from_ical(UNREADABLE_RULE)
    calendar = Mock()
    calendar.search.side_effect = [[item], []]

    export_ics(calendar, None)

    assert "RRULE" in caplog.text


def test_swapping_two_neighbors_writes_the_one_that_moved_up() -> None:
    calendar = Mock()
    todos = {
        letter: _ordered_todo(
            letter, f"SUMMARY:{letter}\r\nX-APPLE-SORT-ORDER:{n * _SORT_GAP}"
        )
        for n, letter in enumerate("abcd")
    }
    calendar.search.return_value = list(todos.values())

    reorder_todos(calendar, ["a", "c", "b", "d"])

    assert [letter for letter, todo in todos.items() if todo.save.called] == ["c"]
    assert _order(todos) == ["a", "c", "b", "d"]


@pytest.mark.parametrize("frequency", ["SECONDLY", "MINUTELY"])
def test_a_new_event_denser_than_hourly_is_refused(frequency: str) -> None:
    calendar = Mock()

    with pytest.raises(Refused, match="rrule_too_dense"):
        create_event(
            calendar,
            {
                "summary": "Tick",
                "dtstart": datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
                "dtend": datetime(2026, 7, 6, 10, 0, tzinfo=UTC),
                "rrule": f"FREQ={frequency}",
            },
        )

    calendar.save_event.assert_not_called()


def test_an_all_day_series_takes_its_end_as_a_date() -> None:
    calendar = Mock()

    create_event(
        calendar,
        {
            "summary": "Trip",
            "dtstart": date(2026, 7, 6),
            "dtend": date(2026, 7, 7),
            "rrule": "FREQ=DAILY;UNTIL=20260710T000000Z",
        },
    )

    body = calendar.save_event.call_args.args[0].to_ical().decode("utf-8")
    assert _component(body)["RRULE"]["UNTIL"] == [date(2026, 7, 10)]
