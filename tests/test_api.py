"""Tests for the CalDAV write operations that services drive."""

from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from caldav.calendarobjectresource import Event as CaldavEvent
from caldav.elements import ical
from caldav.lib.error import NotFoundError, PutError, ReportError
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
from custom_components.ha_caldav.recurrence import delete_event

EVENT = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:uid-1\r\nDTSTAMP:20260101T000000Z\r\n"
    "DTSTART;TZID=Europe/Berlin:20260706T090000\r\n"
    "DTEND;TZID=Europe/Berlin:20260706T100000\r\nSUMMARY:Standup\r\n"
    "RELATED-TO:parent-uid\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)


def _missing_calendar() -> Mock:
    """Return a calendar whose uid lookups all come back empty."""
    calendar = dav_calendar()
    calendar.object_by_uid.side_effect = NotFoundError("nope")
    calendar.event_by_uid.side_effect = NotFoundError("nope")
    calendar.todo_by_uid.side_effect = NotFoundError("nope")
    calendar.search.return_value = []
    return calendar


def _written(calendar: Mock) -> list[str]:
    """Return the documents that reached the wire, in order."""
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

    # One PUT: a second one could fail and leave a half-built event behind.
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
    calendar = Mock()
    calendar.object_by_uid.return_value = Mock()

    with pytest.raises(Refused, match="uid_clash"):
        import_ics(calendar, EVENT)

    assert calendar.save_event.call_count == 0


def test_import_keeps_relations_and_carries_one_timezone() -> None:
    calendar = _missing_calendar()

    assert import_ics(calendar, EVENT) == ["uid-1"]

    body = _written(calendar)[0]
    # Only Calendar.save_object follows RELATED-TO and rewrites what it names;
    # this path saves the resource itself, so a task hierarchy survives the
    # round trip through export and import.
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
    """caldav reads its recurrence handling off the first component that is not
    a timezone, and on finding a RECURRENCE-ID there it goes looking on the
    target for a uid the target does not carry yet. It dies on the None that
    comes back, so an invitation to a single occurrence could not be imported
    at all, nor an object a server returned exception-first."""
    calendar = _missing_calendar()

    assert import_ics(calendar, ORPHAN_OVERRIDE) == ["uid-1"]
    assert "RECURRENCE-ID" in _written(calendar)[0]


def test_move_carries_an_object_whose_first_component_is_an_exception() -> None:
    event = Mock(data=ORPHAN_OVERRIDE)
    source, target = Mock(), dav_calendar()
    source.event_by_uid.return_value = event
    target.object_by_uid.side_effect = NotFoundError("uid-1 not found on server")

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
    """Through a real resource, not a double handing the document back: caldav
    normalizes the line endings of what it read, and RFC 5545 3.1 has them as
    CRLF, so the export of a single object went out as something a strict
    importer refuses while the whole-calendar export beside it was correct."""
    calendar = dav_calendar()
    calendar.object_by_uid.return_value = CaldavEvent(calendar.client, data=EVENT)

    exported = export_ics(calendar, "uid-1")

    assert exported == EVENT
    assert exported.count("\n") == exported.count("\r\n")


def test_export_of_one_object_finds_a_todo_too() -> None:
    """The whole-calendar export carries to-dos, so a uid that names one must
    not come back as missing from the single-object mode."""
    body = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VTODO\r\nUID:t-1\r\nDTSTAMP:20260101T000000Z\r\nSUMMARY:Task\r\n"
        "END:VTODO\r\nEND:VCALENDAR\r\n"
    )
    calendar = Mock()
    calendar.event_by_uid.side_effect = NotFoundError("not an event")
    calendar.object_by_uid.return_value = Mock(data=body)

    assert export_ics(calendar, "t-1") == body


def _export_item(uid: str) -> Mock:
    """One stored resource, at a url of its own as a real one has."""
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
    """A resource holding a VEVENT and a VTODO under one uid comes back from
    the event filter and from the to-do filter alike, and re-importing the
    export would then produce two of everything."""
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
    """A source holding uid-1 and a target holding nothing."""
    event = Mock(data=EVENT)
    source, target = Mock(), dav_calendar()
    source.event_by_uid.return_value = event
    target.object_by_uid.side_effect = NotFoundError("uid-1 not found on server")
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
    """caldav names the resource after the uid, so the write would land on it.

    The delete that follows a move would then take the only copy left.
    """
    event = Mock(data=EVENT)
    calendar = Mock()
    calendar.event_by_uid.return_value = event
    calendar.object_by_uid.return_value = event

    with pytest.raises(Refused, match="uid_clash"):
        move_event(calendar, calendar, "uid-1", keep_original=False)

    calendar.save_event.assert_not_called()
    event.delete.assert_not_called()


def test_moving_onto_a_calendar_that_holds_the_uid_is_refused() -> None:
    event = Mock(data=EVENT)
    source, target = Mock(), dav_calendar()
    source.event_by_uid.return_value = event
    target.object_by_uid.return_value = Mock()

    with pytest.raises(Refused, match="uid_clash"):
        move_event(source, target, "uid-1", keep_original=False)

    assert _written(target) == []
    event.delete.assert_not_called()


class _Resource:
    """A resource whose text follows the document written onto it.

    As caldav's does: the write path hands over the object rather than its
    text, because a string is put through vcal.fix on the way in. A double
    keeping the two apart would report the document the poll read.
    """

    def __init__(self, ics: str) -> None:
        self.data = ics
        self._instance = ICalCalendar.from_ical(ics)
        self.save = Mock(side_effect=self._bump)
        self.delete = Mock()

    def _bump(self, **kwargs: Any) -> None:
        """Move a SEQUENCE the way caldav 2.1.0 does on the way out.

        It bumps the first component that is not a timezone whatever
        increase_seqno says, so a double that skipped it would let a write hold
        the wrong one back and never show it.
        """
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
    # An orphan-override object would have caldav recurse until it gave up.
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
    # Both, as a caldav resource carries both: the component is a subcomponent
    # of the instance, and code that has to pick a kind out of the resource
    # reads the instance.
    todo.icalendar_instance = instance
    todo.icalendar_component = next(iter(instance.walk("VTODO")))

    def save(**_kwargs: object) -> None:
        # caldav 2.1.0 bumps SEQUENCE on the way out whatever increase_seqno
        # says, and only when the property is already there. Modelled, or the
        # double bump this fake used to hide stays invisible.
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
    """A search result carrying its own uid, the way a collection read gives them."""
    todo = _todo(body)
    todo.icalendar_component["UID"] = uid
    return todo


def test_reorder_compares_positions_numerically() -> None:
    """A server-written "00" is the same position as 0, so nothing moved."""
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
    """Renumbering the rest would be a PUT per item on every single drag."""
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
    # Room between them, or the next drag would have to renumber everyone.
    positions = [
        int(str(todo.icalendar_component["X-APPLE-SORT-ORDER"]))
        for todo in todos.values()
    ]
    # Room for many future drags, not merely for one: at a gap of two, nearly
    # every drag falls through to the full renumber this exists to avoid, and
    # that is a PUT per item on the list.
    assert min(abs(a - b) for a in positions for b in positions if a != b) >= _SORT_GAP


def test_reorder_renumbers_when_the_neighbours_leave_no_room() -> None:
    calendar = Mock()
    todos = {
        letter: _ordered_todo(letter, f"SUMMARY:{letter}\r\nX-APPLE-SORT-ORDER:{n}")
        for n, letter in enumerate("abc")
    }
    calendar.search.return_value = list(todos.values())

    # Between 0 and 1 there is no whole number to take.
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
    """Return the uids in the order the written positions put them."""
    return sorted(
        todos,
        key=lambda uid: float(
            str(todos[uid].icalendar_component["X-APPLE-SORT-ORDER"])
        ),
    )


@pytest.mark.parametrize("written", ["nonsense", "", "3.5.1"])
def test_reorder_survives_a_sort_order_the_server_wrote_by_hand(written: str) -> None:
    # Another client is free to put anything in there, and it must not take
    # the whole reorder down.
    calendar = Mock()
    todo = _ordered_todo("a", f"SUMMARY:a\r\nX-APPLE-SORT-ORDER:{written}")
    calendar.search.return_value = [todo]

    reorder_todos(calendar, ["a"])

    assert str(todo.icalendar_component["X-APPLE-SORT-ORDER"]) == "0"


def test_reorder_reads_the_collection_once_for_the_whole_ordering() -> None:
    # Home Assistant sends the entire ordering for a single drag; a lookup per
    # item is a request per item, and a whole download per item on iCloud.
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


def test_a_refusal_of_ours_keeps_its_own_wording() -> None:
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
        {"summary": "Task", "due": date(2026, 7, 10), "description": "Two litres"},
    )

    vtodo = next(iter(todo.icalendar_instance.walk("VTODO")))
    assert vtodo["DUE"].dt == date(2026, 7, 10)
    assert str(vtodo["DESCRIPTION"]) == "Two litres"


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

    # Refused is a ValueError, so it has to be recognised before the arm that
    # forwards a library message verbatim.
    assert reported.translation_key == "etag_conflict"


def test_a_library_error_that_is_also_a_valueerror_keeps_the_url_out() -> None:
    # Through caldav's own alias, which is what errors.py catches: caldav binds
    # requests to niquests where that is importable and to requests where it is
    # not, and naming either directly tests whichever happens to be installed
    # rather than the one the integration will actually meet.
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
    """RFC 5546: only the organizer moves SEQUENCE.

    Written against a real caldav.Event rather than a stub, because the value
    that matters is the one on the wire: caldav 2.1.0 accepts increase_seqno
    and bumps regardless, so this fails loudly if that behaviour ever changes.
    """
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
    """One uid is one resource, so an event would overwrite a to-do."""
    calendar = _missing_calendar()
    calendar.object_by_uid.side_effect = None
    calendar.object_by_uid.return_value = Mock()

    with pytest.raises(Refused, match="uid_clash"):
        import_ics(calendar, EVENT)

    assert calendar.save_event.call_count == 0


def test_a_large_import_sees_a_clash_of_either_kind() -> None:
    calendar = _missing_calendar()
    calendar.object_by_uid.side_effect = ReportError("no uid filter")
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

    # One read per component type, not one per uid, and never the filter-less
    # query that a good few servers answer with nothing at all.
    assert [call.kwargs for call in calendar.search.call_args_list] == [
        {"event": True},
        {"todo": True, "include_completed": True},
    ]


def test_a_created_event_writes_no_property_named_after_an_attribute() -> None:
    """The extras have their own writer; passing one through as a core field
    would put a Python repr on the wire under an invented property name."""
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
    # The names the extras go by are not all iCalendar properties; handing one
    # to create_ical writes it verbatim with a Python repr for a value.
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
    component = todo.icalendar_component

    def set_due(due, **kwargs):
        component.pop("DUE", None)
        component.add("DUE", due)

    todo.set_due.side_effect = set_due

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


def test_a_floating_start_is_compared_with_a_zoned_due_in_local_time(monkeypatch):
    """Both name the same instant under Europe/Berlin, so nothing is refused."""
    from homeassistant.util import dt as dt_util

    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(ZoneInfo("Europe/Berlin"))
    try:
        calendar = Mock()
        todo = _todo("SUMMARY:x\r\nDTSTART:20260706T100000")
        component = todo.icalendar_component

        def set_due(due, **kwargs):
            component.pop("DUE", None)
            component.add("DUE", due)

        todo.set_due.side_effect = set_due

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
    """RFC 5545 3.8.7.2: without a METHOD it is when the object was revised."""
    todo = _todo("SUMMARY:Buy milk")
    calendar = Mock()
    calendar.todo_by_uid.return_value = todo

    update_todo(calendar, "uid-1", {"summary": "Buy oat milk"})

    assert todo.icalendar_component["DTSTAMP"].dt > datetime(2026, 1, 2, tzinfo=UTC)


def test_a_bulk_delete_checks_every_etag_before_deleting_anything() -> None:
    """A conflict on the second must not leave the first already gone."""
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
    """Otherwise the document references a TZID it never defines."""
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
    """Home Assistant has two states for CalDAV's four and echoes the folded
    one back on any edit, so writing it would undo work nobody touched."""
    todo = _todo("SUMMARY:Paint\r\nSTATUS:IN-PROCESS\r\nPERCENT-COMPLETE:60")
    calendar = Mock()
    calendar.todo_by_uid.return_value = todo

    update_todo(
        calendar, "uid-1", {"summary": "Paint hallway", "status": "NEEDS-ACTION"}
    )

    component = todo.icalendar_component
    assert str(component["STATUS"]) == "IN-PROCESS"
    assert int(component["PERCENT-COMPLETE"]) == 60


def test_ticking_a_cancelled_todo_does_not_call_it_done() -> None:
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
    """Left behind, those objects make the retry fail as uid clashes with
    itself, and the user has no way to tell which are theirs."""
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

    # The one that did land is taken back off the server, by url.
    assert calendar.client.deletes == [f"{calendar.url}u0.ics"]
    # caldav answers a refusal by reserializing through vobject and putting the
    # same resource a second time, so the failing uid costs two writes.
    assert [url for url, _ in calendar.client.puts] == [
        f"{calendar.url}u0.ics",
        f"{calendar.url}u1.ics",
        f"{calendar.url}u1.ics",
    ]


def test_an_import_says_so_when_it_cannot_take_back_what_it_wrote() -> None:
    """Whatever will not go is named in the log, not raised: replacing the
    PutError that got us here with a DeleteError would leave the user chasing
    the wrong failure, and the rollback of the objects after it undone."""
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
    """The displayed order is then the server's own, which no tie-break here
    reproduces, so a drag would look like no change and spring back."""
    items = [
        _ordered_todo("a", f"SUMMARY:A\r\n{SORT_ORDER_PROPERTY}:5"),
        _ordered_todo("b", f"SUMMARY:B\r\n{SORT_ORDER_PROPERTY}:5"),
    ]
    calendar = Mock()
    calendar.search.return_value = items

    reorder_todos(calendar, ["b", "a"])

    assert [item.save.call_count for item in items] == [1, 1]


def test_a_scan_reads_the_uid_of_an_object_icalendar_refuses() -> None:
    """icalendar is strict where the vobject of the read path is not, so an
    object another client wrote PRIORITY:high into parses on screen and raises
    here. Dropped, it reports no clash for the uid it holds and an import
    overwrites it; the whole scan raising takes down every write that uses it,
    which on a server refusing the uid filter is all of them."""
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
    """caldav's icalendar_component hands back whichever subcomponent is not a
    timezone, and RFC 4791 does not stop one resource from holding a VEVENT
    ahead of a VTODO. The meeting was renamed, given COMPLETED and
    PERCENT-COMPLETE, and the item the user ticked stayed open."""
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
    """RFC 5545 wants a positive INTERVAL and icalendar takes a zero without
    complaint. dateutil then re-yields the start forever, so .after() never
    returns: a thread out of the executor pool spins until Home Assistant is
    restarted, and no timeout or except reaches it."""
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

    # Closed outright rather than rolled: there is no next occurrence to roll to.
    assert str(next(iter(instance.walk("VTODO")))["STATUS"]) == "COMPLETED"


def test_a_todo_is_saved_without_the_single_recurrence_path() -> None:
    """Left at caldav's default, a to-do holding nothing but a detached
    instance sends it refetching and splicing back the first component, one
    server lookup per turn, until the stack runs out."""
    todo = _todo("SUMMARY:Task")
    calendar = Mock()
    calendar.todo_by_uid.return_value = todo

    update_todo(calendar, "uid-1", {"summary": "Task"})

    assert todo.save.call_args.kwargs["only_this_recurrence"] is False


def test_the_export_leaves_out_an_object_it_cannot_parse() -> None:
    """One object another client wrote a non-numeric PRIORITY into would
    otherwise cost the user the export of the whole calendar, with a message
    naming neither the object nor the calendar."""
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
    """RFC 5545 3.1 breaks a line past 75 octets and continues it with a space,
    and the uid Outlook and Exchange write is 112 characters. Read line by line
    it comes back cut in half, and a cut uid matches nothing: an import reports
    no clash and overwrites the object, and every edit of a visible event
    reports it missing from the server."""
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
    """caldav raises the SEQUENCE of the first component that is not a
    timezone, whichever it is. Held on the to-do instead, the untouched meeting
    beside it announced a new revision to its attendees and the item the user
    actually edited announced none."""
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
    sits at the VCALENDAR level. Rebuilding the document from its components
    alone dropped the privacy marker on every rename."""
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
    """icalendar writes one RDATE per observance carrying every transition as a
    list, and vobject — which the read path parses with — keeps only the first
    value of such a line. An event this integration had just written at 09:00
    Berlin read back an hour out for the whole daylight-saving half of the year,
    on its own calendar."""
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
    # Written to the target as an event and deleted from the source, the to-do
    # it actually held would come back as neither.
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
    """RFC 7232 2.3.2 wants weak comparison here. A proxy is free to add or drop
    the W/ between the read that recorded the tag and the one that checks it,
    and compared literally that account could never write anything and was told
    each time that somebody else had."""
    from caldav.elements import dav

    from custom_components.ha_caldav.api import check_etag

    resource = Mock()
    resource.props = {dav.GetEtag.tag: stored}

    check_etag(resource, expected)


def test_a_rule_naming_a_day_no_month_has_is_refused() -> None:
    """Such a rule yields nothing, so the guard that counts yielded occurrences
    never fires and dateutil walks to the year 9999 — minute by minute for a
    sub-daily frequency, with the collection's write lock held throughout."""
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

    # Closed outright rather than rolled: the rule reaches no next occurrence.
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
    """caldav's set_due writes onto whichever component is not a timezone
    first, which is the very split vtodo_of exists for: the meeting lost its
    DURATION and gained a DUE, and the item the user dated kept the old one."""
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
    """RFC 2445 let a client repeat it, and the list icalendar hands back for
    that stringifies to its own repr, which matches no uid at all. The import
    then reported the collection empty of it and overwrote the stored object."""
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
    calendar.object_by_uid.side_effect = ReportError("no uid filter here")
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
    """Every poll of the collection would then hang on expanding it."""
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
    """Written first and deleted second. The other order costs the user the
    only copy of the event whenever the target refuses the write: a full quota,
    a collection they may only read, a connection that drops."""
    source, target = dav_calendar(), _missing_calendar()
    target.client.fail_from = 0
    stored = CaldavEvent(source.client, data=EVENT, url=f"{source.url}uid-1.ics")
    source.event_by_uid.return_value = stored

    with pytest.raises(PutError):
        move_event(source, target, "uid-1", keep_original=False)

    assert source.client.deletes == []


def test_an_edit_meant_for_a_todo_refuses_a_resource_holding_none() -> None:
    """Falling back to the first component instead wrote the item's summary,
    its COMPLETED and its PERCENT-COMPLETE onto the meeting standing there."""
    calendar = Mock()
    calendar.todo_by_uid.return_value = Mock(
        icalendar_instance=ICalCalendar.from_ical(EVENT)
    )

    with pytest.raises(Refused, match="no_todo_in_object"):
        update_todo(calendar, "uid-1", {"summary": "Ticked"})


def test_a_drag_over_a_position_no_float_can_hold_still_lands() -> None:
    """A value past what a float holds parses as inf, and int(inf) raises an
    OverflowError, which is not one of the write errors: the drag springs back
    with an untranslated "Unknown error" over a number the server only stored."""
    todos = {
        "a": _ordered_todo("a", f"SUMMARY:a\r\n{SORT_ORDER_PROPERTY}:0"),
        "b": _ordered_todo("b", f"SUMMARY:b\r\n{SORT_ORDER_PROPERTY}:1024"),
        "c": _ordered_todo("c", f"SUMMARY:c\r\n{SORT_ORDER_PROPERTY}:{'9' * 400}"),
    }
    calendar = Mock()
    calendar.search.return_value = list(todos.values())

    reorder_todos(calendar, ["b", "a", "c"])

    assert all(todo.save.called for todo in todos.values())


def test_an_object_no_read_can_reach_still_answers_the_clash_check() -> None:
    """Dropped instead, the uid it holds reports no clash anywhere and an
    import overwrites the object standing there."""
    unreadable = Mock(url="https://dav.test/cal/broken.ics")
    type(unreadable).icalendar_component = property(
        lambda self: (_ for _ in ()).throw(ValueError("not iCalendar"))
    )
    type(unreadable).data = property(
        lambda self: (_ for _ in ()).throw(OSError("gone from under us"))
    )
    calendar = Mock()
    calendar.object_by_uid.side_effect = ReportError("no uid filter")
    calendar.search.return_value = [unreadable]

    with pytest.raises(NotFoundError):
        export_ics(calendar, "uid-1")


# Text a caldav rewrite rule matches. RFC 5545 escapes none of it, so it
# travels to the server exactly as the user typed it — or does not.
_TRAP = "Deal COMPLETED:20260101 - archive the file"


def test_an_import_puts_the_text_it_was_given_and_not_a_rewrite_of_it() -> None:
    """caldav puts every string assigned to a resource through vcal.fix, whose
    COMPLETED rule is not anchored to the start of a line. A description
    carrying that shape went on the wire rewritten, the space behind it eaten,
    and the write reported success. Handed the document instead of its text,
    caldav serializes it and leaves it alone."""
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
    """The same rewrite reaches every path that hands caldav a string."""
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
    """RFC 4791 gives a resource one component type, but one holding both under
    a single uid does occur, and then the calendar shows the event while the
    list shows the item. Answered with a DELETE on the resource, clearing the
    item took the meeting with it: gone from the calendar, out of the recorder
    history, with nothing said and no way back."""
    calendar = Mock()
    resource = _Resource(MIXED_RESOURCE)
    calendar.todo_by_uid.return_value = resource

    delete_todos(calendar, ["mixed-1"], {})

    resource.delete.assert_not_called()
    document = ICalCalendar.from_ical(resource.data)
    assert not document.walk("VTODO")
    assert str(_component(resource.data)["SUMMARY"]) == "Contract review"
    # Nothing here revises the meeting, and caldav moves a SEQUENCE on the way
    # out whatever it is told, so it is held back to where the server had it.
    assert int(_component(resource.data)["SEQUENCE"]) == 4


def test_deleting_the_event_leaves_the_todo_sharing_its_resource() -> None:
    """The same resource, taken from the other side."""
    calendar = Mock()
    resource = _Resource(MIXED_RESOURCE)
    calendar.event_by_uid.return_value = resource

    delete_event(calendar, "mixed-1")

    resource.delete.assert_not_called()
    document = ICalCalendar.from_ical(resource.data)
    assert not document.walk("VEVENT")
    assert str(next(iter(document.walk("VTODO")))["SUMMARY"]) == "File the contract"


def test_deleting_the_last_component_removes_the_resource_itself() -> None:
    """A document left holding nothing is one the next poll drops and no edit
    can reach, which is a delete that never says so."""
    calendar = Mock()
    resource = _Resource(EVENT)
    calendar.event_by_uid.return_value = resource

    delete_event(calendar, "uid-1")

    resource.delete.assert_called_once()
    resource.save.assert_not_called()


def test_clearing_a_selection_says_which_item_was_gone_rather_than_scanning() -> None:
    """A server answering the uid filter and reporting nothing is answering.
    Falling back to a whole-collection scan on that would cost one download per
    missing item on exactly the lists a bulk delete is used on."""
    calendar = Mock()
    calendar.todo_by_uid.side_effect = NotFoundError("gone")

    with pytest.raises(NotFoundError):
        delete_todos(calendar, ["todo-1", "todo-2"], {})

    calendar.search.assert_not_called()


def test_a_recurring_todo_with_no_date_at_all_is_simply_closed() -> None:
    """RFC 5545 leaves both DTSTART and DUE optional, and a rule has nothing to
    step from without one. Read as a roll, the completion would raise on the
    missing anchor and the item could never be ticked off."""
    calendar = FakeTodoCalendar(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nBEGIN:VTODO\r\n"
        "UID:t1\r\nDTSTAMP:20260101T000000Z\r\nSUMMARY:Water plants\r\n"
        "RRULE:FREQ=WEEKLY\r\nEND:VTODO\r\nEND:VCALENDAR\r\n"
    )

    update_todo(calendar, "t1", {"summary": "Water plants", "status": "COMPLETED"})

    assert str(calendar.todo.stored()["STATUS"]) == "COMPLETED"
