from datetime import UTC, date, datetime, timedelta
from unittest.mock import Mock, patch

from caldav.lib.error import DAVError
from caldav.lib.url import URL
from conftest import RecordingClient, stored
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import icalendar
from icalendar import Calendar as ICalCalendar
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser
import vobject
import voluptuous as vol

from custom_components.ha_caldav.connection import calendar_key
from custom_components.ha_caldav.const import (
    CONF_CALENDAR_OPTIONS,
    CONF_CALENDARS,
    CONF_READ_ONLY,
    DOMAIN,
)

ENTRY_DATA = {
    CONF_URL: "https://cloud.example.com/remote.php/dav",
    CONF_USERNAME: "iven",
    CONF_PASSWORD: "secret",
    CONF_VERIFY_SSL: True,
}

ICS = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:uid-1\r\nDTSTAMP:20260101T000000Z\r\n"
    "DTSTART:20260706T090000Z\r\nDTEND:20260706T100000Z\r\nSUMMARY:Standup\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)


def _calendar(name: str) -> Mock:
    calendar = Mock()
    calendar.name = name
    calendar.url = f"https://cloud.example.com/remote.php/dav/{name}"
    calendar.search.return_value = []
    return calendar


def _record_writes(calendar: Mock) -> None:
    calendar.client = RecordingClient()
    calendar.client.url = URL("https://cloud.example.com/")
    calendar.url = URL(calendar.url)


def _search_item(summary: str) -> Mock:
    item = Mock()
    item.vobject_instance = vobject.readOne(ICS.replace("Standup", summary))
    return item


async def _setup(
    hass: HomeAssistant,
    calendars: list[Mock] | None = None,
    options: dict | None = None,
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="iven",
        data=ENTRY_DATA,
        options=options or {},
        unique_id="x",
    )
    entry.add_to_hass(hass)
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = (
            calendars if calendars is not None else [_calendar("Personal")]
        )
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _managed(entry, name: str):
    return next(item for item in entry.runtime_data.calendars if item.name == name)


async def test_search_returns_what_the_server_matched(hass: HomeAssistant) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    calendar.search.return_value = [_search_item("Dentist")]

    result = await hass.services.async_call(
        DOMAIN,
        "search_events",
        {"entity_id": "calendar.personal", "text": "dent"},
        blocking=True,
        return_response=True,
    )

    events = result["calendar.personal"]["events"]
    assert [event["summary"] for event in events] == ["Dentist"]
    assert calendar.search.call_args.kwargs["summary"] == "dent"
    assert calendar.search.call_args.kwargs["event"] is True


async def test_search_can_target_another_field(hass: HomeAssistant) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])

    await hass.services.async_call(
        DOMAIN,
        "search_events",
        {
            "entity_id": "calendar.personal",
            "text": "Berlin",
            "field": "location",
        },
        blocking=True,
        return_response=True,
    )

    assert calendar.search.call_args.kwargs["location"] == "Berlin"


async def test_free_busy_flattens_the_periods(hass: HomeAssistant) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    calendar.freebusy_request.return_value = Mock(
        icalendar_instance=icalendar.Calendar.from_ical(
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
            "BEGIN:VFREEBUSY\r\nDTSTART:20260706T000000Z\r\nDTEND:20260707T000000Z\r\n"
            "FREEBUSY:20260706T090000Z/20260706T100000Z\r\n"
            "FREEBUSY;FBTYPE=BUSY:20260706T140000Z/PT1H\r\n"
            "END:VFREEBUSY\r\nEND:VCALENDAR\r\n"
        )
    )

    result = await hass.services.async_call(
        DOMAIN,
        "get_free_busy",
        {
            "entity_id": "calendar.personal",
            "start": "2026-07-06 00:00:00",
            "end": "2026-07-07 00:00:00",
        },
        blocking=True,
        return_response=True,
    )

    periods = result["calendar.personal"]["periods"]
    assert len(periods) == 2
    assert periods[0]["start"].startswith("2026-07-06T09:00:00")
    assert periods[0]["end"].startswith("2026-07-06T10:00:00")
    # The second half of a period may be a duration rather than an end.
    assert periods[1]["end"].startswith("2026-07-06T15:00:00")


async def test_create_event_carries_the_extra_properties(hass: HomeAssistant) -> None:
    await _setup(hass)

    with patch("custom_components.ha_caldav.calendar.create_event") as create:
        await hass.services.async_call(
            DOMAIN,
            "create_event",
            {
                "entity_id": "calendar.personal",
                "summary": "Standup",
                "start_date_time": "2026-07-06 09:00:00",
                "end_date_time": "2026-07-06 10:00:00",
                "url": "https://meet.example.com/x",
                "categories": ["work"],
                "alarms": [15],
                "priority": 3,
            },
            blocking=True,
        )

    data = create.call_args.args[1]
    assert data["summary"] == "Standup"
    # The datetime selector sends a naive value, which caldav would resolve against
    # the OS timezone.
    assert data["dtstart"].tzinfo is not None
    assert data["dtstart"] == dt_util.as_local(datetime(2026, 7, 6, 9, 0))
    assert data["url"] == "https://meet.example.com/x"
    assert data["categories"] == ["work"]
    assert data["alarms"] == [15]
    assert data["priority"] == 3


async def test_update_event_only_forwards_the_named_fields(
    hass: HomeAssistant,
) -> None:
    await _setup(hass)

    with patch("custom_components.ha_caldav.calendar.update_event") as update:
        await hass.services.async_call(
            DOMAIN,
            "update_event",
            {
                "entity_id": "calendar.personal",
                "uid": "uid-1",
                "status": "CANCELLED",
            },
            blocking=True,
        )

    data = update.call_args.args[2]
    assert data == {"status": "CANCELLED"}
    assert update.call_args.args[1] == "uid-1"


@pytest.mark.parametrize(("given", "forwarded"), [("Agenda", "Agenda"), ("", None)])
async def test_update_event_forwards_a_description_and_clears_an_empty_one(
    hass: HomeAssistant, given: str, forwarded: str | None
) -> None:
    await _setup(hass)

    with patch("custom_components.ha_caldav.calendar.update_event") as update:
        await hass.services.async_call(
            DOMAIN,
            "update_event",
            {"entity_id": "calendar.personal", "uid": "uid-1", "description": given},
            blocking=True,
        )

    assert update.call_args.args[2] == {"description": forwarded}


async def test_update_event_forwards_the_recurrence_range(hass: HomeAssistant) -> None:
    await _setup(hass)

    with patch("custom_components.ha_caldav.calendar.update_event") as update:
        await hass.services.async_call(
            DOMAIN,
            "update_event",
            {
                "entity_id": "calendar.personal",
                "uid": "uid-1",
                "summary": "Renamed",
                "recurrence_id": "2026-07-13 09:00:00+00:00",
                "recurrence_range": "THISANDFUTURE",
            },
            blocking=True,
        )

    assert update.call_args.args[3] == "2026-07-13 09:00:00+00:00"
    assert update.call_args.args[4] is True


async def test_an_occurrence_a_template_rendered_empty_edits_the_series(
    hass: HomeAssistant,
) -> None:
    await _setup(hass)

    with patch("custom_components.ha_caldav.calendar.update_event") as update:
        await hass.services.async_call(
            DOMAIN,
            "update_event",
            {
                "entity_id": "calendar.personal",
                "uid": "uid-1",
                "summary": "Renamed",
                "recurrence_id": "",
                "recurrence_range": "",
            },
            blocking=True,
        )

    assert update.call_args.args[3] is None
    assert update.call_args.args[4] is False


async def test_delete_event_removes_the_whole_series_by_default(
    hass: HomeAssistant,
) -> None:
    await _setup(hass)

    with patch("custom_components.ha_caldav.calendar.delete_event") as delete:
        await hass.services.async_call(
            DOMAIN,
            "delete_event",
            {"entity_id": "calendar.personal", "uid": "uid-1"},
            blocking=True,
        )

    assert delete.call_args.args[1:] == ("uid-1", None, False)


async def test_delete_event_forwards_the_occurrence_and_its_range(
    hass: HomeAssistant,
) -> None:
    await _setup(hass)

    with patch("custom_components.ha_caldav.calendar.delete_event") as delete:
        await hass.services.async_call(
            DOMAIN,
            "delete_event",
            {
                "entity_id": "calendar.personal",
                "uid": "uid-1",
                "recurrence_id": "2026-07-13 09:00:00+00:00",
                "recurrence_range": "thisandfuture",
            },
            blocking=True,
        )

    assert delete.call_args.args[1:] == ("uid-1", "2026-07-13 09:00:00+00:00", True)


async def test_a_delete_with_a_range_but_no_occurrence_deletes_nothing(
    hass: HomeAssistant,
) -> None:
    await _setup(hass)

    with (
        patch("custom_components.ha_caldav.calendar.delete_event") as delete,
        pytest.raises(vol.Invalid),
    ):
        await hass.services.async_call(
            DOMAIN,
            "delete_event",
            {
                "entity_id": "calendar.personal",
                "uid": "uid-1",
                "recurrence_range": "THISANDFUTURE",
            },
            blocking=True,
        )

    assert delete.call_count == 0


async def test_move_event_writes_to_the_target_and_refreshes_it(
    hass: HomeAssistant,
) -> None:
    source, target = _calendar("Personal"), _calendar("Work")
    entry = await _setup(hass, [source, target])

    with (
        patch("custom_components.ha_caldav.services.move_event") as move,
        patch.object(_managed(entry, "Work").coordinator, "async_refresh") as refresh,
    ):
        await hass.services.async_call(
            DOMAIN,
            "move_event",
            {
                "entity_id": "calendar.personal",
                "uid": "uid-1",
                "target_entity_id": "calendar.work",
            },
            blocking=True,
        )

    assert move.call_args.args[0] is source
    assert move.call_args.args[1] is target
    assert move.call_args.args[2] == "uid-1"
    assert move.call_args.args[3] is False
    refresh.assert_called_once()


async def test_move_to_something_that_is_not_ours_is_rejected(
    hass: HomeAssistant,
) -> None:
    await _setup(hass)

    with pytest.raises(ServiceValidationError) as refusal:
        await hass.services.async_call(
            DOMAIN,
            "move_event",
            {
                "entity_id": "calendar.personal",
                "uid": "uid-1",
                "target_entity_id": "calendar.somewhere_else",
            },
            blocking=True,
        )

    assert refusal.value.translation_key == "unknown_target"


async def test_export_returns_the_document(hass: HomeAssistant) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])

    with patch(
        "custom_components.ha_caldav.services.export_ics", return_value=ICS
    ) as export:
        result = await hass.services.async_call(
            DOMAIN,
            "export_ics",
            {"entity_id": "calendar.personal", "uid": "uid-1"},
            blocking=True,
            return_response=True,
        )

    assert result["calendar.personal"]["ics"] == ICS
    assert export.call_args.args[1] == "uid-1"


async def test_import_forwards_the_document(hass: HomeAssistant) -> None:
    await _setup(hass)

    with patch("custom_components.ha_caldav.services.import_ics") as importer:
        await hass.services.async_call(
            DOMAIN,
            "import_ics",
            {"entity_id": "calendar.personal", "ics": ICS},
            blocking=True,
        )

    assert importer.call_args.args[1] == ICS


async def test_setting_a_color_refreshes_the_color_poller(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    entry = await _setup(hass, [calendar])

    with (
        patch("custom_components.ha_caldav.services.set_calendar_color") as setter,
        patch.object(entry.runtime_data.colors, "async_request_refresh") as refresh,
    ):
        await hass.services.async_call(
            DOMAIN,
            "set_calendar_color",
            {"entity_id": "calendar.personal", "color": "#00679e"},
            blocking=True,
        )

    assert setter.call_args.args[0] is calendar
    assert setter.call_args.args[1] == "#00679e"
    refresh.assert_called_once()


async def test_invitation_needs_a_scheduling_server(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    assert entry.runtime_data.address_set == []

    with pytest.raises(ServiceValidationError) as refusal:
        await hass.services.async_call(
            DOMAIN,
            "respond_to_invitation",
            {
                "entity_id": "calendar.personal",
                "uid": "uid-1",
                "response": "accept",
            },
            blocking=True,
        )

    assert refusal.value.translation_key == "no_scheduling"


async def test_invitation_maps_the_response_onto_a_partstat(
    hass: HomeAssistant,
) -> None:
    entry = await _setup(hass)
    entry.runtime_data.address_set = ["mailto:iven@example.com"]

    with patch("custom_components.ha_caldav.services.respond_to_invitation") as respond:
        await hass.services.async_call(
            DOMAIN,
            "respond_to_invitation",
            {
                "entity_id": "calendar.personal",
                "uid": "uid-1",
                "response": "decline",
            },
            blocking=True,
        )

    assert respond.call_args.args[2] == "DECLINED"
    assert respond.call_args.args[3] == ["mailto:iven@example.com"]


async def test_create_calendar_reloads_the_account(hass: HomeAssistant) -> None:
    entry = await _setup(hass)

    with (
        patch("custom_components.ha_caldav.services.create_calendar") as create,
        patch.object(hass.config_entries, "async_reload", return_value=True) as reload,
    ):
        await hass.services.async_call(
            DOMAIN,
            "create_calendar",
            {
                "config_entry_id": entry.entry_id,
                "name": "Holidays",
                "components": ["VEVENT"],
            },
            blocking=True,
        )

    assert create.call_args.args[1] == "Holidays"
    assert create.call_args.args[2] == ["VEVENT"]
    reload.assert_called_once_with(entry.entry_id)


async def test_a_read_only_account_gets_no_new_calendar(hass: HomeAssistant) -> None:
    entry = await _setup(hass, options={CONF_READ_ONLY: True})

    with (
        patch("custom_components.ha_caldav.services.create_calendar") as create,
        pytest.raises(ServiceValidationError) as refusal,
    ):
        await hass.services.async_call(
            DOMAIN,
            "create_calendar",
            {"config_entry_id": entry.entry_id, "name": "Holidays"},
            blocking=True,
        )

    assert refusal.value.translation_key == "read_only"
    create.assert_not_called()


async def test_a_new_calendar_is_added_to_an_explicit_selection(
    hass: HomeAssistant,
) -> None:
    entry = await _setup(hass, options={CONF_CALENDARS: ["Personal"]})

    with (
        patch("custom_components.ha_caldav.services.create_calendar") as create,
        patch("custom_components.ha_caldav.caldav.DAVClient") as client,
    ):
        create.return_value.url = "https://dav.example.com/remote.php/dav/Holidays/"
        client.return_value.principal.return_value.calendars.return_value = []
        await hass.services.async_call(
            DOMAIN,
            "create_calendar",
            {"config_entry_id": entry.entry_id, "name": "Holidays"},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_CALENDARS] == [
        "/remote.php/dav/Personal",
        "/remote.php/dav/Holidays",
    ]


async def test_deleting_a_calendar_removes_it_and_its_entities(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    entry = await _setup(hass, [calendar])
    registry = er.async_get(hass)
    assert registry.async_get("calendar.personal") is not None

    with (
        patch("custom_components.ha_caldav.services.delete_calendar") as delete,
        patch("custom_components.ha_caldav.caldav.DAVClient") as client,
    ):
        client.return_value.principal.return_value.calendars.return_value = []
        await hass.services.async_call(
            DOMAIN,
            "delete_calendar",
            {"config_entry_id": entry.entry_id, "name": "Personal"},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert delete.call_args.args[0] is calendar
    assert registry.async_get("calendar.personal") is None
    assert registry.async_get("todo.personal") is None


async def test_deleting_an_ambiguous_calendar_is_refused(
    hass: HomeAssistant,
) -> None:
    first, second = _calendar("Personal"), _calendar("Personal")
    second.url = "https://cloud.example.com/remote.php/dav/Personal-2"
    entry = await _setup(hass, [first, second])

    with (
        patch("custom_components.ha_caldav.services.delete_calendar") as delete,
        pytest.raises(ServiceValidationError) as refusal,
    ):
        await hass.services.async_call(
            DOMAIN,
            "delete_calendar",
            {"config_entry_id": entry.entry_id, "name": "Personal"},
            blocking=True,
        )

    assert refusal.value.translation_key == "ambiguous_calendar"
    delete.assert_not_called()


async def test_deleting_a_calendar_that_is_not_there_is_rejected(
    hass: HomeAssistant,
) -> None:
    entry = await _setup(hass)

    with pytest.raises(ServiceValidationError) as refusal:
        await hass.services.async_call(
            DOMAIN,
            "delete_calendar",
            {"config_entry_id": entry.entry_id, "name": "Nope"},
            blocking=True,
        )

    assert refusal.value.translation_key == "unknown_calendar"


async def test_an_unknown_account_is_rejected(hass: HomeAssistant) -> None:
    await _setup(hass)

    with pytest.raises(ServiceValidationError) as refusal:
        await hass.services.async_call(
            DOMAIN,
            "create_calendar",
            {"config_entry_id": "nope", "name": "Holidays"},
            blocking=True,
        )

    assert refusal.value.translation_key == "unknown_account"


async def test_a_server_error_becomes_a_home_assistant_error(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    calendar.search.side_effect = DAVError(
        "500 Server Error at 'https://cloud.example.com/dav/iven/personal/'"
    )

    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            DOMAIN,
            "search_events",
            {"entity_id": "calendar.personal", "text": "x"},
            blocking=True,
            return_response=True,
        )

    # The collection url carries the account name.
    assert "cloud.example.com" not in str(raised.value)


async def test_search_window_is_passed_through(hass: HomeAssistant) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    start = datetime.now(UTC).replace(microsecond=0)

    await hass.services.async_call(
        DOMAIN,
        "search_events",
        {
            "entity_id": "calendar.personal",
            "text": "x",
            "start": start.isoformat(),
            "end": (start + timedelta(days=1)).isoformat(),
        },
        blocking=True,
        return_response=True,
    )

    assert calendar.search.call_args.kwargs["start"] == start


async def test_an_all_day_event_keeps_its_date_value_type(hass: HomeAssistant) -> None:
    await _setup(hass)

    with patch("custom_components.ha_caldav.calendar.create_event") as create:
        await hass.services.async_call(
            DOMAIN,
            "create_event",
            {
                "entity_id": "calendar.personal",
                "summary": "Holiday",
                "start_date": "2026-07-06",
                "end_date": "2026-07-07",
            },
            blocking=True,
        )

    data = create.call_args.args[1]
    assert not isinstance(data["dtstart"], datetime)
    assert data["dtstart"] == date(2026, 7, 6)


async def test_a_start_cannot_be_given_as_a_date_and_a_time_at_once(
    hass: HomeAssistant,
) -> None:
    await _setup(hass)

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "create_event",
            {
                "entity_id": "calendar.personal",
                "summary": "Ambiguous",
                "start_date": "2026-07-06",
                "start_date_time": "2026-07-06 09:00:00",
                "end_date": "2026-07-07",
            },
            blocking=True,
        )


async def test_an_event_needs_a_start_and_an_end(hass: HomeAssistant) -> None:
    await _setup(hass)

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "create_event",
            {
                "entity_id": "calendar.personal",
                "summary": "Open ended",
                "start_date_time": "2026-07-06 09:00:00",
            },
            blocking=True,
        )


async def test_an_all_day_field_refuses_a_value_carrying_a_time(
    hass: HomeAssistant,
) -> None:
    """cv.date takes a datetime unchanged, datetime being a subclass of date."""
    await _setup(hass)

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "create_event",
            {
                "entity_id": "calendar.personal",
                "summary": "Holiday",
                "start_date": datetime(2026, 7, 6, 9, 0),
                "end_date": date(2026, 7, 7),
            },
            blocking=True,
        )


async def test_a_date_paired_with_a_datetime_is_refused(hass: HomeAssistant) -> None:
    await _setup(hass)

    with pytest.raises(ServiceValidationError) as refusal:
        await hass.services.async_call(
            DOMAIN,
            "create_event",
            {
                "entity_id": "calendar.personal",
                "summary": "Broken",
                "start_date": "2026-07-06",
                "end_date_time": "2026-07-06 10:00:00",
            },
            blocking=True,
        )

    assert refusal.value.translation_key == "mixed_time_types"


async def test_a_misspelled_recurrence_range_is_refused(hass: HomeAssistant) -> None:
    await _setup(hass)

    with pytest.raises(vol.Invalid, match="recurrence_range"):
        await hass.services.async_call(
            DOMAIN,
            "update_event",
            {
                "entity_id": "calendar.personal",
                "uid": "uid-1",
                "summary": "Renamed",
                "recurrence_id": "2026-07-13 09:00:00+00:00",
                "recurrence_range": "this_and_future",
            },
            blocking=True,
        )


async def test_a_read_only_calendar_refuses_every_write_service(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar], options={CONF_READ_ONLY: True})

    for service, data in (
        (
            "create_event",
            {
                "summary": "x",
                "start_date_time": "2026-07-06 09:00:00",
                "end_date_time": "2026-07-06 10:00:00",
            },
        ),
        ("update_event", {"uid": "uid-1", "summary": "x"}),
        ("delete_event", {"uid": "uid-1"}),
        ("import_ics", {"ics": ICS}),
        ("set_calendar_color", {"color": "#00679e"}),
        ("move_event", {"uid": "uid-1", "target_entity_id": "calendar.personal"}),
        ("respond_to_invitation", {"uid": "uid-1", "response": "accept"}),
    ):
        with pytest.raises(ServiceValidationError) as refusal:
            await hass.services.async_call(
                DOMAIN,
                service,
                {"entity_id": "calendar.personal", **data},
                blocking=True,
            )
        assert refusal.value.translation_key == "read_only", service


async def test_moving_into_a_read_only_calendar_is_refused(
    hass: HomeAssistant,
) -> None:
    source, target = _calendar("Personal"), _calendar("Work")
    await _setup(
        hass,
        [source, target],
        options={
            CONF_CALENDAR_OPTIONS: {calendar_key(target.url): {CONF_READ_ONLY: True}}
        },
    )

    with (
        patch("custom_components.ha_caldav.services.move_event") as move,
        pytest.raises(ServiceValidationError) as refusal,
    ):
        await hass.services.async_call(
            DOMAIN,
            "move_event",
            {
                "entity_id": "calendar.personal",
                "uid": "uid-1",
                "target_entity_id": "calendar.work",
            },
            blocking=True,
        )

    assert refusal.value.translation_key == "read_only"
    move.assert_not_called()


async def test_an_unloaded_account_is_rejected(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    # Reaching runtime_data on an unloaded entry raises AttributeError instead.
    with pytest.raises(ServiceValidationError) as refusal:
        await hass.services.async_call(
            DOMAIN,
            "create_calendar",
            {"config_entry_id": entry.entry_id, "name": "Holidays"},
            blocking=True,
        )

    assert refusal.value.translation_key == "unknown_account"


async def test_pushing_a_color_makes_the_entity_follow_the_server_again(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    entity = hass.data["entity_components"]["calendar"].get_entity("calendar.personal")
    entity._picked = True

    await hass.services.async_call(
        DOMAIN,
        "set_calendar_color",
        {"entity_id": "calendar.personal", "color": "#123456"},
        blocking=True,
    )

    assert entity._picked is False
    assert calendar.set_properties.call_count == 1


async def test_an_event_that_ends_before_it_starts_is_refused(
    hass: HomeAssistant,
) -> None:
    await _setup(hass)

    with (
        patch("custom_components.ha_caldav.calendar.create_event") as create,
        pytest.raises(ServiceValidationError) as refusal,
    ):
        await hass.services.async_call(
            DOMAIN,
            "create_event",
            {
                "entity_id": "calendar.personal",
                "summary": "Backwards",
                "start_date_time": "2026-07-06 10:00:00",
                "end_date_time": "2026-07-06 09:00:00",
            },
            blocking=True,
        )

    assert refusal.value.translation_key == "end_before_start"
    assert create.call_count == 0


async def test_move_to_another_integrations_calendar_is_rejected(
    hass: HomeAssistant,
) -> None:
    await _setup(hass)
    foreign = MockConfigEntry(domain="google", unique_id="google-1")
    foreign.add_to_hass(hass)
    foreign.mock_state(hass, ConfigEntryState.LOADED)
    er.async_get(hass).async_get_or_create(
        "calendar",
        "google",
        "some-google-id",
        suggested_object_id="google_work",
        config_entry=foreign,
    )

    with pytest.raises(ServiceValidationError) as refusal:
        await hass.services.async_call(
            DOMAIN,
            "move_event",
            {
                "entity_id": "calendar.personal",
                "uid": "uid-1",
                "target_entity_id": "calendar.google_work",
            },
            blocking=True,
        )

    assert refusal.value.translation_key == "unknown_target"
    # Teardown unloads every loaded entry, and nothing set this one up.
    foreign.mock_state(hass, ConfigEntryState.NOT_LOADED)


async def test_a_move_refreshes_the_calendar_it_came_from(
    hass: HomeAssistant,
) -> None:
    source, target = _calendar("Personal"), _calendar("Work")
    entry = await _setup(hass, [source, target])

    with (
        patch("custom_components.ha_caldav.services.move_event"),
        patch.object(
            _managed(entry, "Personal").coordinator, "async_refresh"
        ) as refresh,
    ):
        await hass.services.async_call(
            DOMAIN,
            "move_event",
            {
                "entity_id": "calendar.personal",
                "uid": "uid-1",
                "target_entity_id": "calendar.work",
            },
            blocking=True,
        )

    refresh.assert_called_once()


async def test_a_free_busy_answer_the_library_cannot_read_is_reported(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    calendar.freebusy_request.return_value = Mock(spec=[])
    await _setup(hass, [calendar])

    with pytest.raises(HomeAssistantError) as failure:
        await hass.services.async_call(
            DOMAIN,
            "get_free_busy",
            {
                "entity_id": "calendar.personal",
                "start": "2026-07-06 00:00:00",
                "end": "2026-07-07 00:00:00",
            },
            blocking=True,
            return_response=True,
        )

    assert failure.value.translation_key == "server_error"


async def test_create_event_forwards_the_recurrence_rule(hass: HomeAssistant) -> None:
    await _setup(hass)

    with patch("custom_components.ha_caldav.calendar.create_event") as create:
        await hass.services.async_call(
            DOMAIN,
            "create_event",
            {
                "entity_id": "calendar.personal",
                "summary": "Standup",
                "start_date_time": "2026-07-06 09:00:00",
                "end_date_time": "2026-07-06 10:00:00",
                "rrule": "FREQ=WEEKLY;BYDAY=MO",
            },
            blocking=True,
        )

    assert create.call_args.args[1]["rrule"] == "FREQ=WEEKLY;BYDAY=MO"


async def test_update_event_forwards_the_recurrence_rule(hass: HomeAssistant) -> None:
    await _setup(hass)

    with patch("custom_components.ha_caldav.calendar.update_event") as update:
        await hass.services.async_call(
            DOMAIN,
            "update_event",
            {
                "entity_id": "calendar.personal",
                "uid": "uid-1",
                "rrule": "FREQ=DAILY",
            },
            blocking=True,
        )

    assert update.call_args.args[2] == {"rrule": "FREQ=DAILY"}


async def test_an_empty_rule_is_how_a_recurrence_is_removed(
    hass: HomeAssistant,
) -> None:
    # Expanding strips RRULE from what the frontend echoes back.
    await _setup(hass)

    with patch("custom_components.ha_caldav.calendar.update_event") as update:
        await hass.services.async_call(
            DOMAIN,
            "update_event",
            {"entity_id": "calendar.personal", "uid": "uid-1", "rrule": ""},
            blocking=True,
        )

    assert update.call_args.args[2] == {"rrule": ""}


async def test_moving_into_a_calendar_the_caller_may_not_control_is_refused(
    hass: HomeAssistant,
) -> None:
    """Home Assistant checks POLICY_CONTROL for entities named in a target, not for
    one named in a field like target_entity_id."""
    from homeassistant.auth.permissions import PolicyPermissions
    from homeassistant.exceptions import Unauthorized

    await _setup(hass, [_calendar("Personal"), _calendar("Private")])
    user = MockUser()
    user.add_to_hass(hass)
    user.permissions = PolicyPermissions(
        {"entities": {"entity_ids": {"calendar.personal": True}}}, user.id
    )

    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "move_event",
            {
                "entity_id": "calendar.personal",
                "uid": "uid-1",
                "target_entity_id": "calendar.private",
            },
            blocking=True,
            context=Context(user_id=user.id),
        )


async def test_a_caller_who_may_control_both_calendars_moves_the_event(
    hass: HomeAssistant,
) -> None:
    from homeassistant.auth.permissions import PolicyPermissions

    await _setup(hass, [_calendar("Personal"), _calendar("Private")])
    user = MockUser()
    user.add_to_hass(hass)
    user.permissions = PolicyPermissions(
        {
            "entities": {
                "entity_ids": {"calendar.personal": True, "calendar.private": True}
            }
        },
        user.id,
    )

    with patch("custom_components.ha_caldav.services.move_event") as move:
        await hass.services.async_call(
            DOMAIN,
            "move_event",
            {
                "entity_id": "calendar.personal",
                "uid": "uid-1",
                "target_entity_id": "calendar.private",
            },
            blocking=True,
            context=Context(user_id=user.id),
        )

    move.assert_called_once()


def test_an_event_that_ends_when_it_starts_is_refused() -> None:
    """end_date reads as "the last day", and that pair is invalid per RFC 5545."""
    from custom_components.ha_caldav.services import _check_span

    with pytest.raises(ServiceValidationError):
        _check_span(date(2026, 8, 6), date(2026, 8, 6))


def test_a_window_that_ends_before_it_starts_is_refused() -> None:
    """A server answers a backwards window with nothing."""
    from custom_components.ha_caldav.services import _check_window

    with pytest.raises(ServiceValidationError):
        _check_window(
            datetime(2026, 8, 7, tzinfo=UTC), datetime(2026, 8, 6, tzinfo=UTC)
        )
    _check_window(None, None)


def test_an_update_naming_nothing_but_a_uid_is_refused() -> None:
    from custom_components.ha_caldav.services import UPDATE_EVENT_SCHEMA

    with pytest.raises(vol.Invalid):
        UPDATE_EVENT_SCHEMA({"entity_id": "calendar.x", "uid": "uid-1"})

    UPDATE_EVENT_SCHEMA({"entity_id": "calendar.x", "uid": "uid-1", "summary": "New"})


def test_a_color_the_read_path_could_not_recognize_is_refused() -> None:
    import voluptuous as voluptuous_schema

    from custom_components.ha_caldav.const import ATTR_COLOR
    from custom_components.ha_caldav.services import COLOR_SCHEMA

    schema = voluptuous_schema.Schema(COLOR_SCHEMA)
    schema({ATTR_COLOR: "#00679e"})

    with pytest.raises(voluptuous_schema.Invalid):
        schema({ATTR_COLOR: "banana"})


async def test_replying_to_an_invitation_clears_the_events_etag(
    hass: HomeAssistant,
) -> None:
    """The reply is a PUT, and the refresh behind it is debounced."""
    entry = await _setup(hass)
    entity = hass.data["entity_components"]["calendar"].get_entity("calendar.personal")
    entity.coordinator.etags = {"uid-1": '"e"'}
    entry.runtime_data.address_set = ["mailto:iven@example.com"]

    with patch("custom_components.ha_caldav.services.respond_to_invitation"):
        await hass.services.async_call(
            DOMAIN,
            "respond_to_invitation",
            {
                "entity_id": "calendar.personal",
                "uid": "uid-1",
                "response": "accept",
            },
            blocking=True,
        )

    assert "uid-1" not in entity.coordinator.etags


async def test_deleting_a_calendar_takes_its_key_out_of_the_selection(
    hass: HomeAssistant,
) -> None:
    entry = await _setup(
        hass,
        [_calendar("Personal"), _calendar("Work")],
        options={CONF_CALENDARS: ["/remote.php/dav/Personal", "/remote.php/dav/Work"]},
    )

    with (
        patch("custom_components.ha_caldav.services.delete_calendar"),
        patch("custom_components.ha_caldav.caldav.DAVClient") as client,
    ):
        client.return_value.principal.return_value.calendars.return_value = [
            _calendar("Work")
        ]
        await hass.services.async_call(
            DOMAIN,
            "delete_calendar",
            {"config_entry_id": entry.entry_id, "name": "Personal"},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_CALENDARS] == ["/remote.php/dav/Work"]


async def test_the_only_selected_calendar_is_not_deleted(hass: HomeAssistant) -> None:
    """With the selection empty, every other calendar of the account would load."""
    entry = await _setup(
        hass,
        [_calendar("Personal"), _calendar("Work")],
        options={CONF_CALENDARS: ["/remote.php/dav/Personal"]},
    )

    with (
        patch("custom_components.ha_caldav.services.delete_calendar") as delete,
        pytest.raises(ServiceValidationError) as refusal,
    ):
        await hass.services.async_call(
            DOMAIN,
            "delete_calendar",
            {"config_entry_id": entry.entry_id, "name": "Personal"},
            blocking=True,
        )

    assert refusal.value.translation_key == "last_selected_calendar"
    delete.assert_not_called()


async def test_a_move_between_two_accounts_sharing_a_path_is_allowed(
    hass: HomeAssistant,
) -> None:
    here = _calendar("Personal")
    await _setup(hass, [here])

    there = Mock()
    there.name = "Personal"
    there.url = "https://nc.work.example/remote.php/dav/Personal"
    there.search.return_value = []
    other = MockConfigEntry(
        domain=DOMAIN,
        title="work",
        data={**ENTRY_DATA, CONF_URL: "https://nc.work.example/remote.php/dav"},
        unique_id="y",
    )
    other.add_to_hass(hass)
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = [there]
        await hass.config_entries.async_setup(other.entry_id)
        await hass.async_block_till_done()

    with patch("custom_components.ha_caldav.services.move_event") as move:
        await hass.services.async_call(
            DOMAIN,
            "move_event",
            {
                "entity_id": "calendar.personal",
                "uid": "uid-1",
                "target_entity_id": "calendar.personal_2",
            },
            blocking=True,
        )

    assert move.call_args.args[1] is there


async def test_a_search_is_read_off_the_event_loop(hass: HomeAssistant) -> None:
    """vobject parses an item the first time its components are asked for."""
    import threading

    from custom_components.ha_caldav import services

    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    seen: list[str] = []
    original = services._found_events

    def record(cal, criteria):
        seen.append(threading.current_thread().name)
        return original(cal, criteria)

    with patch.object(services, "_found_events", record):
        await hass.services.async_call(
            DOMAIN,
            "search_events",
            {"entity_id": "calendar.personal", "text": "standup"},
            blocking=True,
            return_response=True,
        )

    assert seen and all(name != threading.main_thread().name for name in seen)


async def test_a_window_that_starts_and_ends_at_the_same_moment_is_allowed(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    calendar.freebusy_request.return_value = Mock(
        icalendar_instance=ICalCalendar.from_ical(
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
            "BEGIN:VFREEBUSY\r\nEND:VFREEBUSY\r\nEND:VCALENDAR\r\n"
        )
    )
    moment = "2026-07-06 09:00:00"

    response = await hass.services.async_call(
        DOMAIN,
        "get_free_busy",
        {"entity_id": "calendar.personal", "start": moment, "end": moment},
        blocking=True,
        return_response=True,
    )

    assert response == {"calendar.personal": {"periods": []}}


async def test_a_target_named_by_a_user_who_no_longer_exists_is_refused(
    hass: HomeAssistant,
) -> None:
    """Core refuses a call from a user who no longer exists before the service runs."""
    from homeassistant.core import Context
    from homeassistant.exceptions import UnknownUser

    from custom_components.ha_caldav.services import _async_check_control

    await _setup(hass)
    entity = hass.data["entity_components"]["calendar"].get_entity("calendar.personal")
    call = Mock(context=Context(user_id="nobody-by-that-id"))

    with pytest.raises(UnknownUser):
        await _async_check_control(entity, call, "calendar.work")


@pytest.mark.parametrize(
    ("field", "sent", "written"),
    [
        ("status", "tentative", "TENTATIVE"),
        ("transparency", "transparent", "TRANSPARENT"),
        ("classification", "confidential", "CONFIDENTIAL"),
    ],
)
async def test_a_lowercase_option_reaches_the_write_path_as_its_rfc_name(
    hass: HomeAssistant, field: str, sent: str, written: str
) -> None:
    """hassfest holds a selector's option keys to [a-z0-9-_]+, so the picker cannot
    offer OPAQUE or THISANDFUTURE as the value itself."""
    await _setup(hass)

    with patch("custom_components.ha_caldav.calendar.create_event") as create:
        await hass.services.async_call(
            DOMAIN,
            "create_event",
            {
                "entity_id": "calendar.personal",
                "summary": "Standup",
                "start_date_time": "2026-07-06 09:00:00",
                "end_date_time": "2026-07-06 10:00:00",
                field: sent,
            },
            blocking=True,
        )

    assert create.call_args.args[1][field] == written


async def test_the_rfc_spelling_is_still_accepted(hass: HomeAssistant) -> None:
    await _setup(hass)

    with patch("custom_components.ha_caldav.calendar.create_event") as create:
        await hass.services.async_call(
            DOMAIN,
            "create_event",
            {
                "entity_id": "calendar.personal",
                "summary": "Standup",
                "start_date_time": "2026-07-06 09:00:00",
                "end_date_time": "2026-07-06 10:00:00",
                "status": "CONFIRMED",
            },
            blocking=True,
        )

    assert create.call_args.args[1]["status"] == "CONFIRMED"


async def test_a_recurrence_range_without_an_occurrence_is_refused(
    hass: HomeAssistant,
) -> None:
    await _setup(hass)

    with (
        patch("custom_components.ha_caldav.calendar.update_event") as update,
        pytest.raises(vol.Invalid),
    ):
        await hass.services.async_call(
            DOMAIN,
            "update_event",
            {
                "entity_id": "calendar.personal",
                "uid": "uid-1",
                "summary": "Renamed",
                "recurrence_range": "THISANDFUTURE",
            },
            blocking=True,
        )

    assert update.call_count == 0


async def test_a_search_window_that_ends_before_it_starts_is_refused(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    calendar.search.reset_mock()

    with pytest.raises(ServiceValidationError) as refusal:
        await hass.services.async_call(
            DOMAIN,
            "search_events",
            {
                "entity_id": "calendar.personal",
                "text": "dent",
                "start": "2026-07-07 00:00:00",
                "end": "2026-07-06 00:00:00",
            },
            blocking=True,
            return_response=True,
        )

    assert refusal.value.translation_key == "end_before_start"
    calendar.search.assert_not_called()


async def test_a_free_busy_window_that_ends_before_it_starts_is_refused(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])

    with pytest.raises(ServiceValidationError) as refusal:
        await hass.services.async_call(
            DOMAIN,
            "get_free_busy",
            {
                "entity_id": "calendar.personal",
                "start": "2026-07-07 00:00:00",
                "end": "2026-07-06 00:00:00",
            },
            blocking=True,
            return_response=True,
        )

    assert refusal.value.translation_key == "end_before_start"
    calendar.freebusy_request.assert_not_called()


async def test_an_event_created_with_attendees_names_the_account_as_organizer(
    hass: HomeAssistant,
) -> None:
    """RFC 5546 3 requires it, and sabre/dav answers 500 on deleting an object that
    lists attendees without one."""
    calendar = _calendar("Personal")
    _record_writes(calendar)
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        principal = client.return_value.principal.return_value
        principal.calendars.return_value = [calendar]
        principal.calendar_user_address_set.return_value = [
            "mailto:iven@example.com",
            "/remote.php/dav/principals/users/iven/",
        ]
        entry = MockConfigEntry(
            domain=DOMAIN, title="iven", data=ENTRY_DATA, unique_id="x"
        )
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN,
        "create_event",
        {
            "entity_id": "calendar.personal",
            "summary": "Review",
            "start_date_time": "2026-07-06 09:00:00",
            "end_date_time": "2026-07-06 10:00:00",
            "attendees": ["bob@example.com"],
        },
        blocking=True,
    )

    body = calendar.client.bodies[-1]
    assert "ORGANIZER:mailto:iven@example.com" in body
    assert "ATTENDEE" in body


async def test_a_search_hit_the_read_path_cannot_place_is_left_out(
    hass: HomeAssistant,
) -> None:
    """A server matches a text search on the resource, not on the component."""
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    todo_only = Mock()
    todo_only.vobject_instance = vobject.readOne(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
        "BEGIN:VTODO\r\nUID:uid-2\r\nDTSTAMP:20260101T000000Z\r\n"
        "SUMMARY:Slides\r\nEND:VTODO\r\nEND:VCALENDAR\r\n"
    )
    calendar.search.return_value = [todo_only, _search_item("Dentist")]

    result = await hass.services.async_call(
        DOMAIN,
        "search_events",
        {"entity_id": "calendar.personal", "text": "dent"},
        blocking=True,
        return_response=True,
    )

    events = result["calendar.personal"]["events"]
    assert [event["summary"] for event in events] == ["Dentist"]


async def test_free_busy_periods_written_on_one_line_are_all_reported(
    hass: HomeAssistant,
) -> None:
    """RFC 5545 3.8.2.6 lets one FREEBUSY line carry several periods, and icalendar
    hands those back as a list where a single period comes back bare."""
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    calendar.freebusy_request.return_value = Mock(
        icalendar_instance=icalendar.Calendar.from_ical(
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
            "BEGIN:VFREEBUSY\r\nDTSTART:20260706T000000Z\r\nDTEND:20260707T000000Z\r\n"
            "FREEBUSY:20260706T090000Z/20260706T100000Z,20260706T140000Z/PT1H\r\n"
            "END:VFREEBUSY\r\nEND:VCALENDAR\r\n"
        )
    )

    result = await hass.services.async_call(
        DOMAIN,
        "get_free_busy",
        {
            "entity_id": "calendar.personal",
            "start": "2026-07-06 00:00:00",
            "end": "2026-07-07 00:00:00",
        },
        blocking=True,
        return_response=True,
    )

    periods = result["calendar.personal"]["periods"]
    assert [period["end"] for period in periods] == [
        "2026-07-06T10:00:00+00:00",
        "2026-07-06T15:00:00+00:00",
    ]


async def test_an_update_carries_the_window_it_was_given(hass: HomeAssistant) -> None:
    entry = await _setup(hass)

    with (
        patch.object(_managed(entry, "Personal").coordinator, "async_refresh"),
        patch("custom_components.ha_caldav.calendar.update_event") as write,
    ):
        await hass.services.async_call(
            DOMAIN,
            "update_event",
            {
                "entity_id": "calendar.personal",
                "uid": "uid-1",
                "start_date_time": "2026-07-06 11:00:00",
                "end_date_time": "2026-07-06 12:00:00",
            },
            blocking=True,
        )

    data = write.call_args.args[2]
    assert data["dtstart"] == dt_util.as_local(
        dt_util.parse_datetime("2026-07-06 11:00:00")
    )
    assert data["dtend"] == dt_util.as_local(
        dt_util.parse_datetime("2026-07-06 12:00:00")
    )


async def test_moving_an_event_onto_its_own_calendar_is_refused(
    hass: HomeAssistant,
) -> None:
    await _setup(hass)

    with pytest.raises(ServiceValidationError) as refusal:
        await hass.services.async_call(
            DOMAIN,
            "move_event",
            {
                "entity_id": "calendar.personal",
                "uid": "uid-1",
                "target_entity_id": "calendar.personal",
            },
            blocking=True,
        )

    assert refusal.value.translation_key == "same_calendar"


async def test_moving_into_an_account_that_is_not_loaded_is_refused(
    hass: HomeAssistant,
) -> None:
    await _setup(hass)
    asleep = MockConfigEntry(
        domain=DOMAIN,
        title="work",
        data={**ENTRY_DATA, CONF_URL: "https://nc.work.example/remote.php/dav"},
        unique_id="y",
    )
    asleep.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "calendar",
        DOMAIN,
        f"{asleep.entry_id}-/remote.php/dav/Personal",
        suggested_object_id="work_personal",
        config_entry=asleep,
    )
    assert asleep.state is not ConfigEntryState.LOADED

    with pytest.raises(ServiceValidationError) as refusal:
        await hass.services.async_call(
            DOMAIN,
            "move_event",
            {
                "entity_id": "calendar.personal",
                "uid": "uid-1",
                "target_entity_id": "calendar.work_personal",
            },
            blocking=True,
        )

    assert refusal.value.translation_key == "unknown_target"


async def test_moving_onto_a_calendar_the_account_no_longer_holds_is_refused(
    hass: HomeAssistant,
) -> None:
    """A registry entry outlives the calendar it was made for."""
    entry = await _setup(hass)
    er.async_get(hass).async_get_or_create(
        "calendar",
        DOMAIN,
        f"{entry.entry_id}-/remote.php/dav/Gone",
        suggested_object_id="gone",
        config_entry=entry,
    )

    with pytest.raises(ServiceValidationError) as refusal:
        await hass.services.async_call(
            DOMAIN,
            "move_event",
            {
                "entity_id": "calendar.personal",
                "uid": "uid-1",
                "target_entity_id": "calendar.gone",
            },
            blocking=True,
        )

    assert refusal.value.translation_key == "unknown_target"


async def test_search_returns_the_extra_properties(hass: HomeAssistant) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    item = Mock()
    item.vobject_instance = vobject.readOne(
        ICS.replace(
            "SUMMARY:Standup", "SUMMARY:Standup\r\nURL:https://meet.example.com/x"
        )
    )
    calendar.search.return_value = [item]

    result = await hass.services.async_call(
        DOMAIN,
        "search_events",
        {"entity_id": "calendar.personal", "text": "stand"},
        blocking=True,
        return_response=True,
    )

    events = result["calendar.personal"]["events"]
    assert events[0]["url"] == "https://meet.example.com/x"


async def test_a_search_within_a_window_answers_with_each_occurrence(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    calendar.search.return_value = [
        stored(ICS.replace("SUMMARY:", "RRULE:FREQ=WEEKLY;COUNT=2\r\nSUMMARY:"))
    ]

    response = await hass.services.async_call(
        DOMAIN,
        "search_events",
        {
            "entity_id": "calendar.personal",
            "text": "Standup",
            "start": "2026-07-01T00:00:00+00:00",
            "end": "2026-07-31T00:00:00+00:00",
        },
        blocking=True,
        return_response=True,
    )

    events = response["calendar.personal"]["events"]
    assert [event["recurrence_id"] for event in events] == [
        "2026-07-06 09:00:00+00:00",
        "2026-07-13 09:00:00+00:00",
    ]


async def test_one_series_that_cannot_be_expanded_leaves_the_other_matches(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    lunar = ICS.replace("uid-1", "lunar").replace(
        "SUMMARY:", "RRULE:RSCALE=CHINESE;FREQ=YEARLY\r\nSUMMARY:"
    )
    calendar.search.return_value = [stored(lunar), stored(ICS)]

    response = await hass.services.async_call(
        DOMAIN,
        "search_events",
        {
            "entity_id": "calendar.personal",
            "text": "Standup",
            "start": "2026-07-01T00:00:00+00:00",
            "end": "2026-07-31T00:00:00+00:00",
        },
        blocking=True,
        return_response=True,
    )

    assert [event["uid"] for event in response["calendar.personal"]["events"]] == [
        "uid-1"
    ]


async def test_a_search_without_a_window_answers_with_the_series_itself(
    hass: HomeAssistant,
) -> None:
    # RFC 5545 leaves the order open, so an exception may come first.
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    item = Mock()
    item.vobject_instance = vobject.readOne(
        ICS.replace(
            "BEGIN:VEVENT",
            "BEGIN:VEVENT\r\nUID:uid-1\r\nDTSTAMP:20260101T000000Z\r\n"
            "RECURRENCE-ID:20260713T090000Z\r\nDTSTART:20260713T110000Z\r\n"
            "DTEND:20260713T120000Z\r\nSUMMARY:Moved\r\nEND:VEVENT\r\n"
            "BEGIN:VEVENT",
            1,
        ).replace("SUMMARY:Standup", "RRULE:FREQ=WEEKLY\r\nSUMMARY:Standup")
    )
    calendar.search.return_value = [item]

    response = await hass.services.async_call(
        DOMAIN,
        "search_events",
        {"entity_id": "calendar.personal", "text": "Standup"},
        blocking=True,
        return_response=True,
    )

    assert "expand" not in calendar.search.call_args.kwargs
    [event] = response["calendar.personal"]["events"]
    assert event["summary"] == "Standup"
    assert event["recurrence_id"] is None


async def test_a_read_only_calendar_is_not_deleted(hass: HomeAssistant) -> None:
    entry = await _setup(hass, options={CONF_READ_ONLY: True})

    with (
        patch("custom_components.ha_caldav.services.delete_calendar") as delete,
        pytest.raises(ServiceValidationError, match="read-only"),
    ):
        await hass.services.async_call(
            DOMAIN,
            "delete_calendar",
            {"config_entry_id": entry.entry_id, "name": "Personal"},
            blocking=True,
        )

    delete.assert_not_called()


READ_ONLY_PERSONAL = {
    CONF_CALENDAR_OPTIONS: {"/remote.php/dav/Personal": {CONF_READ_ONLY: True}}
}


async def test_a_copy_may_come_out_of_a_read_only_calendar(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, [_calendar("Personal"), _calendar("Work")], READ_ONLY_PERSONAL)

    with patch("custom_components.ha_caldav.services.move_event") as move:
        await hass.services.async_call(
            DOMAIN,
            "move_event",
            {
                "entity_id": "calendar.personal",
                "uid": "uid-1",
                "target_entity_id": "calendar.work",
                "keep_original": True,
            },
            blocking=True,
        )

    move.assert_called_once()


async def test_a_move_out_of_a_read_only_calendar_is_refused(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, [_calendar("Personal"), _calendar("Work")], READ_ONLY_PERSONAL)

    with (
        patch("custom_components.ha_caldav.services.move_event") as move,
        pytest.raises(ServiceValidationError, match="read-only"),
    ):
        await hass.services.async_call(
            DOMAIN,
            "move_event",
            {
                "entity_id": "calendar.personal",
                "uid": "uid-1",
                "target_entity_id": "calendar.work",
            },
            blocking=True,
        )

    move.assert_not_called()


async def test_the_calendar_actions_exist_while_the_account_retries(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="x")
    entry.add_to_hass(hass)
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.side_effect = DAVError("down")
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert hass.services.has_service(DOMAIN, "create_event")
    assert hass.services.has_service(DOMAIN, "search_events")


async def test_an_alarm_takes_its_action_in_either_spelling(
    hass: HomeAssistant,
) -> None:
    await _setup(hass)

    with patch("custom_components.ha_caldav.calendar.create_event") as create:
        await hass.services.async_call(
            DOMAIN,
            "create_event",
            {
                "entity_id": "calendar.personal",
                "summary": "Standup",
                "start_date_time": "2026-07-06T09:00:00+00:00",
                "end_date_time": "2026-07-06T10:00:00+00:00",
                "alarms": [{"minutes_before": 5, "action": "audio", "related": "end"}],
            },
            blocking=True,
        )

    [alarm] = create.call_args.args[1]["alarms"]
    assert (alarm["action"], alarm["related"]) == ("AUDIO", "END")


@pytest.mark.parametrize("service", ["create_calendar", "delete_calendar"])
async def test_only_an_administrator_manages_calendars(
    hass: HomeAssistant, service: str
) -> None:
    entry = await _setup(hass)
    user = MockUser().add_to_hass(hass)

    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            service,
            {"config_entry_id": entry.entry_id, "name": "Personal"},
            blocking=True,
            context=Context(user_id=user.id),
        )


async def test_an_empty_text_from_an_automation_clears_the_field(
    hass: HomeAssistant,
) -> None:
    await _setup(hass)

    with patch("custom_components.ha_caldav.calendar.update_event") as update:
        await hass.services.async_call(
            DOMAIN,
            "update_event",
            {
                "entity_id": "calendar.personal",
                "uid": "uid-1",
                "location": "",
                "url": "",
            },
            blocking=True,
        )

    data = update.call_args.args[2]
    assert data["location"] is None
    assert data["url"] is None


async def test_an_empty_rule_on_a_new_event_is_no_rule(hass: HomeAssistant) -> None:
    calendar = _calendar("Personal")
    _record_writes(calendar)
    await _setup(hass, [calendar])

    await hass.services.async_call(
        DOMAIN,
        "create_event",
        {
            "entity_id": "calendar.personal",
            "summary": "Once",
            "start_date_time": "2026-07-06 09:00:00",
            "end_date_time": "2026-07-06 10:00:00",
            "rrule": "",
            "location": "",
        },
        blocking=True,
    )

    written = calendar.client.bodies[-1]
    assert "RRULE" not in written
    assert "LOCATION" not in written


async def test_deleting_one_of_several_selected_calendars_keeps_the_others(
    hass: HomeAssistant,
) -> None:
    personal, work = _calendar("Personal"), _calendar("Work")
    entry = await _setup(
        hass,
        [personal, work],
        options={
            CONF_CALENDARS: ["/remote.php/dav/Personal", "/remote.php/dav/Work"],
            CONF_CALENDAR_OPTIONS: {
                "/remote.php/dav/Personal": {CONF_READ_ONLY: False},
                "/remote.php/dav/Work": {CONF_READ_ONLY: True},
            },
        },
    )

    with (
        patch("custom_components.ha_caldav.services.delete_calendar"),
        patch("custom_components.ha_caldav.caldav.DAVClient") as client,
    ):
        client.return_value.principal.return_value.calendars.return_value = [work]
        await hass.services.async_call(
            DOMAIN,
            "delete_calendar",
            {"config_entry_id": entry.entry_id, "name": "Personal"},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_CALENDARS] == ["/remote.php/dav/Work"]
    assert entry.options[CONF_CALENDAR_OPTIONS] == {
        "/remote.php/dav/Work": {CONF_READ_ONLY: True}
    }


async def test_deleting_a_calendar_whose_path_ends_in_todo_spares_its_neighbor(
    hass: HomeAssistant,
) -> None:
    work, work_todo = _calendar("work"), _calendar("work-todo")
    entry = await _setup(hass, [work, work_todo])
    registry = er.async_get(hass)
    removed: list[str] = []
    remove = registry.async_remove

    def recording(entity_id: str) -> None:
        removed.append(entity_id)
        remove(entity_id)

    with (
        patch("custom_components.ha_caldav.services.delete_calendar"),
        patch("custom_components.ha_caldav.caldav.DAVClient") as client,
        patch.object(registry, "async_remove", recording),
    ):
        client.return_value.principal.return_value.calendars.return_value = [work]
        await hass.services.async_call(
            DOMAIN,
            "delete_calendar",
            {"config_entry_id": entry.entry_id, "name": "work-todo"},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert sorted(removed) == ["calendar.work_todo", "todo.work_todo"]
