from datetime import UTC, date, datetime
from unittest.mock import Mock, patch

from caldav.lib.error import NotFoundError
from caldav.lib.url import URL
from conftest import dav_calendar, stored
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
from icalendar import Calendar as ICalCalendar
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol

from custom_components.ha_caldav.api import export_ics, import_ics
from custom_components.ha_caldav.capability import Capability
from custom_components.ha_caldav.const import DOMAIN
from custom_components.ha_caldav.errors import Refused
from custom_components.ha_caldav.journal import (
    create_journal,
    delete_journal,
    read_journals,
    update_journal,
)

ENTRY_DATA = {
    CONF_URL: "https://cloud.example.com/remote.php/dav",
    CONF_USERNAME: "iven",
    CONF_PASSWORD: "secret",
    CONF_VERIFY_SSL: True,
}

ENTRY = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VJOURNAL\r\nUID:note-1\r\nDTSTAMP:20260101T000000Z\r\n"
    "DTSTART;VALUE=DATE:20260706\r\nSUMMARY:Monday\r\nDESCRIPTION:Rain\r\n"
    "CATEGORIES:diary\r\nX-CLIENT:kept\r\n"
    "END:VJOURNAL\r\nEND:VCALENDAR\r\n"
)


def _journal(body: str) -> ICalCalendar:
    return ICalCalendar.from_ical(body).walk("VJOURNAL")[0]


def _held(calendar: Mock, ics: str = ENTRY) -> None:
    """Put one entry on a fake calendar, as a real caldav resource."""
    url = "https://dav.test/cal/note-1.ics"
    calendar.client.stored[url] = (ics, '"1"')
    resource = stored(ics, '"1"')
    resource.client = calendar.client
    resource.url = URL(url)
    calendar.journal_by_uid.return_value = resource


def test_a_new_entry_is_written_as_a_journal_of_its_own() -> None:
    calendar = dav_calendar()

    create_journal(
        calendar,
        {
            "summary": "Monday",
            "description": "Rain",
            "start": date(2026, 7, 6),
            "status": "FINAL",
            "categories": ["diary"],
        },
    )

    [body] = calendar.client.bodies
    entry = _journal(body)
    assert entry["SUMMARY"] == "Monday"
    assert entry["DESCRIPTION"] == "Rain"
    assert entry["DTSTART"].dt == date(2026, 7, 6)
    assert entry["STATUS"] == "FINAL"
    assert "BEGIN:VEVENT" not in body
    assert calendar.client.put_headers[0]["If-None-Match"] == "*"


def test_an_entry_dated_to_the_minute_carries_the_definition_of_its_zone() -> None:
    calendar = dav_calendar()
    moment = datetime(2026, 7, 6, 20, 0, tzinfo=dt_util.get_time_zone("Europe/Berlin"))

    create_journal(calendar, {"summary": "Evening", "start": moment})

    [body] = calendar.client.bodies
    assert "DTSTART;TZID=Europe/Berlin:20260706T200000" in body
    assert "BEGIN:VTIMEZONE" in body


def test_an_update_changes_what_it_names_and_keeps_the_rest() -> None:
    calendar = dav_calendar()
    _held(calendar)

    update_journal(calendar, "note-1", {"summary": "Monday evening", "url": None})

    entry = _journal(calendar.client.bodies[-1])
    assert entry["SUMMARY"] == "Monday evening"
    assert entry["DESCRIPTION"] == "Rain"
    assert entry["X-CLIENT"] == "kept"
    assert int(entry["SEQUENCE"]) == 1
    assert calendar.client.put_headers[-1]["If-Match"] == '"1"'


def test_a_field_given_as_nothing_is_cleared() -> None:
    calendar = dav_calendar()
    _held(calendar)

    update_journal(calendar, "note-1", {"description": None, "categories": None})

    entry = _journal(calendar.client.bodies[-1])
    assert "DESCRIPTION" not in entry
    assert "CATEGORIES" not in entry
    assert entry["SUMMARY"] == "Monday"


def test_an_object_holding_no_journal_entry_is_refused() -> None:
    calendar = dav_calendar()
    _held(calendar, ENTRY.replace("VJOURNAL", "VEVENT"))

    with pytest.raises(Refused, match="no_journal_in_object"):
        update_journal(calendar, "note-1", {"summary": "x"})

    assert calendar.client.puts == []


def test_deleting_an_entry_deletes_the_version_read() -> None:
    calendar = dav_calendar()
    _held(calendar)

    delete_journal(calendar, "note-1")

    assert calendar.client.deletes == ["https://dav.test/cal/note-1.ics"]
    assert calendar.client.stored == {}


def test_entries_are_read_with_the_dated_ones_first() -> None:
    calendar = Mock()
    calendar.search.return_value = [
        stored(
            ENTRY.replace("note-1", "undated").replace(
                "DTSTART;VALUE=DATE:20260706\r\n", ""
            )
        ),
        stored(ENTRY.replace("note-1", "later").replace("20260706", "20260708")),
        stored(ENTRY),
    ]

    journals = read_journals(calendar, None, None)

    assert [entry["uid"] for entry in journals] == ["note-1", "later", "undated"]
    assert journals[0] == {
        "uid": "note-1",
        "summary": "Monday",
        "start": "2026-07-06",
        "description": "Rain",
        "categories": ["diary"],
    }
    assert "start" not in journals[2]
    assert calendar.search.call_args.kwargs == {"journal": True}


def test_a_window_is_handed_to_the_server() -> None:
    calendar = Mock()
    calendar.search.return_value = []
    start, end = datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC)

    read_journals(calendar, start, end)

    assert calendar.search.call_args.kwargs == {
        "journal": True,
        "start": start,
        "end": end,
    }


def test_an_import_writes_a_journal_entry_too() -> None:
    calendar = dav_calendar()
    for lookup in ("event_by_uid", "todo_by_uid", "journal_by_uid"):
        getattr(calendar, lookup).side_effect = NotFoundError("nope")

    assert import_ics(calendar, ENTRY) == ["note-1"]

    assert "BEGIN:VJOURNAL" in calendar.client.bodies[0]


def test_an_export_of_the_whole_calendar_carries_its_journal_entries() -> None:
    calendar = Mock()
    calendar.search.side_effect = lambda **kwargs: (
        [stored(ENTRY)] if kwargs.get("journal") else []
    )

    assert "BEGIN:VJOURNAL" in export_ics(calendar, None)


def test_an_export_finds_a_journal_entry_by_its_uid() -> None:
    calendar = Mock()
    calendar.event_by_uid.side_effect = NotFoundError("nope")
    calendar.todo_by_uid.side_effect = NotFoundError("nope")
    calendar.journal_by_uid.return_value = stored(ENTRY)

    assert "UID:note-1" in export_ics(calendar, "note-1")


def _calendar(name: str) -> Mock:
    calendar = Mock()
    calendar.name = name
    calendar.url = f"https://cloud.example.com/remote.php/dav/{name}"
    calendar.search.return_value = []
    return calendar


async def _setup(hass: HomeAssistant, components: dict[str, set[str]]) -> dict:
    calendars = {name: _calendar(name) for name in components}
    capabilities = {
        f"/remote.php/dav/{name}": Capability(frozenset(kinds), True)
        for name, kinds in components.items()
    }
    entry = MockConfigEntry(domain=DOMAIN, title="iven", data=ENTRY_DATA, unique_id="x")
    entry.add_to_hass(hass)
    with (
        patch("custom_components.ha_caldav.caldav.DAVClient") as client,
        patch(
            "custom_components.ha_caldav.fetch_capabilities", return_value=capabilities
        ),
    ):
        principal = client.return_value.principal.return_value
        principal.calendars.return_value = list(calendars.values())
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return calendars


async def test_a_calendar_of_nothing_but_journal_entries_shows_the_dated_ones(
    hass: HomeAssistant, hass_client
) -> None:
    calendars = await _setup(hass, {"Diary": {"VJOURNAL"}})
    calendars["Diary"].search.return_value = [
        stored(ENTRY),
        stored(
            ENTRY.replace("note-1", "undated").replace(
                "DTSTART;VALUE=DATE:20260706\r\n", ""
            )
        ),
    ]

    state = hass.states.get("calendar.diary")
    assert state.attributes["supported_features"] == 0
    assert hass.states.get("todo.diary") is None
    client = await hass_client()
    response = await client.get(
        "/api/calendars/calendar.diary?start=2026-07-01T00:00:00Z&end=2026-08-01T00:00:00Z"
    )
    [event] = await response.json()
    assert event["summary"] == "Monday"
    assert event["start"] == {"date": "2026-07-06"}
    assert event["end"] == {"date": "2026-07-07"}
    assert calendars["Diary"].search.call_args.kwargs["journal"] is True


async def test_a_journal_action_writes_to_a_calendar_that_holds_entries(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, {"Diary": {"VJOURNAL"}})

    with patch("custom_components.ha_caldav.services.create_journal") as create:
        await hass.services.async_call(
            DOMAIN,
            "create_journal",
            {
                "entity_id": "calendar.diary",
                "summary": "Monday",
                "start_date": "2026-07-06",
                "status": "final",
            },
            blocking=True,
        )

    assert create.call_args.args[1] == {
        "summary": "Monday",
        "start": date(2026, 7, 6),
        "status": "FINAL",
    }


async def test_an_entry_is_changed_and_deleted_by_its_uid(hass: HomeAssistant) -> None:
    await _setup(hass, {"Mixed": {"VEVENT", "VTODO", "VJOURNAL"}})

    with (
        patch("custom_components.ha_caldav.services.update_journal") as update,
        patch("custom_components.ha_caldav.services.delete_journal") as delete,
    ):
        await hass.services.async_call(
            DOMAIN,
            "update_journal",
            {"entity_id": "calendar.mixed", "uid": "note-1", "description": ""},
            blocking=True,
        )
        await hass.services.async_call(
            DOMAIN,
            "delete_journal",
            {"entity_id": "calendar.mixed", "uid": "note-1"},
            blocking=True,
        )

    assert update.call_args.args[1:] == ("note-1", {"description": None})
    assert delete.call_args.args[1] == "note-1"


async def test_a_calendar_without_journal_entries_refuses_the_journal_actions(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, {"Personal": {"VEVENT", "VTODO"}})

    with pytest.raises(ServiceValidationError) as refusal:
        await hass.services.async_call(
            DOMAIN,
            "create_journal",
            {"entity_id": "calendar.personal", "summary": "Monday"},
            blocking=True,
        )

    assert refusal.value.translation_key == "no_journals"


async def test_a_search_filters_the_entries_by_the_text_given(
    hass: HomeAssistant,
) -> None:
    calendars = await _setup(hass, {"Diary": {"VJOURNAL"}})
    calendars["Diary"].search.return_value = [
        stored(ENTRY),
        stored(ENTRY.replace("note-1", "note-2").replace("Rain", "Sun")),
    ]

    result = await hass.services.async_call(
        DOMAIN,
        "search_journals",
        {"entity_id": "calendar.diary", "text": "sun", "field": "description"},
        blocking=True,
        return_response=True,
    )

    assert [entry["uid"] for entry in result["calendar.diary"]["journals"]] == [
        "note-2"
    ]


async def test_an_entry_cannot_have_two_dates(hass: HomeAssistant) -> None:
    await _setup(hass, {"Diary": {"VJOURNAL"}})

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "create_journal",
            {
                "entity_id": "calendar.diary",
                "summary": "Monday",
                "start_date": "2026-07-06",
                "start_date_time": "2026-07-06 20:00:00",
            },
            blocking=True,
        )
