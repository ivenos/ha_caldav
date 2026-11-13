from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

from conftest import stored
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_component import EntityComponent
import pytest
from pytest_homeassistant.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)
from requests import Timeout
import vobject

from custom_components.ha_caldav.const import CONF_BUSY_ONLY, DOMAIN, EVENT_REMINDER
from custom_components.ha_caldav.coordinator import _MAX_KEPT_POLLS, occurrences

ENTRY_DATA = {
    CONF_URL: "https://cloud.example.com/remote.php/dav",
    CONF_USERNAME: "iven",
    CONF_PASSWORD: "secret",
    CONF_VERIFY_SSL: True,
}

NOW = datetime(2026, 7, 6, 8, 0, tzinfo=UTC)


def _event(body: str, uid: str = "uid-1"):
    return stored(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//test//EN\n"
        f"BEGIN:VEVENT\nUID:{uid}\nDTSTAMP:20260101T000000Z\nSUMMARY:Standup\n"
        f"{body}\nEND:VEVENT\nEND:VCALENDAR\n"
    )


def _todo(body: str):
    return stored(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//test//EN\n"
        "BEGIN:VTODO\nUID:todo-1\nDTSTAMP:20260101T000000Z\nSUMMARY:Bins\n"
        f"{body}\nEND:VTODO\nEND:VCALENDAR\n"
    )


def _alarm(trigger: str, extra: str = "") -> str:
    return (
        f"BEGIN:VALARM\nACTION:DISPLAY\nDESCRIPTION:Soon\n{trigger}\n{extra}END:VALARM"
    )


def _calendar(events=(), todos=()) -> Mock:
    calendar = Mock()
    calendar.name = "Personal"
    calendar.url = "https://cloud.example.com/remote.php/dav/Personal"
    calendar.held = {"events": list(events), "todos": list(todos)}
    calendar.search.side_effect = lambda **kwargs: list(
        calendar.held["todos" if kwargs.get("todo") else "events"]
    )
    return calendar


def _server(*bodies: str) -> Mock:
    """A calendar whose time-range search returns only what overlaps the range."""
    calendar = _calendar()

    def overlaps(body: str, start: datetime, end: datetime) -> bool:
        vevent = vobject.readOne(_event(body).data).vevent
        return vevent.dtstart.value < end and vevent.dtend.value > start

    calendar.search.side_effect = lambda **kwargs: [
        _event(body)
        for body in bodies
        if not kwargs.get("todo") and overlaps(body, kwargs["start"], kwargs["end"])
    ]
    return calendar


async def _setup(
    hass: HomeAssistant, calendar: Mock, options: dict | None = None
) -> list:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="iven",
        data=ENTRY_DATA,
        options=options or {},
        unique_id="x",
    )
    entry.add_to_hass(hass)
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = [calendar]
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return async_capture_events(hass, EVENT_REMINDER)


async def _move_to(hass: HomeAssistant, freezer, moment: datetime) -> None:
    freezer.move_to(moment)
    async_fire_time_changed(hass, moment)
    await hass.async_block_till_done()


async def _poll(hass: HomeAssistant) -> None:
    """Read the calendar now, whenever the harness would have run the poll."""
    component: EntityComponent = hass.data["entity_components"]["calendar"]
    await component.get_entity("calendar.personal").coordinator.async_refresh()
    await hass.async_block_till_done()


@pytest.fixture(autouse=True)
def frozen(freezer):
    freezer.move_to(NOW)


async def test_an_alarm_fires_an_event_when_it_comes_due(
    hass: HomeAssistant, freezer
) -> None:
    calendar = _calendar(
        events=[
            _event(
                "DTSTART:20260706T100000Z\nDTEND:20260706T110000Z\n"
                "LOCATION:Office\n" + _alarm("TRIGGER:-PT15M")
            )
        ]
    )
    fired = await _setup(hass, calendar)

    await _move_to(hass, freezer, NOW + timedelta(minutes=104))
    assert fired == []

    await _move_to(hass, freezer, NOW + timedelta(minutes=105))

    [event] = fired
    assert event.data["entity_id"] == "calendar.personal"
    assert event.data["uid"] == "uid-1"
    assert event.data["summary"] == "Standup"
    assert event.data["location"] == "Office"
    assert datetime.fromisoformat(event.data["start"]) == NOW + timedelta(hours=2)
    assert event.data["alarm"] == {
        "minutes_before": 15,
        "action": "DISPLAY",
        "description": "Soon",
    }


async def test_an_alarm_of_a_series_names_its_occurrence_and_its_end(
    hass: HomeAssistant, freezer
) -> None:
    calendar = _calendar(
        events=[
            _event(
                "DTSTART:20260706T100000Z\nDTEND:20260706T110000Z\nRRULE:FREQ=DAILY\n"
                + _alarm("TRIGGER:-PT15M")
            )
        ]
    )
    fired = await _setup(hass, calendar)

    await _move_to(hass, freezer, NOW + timedelta(minutes=105))

    [event] = fired
    assert event.data["recurrence_id"] == "2026-07-06 10:00:00+00:00"
    assert datetime.fromisoformat(event.data["end"]) == NOW + timedelta(hours=3)


async def test_an_alarm_one_week_before_fires_with_the_default_days(
    hass: HomeAssistant, freezer
) -> None:
    calendar = _server(
        "DTSTART:20260713T100000Z\nDTEND:20260713T110000Z\n" + _alarm("TRIGGER:-P7D")
    )
    fired = await _setup(hass, calendar)

    due = datetime(2026, 7, 6, 10, 0, tzinfo=UTC)
    for moment in (due - timedelta(minutes=1), due, due + timedelta(minutes=15)):
        await _move_to(hass, freezer, moment)

    assert [event.data["uid"] for event in fired] == ["uid-1"]


async def test_only_a_series_with_an_alarm_is_expanded_past_the_days_ahead(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar(
        events=[
            _event(
                "DTSTART:20260706T100000Z\nDTEND:20260706T110000Z\nRRULE:FREQ=DAILY",
                uid="plain",
            ),
            _event(
                "DTSTART:20260706T120000Z\nDTEND:20260706T130000Z\nRRULE:FREQ=DAILY\n"
                + _alarm("TRIGGER:-PT15M"),
                uid="alarmed",
            ),
        ]
    )

    with patch(
        "custom_components.ha_caldav.coordinator.occurrences", wraps=occurrences
    ) as expand:
        await _setup(hass, calendar)

    spans = {
        str(call.args[0].icalendar_component["UID"]): call.args[2] - call.args[1]
        for call in expand.call_args_list
    }
    assert spans == {
        "plain": timedelta(days=7, hours=28),
        "alarmed": timedelta(days=35, hours=28),
    }


async def test_an_alarm_fires_once_however_often_the_calendar_is_polled(
    hass: HomeAssistant, freezer
) -> None:
    calendar = _calendar(
        events=[
            _event(
                "DTSTART:20260706T100000Z\nDTEND:20260706T110000Z\n"
                + _alarm("TRIGGER:-PT15M")
            )
        ]
    )
    fired = await _setup(hass, calendar)

    for minutes in (105, 121, 137):
        await _move_to(hass, freezer, NOW + timedelta(minutes=minutes))
        await _poll(hass)

    assert len(fired) == 1


async def test_an_alarm_a_day_before_keeps_the_wall_clock_across_a_clock_change(
    hass: HomeAssistant, freezer
) -> None:
    freezer.move_to(datetime(2026, 10, 24, 6, 0, tzinfo=UTC))
    calendar = _calendar(
        events=[
            _event(
                "DTSTART;TZID=Europe/Berlin:20261025T090000\n"
                "DTEND;TZID=Europe/Berlin:20261025T100000\n" + _alarm("TRIGGER:-P1D")
            )
        ]
    )
    fired = await _setup(hass, calendar)

    await _move_to(hass, freezer, datetime(2026, 10, 24, 6, 59, tzinfo=UTC))
    assert fired == []

    await _move_to(hass, freezer, datetime(2026, 10, 24, 7, 0, tzinfo=UTC))
    assert len(fired) == 1


async def test_two_occurrences_alarming_at_the_same_moment_both_remind(
    hass: HomeAssistant, freezer
) -> None:
    calendar = _calendar(
        events=[
            _event(
                "DTSTART:20260707T090000Z\nDTEND:20260707T093000Z\n"
                "RRULE:FREQ=DAILY;COUNT=3\n"
                + _alarm("TRIGGER:PT0S")
                + "\n"
                + _alarm("TRIGGER:-P1D")
            )
        ]
    )
    fired = await _setup(hass, calendar)

    await _move_to(hass, freezer, datetime(2026, 7, 7, 9, 0, tzinfo=UTC))

    assert sorted(event.data["recurrence_id"] for event in fired) == [
        "2026-07-07 09:00:00+00:00",
        "2026-07-08 09:00:00+00:00",
    ]


async def test_an_alarm_that_came_due_before_the_start_is_not_replayed(
    hass: HomeAssistant, freezer
) -> None:
    calendar = _calendar(
        events=[
            _event(
                "DTSTART:20260706T080500Z\nDTEND:20260706T090000Z\n"
                + _alarm("TRIGGER:-PT15M")
            )
        ]
    )
    fired = await _setup(hass, calendar)

    await _move_to(hass, freezer, NOW + timedelta(minutes=16))

    assert fired == []
    assert hass.states.get("event.personal_reminders").state == "unknown"


async def test_an_alarm_found_a_poll_late_still_fires(
    hass: HomeAssistant, freezer
) -> None:
    calendar = _calendar()
    fired = await _setup(hass, calendar)
    await _move_to(hass, freezer, NOW + timedelta(minutes=16))
    calendar.held["events"] = [
        _event(
            "DTSTART:20260706T084000Z\nDTEND:20260706T090000Z\n"
            + _alarm("TRIGGER:-PT15M")
        )
    ]
    calendar.objects_by_sync_token.return_value.sync_token = "moved"

    await _move_to(hass, freezer, NOW + timedelta(minutes=32))
    await _poll(hass)
    await _poll(hass)

    assert [event.data["uid"] for event in fired] == ["uid-1"]


async def test_an_alarm_at_a_fixed_time_fires_once_for_a_whole_series(
    hass: HomeAssistant, freezer
) -> None:
    calendar = _calendar(
        events=[
            _event(
                "DTSTART:20260706T100000Z\nDTEND:20260706T110000Z\nRRULE:FREQ=DAILY\n"
                + _alarm("TRIGGER;VALUE=DATE-TIME:20260706T083000Z")
            )
        ]
    )
    fired = await _setup(hass, calendar)

    await _move_to(hass, freezer, NOW + timedelta(minutes=30))

    [event] = fired
    assert event.data["alarm"]["action"] == "DISPLAY"
    assert "minutes_before" not in event.data["alarm"]


async def test_an_alarm_counted_from_the_end_fires_then(
    hass: HomeAssistant, freezer
) -> None:
    calendar = _calendar(
        events=[
            _event(
                "DTSTART:20260706T083000Z\nDTEND:20260706T090000Z\n"
                + _alarm("TRIGGER;RELATED=END:-PT10M")
            )
        ]
    )
    fired = await _setup(hass, calendar)

    await _move_to(hass, freezer, NOW + timedelta(minutes=49))
    assert fired == []

    await _move_to(hass, freezer, NOW + timedelta(minutes=50))
    assert len(fired) == 1


async def test_a_repeating_alarm_fires_each_time(hass: HomeAssistant, freezer) -> None:
    calendar = _calendar(
        events=[
            _event(
                "DTSTART:20260706T090000Z\nDTEND:20260706T100000Z\n"
                + _alarm("TRIGGER:-PT30M", "REPEAT:2\nDURATION:PT5M\n")
            )
        ]
    )
    fired = await _setup(hass, calendar)

    told = []
    for minutes in (30, 35, 40, 45):
        await _move_to(hass, freezer, NOW + timedelta(minutes=minutes))
        told.append(len(fired))

    assert told == [1, 2, 3, 3]


async def test_a_to_do_alarm_fires_from_its_list(hass: HomeAssistant, freezer) -> None:
    calendar = _calendar(
        todos=[_todo("DUE:20260706T090000Z\n" + _alarm("TRIGGER;RELATED=END:-PT30M"))]
    )
    fired = await _setup(hass, calendar)

    await _move_to(hass, freezer, NOW + timedelta(minutes=30))

    [event] = fired
    assert event.data["entity_id"] == "todo.personal"
    assert event.data["uid"] == "todo-1"
    assert event.data["summary"] == "Bins"
    assert datetime.fromisoformat(event.data["due"]) == NOW + timedelta(hours=1)


async def test_a_to_do_occurrence_moved_elsewhere_reminds_at_its_new_time(
    hass: HomeAssistant, freezer
) -> None:
    alarm = _alarm("TRIGGER;RELATED=END:-PT30M")
    series = stored(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//test//EN\n"
        "BEGIN:VTODO\nUID:todo-1\nDTSTAMP:20260101T000000Z\nSUMMARY:Bins\n"
        f"DUE:20260706T090000Z\nRRULE:FREQ=WEEKLY\n{alarm}\nEND:VTODO\n"
        "BEGIN:VTODO\nUID:todo-1\nDTSTAMP:20260101T000000Z\nSUMMARY:Bins\n"
        f"RECURRENCE-ID:20260706T090000Z\nDUE:20260706T100000Z\n{alarm}\n"
        "END:VTODO\nEND:VCALENDAR\n"
    )
    fired = await _setup(hass, _calendar(todos=[series]))

    await _move_to(hass, freezer, NOW + timedelta(minutes=30))
    assert fired == []

    await _move_to(hass, freezer, NOW + timedelta(minutes=90))
    [event] = fired
    assert datetime.fromisoformat(event.data["due"]) == NOW + timedelta(hours=2)


async def test_an_untitled_to_do_reminds_too(hass: HomeAssistant, freezer) -> None:
    untitled = stored(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//test//EN\nBEGIN:VTODO\n"
        "UID:todo-1\nDTSTAMP:20260101T000000Z\nDUE:20260706T090000Z\n"
        f"{_alarm('TRIGGER;RELATED=END:-PT30M')}\nEND:VTODO\nEND:VCALENDAR\n"
    )
    fired = await _setup(hass, _calendar(todos=[untitled]))

    await _move_to(hass, freezer, NOW + timedelta(minutes=30))

    [event] = fired
    assert event.data["uid"] == "todo-1"
    assert event.data["summary"] == ""


async def test_a_to_do_alarm_a_day_before_keeps_the_wall_clock_across_a_clock_change(
    hass: HomeAssistant, freezer
) -> None:
    freezer.move_to(datetime(2026, 10, 24, 17, 0, tzinfo=UTC))
    calendar = _calendar(
        todos=[
            _todo(
                "DUE;TZID=Europe/Berlin:20261025T200000\n"
                + _alarm("TRIGGER;RELATED=END:-P1D")
            )
        ]
    )
    fired = await _setup(hass, calendar)

    await _move_to(hass, freezer, datetime(2026, 10, 24, 17, 59, tzinfo=UTC))
    assert fired == []

    await _move_to(hass, freezer, datetime(2026, 10, 24, 18, 0, tzinfo=UTC))
    assert len(fired) == 1


async def test_an_offset_on_a_to_do_without_a_start_counts_from_its_due_date(
    hass: HomeAssistant, freezer
) -> None:
    calendar = _calendar(
        todos=[_todo("DUE:20260706T090000Z\n" + _alarm("TRIGGER:-PT30M"))]
    )
    fired = await _setup(hass, calendar)

    await _move_to(hass, freezer, NOW + timedelta(minutes=30))

    assert len(fired) == 1


async def test_a_completed_to_do_does_not_remind(hass: HomeAssistant, freezer) -> None:
    calendar = _calendar(
        todos=[
            _todo(
                "DUE:20260706T090000Z\nSTATUS:COMPLETED\n"
                + _alarm("TRIGGER;RELATED=END:-PT30M")
            )
        ]
    )
    fired = await _setup(hass, calendar)

    await _move_to(hass, freezer, NOW + timedelta(minutes=30))

    assert fired == []


async def test_an_unloaded_account_stops_reminding(
    hass: HomeAssistant, freezer
) -> None:
    calendar = _calendar(
        events=[
            _event(
                "DTSTART:20260706T100000Z\nDTEND:20260706T110000Z\n"
                + _alarm("TRIGGER:-PT15M")
            )
        ]
    )
    fired = await _setup(hass, calendar)
    [entry] = hass.config_entries.async_entries(DOMAIN)

    await hass.config_entries.async_unload(entry.entry_id)
    await _move_to(hass, freezer, NOW + timedelta(minutes=105))

    assert fired == []


async def test_an_event_alarm_reaches_the_reminder_entity(
    hass: HomeAssistant, freezer
) -> None:
    calendar = _calendar(
        events=[
            _event(
                "DTSTART:20260706T100000Z\nDTEND:20260706T110000Z\n"
                "LOCATION:Office\n" + _alarm("TRIGGER:-PT15M")
            )
        ]
    )
    await _setup(hass, calendar)
    state = hass.states.get("event.personal_reminders")
    assert state.name == "Personal reminders"
    assert state.state == "unknown"
    assert state.attributes["event_types"] == ["event", "todo"]

    await _move_to(hass, freezer, NOW + timedelta(minutes=105))

    state = hass.states.get("event.personal_reminders")
    assert datetime.fromisoformat(state.state) == NOW + timedelta(minutes=105)
    assert state.attributes["event_type"] == "event"
    assert state.attributes["uid"] == "uid-1"
    assert state.attributes["summary"] == "Standup"
    assert state.attributes["location"] == "Office"
    assert state.attributes["alarm"]["minutes_before"] == 15
    assert "entity_id" not in state.attributes


async def test_a_to_do_alarm_reaches_the_reminder_entity_as_a_to_do(
    hass: HomeAssistant, freezer
) -> None:
    calendar = _calendar(
        todos=[_todo("DUE:20260706T090000Z\n" + _alarm("TRIGGER;RELATED=END:-PT30M"))]
    )
    await _setup(hass, calendar)

    await _move_to(hass, freezer, NOW + timedelta(minutes=30))

    state = hass.states.get("event.personal_reminders")
    assert state.attributes["event_type"] == "todo"
    assert state.attributes["summary"] == "Bins"
    assert datetime.fromisoformat(state.attributes["due"]) == NOW + timedelta(hours=1)


async def test_alarms_due_together_change_the_reminder_entity_once_each(
    hass: HomeAssistant, freezer
) -> None:
    """An automation on the entity runs once per state change."""
    trigger = "DTSTART:20260706T100000Z\nDTEND:20260706T110000Z\n" + _alarm(
        "TRIGGER:-PT15M"
    )
    calendar = _calendar(
        events=[_event(trigger, uid="uid-1"), _event(trigger, uid="uid-2")]
    )
    await _setup(hass, calendar)
    changes = async_capture_events(hass, "state_changed")

    await _move_to(hass, freezer, NOW + timedelta(minutes=105))

    told = [
        change.data["new_state"].attributes["uid"]
        for change in changes
        if change.data["entity_id"] == "event.personal_reminders"
    ]
    assert sorted(told) == ["uid-1", "uid-2"]


async def test_a_calendar_without_to_dos_offers_only_event_reminders(
    hass: HomeAssistant,
) -> None:
    from custom_components.ha_caldav.capability import Capability

    with patch(
        "custom_components.ha_caldav.fetch_capabilities",
        return_value={
            "/remote.php/dav/Personal": Capability(frozenset({"VEVENT"}), True)
        },
    ):
        await _setup(hass, _calendar())

    state = hass.states.get("event.personal_reminders")
    assert state.attributes["event_types"] == ["event"]


async def test_the_reminder_entity_stays_while_one_of_its_halves_is_still_read(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar()
    await _setup(hass, calendar)

    def search(**kwargs):
        if kwargs.get("todo"):
            raise Timeout("gone")
        return []

    calendar.search.side_effect = search
    calendar.objects_by_sync_token.return_value.sync_token = "moved"
    for _ in range(_MAX_KEPT_POLLS + 1):
        await _poll(hass)

    assert hass.states.get("todo.personal").state == STATE_UNAVAILABLE
    assert hass.states.get("event.personal_reminders").state != STATE_UNAVAILABLE


async def test_a_canceled_event_does_not_remind(hass: HomeAssistant, freezer) -> None:
    calendar = _calendar(
        events=[
            _event(
                "DTSTART:20260706T100000Z\nDTEND:20260706T110000Z\n"
                "STATUS:CANCELLED\n" + _alarm("TRIGGER:-PT15M")
            )
        ]
    )
    fired = await _setup(hass, calendar)

    await _move_to(hass, freezer, NOW + timedelta(minutes=105))

    assert fired == []
    assert hass.states.get("event.personal_reminders").state == "unknown"


async def test_a_free_event_does_not_remind_with_only_busy_events(
    hass: HomeAssistant, freezer
) -> None:
    calendar = _calendar(
        events=[
            _event(
                "DTSTART:20260706T100000Z\nDTEND:20260706T110000Z\n"
                "TRANSP:TRANSPARENT\n" + _alarm("TRIGGER:-PT15M")
            )
        ]
    )
    fired = await _setup(hass, calendar, options={CONF_BUSY_ONLY: True})

    await _move_to(hass, freezer, NOW + timedelta(minutes=105))

    assert fired == []
    assert hass.states.get("event.personal_reminders").state == "unknown"


async def test_an_alarm_that_fired_stays_quiet_when_another_is_added_before_it(
    hass: HomeAssistant, freezer
) -> None:
    times = "DTSTART:20260706T100000Z\nDTEND:20260706T110000Z\n"
    calendar = _calendar(events=[_event(times + _alarm("TRIGGER:-PT15M"))])
    fired = await _setup(hass, calendar)
    await _move_to(hass, freezer, datetime(2026, 7, 6, 9, 45, tzinfo=UTC))
    assert len(fired) == 1

    calendar.held["events"] = [
        _event(times + _alarm("TRIGGER:-PT1H") + "\n" + _alarm("TRIGGER:-PT15M"))
    ]
    calendar.objects_by_sync_token.return_value.sync_token = "added"
    await _move_to(hass, freezer, datetime(2026, 7, 6, 9, 47, tzinfo=UTC))
    await _poll(hass)

    assert len(fired) == 1


async def test_an_alarm_that_fired_stays_quiet_after_a_split_renames_its_series(
    hass: HomeAssistant, freezer
) -> None:
    alarm = _alarm("TRIGGER:-PT15M")
    calendar = _calendar(
        events=[
            _event(
                "DTSTART:20260705T100000Z\nDTEND:20260705T110000Z\n"
                "RRULE:FREQ=DAILY\n" + alarm
            )
        ]
    )
    fired = await _setup(hass, calendar)
    await _move_to(hass, freezer, datetime(2026, 7, 6, 9, 45, tzinfo=UTC))
    assert len(fired) == 1

    calendar.held["events"] = [
        _event(
            "DTSTART:20260705T100000Z\nDTEND:20260705T110000Z\n"
            "RRULE:FREQ=DAILY;UNTIL=20260705T100000Z\n" + alarm
        ),
        _event(
            "DTSTART:20260706T100000Z\nDTEND:20260706T110000Z\n"
            "RRULE:FREQ=DAILY\n" + alarm,
            uid="uid-1-20260706T100000Z",
        ),
    ]
    calendar.objects_by_sync_token.return_value.sync_token = "split"
    await _move_to(hass, freezer, datetime(2026, 7, 6, 9, 47, tzinfo=UTC))
    await _poll(hass)

    assert len(fired) == 1


async def test_two_events_with_the_same_alarm_both_remind_when_one_is_found_later(
    hass: HomeAssistant, freezer
) -> None:
    body = "DTSTART:20260706T100000Z\nDTEND:20260706T110000Z\n" + _alarm(
        "TRIGGER:-PT15M"
    )
    calendar = _calendar(events=[_event(body)])
    fired = await _setup(hass, calendar)
    await _move_to(hass, freezer, datetime(2026, 7, 6, 9, 45, tzinfo=UTC))

    calendar.held["events"] = [_event(body), _event(body, uid="uid-2")]
    calendar.objects_by_sync_token.return_value.sync_token = "added"
    await _move_to(hass, freezer, datetime(2026, 7, 6, 9, 47, tzinfo=UTC))
    await _poll(hass)

    assert [event.data["uid"] for event in fired] == ["uid-1", "uid-2"]


async def test_a_repeat_without_a_delay_reminds_once(
    hass: HomeAssistant, freezer
) -> None:
    freezer.move_to(datetime(2026, 7, 6, 8, 0, tzinfo=UTC))
    calendar = _calendar(
        events=[
            _event(
                "DTSTART:20260706T100000Z\nDTEND:20260706T110000Z\n"
                + _alarm("TRIGGER:-PT15M", "REPEAT:100\nDURATION:PT0S\n")
            )
        ]
    )
    fired = await _setup(hass, calendar)

    await _move_to(hass, freezer, datetime(2026, 7, 6, 9, 45, tzinfo=UTC))

    assert len(fired) == 1
