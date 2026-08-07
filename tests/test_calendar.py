"""Tests for the CalDAV calendar entity."""

from datetime import UTC, datetime, timedelta
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

    state = hass.states.get("calendar.iven_personal")
    assert state is not None
    assert hass.states.get("calendar.iven_work") is not None

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

    assert hass.states.get("calendar.iven_personal") is not None
    assert hass.states.get("calendar.iven_contact_birthdays") is None


async def test_read_only_hides_write_features(hass: HomeAssistant) -> None:
    await _setup(hass, [_calendar("Personal")], options={CONF_READ_ONLY: True})

    features = hass.states.get("calendar.iven_personal").attributes[
        "supported_features"
    ]
    assert features == 0


async def test_scan_interval_option_drives_polling(hass: HomeAssistant) -> None:
    await _setup(hass, [_calendar("Personal")], options={CONF_SCAN_INTERVAL: 5})
    entity = _entity(hass, "calendar.iven_personal")

    assert entity.coordinator.update_interval == timedelta(minutes=5)


async def test_scan_interval_defaults_to_fifteen_minutes(hass: HomeAssistant) -> None:
    await _setup(hass, [_calendar("Personal")])
    entity = _entity(hass, "calendar.iven_personal")

    assert entity.coordinator.update_interval == timedelta(minutes=15)


def _result(body: str) -> Mock:
    """Build a caldav search result from a raw VEVENT body."""
    item = Mock()
    item.vobject_instance = vobject.readOne(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//test//EN\n"
        f"BEGIN:VEVENT\nUID:{abs(hash(body))}\nDTSTAMP:20260101T000000Z\n"
        f"{body}\nEND:VEVENT\nEND:VCALENDAR\n"
    )
    return item


def _search_item(summary: str, start_offset: timedelta) -> Mock:
    """Build a caldav search result holding one future timed event."""
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
    """The polled window starts at local midnight, so it is full of events that
    are already over. is_over is exhaustively unit-tested; that the entity's
    own state calls it was not, because no test ever put a past event in the
    window, and the entity would have shown yesterday's meeting all day."""
    calendar = _calendar("Personal")
    calendar.search.return_value = [
        _search_item("Over", timedelta(hours=-4)),
        _search_item("Upcoming", timedelta(hours=4)),
    ]
    await _setup(hass, [calendar])

    assert _entity(hass, "calendar.iven_personal").event.summary == "Upcoming"


async def test_a_window_holding_only_past_events_has_no_upcoming_one(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    calendar.search.return_value = [_search_item("Over", timedelta(hours=-4))]
    await _setup(hass, [calendar])

    assert _entity(hass, "calendar.iven_personal").event is None


async def test_next_event_ignores_server_result_order(hass: HomeAssistant) -> None:
    # caldav's search makes no ordering promise, so the earliest event must be
    # picked even when the server returns it last.
    calendar = _calendar("Personal")
    calendar.search.return_value = [
        _search_item("Later", timedelta(days=3)),
        _search_item("Sooner", timedelta(days=1)),
    ]
    await _setup(hass, [calendar])

    entity = _entity(hass, "calendar.iven_personal")
    assert entity.event.summary == "Sooner"


async def test_delete_forwards_recurrence_range(hass: HomeAssistant) -> None:
    await _setup(hass, [_calendar("Personal")])
    entity = _entity(hass, "calendar.iven_personal")

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
    entity = _entity(hass, "calendar.iven_personal")

    with patch("custom_components.ha_caldav.calendar.delete_event") as delete:
        await entity.async_delete_event(
            "uid-1", recurrence_id="2026-07-13 09:00:00+00:00"
        )

    assert delete.call_args.args[1:] == ("uid-1", "2026-07-13 09:00:00+00:00", False)


async def test_server_error_becomes_home_assistant_error(hass: HomeAssistant) -> None:
    await _setup(hass, [_calendar("Personal")])
    entity = _entity(hass, "calendar.iven_personal")

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
    entity = _entity(hass, "calendar.iven_personal")

    calls = calendar.search.call_count
    await entity.coordinator.async_refresh()

    assert calendar.search.call_count == calls


async def test_changed_calendar_refetches(hass: HomeAssistant) -> None:
    calendar = _calendar("Personal")
    calendar.objects_by_sync_token.side_effect = lambda token=None: Mock(
        sync_token=object()
    )
    await _setup(hass, [calendar])
    entity = _entity(hass, "calendar.iven_personal")

    calls = calendar.search.call_count
    await entity.coordinator.async_refresh()

    assert calendar.search.call_count > calls


async def test_change_survives_a_failed_poll(hass: HomeAssistant) -> None:
    from custom_components.ha_caldav.capability import Capability
    from custom_components.ha_caldav.coordinator import HaCaldavCoordinator

    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="x")
    entry.add_to_hass(hass)
    calendar = _calendar("Personal")
    # Poll 2's fetch fails right after the token advanced to t2; the token must
    # not be committed, so poll 3 (token still t2) re-detects and refetches.
    calendar.objects_by_sync_token.side_effect = [
        Mock(sync_token="t1"),
        Mock(sync_token="t2"),
        Mock(sync_token="t2"),
    ]
    # A refetch is two searches: the expanded one for the window and the plain
    # one that carries the etags.
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
        # Events only, so the three scripted searches line up with the polls.
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
    entity = _entity(hass, "calendar.iven_personal")
    entity.coordinator.etags = {"uid-1": '"e"'}

    with patch("custom_components.ha_caldav.calendar.update_event") as update:
        await entity.async_update_event("uid-1", _event_fields())

    assert update.call_args.kwargs["expected_etag"] == '"e"'
    assert "uid-1" not in entity.coordinator.etags


async def test_delete_forwards_etag_and_clears_it_after_write(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, [_calendar("Personal")])
    entity = _entity(hass, "calendar.iven_personal")
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
    # The non-expanded (etag) search carries the etags; the expanded display
    # search does not.
    calendar.search.side_effect = lambda **kw: (
        [_etag_item("uid-1", '"e"')] if kw.get("expand") is False else []
    )
    await _setup(hass, [calendar])
    entity = _entity(hass, "calendar.iven_personal")

    await entity.async_get_events(
        hass, dt_util.utcnow(), dt_util.utcnow() + timedelta(days=7)
    )

    assert entity.coordinator.etags == {"uid-1": '"e"'}


async def test_failed_write_keeps_etag_for_retry(hass: HomeAssistant) -> None:
    # The pop must run only after a successful write; a failed one leaves the
    # cached etag so the retry still validates against the unchanged server copy.
    await _setup(hass, [_calendar("Personal")])
    entity = _entity(hass, "calendar.iven_personal")
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
    entity = _entity(hass, "calendar.iven_personal")

    events = await entity.async_get_events(
        hass, dt_util.utcnow(), dt_util.utcnow() + timedelta(days=7)
    )

    assert events == []
    assert entity.coordinator.etags == {}


async def test_a_rejected_password_starts_a_reauth_flow(hass: HomeAssistant) -> None:
    from caldav.lib.error import AuthorizationError

    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.iven_personal").coordinator
    calendar.objects_by_sync_token.side_effect = AuthorizationError(
        reason="Unauthorized"
    )
    calendar.search.side_effect = AuthorizationError(reason="Unauthorized")

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # Otherwise a password change on the server is only visible in the log.
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
    coordinator = _entity(hass, "calendar.iven_personal").coordinator
    calendar.objects_by_sync_token.side_effect = DAVError("boom")
    calendar.search.side_effect = DAVError("boom")

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
    coordinator = _entity(hass, "calendar.iven_personal").coordinator

    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert coordinator.data.next_event.summary == "Standup"


async def test_a_rejected_sync_token_is_dropped(hass: HomeAssistant) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.iven_personal").coordinator
    coordinator._sync_token = "stale"
    calendar.objects_by_sync_token.side_effect = DAVError("403 valid-sync-token")

    await coordinator.async_refresh()

    # Re-sending a token the server rejected keeps the probe failing forever.
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

    # An automation writing after a restart is conflict-checked without the
    # frontend ever having opened the calendar.
    assert _entity(hass, "calendar.iven_personal").coordinator.etags == {
        "Standup": '"etag-1"'
    }


async def test_the_panel_window_caches_its_own_etags(hass: HomeAssistant) -> None:
    calendar = _calendar("Personal")
    far = _search_item("Retro", timedelta(days=90))
    window = dt_util.now() + timedelta(days=80)

    def search(**kwargs):
        # Nothing in the polled week; the event only exists in the far window.
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
    coordinator = _entity(hass, "calendar.iven_personal").coordinator
    assert coordinator.etags == {}

    await coordinator.async_get_events(hass, window, window + timedelta(days=20))

    # An event the user scrolled to is outside the polled window; editing it
    # still has to be conflict-checked.
    assert coordinator.etags == {"Retro": '"etag-far"'}


async def test_a_half_that_never_answered_fails_the_poll(hass: HomeAssistant) -> None:
    calendar = _calendar("Personal")

    def search(**kwargs):
        # The to-do half answers, so only the event half has nothing to keep.
        if kwargs.get("todo"):
            return []
        raise Timeout("boom")

    calendar.search.side_effect = search
    entry = await _setup(hass, [calendar])

    # Half a poll is not a poll: reporting an empty calendar here would be a
    # lie the user has no way of noticing.
    assert entry.state is ConfigEntryState.SETUP_RETRY


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
    coordinator = _entity(hass, "calendar.iven_personal").coordinator

    fail_todos = True
    calendar.objects_by_sync_token.side_effect = Timeout("boom")
    await coordinator.async_refresh()

    # A to-do search timing out on a long list must not take the calendar down.
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
    coordinator = _entity(hass, "calendar.iven_personal").coordinator

    assert coordinator.last_update_success is True
    assert coordinator.data.next_event.summary == "Standup"


async def test_the_extra_attributes_stay_out_of_the_recorder(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, [_calendar("Personal")])
    entity = _entity(hass, "calendar.iven_personal")

    # Core reads the combined set, not the per-entity attribute, so this is
    # what proves the exclusion is actually wired up.
    combined = entity._Entity__combined_unrecorded_attributes
    assert {"attendees", "organizer", "url", "alarms"} <= combined


async def test_the_days_option_sets_the_search_window(hass: HomeAssistant) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar], options={CONF_DAYS: 30})

    # Asserting the constructor argument would pass for a coordinator that
    # stores the number and searches a week anyway.
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

    assert hass.states.get("calendar.iven_personal").state == "off"
    assert hass.states.get("calendar.iven_personal").attributes.get("message") is None


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

    assert hass.states.get("calendar.iven_personal").attributes["message"] == "Holiday"


async def test_an_event_without_a_start_does_not_take_the_calendar_down(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    start = dt_util.utcnow() + timedelta(hours=1)
    calendar.search.return_value = [
        # RFC 5545 makes DTSTART optional once the object carries a METHOD, and
        # ordering the window would raise on it.
        _result("SUMMARY:No start"),
        _result(
            f"DTSTART:{start:%Y%m%dT%H%M%S}Z\n"
            f"DTEND:{start + timedelta(hours=1):%Y%m%dT%H%M%S}Z\nSUMMARY:Standup"
        ),
    ]

    await _setup(hass, [calendar])

    assert hass.states.get("calendar.iven_personal").attributes["message"] == "Standup"


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

    # Users are asked to paste this line into an issue, and a caldav error
    # prints the collection it was reading, hence the account.
    logged = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("Could not read Personal: DAVError" in line for line in logged)
    assert not any("iven" in line for line in logged)


async def test_a_calendar_without_a_name_still_gets_one(hass: HomeAssistant) -> None:
    nameless = _calendar("Personal")
    nameless.name = None
    await _setup(hass, [nameless])
    entity = _entity(hass, "calendar.iven_caldav")

    # A collection is not required to carry a display name, and the coordinator
    # name reaches the log lines and the entity id.
    assert entity.coordinator.name == "CalDAV"


async def test_the_only_half_of_a_calendar_failing_is_a_failed_poll(
    hass: HomeAssistant,
) -> None:
    from custom_components.ha_caldav.capability import Capability

    calendar = _calendar("Personal")
    with patch(
        "custom_components.ha_caldav.fetch_capabilities",
        return_value={
            "/remote.php/dav/Personal": Capability(frozenset({"VEVENT"}), True)
        },
    ):
        await _setup(hass, [calendar])
    entity = _entity(hass, "calendar.iven_personal")
    # The server says something changed, so the poll really does go and fetch.
    calendar.objects_by_sync_token.return_value.sync_token = "moved-on"
    calendar.search.side_effect = Timeout("gone")

    await entity.coordinator.async_refresh()

    # One half of two may fail back onto cached data; the only half there is
    # may not, or the entity looks healthy while it is blind.
    assert entity.coordinator.last_update_success is False


async def test_a_poll_drops_the_etag_of_an_event_it_no_longer_finds(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    entity = _entity(hass, "calendar.iven_personal")
    # Seen by the polled window before, so its absence now means it is gone.
    entity.coordinator._etag_window = {"deleted-1"}
    entity.coordinator.etags = {"deleted-1": "old-etag"}
    entity.coordinator._window_events = None

    await entity.coordinator.async_refresh()

    # An event that is gone from the server must lose its etag, or the next
    # write against a reused uid is flagged as a conflict that never happened.
    assert "deleted-1" not in entity.coordinator.etags


async def test_a_poll_keeps_the_etags_of_a_window_it_did_not_read(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    entity = _entity(hass, "calendar.iven_personal")
    # Cached by the panel for a month the poll window does not reach.
    entity.coordinator.etags = {"next-month-1": "panel-etag"}
    entity.coordinator._window_events = None

    await entity.coordinator.async_refresh()

    # Dropping it would leave an edit made from that still-open panel window
    # unchecked, silently overwriting whatever another client changed.
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
    entity = _entity(hass, "calendar.iven_personal")
    calendar.search.side_effect = failure

    with pytest.raises(HomeAssistantError):
        await entity.async_get_events(
            hass, dt_util.utcnow(), dt_util.utcnow() + timedelta(days=7)
        )


async def test_a_partly_failed_poll_does_not_commit_the_sync_token(
    hass: HomeAssistant,
) -> None:
    # Committing it would leave the half that failed unread until the server's
    # own token moves again, which for a quiet calendar is never.
    calendar = _calendar("Personal")
    calendar.objects_by_sync_token.return_value.sync_token = "token-1"
    await _setup(hass, [calendar])
    entity = _entity(hass, "calendar.iven_personal")
    calendar.objects_by_sync_token.return_value.sync_token = "token-2"

    def half(**kw):
        if kw.get("todo"):
            raise Timeout("todo half is down")
        return []

    calendar.search.side_effect = half
    await entity.coordinator.async_refresh()

    assert entity.coordinator._sync_token != "token-2"


async def test_the_window_rolls_over_at_midnight(hass: HomeAssistant) -> None:
    # The sync token is unchanged across the day boundary, so only the moved
    # window forces the refetch; without it the panel shows yesterday forever.
    calendar = _calendar("Personal")
    calendar.objects_by_sync_token.return_value.sync_token = "steady"
    await _setup(hass, [calendar])
    entity = _entity(hass, "calendar.iven_personal")

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
    entity = _entity(hass, "calendar.iven_personal")

    assert entity.coordinator.last_update_success is True


async def test_the_panel_gets_the_recurrence_rule_of_a_series(
    hass: HomeAssistant,
) -> None:
    """Expanding a series strips RRULE from every occurrence it produces.

    Without carrying it across, the panel shows no "repeats weekly" line and
    opens its recurrence editor blank on an event that plainly recurs.
    """
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
    entity = _entity(hass, "calendar.iven_personal")

    events = await entity.async_get_events(
        hass, dt_util.utcnow(), dt_util.utcnow() + timedelta(days=7)
    )

    assert events[0].rrule == "FREQ=WEEKLY;BYDAY=MO"


async def test_a_single_event_carries_no_recurrence_rule(hass: HomeAssistant) -> None:
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    entity = _entity(hass, "calendar.iven_personal")
    calendar.search.return_value = [_search_item("Dentist", timedelta(days=1))]

    events = await entity.async_get_events(
        hass, dt_util.utcnow(), dt_util.utcnow() + timedelta(days=7)
    )

    assert events[0].rrule is None


async def test_a_series_that_stops_recurring_loses_its_recorded_rule(
    hass: HomeAssistant,
) -> None:
    # Another client can turn a series into a single event. Keeping the old
    # rule would have the panel offer to edit a recurrence that is gone.
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
    entity = _entity(hass, "calendar.iven_personal")
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
    """Dropping it after the refresh would leave the next edit unchecked."""
    calendar = _calendar("Personal")
    etag = '"v1"'

    def search(**kwargs):
        if kwargs.get("todo") or kwargs.get("expand") is not False:
            return []
        return [_etag_item("uid-1", etag)]

    calendar.search.side_effect = search
    await _setup(hass, [calendar])
    entity = _entity(hass, "calendar.iven_personal")
    assert entity.coordinator.etags == {"uid-1": '"v1"'}

    seen: dict = {}

    async def refresh() -> None:
        seen["etags"] = dict(entity.coordinator.etags)

    with (
        patch("custom_components.ha_caldav.calendar.update_event"),
        patch.object(entity.coordinator, "async_request_refresh", refresh),
    ):
        await entity.async_update_event("uid-1", _event_fields())

    # Gone by the time the refresh runs, so the refresh can read the new one
    # back; dropped afterwards it would take that new one with it.
    assert seen["etags"] == {}


async def test_a_poll_that_could_not_read_the_etags_keeps_its_token(
    hass: HomeAssistant,
) -> None:
    """Committing it would freeze those etags with no later poll to repair them."""
    calendar = _calendar("Personal")

    def search(**kwargs):
        if kwargs.get("expand") is False:
            raise DAVError("etag report refused")
        return []

    calendar.search.side_effect = search
    calendar.objects_by_sync_token.return_value = Mock(sync_token="t2")
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.iven_personal").coordinator

    assert coordinator.last_update_success
    assert coordinator._sync_token is None


async def test_a_half_that_keeps_failing_stops_looking_healthy(
    hass: HomeAssistant,
) -> None:
    """A months-old snapshot nobody can tell is old also validates writes."""
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.iven_personal").coordinator

    tokens = iter(f"t{n}" for n in range(99))
    calendar.objects_by_sync_token.side_effect = lambda _token: Mock(
        sync_token=next(tokens)
    )
    calendar.search.side_effect = lambda **kwargs: (
        [] if kwargs.get("todo") is None else _raise(DAVError("todo report failed"))
    )
    for _ in range(3):
        await coordinator.async_refresh()
        assert coordinator.last_update_success

    await coordinator.async_refresh()

    assert not coordinator.last_update_success


def _raise(err: Exception):
    raise err


async def test_a_server_that_never_moves_its_sync_token_is_read_again(
    hass: HomeAssistant,
) -> None:
    """A token that lies would otherwise freeze the entry with nothing to see."""
    calendar = _calendar("Personal")
    calendar.objects_by_sync_token.return_value = Mock(sync_token="frozen")
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.iven_personal").coordinator

    calendar.search.reset_mock()
    await coordinator.async_refresh()
    assert calendar.search.call_count == 0

    coordinator._fetched_at = dt_util.utcnow() - timedelta(days=1)
    await coordinator.async_refresh()

    assert calendar.search.call_count > 0


async def test_an_edit_without_a_rule_leaves_the_recurrence_alone(
    hass: HomeAssistant,
) -> None:
    """expand strips RRULE from what the frontend echoes back, so an absent
    rule is never a request to drop the series."""
    await _setup(hass, [_calendar("Personal")])
    entity = _entity(hass, "calendar.iven_personal")

    with patch("custom_components.ha_caldav.calendar.update_event") as update:
        await entity.async_update_event("uid-1", _event_fields())

    assert "rrule" not in update.call_args.args[2]


async def test_a_cleared_description_is_removed_not_emptied(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, [_calendar("Personal")])
    entity = _entity(hass, "calendar.iven_personal")

    with patch("custom_components.ha_caldav.calendar.update_event") as update:
        await entity.async_update_event("uid-1", _event_fields() | {"description": ""})

    assert update.call_args.args[2]["description"] is None


async def test_the_panel_etags_and_the_polled_ones_live_side_by_side(
    hass: HomeAssistant,
) -> None:
    """Replacing them would leave the polled window's next edit unchecked."""
    calendar = _calendar("Personal")
    far = _etag_item("far-1", '"far"')

    def search(**kwargs):
        if kwargs.get("todo") or kwargs.get("expand") is not False:
            return []
        far_away = kwargs["start"] > dt_util.now() + timedelta(days=30)
        return [far] if far_away else [_etag_item("near-1", '"near"')]

    calendar.search.side_effect = search
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.iven_personal").coordinator
    assert coordinator.etags == {"near-1": '"near"'}

    window = dt_util.now() + timedelta(days=80)
    await coordinator.async_get_events(hass, window, window + timedelta(days=20))

    assert coordinator.etags == {"near-1": '"near"', "far-1": '"far"'}


async def test_a_transient_403_during_a_poll_does_not_force_a_reauth(
    hass: HomeAssistant,
) -> None:
    """caldav raises the same error for 403, which a proxy produces in passing."""
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.iven_personal").coordinator
    calendar.search.side_effect = AuthorizationError(
        url="https://cloud.example.com/dav/", reason="Forbidden"
    )
    coordinator._fetched_at = dt_util.utcnow() - timedelta(days=1)

    await coordinator.async_refresh()

    assert not coordinator.last_update_success
    assert not [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"].get("source") == "reauth"
    ]


async def test_a_window_the_server_put_no_etag_on_reads_as_unread(
    hass: HomeAssistant,
) -> None:
    """Read as an empty window instead, every etag kept here is treated as gone
    and the next edit of each of those objects goes out unchecked."""
    calendar = _calendar("Personal")
    stripped = _etag_item("uid-1", '"e"')
    stripped.props = {}
    calendar.search.side_effect = lambda **kw: (
        [stripped] if kw.get("expand") is False else []
    )
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.iven_personal").coordinator

    start = dt_util.utcnow()

    assert coordinator._window_index(start, start + timedelta(days=7))[0] is None


async def test_the_recorded_rule_comes_off_the_master_of_the_object(
    hass: HomeAssistant,
) -> None:
    """RFC 5545 leaves the component order open, and reading the first one
    records no rule for a series that has one."""
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
    entity = _entity(hass, "calendar.iven_personal")

    assert entity.coordinator.rrules == {"series-1": "FREQ=WEEKLY"}


async def test_a_dead_half_still_lets_the_other_one_read(
    hass: HomeAssistant,
) -> None:
    """The poll still fails, but the healthy half is fetched first, so its
    cache and etags do not freeze at whatever they held when the other broke."""
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.iven_personal").coordinator

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

    assert not coordinator.last_update_success
    # Reached on the failing poll too, not only on the tolerated ones.
    assert len(seen) == 4


async def test_a_dead_half_does_not_leave_the_other_ones_etags_behind(
    hass: HomeAssistant,
) -> None:
    """A half reads its etags in the same request as its data. Committed on the
    way out, they described a revision whose data the failing other half then
    threw away: the next edit of one of those objects was checked against an
    etag that did match the server, passed, and overwrote the change that had
    arrived with it.
    """
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.iven_personal").coordinator

    tokens = iter(f"t{n}" for n in range(99))
    calendar.objects_by_sync_token.side_effect = lambda _token: Mock(
        sync_token=next(tokens)
    )
    etags = iter(['"t1"', '"t2"', '"t3"', '"t4"'])

    def search(**kwargs):
        if kwargs.get("todo") is None:
            raise DAVError("event report failed")
        return [_etag_item("todo-1", next(etags))]

    calendar.search.side_effect = search
    for _ in range(4):
        await coordinator.async_refresh()

    assert not coordinator.last_update_success
    # Nothing kept: the to-do list the user sees was never updated either, so
    # an etag from those reads would validate a write against data the entity
    # never showed.
    assert coordinator.todo_etags == {}


async def test_a_half_that_recovers_starts_counting_again(
    hass: HomeAssistant,
) -> None:
    """Without the reset a server that fails one poll in ten reaches the limit
    over days of ordinary flakiness and then fails every poll for good."""
    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.iven_personal").coordinator

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
    # Two failures, then one good poll, then two more: without the reset the
    # counter reaches three and the half is declared dead.
    for run in (True, True, False, True, True):
        broken[0] = run
        await coordinator.async_refresh()

    # Still alive: without the reset the count would stand at four, past the
    # limit, and the half would be declared dead over failures that were never
    # consecutive.
    assert coordinator.last_update_success
    assert coordinator._misses["events"] == 2


async def test_one_unplaceable_event_does_not_cost_the_collection_its_entities(
    hass: HomeAssistant,
) -> None:
    """A DTEND in the year 9999 overflows the moment a zone offset reaches it,
    and OverflowError is an ArithmeticError, which the mapping guard did not
    name. One such object anywhere in the window failed every poll and took
    the calendar entity and the to-do list down with it, indefinitely."""
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
        entity = _entity(hass, "calendar.iven_personal")

        assert entity.coordinator.last_update_success
        assert entity.event.summary == "Upcoming"
    finally:
        dt_util.set_default_time_zone(previous)


async def test_a_polled_window_survives_an_etag_cache_at_its_limit(
    hass: HomeAssistant,
) -> None:
    """Merged behind the entries already held, a cache at its limit gives up
    the window it just read instead of what has been out of view longest. Those
    uids then never regain an etag, and their next edit is written with nothing
    to check against — silently, and only on the large calendars where a clash
    is likeliest."""
    from custom_components.ha_caldav.coordinator import _CACHE_LIMIT

    calendar = _calendar("Personal")
    await _setup(hass, [calendar])
    coordinator = _entity(hass, "calendar.iven_personal").coordinator
    coordinator.etags = {f"old-{n}": f'"o{n}"' for n in range(_CACHE_LIMIT)}
    # Left behind by a window the panel asked for, so the poll's window does
    # not name them and none of them counts as gone. They are exactly what the
    # limit has to give up, and the fresh ones are what it has to keep.
    coordinator._etag_window = set()

    coordinator._merge_etags({"fresh-1": '"f1"', "fresh-2": '"f2"'})

    assert coordinator.etags["fresh-1"] == '"f1"'
    assert coordinator.etags["fresh-2"] == '"f2"'
