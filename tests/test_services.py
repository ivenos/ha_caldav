"""Tests for the CalDAV services."""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import Mock, patch

from caldav.lib.error import DAVError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
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
        {"entity_id": "calendar.iven_personal", "text": "dent"},
        blocking=True,
        return_response=True,
    )

    events = result["calendar.iven_personal"]["events"]
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
            "entity_id": "calendar.iven_personal",
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
            "entity_id": "calendar.iven_personal",
            "start": "2026-07-06 00:00:00",
            "end": "2026-07-07 00:00:00",
        },
        blocking=True,
        return_response=True,
    )

    periods = result["calendar.iven_personal"]["periods"]
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
                "entity_id": "calendar.iven_personal",
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
    # A naive value from the selector must arrive anchored in Home Assistant's
    # timezone, not left floating for caldav to resolve against the OS.
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
                "entity_id": "calendar.iven_personal",
                "uid": "uid-1",
                "status": "CANCELLED",
            },
            blocking=True,
        )

    data = update.call_args.args[2]
    # Nothing else was named, so nothing else may be touched on the server.
    assert data == {"status": "CANCELLED"}
    assert update.call_args.args[1] == "uid-1"


async def test_update_event_forwards_the_recurrence_range(hass: HomeAssistant) -> None:
    await _setup(hass)

    with patch("custom_components.ha_caldav.calendar.update_event") as update:
        await hass.services.async_call(
            DOMAIN,
            "update_event",
            {
                "entity_id": "calendar.iven_personal",
                "uid": "uid-1",
                "summary": "Renamed",
                "recurrence_id": "2026-07-13 09:00:00+00:00",
                "recurrence_range": "THISANDFUTURE",
            },
            blocking=True,
        )

    assert update.call_args.args[3] == "2026-07-13 09:00:00+00:00"
    assert update.call_args.args[4] is True


async def test_move_event_writes_to_the_target_and_refreshes_it(
    hass: HomeAssistant,
) -> None:
    source, target = _calendar("Personal"), _calendar("Work")
    entry = await _setup(hass, [source, target])

    with (
        patch("custom_components.ha_caldav.services.move_event") as move,
        patch.object(
            _managed(entry, "Work").coordinator, "async_request_refresh"
        ) as refresh,
    ):
        await hass.services.async_call(
            DOMAIN,
            "move_event",
            {
                "entity_id": "calendar.iven_personal",
                "uid": "uid-1",
                "target_entity_id": "calendar.iven_work",
            },
            blocking=True,
        )

    assert move.call_args.args[0] is source
    assert move.call_args.args[1] is target
    assert move.call_args.args[2] == "uid-1"
    assert move.call_args.args[3] is False
    # Both lists changed, so the target has to poll again as well.
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
                "entity_id": "calendar.iven_personal",
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
            {"entity_id": "calendar.iven_personal", "uid": "uid-1"},
            blocking=True,
            return_response=True,
        )

    assert result["calendar.iven_personal"]["ics"] == ICS
    assert export.call_args.args[1] == "uid-1"


async def test_import_forwards_the_document(hass: HomeAssistant) -> None:
    await _setup(hass)

    with patch("custom_components.ha_caldav.services.import_ics") as importer:
        await hass.services.async_call(
            DOMAIN,
            "import_ics",
            {"entity_id": "calendar.iven_personal", "ics": ICS},
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
            {"entity_id": "calendar.iven_personal", "color": "#00679e"},
            blocking=True,
        )

    assert setter.call_args.args[0] is calendar
    assert setter.call_args.args[1] == "#00679e"
    # The color we just pushed has to become the one the entity shows.
    refresh.assert_called_once()


async def test_invitation_needs_a_scheduling_server(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    assert entry.runtime_data.address_set == []

    with pytest.raises(ServiceValidationError) as refusal:
        await hass.services.async_call(
            DOMAIN,
            "respond_to_invitation",
            {
                "entity_id": "calendar.iven_personal",
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
                "entity_id": "calendar.iven_personal",
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
    # Without the reload the collection exists on the server and nowhere else.
    reload.assert_called_once_with(entry.entry_id)


async def test_a_new_calendar_is_added_to_an_explicit_selection(
    hass: HomeAssistant,
) -> None:
    # Anyone who has opened the options once has a list, and a calendar
    # missing from it is skipped at setup.
    entry = await _setup(hass, options={CONF_CALENDARS: ["Personal"]})

    with (
        patch("custom_components.ha_caldav.services.create_calendar"),
        patch("custom_components.ha_caldav.caldav.DAVClient") as client,
    ):
        client.return_value.principal.return_value.calendars.return_value = []
        await hass.services.async_call(
            DOMAIN,
            "create_calendar",
            {"config_entry_id": entry.entry_id, "name": "Holidays"},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_CALENDARS] == ["Personal", "Holidays"]


async def test_deleting_a_calendar_removes_it_and_its_entities(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    entry = await _setup(hass, [calendar])
    registry = er.async_get(hass)
    assert registry.async_get("calendar.iven_personal") is not None

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
    assert registry.async_get("calendar.iven_personal") is None
    assert registry.async_get("todo.iven_personal") is None


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
            {"entity_id": "calendar.iven_personal", "text": "x"},
            blocking=True,
            return_response=True,
        )

    # The collection url carries the account name; it belongs in the log only.
    assert "cloud.example.com" not in str(raised.value)


async def test_search_window_is_passed_through(hass: HomeAssistant) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    start = datetime.now(UTC).replace(microsecond=0)

    await hass.services.async_call(
        DOMAIN,
        "search_events",
        {
            "entity_id": "calendar.iven_personal",
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
                "entity_id": "calendar.iven_personal",
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
                "entity_id": "calendar.iven_personal",
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
                "entity_id": "calendar.iven_personal",
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
                "entity_id": "calendar.iven_personal",
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
                "entity_id": "calendar.iven_personal",
                "summary": "Broken",
                "start_date": "2026-07-06",
                "end_date_time": "2026-07-06 10:00:00",
            },
            blocking=True,
        )

    assert refusal.value.translation_key == "mixed_time_types"


async def test_a_misspelled_recurrence_range_is_refused(hass: HomeAssistant) -> None:
    await _setup(hass)

    # Accepting it would silently edit the single occurrence instead.
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "update_event",
            {
                "entity_id": "calendar.iven_personal",
                "uid": "uid-1",
                "recurrence_id": "2026-07-13 09:00:00+00:00",
                "recurrence_range": "thisandfuture",
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
        ("import_ics", {"ics": ICS}),
        ("set_calendar_color", {"color": "#00679e"}),
        ("move_event", {"uid": "uid-1", "target_entity_id": "calendar.iven_personal"}),
        ("respond_to_invitation", {"uid": "uid-1", "response": "accept"}),
    ):
        with pytest.raises(ServiceValidationError) as refusal:
            await hass.services.async_call(
                DOMAIN,
                service,
                {"entity_id": "calendar.iven_personal", **data},
                blocking=True,
            )
        # Named, because any other failure would satisfy a bare raises() too.
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

    # The source is writable, so only the target's own state can stop this.
    with pytest.raises(ServiceValidationError) as refusal:
        await hass.services.async_call(
            DOMAIN,
            "move_event",
            {
                "entity_id": "calendar.iven_personal",
                "uid": "uid-1",
                "target_entity_id": "calendar.iven_work",
            },
            blocking=True,
        )

    assert refusal.value.translation_key == "read_only"
    assert target.save_event.call_count == 0


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
    entity = hass.data["entity_components"]["calendar"].get_entity(
        "calendar.iven_personal"
    )
    entity._picked = True

    await hass.services.async_call(
        DOMAIN,
        "set_calendar_color",
        {"entity_id": "calendar.iven_personal", "color": "#123456"},
        blocking=True,
    )

    # Otherwise the entity keeps showing the old color it was given by hand,
    # while every other client already has the new one.
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
                "entity_id": "calendar.iven_personal",
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
    # Registered by somebody else, and their entry is loaded. Aiming at an
    # unregistered entity, or one whose entry is not loaded, would be caught by
    # a different check and leave this one untested.
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
                "entity_id": "calendar.iven_personal",
                "uid": "uid-1",
                "target_entity_id": "calendar.google_work",
            },
            blocking=True,
        )

    assert refusal.value.translation_key == "unknown_target"
    # Nothing set this entry up, so nothing may try to tear it down either.
    foreign.mock_state(hass, ConfigEntryState.NOT_LOADED)


async def test_a_move_refreshes_the_calendar_it_came_from(
    hass: HomeAssistant,
) -> None:
    source, target = _calendar("Personal"), _calendar("Work")
    entry = await _setup(hass, [source, target])

    with (
        patch("custom_components.ha_caldav.services.move_event"),
        patch.object(
            _managed(entry, "Personal").coordinator, "async_request_refresh"
        ) as refresh,
    ):
        await hass.services.async_call(
            DOMAIN,
            "move_event",
            {
                "entity_id": "calendar.iven_personal",
                "uid": "uid-1",
                "target_entity_id": "calendar.iven_work",
            },
            blocking=True,
        )

    # The event left this calendar, so it is as stale as the target.
    refresh.assert_called_once()


async def test_a_free_busy_answer_the_library_cannot_read_is_reported(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    calendar.freebusy_request.return_value = Mock(
        instance=Mock(vfreebusy=Mock(spec=[]))
    )
    await _setup(hass, [calendar])

    with pytest.raises(HomeAssistantError) as failure:
        await hass.services.async_call(
            DOMAIN,
            "get_free_busy",
            {
                "entity_id": "calendar.iven_personal",
                "start": "2026-07-06 00:00:00",
                "end": "2026-07-07 00:00:00",
            },
            blocking=True,
            return_response=True,
        )

    # A server is free to answer with something vobject parses and we cannot
    # read; that has to reach the user as a failure, not as a traceback.
    assert failure.value.translation_key is not None


async def test_create_event_forwards_the_recurrence_rule(hass: HomeAssistant) -> None:
    # rrule is declared in both schemas and described in services.yaml; without
    # this the field could stop reaching the server and every test stay green.
    await _setup(hass)

    with patch("custom_components.ha_caldav.calendar.create_event") as create:
        await hass.services.async_call(
            DOMAIN,
            "create_event",
            {
                "entity_id": "calendar.iven_personal",
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
                "entity_id": "calendar.iven_personal",
                "uid": "uid-1",
                "rrule": "FREQ=DAILY",
            },
            blocking=True,
        )

    assert update.call_args.args[2] == {"rrule": "FREQ=DAILY"}


async def test_an_empty_rule_is_how_a_recurrence_is_removed(
    hass: HomeAssistant,
) -> None:
    # Otherwise a series is a one-way door: expand strips RRULE from what the
    # frontend echoes back, so nothing else can ever ask for it to go.
    await _setup(hass)

    with patch("custom_components.ha_caldav.calendar.update_event") as update:
        await hass.services.async_call(
            DOMAIN,
            "update_event",
            {"entity_id": "calendar.iven_personal", "uid": "uid-1", "rrule": ""},
            blocking=True,
        )

    assert update.call_args.args[2] == {"rrule": ""}


async def test_moving_into_a_calendar_the_caller_may_not_control_is_refused(
    hass: HomeAssistant,
) -> None:
    """target_entity_id is a field, not a service target.

    Home Assistant checks POLICY_CONTROL for entities named in a target and
    not for one named in a field, and this service writes into the entity
    named here.
    """
    from homeassistant.auth.permissions import PolicyPermissions
    from homeassistant.exceptions import Unauthorized

    await _setup(hass, [_calendar("Personal"), _calendar("Private")])
    user = MockUser()
    user.add_to_hass(hass)
    user.permissions = PolicyPermissions(
        {"entities": {"entity_ids": {"calendar.iven_personal": True}}}, user.id
    )

    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "move_event",
            {
                "entity_id": "calendar.iven_personal",
                "uid": "uid-1",
                "target_entity_id": "calendar.iven_private",
            },
            blocking=True,
            context=Context(user_id=user.id),
        )


def test_an_event_that_ends_when_it_starts_is_refused() -> None:
    """end_date reads as "the last day", and that pair is invalid per RFC 5545."""
    from custom_components.ha_caldav.services import _check_span

    with pytest.raises(ServiceValidationError):
        _check_span(date(2026, 8, 6), date(2026, 8, 6))


def test_a_window_that_ends_before_it_starts_is_refused() -> None:
    """A server answers one with nothing, which reads as "everything is free"."""
    from custom_components.ha_caldav.services import _check_window

    with pytest.raises(ServiceValidationError):
        _check_window(
            datetime(2026, 8, 7, tzinfo=UTC), datetime(2026, 8, 6, tzinfo=UTC)
        )
    _check_window(None, None)


def test_an_update_naming_nothing_but_a_uid_is_refused() -> None:
    """The PUT would move SEQUENCE and announce a revision nobody made."""
    from custom_components.ha_caldav.services import UPDATE_EVENT_SCHEMA

    with pytest.raises(vol.Invalid):
        UPDATE_EVENT_SCHEMA({"entity_id": "calendar.x", "uid": "uid-1"})

    UPDATE_EVENT_SCHEMA({"entity_id": "calendar.x", "uid": "uid-1", "summary": "New"})


def test_a_color_the_read_path_could_not_recognise_is_refused() -> None:
    """Stored verbatim it comes back as no color, clearing the one there was."""
    import voluptuous as voluptuous_schema

    from custom_components.ha_caldav.const import ATTR_COLOR
    from custom_components.ha_caldav.services import COLOR_SCHEMA

    schema = voluptuous_schema.Schema(COLOR_SCHEMA)
    schema({ATTR_COLOR: "#00679e"})

    with pytest.raises(voluptuous_schema.Invalid):
        schema({ATTR_COLOR: "banana"})


async def test_a_move_named_by_a_user_who_no_longer_exists_is_refused(
    hass: HomeAssistant,
) -> None:
    """Home Assistant checks POLICY_CONTROL for entities named in a target, not
    for one named in a field, and this service writes into the entity named
    there. The arm beside the permission check was never exercised."""
    from homeassistant.core import Context
    from homeassistant.exceptions import UnknownUser

    await _setup(hass)

    with pytest.raises(UnknownUser):
        await hass.services.async_call(
            DOMAIN,
            "move_event",
            {
                "entity_id": "calendar.iven_personal",
                "uid": "uid-1",
                "target_entity_id": "calendar.iven_work",
            },
            blocking=True,
            context=Context(user_id="nobody-by-that-id"),
        )


async def test_replying_to_an_invitation_clears_the_events_etag(
    hass: HomeAssistant,
) -> None:
    """The reply is a PUT, so the etag the coordinator holds is stale the
    moment it lands, and the refresh behind it is debounced. Editing the event
    straight afterwards was refused over a conflict that was the user's own
    reply."""
    entry = await _setup(hass)
    entity = hass.data["entity_components"]["calendar"].get_entity(
        "calendar.iven_personal"
    )
    entity.coordinator.etags = {"uid-1": '"e"'}
    entry.runtime_data.address_set = ["mailto:iven@example.com"]

    with patch("custom_components.ha_caldav.services.respond_to_invitation"):
        await hass.services.async_call(
            DOMAIN,
            "respond_to_invitation",
            {
                "entity_id": "calendar.iven_personal",
                "uid": "uid-1",
                "response": "accept",
            },
            blocking=True,
        )

    assert "uid-1" not in entity.coordinator.etags


async def test_deleting_a_calendar_takes_its_key_out_of_the_selection(
    hass: HomeAssistant,
) -> None:
    """The options flow writes calendar keys and create_calendar writes a
    display name, so matching only one shape left the other behind forever and
    the option ended up holding keys for collections that no longer exist."""
    calendar = _calendar("Personal")
    entry = await _setup(
        hass, [calendar], options={CONF_CALENDARS: ["/remote.php/dav/Personal"]}
    )

    with (
        patch("custom_components.ha_caldav.services.delete_calendar"),
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

    assert entry.options[CONF_CALENDARS] == []


async def test_a_move_between_two_accounts_sharing_a_path_is_allowed(
    hass: HomeAssistant,
) -> None:
    """Two Nextcloud accounts both call their default calendar /personal/, and
    the guard compared a key that drops the scheme and the host by design. A
    move from one server to the other was refused as a move onto itself."""
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
                "entity_id": "calendar.iven_personal",
                "uid": "uid-1",
                "target_entity_id": "calendar.work_personal",
            },
            blocking=True,
        )

    assert move.call_args.args[1] is there


async def test_a_search_is_read_off_the_event_loop(hass: HomeAssistant) -> None:
    """vobject parses an item the first time its components are asked for, so
    building the result costs as much as the request does, and the window is
    optional here: an unbounded text search over a large collection is
    unbounded time with all of Home Assistant waiting on it."""
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
            {"entity_id": "calendar.iven_personal", "text": "standup"},
            blocking=True,
            return_response=True,
        )

    assert seen and all(name != threading.main_thread().name for name in seen)


async def test_a_window_that_starts_and_ends_at_the_same_moment_is_allowed(
    hass: HomeAssistant,
) -> None:
    """The refusal is for a window that ends before it starts. One step out and
    an instantaneous window is refused too, which is a legitimate thing to ask
    a free/busy report for."""
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
        {"entity_id": "calendar.iven_personal", "start": moment, "end": moment},
        blocking=True,
        return_response=True,
    )

    assert response == {"calendar.iven_personal": {"periods": []}}


async def test_a_target_named_by_a_user_who_no_longer_exists_is_refused(
    hass: HomeAssistant,
) -> None:
    """Home Assistant checks POLICY_CONTROL for entities named in a target, not
    for one named in a field, and this service writes into the entity named
    there. Driven directly: the service call is refused by core's own check
    before ever reaching here, so nothing going through it can tell whether
    this arm exists.
    """
    from homeassistant.core import Context
    from homeassistant.exceptions import UnknownUser

    from custom_components.ha_caldav.services import _async_check_control

    await _setup(hass)
    entity = hass.data["entity_components"]["calendar"].get_entity(
        "calendar.iven_personal"
    )
    call = Mock(context=Context(user_id="nobody-by-that-id"))

    with pytest.raises(UnknownUser):
        await _async_check_control(entity, call, "calendar.iven_work")
