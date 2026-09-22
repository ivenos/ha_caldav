import asyncio
from datetime import UTC, datetime, timedelta
import time
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from caldav.elements import dav
from caldav.lib.error import AuthorizationError, DAVError
from homeassistant.components.calendar import CalendarEntityFeature
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from requests import Timeout
import vobject

from custom_components.ha_caldav.const import (
    CONF_CALENDARS,
    CONF_DAYS,
    CONF_INCLUDE_ALL_DAY,
    CONF_READ_ONLY,
    DOMAIN,
)
from custom_components.ha_caldav.coordinator import _MAX_KEPT_POLLS, _TodoRead

ENTRY_DATA = {
    CONF_URL: "https://cloud.example.com/remote.php/dav",
    CONF_USERNAME: "iven",
    CONF_PASSWORD: "secret",
    CONF_VERIFY_SSL: True,
}


def _calendar(name: str) -> Mock:
    calendar = Mock()
    calendar.name = name
    calendar.url = f"https://cloud.example.com/remote.php/dav/{name}"
    calendar.search.return_value = []
    return calendar


async def _setup(
    hass: HomeAssistant, calendars: list[Mock], options: dict | None = None
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
        client.return_value.principal.return_value.calendars.return_value = calendars
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _entity(hass: HomeAssistant, entity_id: str):
    component: EntityComponent = hass.data["entity_components"]["calendar"]
    return component.get_entity(entity_id)


async def test_entity_per_calendar_with_full_crud(hass: HomeAssistant) -> None:
    await _setup(hass, [_calendar("Personal"), _calendar("Work")])

    state = hass.states.get("calendar.personal")
    assert state is not None
    assert hass.states.get("calendar.work") is not None

    features = state.attributes["supported_features"]
    assert features & CalendarEntityFeature.CREATE_EVENT
    assert features & CalendarEntityFeature.UPDATE_EVENT
    assert features & CalendarEntityFeature.DELETE_EVENT


async def test_calendar_selection_filters_entities(hass: HomeAssistant) -> None:
    await _setup(
        hass,
        [_calendar("Personal"), _calendar("Contact birthdays")],
        options={CONF_CALENDARS: ["Personal"]},
    )

    assert hass.states.get("calendar.personal") is not None
    assert hass.states.get("calendar.contact_birthdays") is None


async def test_read_only_hides_write_features(hass: HomeAssistant) -> None:
    await _setup(hass, [_calendar("Personal")], options={CONF_READ_ONLY: True})

    features = hass.states.get("calendar.personal").attributes["supported_features"]
    assert features == 0


async def test_scan_interval_option_drives_polling(hass: HomeAssistant) -> None:
    await _setup(hass, [_calendar("Personal")], options={CONF_SCAN_INTERVAL: 5})
    entity = _entity(hass, "calendar.personal")

    assert entity.coordinator.update_interval == timedelta(minutes=5)


async def test_scan_interval_defaults_to_fifteen_minutes(hass: HomeAssistant) -> None:
    await _setup(hass, [_calendar("Personal")])
    entity = _entity(hass, "calendar.personal")

    assert entity.coordinator.update_interval == timedelta(minutes=15)


def _result(body: str) -> Mock:
    item = Mock()
    item.vobject_instance = vobject.readOne(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//test//EN\n"
        f"BEGIN:VEVENT\nUID:{abs(hash(body))}\nDTSTAMP:20260101T000000Z\n"
        f"{body}\nEND:VEVENT\nEND:VCALENDAR\n"
    )
    return item


def _search_item(summary: str, start_offset: timedelta) -> Mock:
    start = dt_util.utcnow() + start_offset
    end = start + timedelta(hours=1)
    item = Mock()
    item.vobject_instance = vobject.readOne(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//test//EN\n"
        f"BEGIN:VEVENT\nUID:{summary}\nDTSTAMP:20260101T000000Z\n"
        f"DTSTART:{start:%Y%m%dT%H%M%S}Z\nDTEND:{end:%Y%m%dT%H%M%S}Z\n"
        f"SUMMARY:{summary}\nEND:VEVENT\nEND:VCALENDAR\n"
    )
    return item


async def test_an_event_that_already_ended_is_not_the_upcoming_one(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    calendar.search.return_value = [
        _search_item("Over", timedelta(hours=-4)),
        _search_item("Upcoming", timedelta(hours=4)),
    ]
    await _setup(hass, [calendar])

    assert _entity(hass, "calendar.personal").event.summary == "Upcoming"


async def test_a_window_holding_only_past_events_has_no_upcoming_one(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    calendar.search.return_value = [_search_item("Over", timedelta(hours=-4))]
    await _setup(hass, [calendar])

    assert _entity(hass, "calendar.personal").event is None


async def test_next_event_ignores_server_result_order(hass: HomeAssistant) -> None:
    # caldav's search makes no ordering promise.
    calendar = _calendar("Personal")
    calendar.search.return_value = [
        _search_item("Later", timedelta(days=3)),
        _search_item("Sooner", timedelta(days=1)),
    ]
    await _setup(hass, [calendar])

    entity = _entity(hass, "calendar.personal")
    assert entity.event.summary == "Sooner"


async def test_delete_forwards_recurrence_range(hass: HomeAssistant) -> None:
    await _setup(hass, [_calendar("Personal")])
    entity = _entity(hass, "calendar.personal")

    with patch("custom_components.ha_caldav.calendar.delete_event") as delete:
        await entity.async_delete_event(
            "uid-1",
            recurrence_id="2026-07-13 09:00:00+00:00",
            recurrence_range="THISANDFUTURE",
        )

    assert delete.call_args.args[1:] == ("uid-1", "2026-07-13 09:00:00+00:00", True)


async def test_delete_single_occurrence_is_not_this_and_future(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, [_calendar("Personal")])
    entity = _entity(hass, "calendar.personal")

    with patch("custom_components.ha_caldav.calendar.delete_event") as delete:
        await entity.async_delete_event(
            "uid-1", recurrence_id="2026-07-13 09:00:00+00:00"
        )

    assert delete.call_args.args[1:] == ("uid-1", "2026-07-13 09:00:00+00:00", False)


async def test_server_error_becomes_home_assistant_error(hass: HomeAssistant) -> None:
    await _setup(hass, [_calendar("Personal")])
    entity = _entity(hass, "calendar.personal")

    with (
        patch(
            "custom_components.ha_caldav.calendar.delete_event",
            side_effect=DAVError(
                "500 Server Error at 'https://cloud.example.com/dav/iven/personal/'"
            ),
        ),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await entity.async_delete_event("uid-1")

    # The server's own text names the collection, and with it the account.
    assert "cloud.example.com" not in str(raised.value)


async def test_unchanged_calendar_skips_the_expand(hass: HomeAssistant) -> None:
    calendar = _calendar("Personal")
    # A stable sync-token means nothing was touched between polls.
    calendar.objects_by_sync_token.return_value.sync_token = "token-1"
    await _setup(hass, [calendar])
    entity = _entity(hass, "calendar.personal")

    calls = calendar.search.call_count
    await entity.coordinator.async_refresh()

    assert calendar.search.call_count == calls


async def test_changed_calendar_refetches(hass: HomeAssistant) -> None:
    calendar = _calendar("Personal")
    calendar.objects_by_sync_token.side_effect = lambda token=None: Mock(
        sync_token=object()
    )
    await _setup(hass, [calendar])
    entity = _entity(hass, "calendar.personal")

    calls = calendar.search.call_count
    await entity.coordinator.async_refresh()

    assert calendar.search.call_count > calls


async def test_change_survives_a_failed_poll(hass: HomeAssistant) -> None:
    from custom_components.ha_caldav.capability import Capability
    from custom_components.ha_caldav.coordinator import HaCaldavCoordinator

    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="x")
    entry.add_to_hass(hass)
    calendar = _calendar("Personal")
    calendar.objects_by_sync_token.side_effect = [
        Mock(sync_token="t1"),
        Mock(sync_token="t2"),
        Mock(sync_token="t2"),
    ]
    displays = iter(
        [
            [_search_item("First", timedelta(days=1))],
            Timeout("boom"),
            [_search_item("Second", timedelta(days=1))],
        ]
    )

    def search(**kwargs):
        if kwargs.get("expand"):
            result = next(displays)
            if isinstance(result, Exception):
                raise result
            return result
        return []

    calendar.search.side_effect = search
    coordinator = HaCaldavCoordinator(
        hass,
        entry,
        calendar,
        Capability(frozenset({"VEVENT"}), writable=True),
        days=7,
        include_all_day=True,
        scan_interval=timedelta(minutes=15),
    )

    await coordinator.async_refresh()
    assert coordinator.data.next_event.summary == "First"
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert coordinator.data.next_event.summary == "Second"


def _event_fields() -> dict:
    return {
        "summary": "x",
        "dtstart": datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
        "dtend": datetime(2026, 7, 6, 10, 0, tzinfo=UTC),
    }


async def test_update_forwards_etag_and_clears_it_after_write(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, [_calendar("Personal")])
    entity = _entity(hass, "calendar.personal")
    entity.coordinator.etags = {"uid-1": '"e"'}

    with patch("custom_components.ha_caldav.calendar.update_event") as update:
        await entity.async_update_event("uid-1", _event_fields())

    assert update.call_args.kwargs["expected_etag"] == '"e"'
    assert "uid-1" not in entity.coordinator.etags


async def test_delete_forwards_etag_and_clears_it_after_write(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, [_calendar("Personal")])
    entity = _entity(hass, "calendar.personal")
    entity.coordinator.etags = {"uid-1": '"e"'}

    with patch("custom_components.ha_caldav.calendar.delete_event") as delete:
        await entity.async_delete_event("uid-1")

    assert delete.call_args.kwargs["expected_etag"] == '"e"'
    assert "uid-1" not in entity.coordinator.etags


def _etag_item(uid: str, etag: str) -> Mock:
    item = Mock()
    item.vobject_instance = vobject.readOne(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//test//EN\n"
        f"BEGIN:VEVENT\nUID:{uid}\nDTSTAMP:20260101T000000Z\n"
        "DTSTART:20260706T090000Z\nDTEND:20260706T100000Z\nSUMMARY:x\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )
    item.props = {dav.GetEtag.tag: etag}
    return item


async def test_get_events_caches_etags(hass: HomeAssistant) -> None:
    calendar = _calendar("Personal")
    calendar.search.side_effect = lambda **kw: (
        [_etag_item("uid-1", '"e"')] if kw.get("expand") is False else []
    )
    await _setup(hass, [calendar])
    entity = _entity(hass, "calendar.personal")

    await entity.async_get_events(
        hass, dt_util.utcnow(), dt_util.utcnow() + timedelta(days=7)
    )

    assert entity.coordinator.etags == {"uid-1": '"e"'}


async def test_failed_write_keeps_etag_for_retry(hass: HomeAssistant) -> None:
    await _setup(hass, [_calendar("Personal")])
    entity = _entity(hass, "calendar.personal")
    entity.coordinator.etags = {"uid-1": '"e"'}

    with (
        patch(
            "custom_components.ha_caldav.calendar.update_event",
            side_effect=DAVError("boom"),
        ),
        pytest.raises(HomeAssistantError),
    ):
        await entity.async_update_event("uid-1", _event_fields())

    assert entity.coordinator.etags["uid-1"] == '"e"'


async def test_get_events_survives_etag_refresh_failure(hass: HomeAssistant) -> None:
    calendar = _calendar("Personal")

    def search(**kw):
        if kw.get("expand") is False:
            raise DAVError("boom")
        return []

    calendar.search.side_effect = search
    await _setup(hass, [calendar])
    entity = _entity(hass, "calendar.personal")

    events = await entity.async_get_events(
        hass, dt_util.utcnow(), dt_util.utcnow() + timedelta(days=7)
    )

    assert events == []
    assert entity.coordinator.etags == {}


async def test_a_rejected_password_starts_a_reauth_flow(hass: HomeAssistant) -> None:
    from caldav.lib.error import AuthorizationError

    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.personal").coordinator
    calendar.objects_by_sync_token.side_effect = AuthorizationError(
        reason="Unauthorized"
    )
    calendar.search.side_effect = AuthorizationError(reason="Unauthorized")

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert any(
        flow["context"]["source"] == "reauth"
        for flow in hass.config_entries.flow.async_progress()
    )
    assert coordinator.last_update_success is False


async def test_a_server_failure_is_reported_once_not_as_a_traceback(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.personal").coordinator
    calendar.objects_by_sync_token.side_effect = DAVError("boom")
    calendar.search.side_effect = DAVError("boom")

    for _ in range(_MAX_KEPT_POLLS + 1):
        await coordinator.async_refresh()

    # UpdateFailed is the one the coordinator logs once instead of per poll.
    assert coordinator.last_update_success is False
    assert coordinator.last_exception is not None
    assert type(coordinator.last_exception).__name__ == "UpdateFailed"


async def test_a_failing_todo_half_does_not_take_the_calendar_down(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    events = [_search_item("Standup", timedelta(days=1))]
    calls = {"todo": 0}

    def search(**kwargs):
        if kwargs.get("todo"):
            calls["todo"] += 1
            if calls["todo"] > 1:
                raise Timeout("the completed items time out")
            return []
        return events if kwargs.get("expand") else []

    calendar.search.side_effect = search
    calendar.objects_by_sync_token.side_effect = lambda token=None: Mock(
        sync_token=object()
    )
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.personal").coordinator

    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert coordinator.data.next_event.summary == "Standup"


async def test_a_rejected_sync_token_is_dropped(hass: HomeAssistant) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.personal").coordinator
    coordinator._sync_token = "stale"
    calendar.objects_by_sync_token.side_effect = DAVError("403 valid-sync-token")

    await coordinator.async_refresh()

    assert coordinator._sync_token is None


async def test_etags_are_cached_by_the_poll_not_only_by_the_panel(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    item = _search_item("Standup", timedelta(days=1))
    tagged = Mock()
    tagged.vobject_instance = item.vobject_instance
    tagged.props = {dav.GetEtag.tag: '"etag-1"'}

    def search(**kwargs):
        if kwargs.get("todo"):
            return []
        return [item] if kwargs.get("expand") else [tagged]

    calendar.search.side_effect = search
    await _setup(hass, [calendar])

    assert _entity(hass, "calendar.personal").coordinator.etags == {
        "Standup": '"etag-1"'
    }


async def test_the_panel_window_caches_its_own_etags(hass: HomeAssistant) -> None:
    calendar = _calendar("Personal")
    far = _search_item("Retro", timedelta(days=90))
    window = dt_util.now() + timedelta(days=80)

    def search(**kwargs):
        if kwargs.get("todo") or kwargs.get("start") is None:
            return []
        if kwargs["start"] < window:
            return []
        if kwargs.get("expand"):
            return [far]
        tagged = Mock()
        tagged.vobject_instance = far.vobject_instance
        tagged.props = {dav.GetEtag.tag: '"etag-far"'}
        return [tagged]

    calendar.search.side_effect = search
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.personal").coordinator
    assert coordinator.etags == {}

    await coordinator.async_get_events(window, window + timedelta(days=20))

    assert coordinator.etags == {"Retro": '"etag-far"'}


async def test_a_half_that_never_answered_takes_only_its_own_entity_down(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")

    def search(**kwargs):
        if kwargs.get("todo"):
            return []
        raise Timeout("boom")

    calendar.search.side_effect = search
    entry = await _setup(hass, [calendar])

    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("calendar.personal").state == STATE_UNAVAILABLE
    assert hass.states.get("todo.personal").state != STATE_UNAVAILABLE


async def test_a_half_that_answered_before_keeps_its_last_result(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    item = _search_item("Standup", timedelta(days=1))
    fail_todos = False

    def search(**kwargs):
        if kwargs.get("todo"):
            if fail_todos:
                raise Timeout("boom")
            return []
        return [item] if kwargs.get("expand") else []

    calendar.search.side_effect = search
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.personal").coordinator

    fail_todos = True
    calendar.objects_by_sync_token.side_effect = Timeout("boom")
    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert coordinator.data.next_event.summary == "Standup"


async def test_an_event_core_refuses_does_not_take_the_calendar_down(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    good = _search_item("Standup", timedelta(days=1))
    broken = Mock()
    broken.vobject_instance = vobject.readOne(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VEVENT\r\nUID:Backwards\r\nDTSTAMP:20260101T000000Z\r\n"
        "DTSTART:20260706T100000Z\r\nDTEND:20260706T090000Z\r\n"
        "SUMMARY:Backwards\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )

    def search(**kwargs):
        return [] if kwargs.get("todo") else [broken, good]

    calendar.search.side_effect = search
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.personal").coordinator

    assert coordinator.last_update_success is True
    assert coordinator.data.next_event.summary == "Standup"


async def test_the_extra_attributes_stay_out_of_the_recorder(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, [_calendar("Personal")])
    entity = _entity(hass, "calendar.personal")

    # Core reads the combined set, not the per-entity attribute.
    combined = entity._Entity__combined_unrecorded_attributes
    assert {"attendees", "organizer", "url", "alarms"} <= combined


async def test_the_days_option_sets_the_search_window(hass: HomeAssistant) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar], options={CONF_DAYS: 30})

    expand = next(
        call for call in calendar.search.call_args_list if call.kwargs.get("expand")
    )
    window = expand.kwargs["end"] - expand.kwargs["start"]
    assert window == timedelta(days=30)


async def test_an_all_day_event_is_skipped_when_the_option_is_off(
    hass: HomeAssistant,
) -> None:
    day = dt_util.now().date() + timedelta(days=1)
    calendar = _calendar("Personal")
    calendar.search.return_value = [
        _result(
            f"DTSTART;VALUE=DATE:{day:%Y%m%d}\n"
            f"DTEND;VALUE=DATE:{day + timedelta(days=1):%Y%m%d}\nSUMMARY:Holiday"
        )
    ]

    await _setup(hass, [calendar], options={CONF_INCLUDE_ALL_DAY: False})

    assert hass.states.get("calendar.personal").state == "off"
    assert hass.states.get("calendar.personal").attributes.get("message") is None


async def test_an_all_day_event_is_used_when_the_option_is_on(
    hass: HomeAssistant,
) -> None:
    day = dt_util.now().date() + timedelta(days=1)
    calendar = _calendar("Personal")
    calendar.search.return_value = [
        _result(
            f"DTSTART;VALUE=DATE:{day:%Y%m%d}\n"
            f"DTEND;VALUE=DATE:{day + timedelta(days=1):%Y%m%d}\nSUMMARY:Holiday"
        )
    ]

    await _setup(hass, [calendar], options={CONF_INCLUDE_ALL_DAY: True})

    assert hass.states.get("calendar.personal").attributes["message"] == "Holiday"


async def test_an_event_without_a_start_does_not_take_the_calendar_down(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    start = dt_util.utcnow() + timedelta(hours=1)
    calendar.search.return_value = [
        # RFC 5545 makes DTSTART optional once the object carries a METHOD.
        _result("SUMMARY:No start"),
        _result(
            f"DTSTART:{start:%Y%m%dT%H%M%S}Z\n"
            f"DTEND:{start + timedelta(hours=1):%Y%m%dT%H%M%S}Z\nSUMMARY:Standup"
        ),
    ]

    await _setup(hass, [calendar])

    assert hass.states.get("calendar.personal").attributes["message"] == "Standup"


async def test_the_poll_does_not_ask_caldav_to_split_the_expansion(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])

    # caldav's split copies and reparses the whole expanded object once per
    # occurrence, which is quadratic in their number.
    expand = next(
        call for call in calendar.search.call_args_list if call.kwargs.get("expand")
    )
    assert expand.kwargs["split_expanded"] is False


async def test_a_failed_poll_does_not_log_the_collection_url(
    hass: HomeAssistant, caplog
) -> None:
    calendar = _calendar("Personal")
    calendar.search.side_effect = DAVError(
        "AuthorizationError at 'https://cloud.example.com/dav/calendars/iven/', "
        "reason Forbidden"
    )

    await _setup(hass, [calendar])

    # A caldav error prints the collection it was reading, hence the account.
    logged = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("Could not read Personal: DAVError" in line for line in logged)
    assert not any("iven" in line for line in logged)


async def test_a_calendar_without_a_name_still_gets_one(hass: HomeAssistant) -> None:
    nameless = _calendar("Personal")
    nameless.name = None
    await _setup(hass, [nameless])
    entity = _entity(hass, "calendar.caldav")

    # A collection is not required to carry a display name.
    assert entity.coordinator.name == "CalDAV"


async def test_the_only_half_of_a_calendar_rides_out_a_failure_then_gives_up(
    hass: HomeAssistant,
) -> None:
    from custom_components.ha_caldav.capability import Capability

    calendar = _calendar("Personal")
    events = [_search_item("Standup", timedelta(days=1))]
    calendar.search.side_effect = lambda **kw: events if kw.get("expand") else []
    with patch(
        "custom_components.ha_caldav.fetch_capabilities",
        return_value={
            "/remote.php/dav/Personal": Capability(frozenset({"VEVENT"}), True)
        },
    ):
        await _setup(hass, [calendar])
    entity = _entity(hass, "calendar.personal")
    calendar.objects_by_sync_token.return_value.sync_token = "moved-on"
    calendar.search.side_effect = Timeout("gone")

    await entity.coordinator.async_refresh()

    assert entity.coordinator.last_update_success is True
    assert entity.available is True
    assert entity.coordinator.data.next_event.summary == "Standup"

    for _ in range(_MAX_KEPT_POLLS):
        await entity.coordinator.async_refresh()

    assert entity.coordinator.last_update_success is False
    assert entity.available is False


async def test_a_poll_drops_the_etag_of_an_event_it_no_longer_finds(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    entity = _entity(hass, "calendar.personal")
    entity.coordinator._etag_window = {"deleted-1"}
    entity.coordinator.etags = {"deleted-1": "old-etag"}
    entity.coordinator.halves["events"].cached = None

    await entity.coordinator.async_refresh()

    assert "deleted-1" not in entity.coordinator.etags


async def test_a_poll_keeps_the_etags_of_a_window_it_did_not_read(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    entity = _entity(hass, "calendar.personal")
    entity.coordinator.etags = {"next-month-1": "panel-etag"}
    entity.coordinator.halves["events"].cached = None

    await entity.coordinator.async_refresh()

    assert entity.coordinator.etags["next-month-1"] == "panel-etag"


@pytest.mark.parametrize(
    "failure",
    [
        TypeError("caldav choked on a captive portal's html"),
        AssertionError("unexpected multistatus"),
        OSError("connection reset"),
    ],
)
async def test_a_failed_panel_read_is_reported_as_a_home_assistant_error(
    hass: HomeAssistant, failure: Exception
) -> None:
    # The calendar panel drops a subscription that raises anything else and
    # then waits forever, and the REST view answers a plain-text traceback.
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    entity = _entity(hass, "calendar.personal")
    calendar.search.side_effect = failure

    with pytest.raises(HomeAssistantError):
        await entity.async_get_events(
            hass, dt_util.utcnow(), dt_util.utcnow() + timedelta(days=7)
        )


async def test_a_partly_failed_poll_does_not_commit_the_sync_token(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    calendar.objects_by_sync_token.return_value.sync_token = "token-1"
    await _setup(hass, [calendar])
    entity = _entity(hass, "calendar.personal")
    calendar.objects_by_sync_token.return_value.sync_token = "token-2"

    def half(**kw):
        if kw.get("todo"):
            raise Timeout("todo half is down")
        return []

    calendar.search.side_effect = half
    await entity.coordinator.async_refresh()

    assert entity.coordinator._sync_token != "token-2"


async def test_the_window_rolls_over_at_midnight(hass: HomeAssistant) -> None:
    calendar = _calendar("Personal")
    calendar.objects_by_sync_token.return_value.sync_token = "steady"
    await _setup(hass, [calendar])
    entity = _entity(hass, "calendar.personal")

    calls = calendar.search.call_count
    with patch(
        "custom_components.ha_caldav.coordinator.dt_util.start_of_local_day",
        return_value=dt_util.start_of_local_day() + timedelta(days=1),
    ):
        await entity.coordinator.async_refresh()

    assert calendar.search.call_count > calls


async def test_an_event_without_a_uid_does_not_take_the_poll_down(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    item = Mock()
    item.vobject_instance = vobject.readOne(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//EN\n"
        "BEGIN:VEVENT\nDTSTAMP:20260101T000000Z\nDTSTART:20260706T090000Z\n"
        "SUMMARY:No uid\nEND:VEVENT\nEND:VCALENDAR\n"
    )
    item.props = {dav.GetEtag.tag: '"e"'}
    calendar.search.return_value = [item]
    await _setup(hass, [calendar])
    entity = _entity(hass, "calendar.personal")

    assert entity.coordinator.last_update_success is True


async def test_the_panel_gets_the_recurrence_rule_of_a_series(
    hass: HomeAssistant,
) -> None:
    """Expanding a series strips RRULE from every occurrence it produces."""
    calendar = _calendar("Personal")
    start = dt_util.utcnow() + timedelta(days=1)
    occurrence = Mock()
    occurrence.vobject_instance = vobject.readOne(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//EN\n"
        f"BEGIN:VEVENT\nUID:series-1\nDTSTAMP:20260101T000000Z\n"
        f"DTSTART:{start:%Y%m%dT%H%M%S}Z\n"
        f"DTEND:{start + timedelta(hours=1):%Y%m%dT%H%M%S}Z\n"
        "SUMMARY:Standup\nEND:VEVENT\nEND:VCALENDAR\n"
    )
    master = Mock()
    master.vobject_instance = vobject.readOne(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//EN\n"
        f"BEGIN:VEVENT\nUID:series-1\nDTSTAMP:20260101T000000Z\n"
        f"DTSTART:{start:%Y%m%dT%H%M%S}Z\n"
        f"DTEND:{start + timedelta(hours=1):%Y%m%dT%H%M%S}Z\n"
        "RRULE:FREQ=WEEKLY;BYDAY=MO\nSUMMARY:Standup\nEND:VEVENT\nEND:VCALENDAR\n"
    )
    master.props = {dav.GetEtag.tag: '"e"'}
    calendar.search.side_effect = lambda **kw: (
        [master] if kw.get("expand") is False else [occurrence]
    )
    await _setup(hass, [calendar])
    entity = _entity(hass, "calendar.personal")

    events = await entity.async_get_events(
        hass, dt_util.utcnow(), dt_util.utcnow() + timedelta(days=7)
    )

    assert events[0].rrule == "FREQ=WEEKLY;BYDAY=MO"


async def test_a_single_event_carries_no_recurrence_rule(hass: HomeAssistant) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    entity = _entity(hass, "calendar.personal")
    calendar.search.return_value = [_search_item("Dentist", timedelta(days=1))]

    events = await entity.async_get_events(
        hass, dt_util.utcnow(), dt_util.utcnow() + timedelta(days=7)
    )

    assert events[0].rrule is None


async def test_a_series_that_stops_recurring_loses_its_recorded_rule(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    start = dt_util.utcnow() + timedelta(days=1)

    def body(rule: str) -> Mock:
        item = Mock()
        item.vobject_instance = vobject.readOne(
            "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//EN\n"
            f"BEGIN:VEVENT\nUID:series-1\nDTSTAMP:20260101T000000Z\n"
            f"DTSTART:{start:%Y%m%dT%H%M%S}Z\n"
            f"DTEND:{start + timedelta(hours=1):%Y%m%dT%H%M%S}Z\n"
            f"{rule}SUMMARY:Standup\nEND:VEVENT\nEND:VCALENDAR\n"
        )
        item.props = {dav.GetEtag.tag: '"e"'}
        return item

    calendar.search.side_effect = lambda **kw: (
        [body("RRULE:FREQ=WEEKLY\n")] if kw.get("expand") is False else [body("")]
    )
    await _setup(hass, [calendar])
    entity = _entity(hass, "calendar.personal")
    await entity.async_get_events(
        hass, dt_util.utcnow(), dt_util.utcnow() + timedelta(days=7)
    )
    assert entity.coordinator.rrules == {"series-1": "FREQ=WEEKLY"}

    calendar.search.side_effect = lambda **kw: [body("")]
    events = await entity.async_get_events(
        hass, dt_util.utcnow(), dt_util.utcnow() + timedelta(days=7)
    )

    assert entity.coordinator.rrules == {}
    assert events[0].rrule is None


async def test_an_edit_leaves_the_etag_the_refresh_read_back(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    etag = '"v1"'

    def search(**kwargs):
        if kwargs.get("todo") or kwargs.get("expand") is not False:
            return []
        return [_etag_item("uid-1", etag)]

    calendar.search.side_effect = search
    await _setup(hass, [calendar])
    entity = _entity(hass, "calendar.personal")
    assert entity.coordinator.etags == {"uid-1": '"v1"'}

    seen: dict = {}

    async def refresh() -> None:
        seen["etags"] = dict(entity.coordinator.etags)

    with (
        patch("custom_components.ha_caldav.calendar.update_event"),
        patch.object(entity.coordinator, "async_refresh", refresh),
    ):
        await entity.async_update_event("uid-1", _event_fields())

    assert seen["etags"] == {}


async def test_a_poll_that_could_not_read_the_etags_keeps_its_token(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")

    def search(**kwargs):
        if kwargs.get("expand") is False:
            raise DAVError("etag report refused")
        return []

    calendar.search.side_effect = search
    calendar.objects_by_sync_token.return_value = Mock(sync_token="t2")
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.personal").coordinator

    assert coordinator.last_update_success
    assert coordinator._sync_token is None


async def test_a_half_that_keeps_failing_stops_looking_healthy(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.personal").coordinator

    tokens = iter(f"t{n}" for n in range(99))
    calendar.objects_by_sync_token.side_effect = lambda _token: Mock(
        sync_token=next(tokens)
    )
    calendar.search.side_effect = lambda **kwargs: (
        [] if kwargs.get("todo") is None else _raise(DAVError("todo report failed"))
    )
    for _ in range(3):
        await coordinator.async_refresh()
        assert not coordinator.halves["todos"].dead

    await coordinator.async_refresh()

    assert coordinator.halves["todos"].dead
    assert not coordinator.halves["events"].dead
    await hass.async_block_till_done()
    assert hass.states.get("todo.personal").state == STATE_UNAVAILABLE
    assert hass.states.get("calendar.personal").state != STATE_UNAVAILABLE


def _raise(err: Exception):
    raise err


async def test_a_server_that_never_moves_its_sync_token_is_read_again(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    calendar.objects_by_sync_token.return_value = Mock(sync_token="frozen")
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.personal").coordinator

    calendar.search.reset_mock()
    await coordinator.async_refresh()
    assert calendar.search.call_count == 0

    coordinator._fetched_at = dt_util.utcnow() - timedelta(days=1)
    await coordinator.async_refresh()

    assert calendar.search.call_count > 0


async def test_an_edit_without_a_rule_core_never_showed_leaves_it_alone(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, [_calendar("Personal")])
    entity = _entity(hass, "calendar.personal")

    with patch("custom_components.ha_caldav.calendar.update_event") as update:
        await entity.async_update_event("uid-1", _event_fields())

    assert "rrule" not in update.call_args.args[2]


async def test_a_cleared_description_is_removed_not_emptied(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, [_calendar("Personal")])
    entity = _entity(hass, "calendar.personal")

    with patch("custom_components.ha_caldav.calendar.update_event") as update:
        await entity.async_update_event("uid-1", _event_fields() | {"description": ""})

    assert update.call_args.args[2]["description"] is None


async def test_the_panel_etags_and_the_polled_ones_live_side_by_side(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    far = _etag_item("far-1", '"far"')

    def search(**kwargs):
        if kwargs.get("todo") or kwargs.get("expand") is not False:
            return []
        far_away = kwargs["start"] > dt_util.now() + timedelta(days=30)
        return [far] if far_away else [_etag_item("near-1", '"near"')]

    calendar.search.side_effect = search
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.personal").coordinator
    assert coordinator.etags == {"near-1": '"near"'}

    window = dt_util.now() + timedelta(days=80)
    await coordinator.async_get_events(window, window + timedelta(days=20))

    assert coordinator.etags == {"near-1": '"near"', "far-1": '"far"'}


async def test_a_server_that_puts_no_etag_on_a_window_still_reports_a_read(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    bare = _etag_item("uid-1", '"e"')
    bare.props = {}
    calendar.search.side_effect = lambda **kw: [] if kw.get("todo") else [bare]
    calendar.objects_by_sync_token.side_effect = lambda token=None: Mock(
        sync_token=object()
    )
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.personal").coordinator

    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert coordinator.poll_health["last_full_read"] is not None


async def test_a_transient_403_during_a_poll_does_not_force_a_reauth(
    hass: HomeAssistant,
) -> None:
    """caldav raises the same error for 403, which a proxy produces in passing."""
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.personal").coordinator
    calendar.search.side_effect = AuthorizationError(
        url="https://cloud.example.com/dav/", reason="Forbidden"
    )
    coordinator._fetched_at = dt_util.utcnow() - timedelta(days=1)

    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert not [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"].get("source") == "reauth"
    ]


async def test_the_recorded_rule_comes_off_the_master_of_the_object(
    hass: HomeAssistant,
) -> None:
    """RFC 5545 leaves the component order open."""
    calendar = _calendar("Personal")
    item = Mock()
    item.vobject_instance = vobject.readOne(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//test//EN\n"
        "BEGIN:VEVENT\nUID:series-1\nRECURRENCE-ID:20260713T090000Z\n"
        "DTSTAMP:20260101T000000Z\nDTSTART:20260713T110000Z\n"
        "DTEND:20260713T120000Z\nSUMMARY:Moved\nEND:VEVENT\n"
        "BEGIN:VEVENT\nUID:series-1\nDTSTAMP:20260101T000000Z\n"
        "DTSTART:20260706T090000Z\nDTEND:20260706T100000Z\n"
        "RRULE:FREQ=WEEKLY\nSUMMARY:Standup\nEND:VEVENT\nEND:VCALENDAR\n"
    )
    item.props = {dav.GetEtag.tag: '"e"'}
    calendar.search.side_effect = lambda **kw: (
        [item] if kw.get("expand") is False else []
    )

    await _setup(hass, [calendar])
    entity = _entity(hass, "calendar.personal")

    assert entity.coordinator.rrules == {"series-1": "FREQ=WEEKLY"}


async def test_a_dead_half_still_lets_the_other_one_read(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.personal").coordinator

    tokens = iter(f"t{n}" for n in range(99))
    calendar.objects_by_sync_token.side_effect = lambda _token: Mock(
        sync_token=next(tokens)
    )
    seen: list[bool] = []

    def search(**kwargs):
        if kwargs.get("todo") is None:
            raise DAVError("event report failed")
        seen.append(True)
        return []

    calendar.search.side_effect = search
    for _ in range(4):
        await coordinator.async_refresh()

    assert coordinator.halves["events"].dead
    assert len(seen) == 4


async def test_a_dead_half_keeps_the_other_ones_data_and_etags_together(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.personal").coordinator

    tokens = iter(f"t{n}" for n in range(99))
    calendar.objects_by_sync_token.side_effect = lambda _token: Mock(
        sync_token=next(tokens)
    )
    etags = iter(['"t1"', '"t2"', '"t3"', '"t4"'])

    def search(**kwargs):
        if kwargs.get("todo") is None:
            raise DAVError("event report failed")
        return [_todo_etag_item("todo-1", next(etags))]

    calendar.search.side_effect = search
    for _ in range(4):
        await coordinator.async_refresh()

    assert coordinator.halves["events"].dead
    assert coordinator.todo_etags == {"todo-1": '"t4"'}
    assert [item.uid for item in coordinator.data.todos] == ["todo-1"]


def _todo_etag_item(uid: str, etag: str) -> Mock:
    item = Mock()
    item.vobject_instance = vobject.readOne(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//test//EN\n"
        f"BEGIN:VTODO\nUID:{uid}\nDTSTAMP:20260101T000000Z\nSUMMARY:x\n"
        "END:VTODO\nEND:VCALENDAR\n"
    )
    item.props = {dav.GetEtag.tag: etag}
    return item


async def test_a_half_that_recovers_starts_counting_again(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.personal").coordinator

    tokens = iter(f"t{n}" for n in range(99))
    calendar.objects_by_sync_token.side_effect = lambda _token: Mock(
        sync_token=next(tokens)
    )
    broken = [True]

    def search(**kwargs):
        if kwargs.get("todo") is None and broken[0]:
            raise DAVError("event report failed")
        return []

    calendar.search.side_effect = search
    for run in (True, True, False, True, True):
        broken[0] = run
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert coordinator.halves["events"].misses == 2


async def test_one_unplaceable_event_does_not_cost_the_collection_its_entities(
    hass: HomeAssistant,
) -> None:
    """A DTEND in the year 9999 overflows the moment a zone offset reaches it, and
    OverflowError is an ArithmeticError."""
    calendar = _calendar("Personal")
    forever = Mock()
    forever.vobject_instance = vobject.readOne(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//EN\n"
        "BEGIN:VEVENT\nUID:forever\nDTSTAMP:20260101T000000Z\n"
        "DTSTART:20260101T100000Z\nDTEND:99991231T235959Z\n"
        "SUMMARY:Office open\nEND:VEVENT\nEND:VCALENDAR\n"
    )
    calendar.search.return_value = [
        forever,
        _search_item("Upcoming", timedelta(hours=4)),
    ]
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(ZoneInfo("Europe/Berlin"))
    try:
        await _setup(hass, [calendar])
        entity = _entity(hass, "calendar.personal")

        assert entity.coordinator.last_update_success
        assert entity.event.summary == "Upcoming"
    finally:
        dt_util.set_default_time_zone(previous)


async def test_a_polled_window_survives_an_etag_cache_at_its_limit(
    hass: HomeAssistant,
) -> None:
    from custom_components.ha_caldav.coordinator import _CACHE_LIMIT

    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.personal").coordinator
    coordinator.etags = {f"old-{n}": f'"o{n}"' for n in range(_CACHE_LIMIT)}
    coordinator._etag_window = set()

    coordinator._merge_etags(
        {"fresh-1": '"f1"', "fresh-2": '"f2"'}, coordinator._etag_epoch
    )

    assert coordinator.etags["fresh-1"] == '"f1"'
    assert coordinator.etags["fresh-2"] == '"f2"'


async def test_a_todo_report_without_etags_does_not_empty_the_cache(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.personal").coordinator

    tokens = iter(f"t{n}" for n in range(99))
    calendar.objects_by_sync_token.side_effect = lambda _token: Mock(
        sync_token=next(tokens)
    )
    with_etag = [True]

    def search(**kwargs):
        if kwargs.get("todo") is None:
            return []
        item = _todo_etag_item("todo-1", '"v1"')
        if not with_etag[0]:
            item.props = {}
        return [item]

    calendar.search.side_effect = search
    await coordinator.async_refresh()
    assert coordinator.todo_etags == {"todo-1": '"v1"'}

    with_etag[0] = False
    await coordinator.async_refresh()

    assert coordinator.todo_etags == {"todo-1": '"v1"'}


async def test_a_poll_already_reading_cannot_restore_an_etag_a_write_dropped(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.personal").coordinator

    read_at = coordinator._etag_epoch
    coordinator.etags = {"uid-1": '"v1"'}
    coordinator.forget_etags("etags", ("uid-1",))
    assert coordinator.etags == {}

    assert not coordinator._merge_etags({"uid-1": '"v1"'}, read_at)
    assert coordinator.etags == {}


def test_an_event_keeps_its_place_when_home_assistant_refuses_its_rule() -> None:
    """Home Assistant refuses an RRULE its own editor cannot offer, FREQ=HOURLY
    and FREQ=MINUTELY among them."""
    from custom_components.ha_caldav.coordinator import to_event

    vevent = vobject.readOne(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//EN\n"
        "BEGIN:VEVENT\nUID:hourly-1\nDTSTAMP:20260101T000000Z\n"
        "DTSTART:20260706T090000Z\nDTEND:20260706T100000Z\nSUMMARY:Medication\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    ).vevent

    event = to_event(vevent, "FREQ=HOURLY")

    assert event is not None
    assert event.summary == "Medication"
    assert event.rrule is None


async def test_two_edits_of_one_collection_do_not_run_at_the_same_time(
    hass: HomeAssistant,
) -> None:
    """PARALLEL_UPDATES does not reach the panels, which call the entity directly,
    and the Home Assistant test plugin runs a mocked executor job inline."""
    await _setup(hass, [_calendar("Personal")])
    entity = _entity(hass, "calendar.personal")
    inside = 0
    overlapped = False

    def slow_write(*args, **kwargs) -> None:
        nonlocal inside, overlapped
        inside += 1
        overlapped = overlapped or inside > 1
        time.sleep(0.05)
        inside -= 1

    with patch("custom_components.ha_caldav.calendar.update_event", slow_write):
        await asyncio.gather(
            entity.async_update_full_event("uid-1", {"summary": "One"}),
            entity.async_update_full_event("uid-1", {"summary": "Two"}),
        )

    assert not overlapped


async def test_the_panel_creates_an_event_through_the_platform_call(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])

    with patch("custom_components.ha_caldav.calendar.create_event") as write:
        await hass.services.async_call(
            "calendar",
            "create_event",
            {
                "entity_id": "calendar.personal",
                "summary": "Standup",
                "start_date_time": "2026-07-06 09:00:00",
                "end_date_time": "2026-07-06 10:00:00",
                "description": "Daily sync",
                "location": "Room 1",
            },
            blocking=True,
        )

    data = write.call_args.args[1]
    assert data["summary"] == "Standup"
    assert data["description"] == "Daily sync"
    assert data["location"] == "Room 1"


async def test_an_edit_from_the_panel_carries_the_rule_it_was_given(
    hass: HomeAssistant,
) -> None:
    """Home Assistant strips RRULE from the event it echoes back for an occurrence."""
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    entity = _entity(hass, "calendar.personal")

    with patch("custom_components.ha_caldav.calendar.update_event") as write:
        await entity.async_update_event(
            "uid-1",
            {
                "summary": "Standup",
                "dtstart": datetime(2026, 7, 6, 9, tzinfo=UTC),
                "dtend": datetime(2026, 7, 6, 10, tzinfo=UTC),
                "rrule": "FREQ=WEEKLY",
            },
        )

    assert write.call_args.args[2]["rrule"] == "FREQ=WEEKLY"


async def test_a_color_hook_that_arrives_before_registration_is_ignored(
    hass: HomeAssistant,
) -> None:
    """An entity has no registry entry until it is registered."""
    calendar = _calendar("Personal")
    entry = await _setup(hass, [calendar])
    entity = _entity(hass, "calendar.personal")
    entity.registry_entry = None

    entity.async_registry_entry_updated()
    entity.async_follow_server_color()

    assert entity.registry_entry is None
    assert entry.state is ConfigEntryState.LOADED


async def test_a_collection_that_holds_no_events_is_not_searched(
    hass: HomeAssistant,
) -> None:
    """The panel asks every calendar entity it can see for a window."""
    from custom_components.ha_caldav.capability import Capability
    from custom_components.ha_caldav.const import COMPONENT_TODO

    calendar = _calendar("Tasks")
    with patch(
        "custom_components.ha_caldav.capability_for",
        return_value=Capability(frozenset({COMPONENT_TODO}), writable=True),
    ):
        await _setup(hass, [calendar])
    coordinator = (
        hass.data["entity_components"]["todo"].get_entity("todo.tasks").coordinator
    )
    calendar.search.reset_mock()

    found = await coordinator.async_get_events(
        dt_util.utcnow(), dt_util.utcnow() + timedelta(days=1)
    )

    assert found == []
    calendar.search.assert_not_called()


async def test_a_dead_half_beside_one_still_serving_is_not_a_failed_poll(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    events = [_search_item("Standup", timedelta(days=1))]

    def search(**kwargs):
        if kwargs.get("todo"):
            raise Timeout("the list never answered")
        return events if kwargs.get("expand") else []

    calendar.search.side_effect = search
    calendar.objects_by_sync_token.side_effect = lambda token=None: Mock(
        sync_token=object()
    )
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.personal").coordinator
    assert coordinator.halves["todos"].dead

    calendar.search.side_effect = Timeout("and now neither does the window")
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    assert coordinator.data.next_event.summary == "Standup"
    assert hass.states.get("calendar.personal").state != STATE_UNAVAILABLE
    assert hass.states.get("todo.personal").state == STATE_UNAVAILABLE


async def test_a_401_beside_a_half_that_read_cleanly_is_not_a_reauth(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    calendar.objects_by_sync_token.side_effect = lambda token=None: Mock(
        sync_token=object()
    )
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.personal").coordinator

    def search(**kwargs):
        if kwargs.get("todo"):
            raise AuthorizationError(
                url="https://cloud.example.com/dav/", reason="Unauthorized"
            )
        return []

    calendar.search.side_effect = search
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    assert not [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"].get("source") == "reauth"
    ]


async def test_a_401_beside_one_bad_minute_is_not_a_reauth(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    calendar.objects_by_sync_token.side_effect = lambda token=None: Mock(
        sync_token=object()
    )
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.personal").coordinator

    def search(**kwargs):
        if kwargs.get("todo"):
            raise AuthorizationError(
                url="https://cloud.example.com/dav/", reason="Unauthorized"
            )
        raise Timeout("no answer at all")

    calendar.search.side_effect = search
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    assert not [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"].get("source") == "reauth"
    ]


@pytest.mark.parametrize("unauthorized", ["events", "todos"])
async def test_a_401_reauths_beside_a_half_that_has_been_broken_for_good(
    hass: HomeAssistant, unauthorized: str
) -> None:
    calendar = _calendar("Personal")
    calendar.objects_by_sync_token.side_effect = lambda token=None: Mock(
        sync_token=object()
    )
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.personal").coordinator
    broken = "events" if unauthorized == "todos" else "todos"

    def half_of(kwargs):
        return "todos" if kwargs.get("todo") else "events"

    def timing_out(**kwargs):
        if half_of(kwargs) == broken:
            raise Timeout("no answer at all")
        return []

    calendar.search.side_effect = timing_out
    for _ in range(_MAX_KEPT_POLLS + 1):
        await coordinator.async_refresh()
    assert coordinator.halves[broken].dead

    def rotated(**kwargs):
        if half_of(kwargs) == broken:
            raise Timeout("no answer at all")
        raise AuthorizationError(
            url="https://cloud.example.com/dav/", reason="Unauthorized"
        )

    calendar.search.side_effect = rotated
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"].get("source") == "reauth"
    ]


async def test_leaving_out_a_rule_the_editor_showed_ends_the_recurrence(
    hass: HomeAssistant,
) -> None:
    # The editor sends no rrule at all for "Does not repeat".
    await _setup(hass, [_calendar("Personal")])
    entity = _entity(hass, "calendar.personal")
    entity.coordinator.rrules = {"uid-1": "FREQ=WEEKLY"}

    with patch("custom_components.ha_caldav.calendar.update_event") as update:
        await entity.async_update_event(
            "uid-1",
            _event_fields(),
            recurrence_id="2026-07-13 09:00:00+00:00",
            recurrence_range="THISANDFUTURE",
        )

    assert update.call_args.args[2]["rrule"] == ""


async def test_leaving_out_a_rule_core_refused_to_show_keeps_it(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, [_calendar("Personal")])
    entity = _entity(hass, "calendar.personal")
    entity.coordinator.rrules = {"uid-1": "FREQ=HOURLY"}

    with patch("custom_components.ha_caldav.calendar.update_event") as update:
        await entity.async_update_event("uid-1", _event_fields())

    assert "rrule" not in update.call_args.args[2]


async def test_the_state_moves_on_once_the_current_event_ends(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    calendar.search.return_value = [
        _search_item("First", timedelta(hours=1)),
        _search_item("Second", timedelta(hours=2)),
    ]
    await _setup(hass, [calendar])
    entity = _entity(hass, "calendar.personal")
    assert entity.event.summary == "First"

    later = dt_util.now() + timedelta(hours=2, minutes=30)
    with patch("homeassistant.util.dt.now", return_value=later):
        assert entity.event.summary == "Second"
        assert entity.state == "on"


async def test_a_todo_read_already_on_the_wire_cannot_restore_a_dropped_etag(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.personal").coordinator
    coordinator.todo_etags = {"todo-1": '"v1"'}

    read_at = coordinator._etag_epoch
    coordinator.forget_etags("todo_etags", ("todo-1",))
    coordinator._commit_todos(
        _TodoRead(items=[], etags={"todo-1": '"v1"'}, epoch=read_at)
    )

    assert coordinator.todo_etags == {}


async def test_a_panel_read_already_on_the_wire_cannot_restore_a_dropped_etag(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.personal").coordinator

    def read_while_a_write_lands(start, end):
        coordinator.forget_etags("etags", ("uid-1",))
        return {"uid-1": '"v1"'}, {}

    now = dt_util.now()
    with patch.object(coordinator, "_window_index", read_while_a_write_lands):
        await coordinator.async_get_events(now, now + timedelta(days=30))

    assert "uid-1" not in coordinator.etags


async def test_a_403_that_persists_fails_the_poll_without_a_reauth(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.personal").coordinator
    calendar.search.side_effect = AuthorizationError(
        url="https://cloud.example.com/dav/", reason="Forbidden"
    )

    for _ in range(_MAX_KEPT_POLLS + 1):
        coordinator._fetched_at = dt_util.utcnow() - timedelta(days=1)
        await coordinator.async_refresh()

    assert not coordinator.last_update_success
    assert not [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"].get("source") == "reauth"
    ]


@pytest.mark.parametrize(
    ("rule", "stored"),
    [
        ("FREQ=WEEKLY;UNTIL=20260831T203000", "FREQ=WEEKLY;UNTIL=20260831T203000Z"),
        ("FREQ=WEEKLY;UNTIL=20260831", "FREQ=WEEKLY;UNTIL=20260831"),
        ("FREQ=WEEKLY;UNTIL=20260831T203000Z", "FREQ=WEEKLY;UNTIL=20260831T203000Z"),
    ],
)
async def test_an_end_the_frontend_wrote_is_read_as_utc(
    hass: HomeAssistant, rule: str, stored: str
) -> None:
    await _setup(hass, [_calendar("Personal")])
    entity = _entity(hass, "calendar.personal")

    with patch("custom_components.ha_caldav.calendar.update_event") as update:
        await entity.async_update_event("uid-1", {**_event_fields(), "rrule": rule})

    assert update.call_args.args[2]["rrule"] == stored
