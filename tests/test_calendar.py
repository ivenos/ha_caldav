"""Tests for the CalDAV calendar entity."""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

from caldav.elements import dav
from caldav.lib.error import DAVError
from homeassistant.components.calendar import CalendarEntityFeature
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

from custom_components.ha_caldav.const import CONF_CALENDARS, CONF_READ_ONLY, DOMAIN

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
            side_effect=DAVError("boom"),
        ),
        pytest.raises(HomeAssistantError, match="CalDAV delete error"),
    ):
        await entity.async_delete_event("uid-1")


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
    # A fresh token every poll means the calendar always looks changed.
    calendar.objects_by_sync_token.side_effect = lambda token=None: Mock(
        sync_token=object()
    )
    await _setup(hass, [calendar])
    entity = _entity(hass, "calendar.iven_personal")

    calls = calendar.search.call_count
    await entity.coordinator.async_refresh()

    assert calendar.search.call_count > calls


async def test_change_survives_a_failed_poll(hass: HomeAssistant) -> None:
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
    calendar.search.side_effect = [
        [_search_item("First", timedelta(days=1))],
        Timeout("boom"),
        [_search_item("Second", timedelta(days=1))],
    ]
    coordinator = HaCaldavCoordinator(
        hass,
        entry,
        calendar,
        days=7,
        include_all_day=True,
        scan_interval=timedelta(minutes=15),
    )

    await coordinator.async_refresh()
    assert coordinator.data.summary == "First"
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert coordinator.data.summary == "Second"


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
