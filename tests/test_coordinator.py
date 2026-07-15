"""Tests for the read path: mapping CalDAV events onto Home Assistant events."""

from datetime import UTC, date, datetime, timedelta

from homeassistant.components.todo import TodoItemStatus
from homeassistant.util import dt as dt_util
import pytest
import vobject

from custom_components.ha_caldav.coordinator import (
    get_attr_value,
    get_end_date,
    is_all_day,
    is_over,
    sort_key,
    to_event,
    to_local,
    to_todo,
)


def vevent(body: str):
    """Build a real vobject VEVENT so the tests see the actual attribute shape."""
    ics = (
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//test//EN\n"
        f"BEGIN:VEVENT\nUID:test-1\nDTSTAMP:20260101T000000Z\n{body}\nEND:VEVENT\n"
        "END:VCALENDAR\n"
    )
    return vobject.readOne(ics).vevent


def test_get_attr_value_reads_and_tolerates_missing() -> None:
    v = vevent("DTSTART:20260706T090000Z\nDTEND:20260706T100000Z\nSUMMARY:Standup")
    assert get_attr_value(v, "summary") == "Standup"
    assert get_attr_value(v, "location") is None


def test_get_end_date_from_dtend() -> None:
    v = vevent("DTSTART:20260706T090000Z\nDTEND:20260706T100000Z\nSUMMARY:x")
    assert get_end_date(v) == datetime(2026, 7, 6, 10, 0, tzinfo=UTC)


def test_get_end_date_from_duration() -> None:
    v = vevent("DTSTART:20260706T090000Z\nDURATION:PT90M\nSUMMARY:x")
    assert get_end_date(v) == datetime(2026, 7, 6, 10, 30, tzinfo=UTC)


def test_get_end_date_without_dtend_or_duration() -> None:
    v = vevent("DTSTART:20260706T090000Z\nSUMMARY:x")
    assert get_end_date(v) == datetime(2026, 7, 7, 9, 0, tzinfo=UTC)


def test_get_end_date_bumps_zero_length_all_day() -> None:
    # A single all-day event where the server reports dtend == dtstart must
    # still cover the whole day, otherwise it would be zero length.
    v = vevent("DTSTART;VALUE=DATE:20260706\nDTEND;VALUE=DATE:20260706\nSUMMARY:x")
    assert get_end_date(v) == date(2026, 7, 7)


def test_is_all_day() -> None:
    timed = vevent("DTSTART:20260706T090000Z\nDTEND:20260706T100000Z\nSUMMARY:x")
    allday = vevent("DTSTART;VALUE=DATE:20260706\nDTEND;VALUE=DATE:20260707\nSUMMARY:x")
    assert not is_all_day(timed)
    assert is_all_day(allday)


def test_to_local_leaves_dates_alone() -> None:
    assert to_local(date(2026, 7, 6)) == date(2026, 7, 6)
    assert isinstance(to_local(datetime(2026, 7, 6, 9, 0, tzinfo=UTC)), datetime)


def test_is_over_timed_event() -> None:
    # utcnow, because the literals below are written with a trailing Z; the
    # default timezone varies with whichever test touched hass last.
    past = dt_util.utcnow() - timedelta(hours=2)
    future = dt_util.utcnow() + timedelta(hours=2)

    def window(start):
        end = start + timedelta(hours=1)
        return vevent(
            f"DTSTART:{start:%Y%m%dT%H%M%S}Z\nDTEND:{end:%Y%m%dT%H%M%S}Z\nSUMMARY:x"
        )

    over = window(past)
    upcoming = window(future)
    assert is_over(over)
    assert not is_over(upcoming)


@pytest.mark.parametrize(
    ("offset_days", "expected"),
    [(-5, True), (5, False)],
)
def test_is_over_all_day_event(offset_days: int, expected: bool) -> None:
    # Home Assistant core compares an all-day date against a datetime here and
    # would raise TypeError; this asserts our per-type comparison instead.
    day = dt_util.now().date() + timedelta(days=offset_days)
    v = vevent(
        f"DTSTART;VALUE=DATE:{day:%Y%m%d}\n"
        f"DTEND;VALUE=DATE:{day + timedelta(days=1):%Y%m%d}\nSUMMARY:x"
    )
    assert is_over(v) is expected


def test_sort_key_orders_dates_and_datetimes_together() -> None:
    # The server may return results in any order and may mix all-day and timed
    # events; both must sort by their actual start.
    allday = vevent("DTSTART;VALUE=DATE:20260707\nDTEND;VALUE=DATE:20260708\nSUMMARY:x")
    earlier = vevent("DTSTART:20260706T090000Z\nDTEND:20260706T100000Z\nSUMMARY:x")
    later = vevent("DTSTART:20260708T090000Z\nDTEND:20260708T100000Z\nSUMMARY:x")

    ordered = sorted([later, allday, earlier], key=sort_key)
    assert ordered == [earlier, allday, later]
    v = vevent(
        "DTSTART:20260706T090000Z\nDTEND:20260706T100000Z\n"
        "SUMMARY:Standup\nLOCATION:Office\nDESCRIPTION:Daily sync"
    )
    event = to_event(v)
    assert event.summary == "Standup"
    assert event.location == "Office"
    assert event.description == "Daily sync"
    assert event.uid == "test-1"
    assert event.recurrence_id is None
    assert event.start == datetime(2026, 7, 6, 9, 0, tzinfo=UTC)


def test_to_event_without_summary() -> None:
    v = vevent("DTSTART:20260706T090000Z\nDTEND:20260706T100000Z")
    assert to_event(v).summary == ""


def test_to_event_all_day_keeps_dates() -> None:
    v = vevent("DTSTART;VALUE=DATE:20260706\nDTEND;VALUE=DATE:20260707\nSUMMARY:Plants")
    event = to_event(v)
    assert event.start == date(2026, 7, 6)
    assert event.end == date(2026, 7, 7)


def test_to_event_stringifies_recurrence_id() -> None:
    v = vevent(
        "DTSTART:20260713T090000Z\nDTEND:20260713T100000Z\n"
        "RECURRENCE-ID:20260713T090000Z\nSUMMARY:Standup"
    )
    # The string form is what Home Assistant hands back to update/delete, so it
    # has to stay parseable by api.parse_recurrence_id.
    assert to_event(v).recurrence_id == "2026-07-13 09:00:00+00:00"


def vtodo(body: str):
    """Build a real vobject VTODO."""
    ics = (
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//test//EN\n"
        f"BEGIN:VTODO\nUID:todo-1\nDTSTAMP:20260101T000000Z\n{body}\nEND:VTODO\n"
        "END:VCALENDAR\n"
    )
    return vobject.readOne(ics).vtodo


def test_to_todo_maps_every_field() -> None:
    item = to_todo(
        vtodo("SUMMARY:Buy milk\nDESCRIPTION:Two litres\nDUE;VALUE=DATE:20260710")
    )
    assert item.uid == "todo-1"
    assert item.summary == "Buy milk"
    assert item.description == "Two litres"
    assert item.due == date(2026, 7, 10)
    assert item.status is TodoItemStatus.NEEDS_ACTION


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("NEEDS-ACTION", TodoItemStatus.NEEDS_ACTION),
        ("IN-PROCESS", TodoItemStatus.NEEDS_ACTION),
        ("COMPLETED", TodoItemStatus.COMPLETED),
        ("CANCELLED", TodoItemStatus.COMPLETED),
    ],
)
def test_to_todo_folds_four_caldav_states_into_two(status, expected) -> None:
    assert to_todo(vtodo(f"SUMMARY:x\nSTATUS:{status}")).status is expected


def test_to_todo_defaults_unknown_status_to_needs_action() -> None:
    assert (
        to_todo(vtodo("SUMMARY:x\nSTATUS:BOGUS")).status is TodoItemStatus.NEEDS_ACTION
    )


def test_to_todo_without_due() -> None:
    assert to_todo(vtodo("SUMMARY:x")).due is None


def test_to_todo_skips_items_home_assistant_cannot_address() -> None:
    # No summary means nothing to render; the item is dropped rather than
    # surfaced as a blank row.
    assert to_todo(vtodo("STATUS:NEEDS-ACTION")) is None


def test_recurrence_id_round_trips_into_the_write_path() -> None:
    from custom_components.ha_caldav.api import parse_recurrence_id

    timed = vevent(
        "DTSTART:20260713T090000Z\nDTEND:20260713T100000Z\n"
        "RECURRENCE-ID:20260713T090000Z\nSUMMARY:x"
    )
    allday = vevent(
        "DTSTART;VALUE=DATE:20260708\nDTEND;VALUE=DATE:20260709\n"
        "RECURRENCE-ID;VALUE=DATE:20260708\nSUMMARY:x"
    )
    assert parse_recurrence_id(to_event(timed).recurrence_id) == datetime(
        2026, 7, 13, 9, 0, tzinfo=UTC
    )
    assert parse_recurrence_id(to_event(allday).recurrence_id) == date(2026, 7, 8)
