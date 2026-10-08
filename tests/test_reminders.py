from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

from conftest import stored
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_component import EntityComponent
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.ha_caldav.const import DOMAIN, EVENT_REMINDER

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


async def _setup(hass: HomeAssistant, calendar: Mock) -> list:
    entry = MockConfigEntry(domain=DOMAIN, title="iven", data=ENTRY_DATA, unique_id="x")
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

    for minutes in (30, 35, 40, 45):
        await _move_to(hass, freezer, NOW + timedelta(minutes=minutes))

    assert len(fired) == 3


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
