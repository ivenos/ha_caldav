"""Tests for the read path: mapping CalDAV events onto Home Assistant events."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from homeassistant.components.todo import TodoItemStatus
from homeassistant.util import dt as dt_util
import pytest
import vobject

from custom_components.ha_caldav.coordinator import (
    calendar_unique_id,
    component_of,
    components_of,
    get_attr_value,
    get_end_date,
    is_all_day,
    is_over,
    sort_key,
    to_event,
    to_local,
    to_todo,
    todo_unique_id,
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


def test_a_timed_event_without_an_end_lasts_no_time() -> None:
    # RFC 5545 3.6.1: a DATE-TIME start with neither DTEND nor DURATION "ends
    # on the same calendar date and time of day". The one-day fallback core
    # applies to both value types left every zero-length marker another client
    # writes holding the entity on for twenty-four hours.
    v = vevent("DTSTART:20260706T090000Z\nSUMMARY:x")
    assert get_end_date(v) == datetime(2026, 7, 6, 9, 0, tzinfo=UTC)


def test_an_all_day_event_without_an_end_lasts_the_day() -> None:
    v = vevent("DTSTART;VALUE=DATE:20260706\nSUMMARY:x")
    assert get_end_date(v) == date(2026, 7, 7)


def test_an_occurrence_in_the_spring_forward_gap_is_shown(berlin) -> None:
    """Both ends of an expanded occurrence carry the same zone object, and
    Python compares two such datetimes by wall clock, so the 45 minutes survive
    even though the instants are reversed. A repair here fired for nothing and
    cost the mixed-zone objects below their place on the calendar."""
    v = vevent(
        "DTSTART;TZID=America/New_York:20260308T023000\n"
        "DTEND;TZID=America/New_York:20260308T031500\nSUMMARY:Backup"
    )
    assert to_event(v) is not None
    assert get_end_date(v) - v.dtstart.value == timedelta(minutes=45)


def test_an_event_whose_ends_disagree_about_a_zone_is_still_shown(berlin) -> None:
    """Another client is free to write a floating start against a zoned end.
    to_local settles both before they are ever compared; comparing them any
    earlier raises TypeError, and the event disappears from the calendar
    without a word."""
    v = vevent(
        "DTSTART:20260710T100000\n"
        "DTEND;TZID=Europe/Berlin:20260710T113000\nSUMMARY:Gemischt"
    )
    event = to_event(v)

    assert event is not None
    assert event.end - event.start == timedelta(minutes=90)


def test_an_end_stored_before_its_start_is_dropped() -> None:
    # Malformed rather than ambiguous: core refuses it, and one such object
    # must not take the collection down.
    v = vevent("DTSTART:20260706T090000Z\nDTEND:20260706T080000Z\nSUMMARY:x")
    assert to_event(v) is None


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


@pytest.fixture
def berlin():
    """Pin the local zone, so a conversion into it is observable at all."""
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(ZoneInfo("Europe/Berlin"))
    yield
    dt_util.set_default_time_zone(previous)


def test_to_local_leaves_dates_alone() -> None:
    assert to_local(date(2026, 7, 6)) == date(2026, 7, 6)


def test_to_local_moves_a_datetime_into_the_local_zone(berlin) -> None:
    converted = to_local(datetime(2026, 7, 6, 9, 0, tzinfo=UTC))

    # Berlin is two hours ahead in July. Comparing instants would pass either
    # way, so the wall clock is what gets asserted.
    assert converted.hour == 11
    assert converted.utcoffset() == timedelta(hours=2)


def test_to_event_hands_back_local_times(berlin) -> None:
    v = vevent("DTSTART:20260706T090000Z\nDTEND:20260706T100000Z\nSUMMARY:x")

    event = to_event(v)

    assert (event.start.hour, event.end.hour) == (11, 12)


def test_to_todo_hands_back_a_local_due_time(berlin) -> None:
    item = to_todo(vtodo("SUMMARY:Buy milk\nDUE:20260710T090000Z"))

    assert item.due.hour == 11


def test_to_event_skips_an_event_core_would_refuse() -> None:
    # Another client can leave an end before its start. Mapping it raises, and
    # an unhandled raise here takes the whole collection down on every poll.
    v = vevent("DTSTART:20260706T100000Z\nDTEND:20260706T090000Z\nSUMMARY:Backwards")

    assert to_event(v) is None


def test_is_over_counts_an_event_that_just_ended(berlin) -> None:
    ended = dt_util.utcnow() - timedelta(seconds=1)
    v = vevent(
        f"DTSTART:{ended - timedelta(hours=1):%Y%m%dT%H%M%S}Z\n"
        f"DTEND:{ended:%Y%m%dT%H%M%S}Z\nSUMMARY:x"
    )

    assert is_over(v)


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


def test_to_event_maps_the_core_fields() -> None:
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
    # has to stay parseable by recurrence.parse_recurrence_id.
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


@pytest.mark.parametrize(
    "body",
    [
        "DTSTART:20260101T100000Z\nDTEND:99991231T235959Z",
        "DTSTART:20260101T100000Z\nDURATION:P999999999D",
    ],
)
def test_an_end_no_datetime_can_hold_is_skipped_not_raised(berlin, body: str) -> None:
    # Adding a zone offset to a date near the year 9999 overflows, and
    # OverflowError is an ArithmeticError, which the mapping guard did not
    # name. One such object anywhere in the window failed every poll and took
    # the calendar entity and the to-do list of the collection down with it,
    # indefinitely and without naming itself in the log. Only east of UTC: a
    # zone behind it moves such a date away from the limit rather than past it.
    v = vevent(f"{body}\nSUMMARY:Office open")
    with pytest.raises(OverflowError):
        # The failure is real, so the guard cannot quietly stop being needed.
        dt_util.as_local(get_end_date(v))
    assert to_event(v) is None


def test_a_due_date_no_datetime_can_hold_is_skipped_not_raised(berlin) -> None:
    # Nothing bounds the to-do search by window, so unlike an event this one
    # cannot even be escaped by paging away from it.
    assert to_todo(vtodo("UID:t\nSUMMARY:Renew passport\nDUE:99991231T235959Z")) is None


def test_recurrence_id_round_trips_into_the_write_path() -> None:
    from custom_components.ha_caldav.recurrence import parse_recurrence_id

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


class _Item:
    """A search result whose body is only parsed when it is read."""

    def __init__(self, ics: str) -> None:
        self._ics = ics
        self.props: dict = {}

    @property
    def vobject_instance(self):
        return vobject.readOne(self._ics)


def _calendar_ics(*bodies: str) -> str:
    events = "".join(
        f"BEGIN:VEVENT\nUID:test-{n}\nDTSTAMP:20260101T000000Z\n{body}\nEND:VEVENT\n"
        for n, body in enumerate(bodies)
    )
    return f"BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//t//EN\n{events}END:VCALENDAR\n"


def test_components_of_reads_every_occurrence_of_an_expanded_object() -> None:
    # The poll asks caldav not to split, so one object carries them all.
    item = _Item(
        _calendar_ics(
            "DTSTART:20260706T090000Z\nDTEND:20260706T100000Z\nSUMMARY:a",
            "DTSTART:20260713T090000Z\nDTEND:20260713T100000Z\nSUMMARY:b",
        )
    )
    assert [v.summary.value for v in components_of(item, "vevent")] == ["a", "b"]


def test_components_of_skips_an_object_that_cannot_be_parsed() -> None:
    assert components_of(_Item("<html>503 backend down</html>"), "vevent") == []


def test_component_of_takes_the_first_and_tolerates_an_empty_object() -> None:
    item = _Item(_calendar_ics("DTSTART:20260706T090000Z\nSUMMARY:a"))
    assert component_of(item, "vevent").summary.value == "a"
    assert component_of(item, "vtodo") is None


def test_the_unique_id_shapes_are_pinned() -> None:
    # Changing either shape orphans every entity already in the registry, and
    # services.py reverses the calendar one to find a calendar from an entity.
    assert calendar_unique_id("entry", "https://dav/personal") == "entry-/personal"
    assert todo_unique_id("entry", "https://dav/personal") == "entry-/personal-todo"


@pytest.mark.parametrize(
    "url",
    [
        "http://dav.example.com/cal/personal/",
        "https://dav.example.com/cal/personal/",
        "https://dav.example.com:443/cal/personal",
        "https://DAV.example.com/cal/personal/",
    ],
)
def test_the_unique_id_survives_the_move_reconfigure_exists_to_make(url: str) -> None:
    # The one thing the reconfigure step is for is changing the address, and
    # the calendar urls come back carrying it. Keyed on the url as it stands,
    # every entity of the account was renamed to _2 on the first such change
    # and the originals left permanently unavailable.
    assert calendar_unique_id("entry", url) == "entry-/cal/personal"


def test_an_event_ending_exactly_now_counts_as_over() -> None:
    # The boundary is the only place the comparison operator shows at all, and
    # an event ending this instant must not still be the upcoming one.
    from unittest.mock import patch

    now = dt_util.now().replace(microsecond=0)
    stamp = now.astimezone(UTC)
    body = f"DTSTART:{stamp:%Y%m%dT%H%M%S}Z\nDTEND:{stamp:%Y%m%dT%H%M%S}Z"
    with patch("custom_components.ha_caldav.coordinator.dt_util.now", return_value=now):
        assert is_over(vevent(body))


def test_an_all_day_event_whose_exclusive_end_is_today_counts_as_over() -> None:
    # DTEND is exclusive, so an all-day event ending today finished yesterday.
    today = dt_util.now().date()
    yesterday = today - timedelta(days=1)
    body = f"DTSTART;VALUE=DATE:{yesterday:%Y%m%d}\nDTEND;VALUE=DATE:{today:%Y%m%d}"

    assert is_over(vevent(body))


def test_a_uid_cache_is_bounded() -> None:
    """A window the panel asked for has no later poll to evict what it cached."""
    from custom_components.ha_caldav.coordinator import _CACHE_LIMIT, _bounded

    cache = {f"uid-{n}": f'"e{n}"' for n in range(_CACHE_LIMIT + 10)}

    trimmed = _bounded(cache)

    assert len(trimmed) == _CACHE_LIMIT
    # From the far end: callers put the window they just read at the front, so
    # what goes is what has been out of view longest. Trimmed from the front
    # instead, the same leading uids were dropped on every poll and never got
    # an etag again, and their next edit went out with nothing to check.
    assert "uid-0" in trimmed
    assert f"uid-{_CACHE_LIMIT + 9}" not in trimmed


def test_a_freshly_read_window_survives_a_cache_at_its_limit() -> None:
    """Trimmed the other way round, the same leading slice went on every poll.

    Those uids never regained an etag, so every edit of one of them was written
    with nothing to check against, silently, and only on the large calendars
    where a clash is likeliest.
    """
    from custom_components.ha_caldav.coordinator import _CACHE_LIMIT, _bounded

    old = {f"old-{n}": f'"o{n}"' for n in range(_CACHE_LIMIT)}
    window = {f"new-{n}": f'"n{n}"' for n in range(10)}

    trimmed = _bounded(window | old)

    assert all(uid in trimmed for uid in window)


def test_the_rule_is_read_off_the_master_not_whichever_component_came_first() -> None:
    """RFC 5545 leaves the order open, and a detached instance carries no rule."""
    from custom_components.ha_caldav.coordinator import master_of

    class _Item:
        pass

    item = _Item()
    item.vobject_instance = vobject.readOne(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//EN\n"
        "BEGIN:VEVENT\nUID:series-1\nRECURRENCE-ID:20260713T090000Z\n"
        "DTSTAMP:20260101T000000Z\nDTSTART:20260713T110000Z\nSUMMARY:Moved\n"
        "END:VEVENT\n"
        "BEGIN:VEVENT\nUID:series-1\nDTSTAMP:20260101T000000Z\n"
        "DTSTART:20260706T090000Z\nRRULE:FREQ=WEEKLY\nSUMMARY:Standup\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )

    assert master_of(item).rrule.value == "FREQ=WEEKLY"
