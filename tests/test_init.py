from datetime import timedelta
from unittest.mock import Mock, patch

from caldav.davclient import requests
from caldav.elements import dav
from caldav.lib.error import AuthorizationError
from conftest import propfind_answer
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_TIMEOUT,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.ha_caldav import async_migrate_entry
from custom_components.ha_caldav.connection import calendar_key
from custom_components.ha_caldav.const import (
    CONF_CALENDAR_OPTIONS,
    CONF_CALENDARS,
    CONF_READ_ONLY,
    DOMAIN,
    ISSUE_BUILTIN_CALDAV,
    ISSUE_NO_SYNC_COLLECTION,
)
from custom_components.ha_caldav.coordinator import todo_unique_id

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


async def _setup(hass: HomeAssistant, sync_collection: bool = True):
    entry = MockConfigEntry(domain=DOMAIN, title="iven", data=ENTRY_DATA, unique_id="x")
    entry.add_to_hass(hass)
    with (
        patch("custom_components.ha_caldav.caldav.DAVClient") as client,
        patch(
            "custom_components.ha_caldav.supports_sync_collection",
            return_value=sync_collection,
        ),
    ):
        client.return_value.principal.return_value.calendars.return_value = [
            _calendar("Personal")
        ]
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entry.principal = client.return_value.principal
    return entry


def _entity(hass: HomeAssistant, domain: str, entity_id: str):
    component: EntityComponent = hass.data["entity_components"][domain]
    return component.get_entity(entity_id)


async def test_calendar_and_todo_share_one_poller(hass: HomeAssistant) -> None:
    await _setup(hass)

    calendar = _entity(hass, "calendar", "calendar.personal")
    todo = _entity(hass, "todo", "todo.personal")

    # Both live in the same collection under one sync token.
    assert calendar.coordinator is todo.coordinator


async def test_the_account_is_discovered_once_for_both_platforms(
    hass: HomeAssistant,
) -> None:
    entry = await _setup(hass)

    assert entry.principal.return_value.calendars.call_count == 1


async def test_a_missing_sync_report_raises_a_repair_issue(
    hass: HomeAssistant,
) -> None:
    entry = await _setup(hass, sync_collection=False)

    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"{ISSUE_NO_SYNC_COLLECTION}_{entry.entry_id}"
    )
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_placeholders == {"account": "iven"}


async def test_a_server_with_the_sync_report_raises_nothing(
    hass: HomeAssistant,
) -> None:
    entry = await _setup(hass)

    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, f"{ISSUE_NO_SYNC_COLLECTION}_{entry.entry_id}"
        )
        is None
    )


async def test_the_issue_clears_once_the_server_can_report_again(
    hass: HomeAssistant,
) -> None:
    entry = await _setup(hass, sync_collection=False)
    registry = ir.async_get(hass)
    issue_id = f"{ISSUE_NO_SYNC_COLLECTION}_{entry.entry_id}"
    assert registry.async_get_issue(DOMAIN, issue_id) is not None

    with (
        patch("custom_components.ha_caldav.caldav.DAVClient") as client,
        patch(
            "custom_components.ha_caldav.supports_sync_collection", return_value=True
        ),
    ):
        client.return_value.principal.return_value.calendars.return_value = [
            _calendar("Personal")
        ]
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_entities_for_a_component_the_calendar_lost_are_removed(
    hass: HomeAssistant,
) -> None:
    from custom_components.ha_caldav.capability import Capability

    entry = await _setup(hass)
    registry = er.async_get(hass)
    assert registry.async_get("todo.personal") is not None

    with (
        patch("custom_components.ha_caldav.caldav.DAVClient") as client,
        patch(
            "custom_components.ha_caldav.fetch_capabilities",
            return_value={
                "/remote.php/dav/Personal": Capability(frozenset({"VEVENT"}), True)
            },
        ),
        patch(
            "custom_components.ha_caldav.supports_sync_collection", return_value=True
        ),
    ):
        client.return_value.principal.return_value.calendars.return_value = [
            _calendar("Personal")
        ]
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    assert registry.async_get("todo.personal") is None
    assert registry.async_get("calendar.personal") is not None


async def test_the_builtin_integration_on_the_same_account_is_flagged(
    hass: HomeAssistant,
) -> None:
    builtin = MockConfigEntry(
        domain="caldav",
        data={CONF_URL: ENTRY_DATA[CONF_URL], CONF_USERNAME: "iven"},
        unique_id="builtin",
    )
    builtin.add_to_hass(hass)

    entry = await _setup(hass)

    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, f"{ISSUE_BUILTIN_CALDAV}_{entry.entry_id}"
        )
        is not None
    )


async def test_the_builtin_integration_on_another_account_is_fine(
    hass: HomeAssistant,
) -> None:
    builtin = MockConfigEntry(
        domain="caldav",
        data={CONF_URL: ENTRY_DATA[CONF_URL], CONF_USERNAME: "someone-else"},
        unique_id="builtin",
    )
    builtin.add_to_hass(hass)

    entry = await _setup(hass)

    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, f"{ISSUE_BUILTIN_CALDAV}_{entry.entry_id}"
        )
        is None
    )


async def test_a_reload_keeps_a_dismissed_issue(hass: HomeAssistant) -> None:
    entry = await _setup(hass, sync_collection=False)
    registry = ir.async_get(hass)
    issue_id = f"{ISSUE_NO_SYNC_COLLECTION}_{entry.entry_id}"
    ir.async_ignore_issue(hass, DOMAIN, issue_id, ignore=True)

    with (
        patch("custom_components.ha_caldav.caldav.DAVClient") as client,
        patch(
            "custom_components.ha_caldav.supports_sync_collection", return_value=False
        ),
    ):
        client.return_value.principal.return_value.calendars.return_value = [
            _calendar("Personal")
        ]
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    # Deleting an issue drops the record of its dismissal, and changing any option
    # reloads the entry.
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.dismissed_version is not None


async def test_removing_the_entry_clears_the_issues(hass: HomeAssistant) -> None:
    entry = await _setup(hass, sync_collection=False)
    registry = ir.async_get(hass)
    assert registry.async_get_issue(
        DOMAIN, f"{ISSUE_NO_SYNC_COLLECTION}_{entry.entry_id}"
    )

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert (
        registry.async_get_issue(DOMAIN, f"{ISSUE_NO_SYNC_COLLECTION}_{entry.entry_id}")
        is None
    )


async def test_the_services_are_registered(hass: HomeAssistant) -> None:
    await _setup(hass)

    assert hass.services.has_service(DOMAIN, "search_events")
    assert hass.services.has_service(DOMAIN, "create_calendar")


async def test_upcoming_event_attributes_come_from_the_snapshot(
    hass: HomeAssistant,
) -> None:
    await _setup(hass)
    entity = _entity(hass, "calendar", "calendar.personal")
    entity.coordinator.data.extras = {"url": "https://meet.example.com/x"}

    assert entity.extra_state_attributes["url"] == "https://meet.example.com/x"


async def _connect_with(hass: HomeAssistant, error, url: str | None = None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="iven",
        data={**ENTRY_DATA, **({CONF_URL: url} if url else {})},
        unique_id="x",
    )
    entry.add_to_hass(hass)
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.side_effect = error
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry, client


async def test_a_rejected_password_at_setup_asks_for_a_new_one(
    hass: HomeAssistant,
) -> None:
    entry, _ = await _connect_with(hass, AuthorizationError(reason="Unauthorized"))

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(
        flow["context"]["source"] == "reauth"
        for flow in hass.config_entries.flow.async_progress()
    )


async def test_a_403_is_retried_rather_than_sent_to_reauth(hass: HomeAssistant) -> None:
    # A reverse proxy or a rate limiter produces this transiently, and caldav
    # raises the same type for it as for a bad password.
    entry, client = await _connect_with(hass, AuthorizationError(reason="Forbidden"))

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not hass.config_entries.flow.async_progress()
    assert client.return_value.close.call_count == 2


async def test_an_unreachable_server_is_retried(hass: HomeAssistant) -> None:
    entry, _ = await _connect_with(hass, requests.ConnectionError("boom"))

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.error_reason_translation_key == "cannot_connect"


async def test_the_well_known_url_is_tried_after_the_entered_one(
    hass: HomeAssistant,
) -> None:
    entry, client = await _connect_with(
        hass, requests.ConnectionError("boom"), url="https://cloud.example.com"
    )

    tried = [call.args[0] for call in client.call_args_list]
    assert tried == [
        "https://cloud.example.com",
        "https://cloud.example.com/.well-known/caldav",
    ]


async def test_the_session_is_handed_back_when_setup_gives_up(
    hass: HomeAssistant,
) -> None:
    _, client = await _connect_with(hass, requests.ConnectionError("boom"))

    # The test harness runs a Mock handed to async_add_executor_job on the loop.
    assert client.return_value.close.call_count == 2


async def test_an_account_whose_calendars_all_fail_is_not_ready(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="iven", data=ENTRY_DATA, unique_id="x")
    entry.add_to_hass(hass)
    calendar = _calendar("Personal")
    calendar.search.side_effect = requests.ConnectionError("boom")
    calendar.objects_by_sync_token.side_effect = requests.ConnectionError("boom")
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = [calendar]
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_a_server_answering_with_nonsense_is_retried_not_given_up_on(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="iven", data=ENTRY_DATA, unique_id="q")
    entry.add_to_hass(hass)
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        # A captive portal or a failing proxy answers 207 with html, and caldav
        # comes out of that with a TypeError rather than a network error.
        client.return_value.principal.side_effect = TypeError("NoneType")
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        # Home Assistant never retries SETUP_ERROR.
        assert entry.state is ConfigEntryState.SETUP_RETRY
        assert client.return_value.close.called


async def test_a_calendar_renamed_on_the_server_stays_selected(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="iven",
        data=ENTRY_DATA,
        options={"calendars": ["/remote.php/dav/Personal"]},
        unique_id="r",
    )
    entry.add_to_hass(hass)
    renamed = _calendar("Personal")
    renamed.name = "Privat"
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = [renamed]
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert [item.name for item in entry.runtime_data.calendars] == ["Privat"]


async def test_a_selection_stored_by_name_still_loads_its_calendar(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="iven",
        data=ENTRY_DATA,
        options={"calendars": ["Personal"]},
        unique_id="s",
    )
    entry.add_to_hass(hass)
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = [
            _calendar("Personal"),
            _calendar("Work"),
        ]
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert [item.name for item in entry.runtime_data.calendars] == ["Personal"]


async def _setup_with(
    hass: HomeAssistant, calendars: list, options: dict | None = None
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


async def test_an_account_with_no_calendars_still_sets_up(hass: HomeAssistant) -> None:
    entry = await _setup_with(hass, [])

    assert entry.state is ConfigEntryState.LOADED


async def test_an_entry_with_an_empty_selection_falls_back_to_every_calendar(
    hass: HomeAssistant,
) -> None:
    await _setup_with(hass, [_calendar("Personal")], options={CONF_CALENDARS: []})

    assert hass.states.get("calendar.personal") is not None


async def test_an_entry_keyed_before_normalization_is_rekeyed(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_URL: "https://Cloud.Example.COM/remote.php/dav/",
            CONF_USERNAME: "iven",
            CONF_PASSWORD: "secret",
            CONF_VERIFY_SSL: True,
        },
        unique_id="https://Cloud.Example.COM/remote.php/dav/#iven",
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)

    assert entry.unique_id == "https://cloud.example.com/remote.php/dav#iven"


async def _reload_with(hass: HomeAssistant, entry, calendars: list) -> None:
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = calendars
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()


async def test_a_calendar_missing_from_one_answer_keeps_its_entities(
    hass: HomeAssistant,
) -> None:
    personal, work = _calendar("Personal"), _calendar("Work")
    entry = await _setup_with(
        hass,
        [personal, work],
        options={CONF_CALENDARS: ["/remote.php/dav/Personal", "/remote.php/dav/Work"]},
    )
    registry = er.async_get(hass)
    before = {
        e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert "calendar.work" in before

    await _reload_with(hass, entry, [personal])

    after = {
        e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert after == before


async def test_a_calendar_the_user_unticked_loses_its_entities(
    hass: HomeAssistant,
) -> None:
    personal, work = _calendar("Personal"), _calendar("Work")
    entry = await _setup_with(
        hass,
        [personal, work],
        options={CONF_CALENDARS: ["/remote.php/dav/Personal", "/remote.php/dav/Work"]},
    )
    registry = er.async_get(hass)

    hass.config_entries.async_update_entry(
        entry, options={CONF_CALENDARS: ["/remote.php/dav/Personal"]}
    )
    await _reload_with(hass, entry, [personal, work])

    kept = {
        e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert kept == {"calendar.personal", "todo.personal"}


async def test_the_migration_is_reachable_at_all(hass: HomeAssistant) -> None:
    """Home Assistant runs a migration only when the stored version differs from
    the handler's."""
    from custom_components.ha_caldav.config_flow import HaCaldavConfigFlow

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=ENTRY_DATA,
        unique_id="stale",
        version=HaCaldavConfigFlow.VERSION,
        minor_version=HaCaldavConfigFlow.MINOR_VERSION - 1,
    )
    entry.add_to_hass(hass)

    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = []
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.unique_id == "https://cloud.example.com/remote.php/dav#iven"
    assert entry.minor_version == HaCaldavConfigFlow.MINOR_VERSION


async def test_the_migration_rekeys_entities_off_the_bare_url(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, unique_id="stale", minor_version=1
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "calendar",
        DOMAIN,
        f"{entry.entry_id}-http://cloud.example.com/remote.php/dav/Personal/",
        config_entry=entry,
    )

    assert await async_migrate_entry(hass, entry)

    assert registry.async_get(old.entity_id).unique_id == (
        f"{entry.entry_id}-/remote.php/dav/Personal"
    )


async def test_a_password_revoked_before_the_first_poll_asks_for_reauth(
    hass: HomeAssistant,
) -> None:
    calendar = _calendar("Personal")
    calendar.search.side_effect = AuthorizationError(reason="Unauthorized")
    entry = MockConfigEntry(domain=DOMAIN, title="iven", data=ENTRY_DATA, unique_id="x")
    entry.add_to_hass(hass)

    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = [calendar]
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(
        flow["context"]["source"] == "reauth"
        for flow in hass.config_entries.flow.async_progress()
    )


async def test_the_builtin_conflict_is_found_through_another_spelling(
    hass: HomeAssistant,
) -> None:
    builtin = MockConfigEntry(
        domain="caldav",
        data={
            CONF_URL: "https://cloud.example.com/remote.php/dav",
            CONF_USERNAME: "iven",
            CONF_PASSWORD: "secret",
        },
    )
    builtin.add_to_hass(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="iven",
        data={**ENTRY_DATA, CONF_URL: "https://Cloud.Example.COM/remote.php/dav/"},
        unique_id="x",
    )
    entry.add_to_hass(hass)
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = []
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"{ISSUE_BUILTIN_CALDAV}_{entry.entry_id}")


async def test_a_calendar_the_account_stopped_listing_is_not_pruned(
    hass: HomeAssistant,
) -> None:
    from custom_components.ha_caldav import _async_prune_deselected

    personal, work = _calendar("Personal"), _calendar("Work")
    entry = await _setup_with(
        hass,
        [personal, work],
        options={CONF_CALENDARS: ["/remote.php/dav/Personal", "/remote.php/dav/Work"]},
    )
    registry = er.async_get(hass)
    before = {
        record.entity_id
        for record in er.async_entries_for_config_entry(registry, entry.entry_id)
    }

    _async_prune_deselected(hass, entry, [personal], registry)

    after = {
        record.entity_id
        for record in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert after == before


async def test_the_migration_tells_the_two_halves_apart_by_platform(
    hass: HomeAssistant,
) -> None:
    """A collection url may itself end in "-todo"."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, unique_id="stale", minor_version=1
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    url = "https://cloud.example.com/remote.php/dav/personal?view=-todo"
    calendar = registry.async_get_or_create(
        "calendar", DOMAIN, f"{entry.entry_id}-{url}", config_entry=entry
    )
    todo = registry.async_get_or_create(
        "todo", DOMAIN, f"{entry.entry_id}-{url}-todo", config_entry=entry
    )

    assert await async_migrate_entry(hass, entry)

    kept = f"{entry.entry_id}-/remote.php/dav/personal"
    assert registry.async_get(calendar.entity_id).unique_id == kept
    assert registry.async_get(todo.entity_id).unique_id == f"{kept}-todo"


async def test_the_migration_leaves_a_key_that_is_not_ours_alone(
    hass: HomeAssistant,
) -> None:
    """An entity registered under this entry need not be one of ours."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, unique_id="stale", minor_version=1
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    foreign = registry.async_get_or_create(
        "calendar", DOMAIN, "something-else-entirely", config_entry=entry
    )

    assert await async_migrate_entry(hass, entry)

    assert registry.async_get(foreign.entity_id).unique_id == "something-else-entirely"


async def test_the_migration_keeps_a_duplicate_visible_rather_than_failing(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, unique_id="stale", minor_version=1
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    url = "https://cloud.example.com/remote.php/dav/personal"
    first = registry.async_get_or_create(
        "calendar", DOMAIN, f"{entry.entry_id}-{url}", config_entry=entry
    )
    second = registry.async_get_or_create(
        "calendar", DOMAIN, f"{entry.entry_id}-{url}/", config_entry=entry
    )

    assert await async_migrate_entry(hass, entry)

    normalized = f"{entry.entry_id}-/remote.php/dav/personal"
    assert registry.async_get(first.entity_id).unique_id == normalized
    assert registry.async_get(second.entity_id).unique_id == f"{entry.entry_id}-{url}/"


async def test_the_timeout_option_is_what_a_request_gets(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="iven",
        data=ENTRY_DATA,
        options={CONF_TIMEOUT: 90},
        unique_id="x",
    )
    entry.add_to_hass(hass)
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = []
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert client.call_args.kwargs["timeout"] == 90


async def test_an_account_without_the_option_gets_the_default(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="iven", data=ENTRY_DATA, unique_id="y")
    entry.add_to_hass(hass)
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = []
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert client.call_args.kwargs["timeout"] == 30


async def test_a_server_without_sync_collection_is_not_asked_for_a_token(
    hass: HomeAssistant,
) -> None:
    entry = await _setup(hass, sync_collection=False)
    calendar = entry.principal.return_value.calendars.return_value[0]

    calendar.objects_by_sync_token.assert_not_called()
    assert calendar.search.called


async def test_an_entity_carries_the_calendar_name_alone(hass: HomeAssistant) -> None:
    await _setup(hass)

    assert hass.states.get("calendar.personal").name == "Personal"
    assert hass.states.get("todo.personal").name == "Personal"


def _older_install(hass: HomeAssistant, **entity: object) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="iven", data=ENTRY_DATA, unique_id="x")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        entry_type=dr.DeviceEntryType.SERVICE,
        name=entry.title,
    )
    er.async_get(hass).async_get_or_create(
        "todo",
        DOMAIN,
        todo_unique_id(entry.entry_id, _calendar("Personal").url),
        suggested_object_id="iven_personal",
        config_entry=entry,
        device_id=device.id,
        **entity,
    )
    return entry


async def _setup_existing(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = [
            _calendar("Personal")
        ]
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def test_entities_outlive_the_account_device_of_an_older_version(
    hass: HomeAssistant,
) -> None:
    entry = _older_install(hass)
    devices = dr.async_get(hass)
    [device] = dr.async_entries_for_config_entry(devices, entry.entry_id)
    kitchen = ar.async_get(hass).async_create("Kitchen")
    devices.async_update_device(device.id, area_id=kitchen.id)

    await _setup_existing(hass, entry)

    record = er.async_get(hass).async_get("todo.iven_personal")
    assert record.device_id is None
    assert record.area_id == kitchen.id
    assert devices.async_get(device.id) is None
    assert hass.states.get("todo.iven_personal").name == "Personal"


async def test_an_entity_disabled_with_the_account_device_stays_disabled(
    hass: HomeAssistant,
) -> None:
    # Only a device being enabled again clears a device disabler.
    entry = _older_install(hass, disabled_by=er.RegistryEntryDisabler.DEVICE)

    await _setup_existing(hass, entry)

    record = er.async_get(hass).async_get("todo.iven_personal")
    assert record.disabled_by is er.RegistryEntryDisabler.USER


def _home_set(principal: Mock) -> None:
    found: dict[str, dict] = {"/remote.php/dav/": {}, "/remote.php/dav/inbox/": {}}
    for calendar in principal.calendars.return_value:
        found[f"{calendar_key(calendar.url)}/"] = {
            dav.DisplayName.tag: Mock(text=calendar.name)
        }
    principal.calendar_home_set.get_properties.return_value = propfind_answer(found)


@pytest.fixture
def principal():
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        principal = client.return_value.principal.return_value
        principal.calendars.return_value = [_calendar("Personal")]
        _home_set(principal)
        yield principal


async def _setup_on_account(
    hass: HomeAssistant, options: dict | None = None
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="iven",
        data=ENTRY_DATA,
        options=options or {},
        unique_id="x",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _add_shopping(principal: Mock) -> None:
    principal.calendars.return_value = [_calendar("Personal"), _calendar("Shopping")]
    _home_set(principal)


async def _next_poll(hass: HomeAssistant) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=16))
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_a_calendar_added_on_the_server_is_loaded_at_the_next_poll_only(
    hass: HomeAssistant, principal: Mock
) -> None:
    await _setup_on_account(hass)

    _add_shopping(principal)
    await _next_poll(hass)

    assert hass.states.get("todo.shopping") is not None
    await _next_poll(hass)
    assert principal.calendars.call_count == 2


async def test_a_calendar_added_on_the_server_stays_out_of_a_selection(
    hass: HomeAssistant, principal: Mock
) -> None:
    await _setup_on_account(hass, {CONF_CALENDARS: ["/remote.php/dav/Personal"]})

    _add_shopping(principal)
    await _next_poll(hass)

    assert principal.calendars.call_count == 1


async def test_a_failed_first_color_read_does_not_make_every_calendar_new(
    hass: HomeAssistant, principal: Mock
) -> None:
    home = principal.calendar_home_set
    home.get_properties.side_effect = OSError("boom")
    await _setup_on_account(hass)

    home.get_properties.side_effect = None
    await _next_poll(hass)

    assert principal.calendars.call_count == 1


def _rename_on_server(principal: Mock, name: str) -> None:
    principal.calendars.return_value[0].name = name
    _home_set(principal)


@pytest.mark.parametrize(
    "options", [{}, {CONF_CALENDARS: ["/remote.php/dav/Personal"]}]
)
async def test_a_calendar_renamed_on_the_server_is_renamed_at_the_next_poll(
    hass: HomeAssistant, principal: Mock, options: dict
) -> None:
    await _setup_on_account(hass, options)

    _rename_on_server(principal, "Private")
    await _next_poll(hass)

    assert hass.states.get("calendar.personal").name == "Private"
    assert hass.states.get("todo.personal").name == "Private"
    await _next_poll(hass)
    assert principal.calendars.call_count == 2


def _renamed_by_the_server(principal: Mock) -> Mock:
    calendar = principal.calendars.return_value[0]
    calendar.set_properties.side_effect = lambda props: setattr(
        calendar, "name", props[0].value
    )
    return calendar


@pytest.mark.parametrize("entity_id", ["calendar.personal", "todo.personal"])
async def test_a_rename_in_home_assistant_renames_the_calendar_on_the_server(
    hass: HomeAssistant, principal: Mock, entity_id: str
) -> None:
    await _setup_on_account(hass)
    calendar = _renamed_by_the_server(principal)

    er.async_get(hass).async_update_entity(entity_id, name="Private")
    await hass.async_block_till_done()

    calendar.set_properties.assert_called_once()
    assert calendar.set_properties.call_args.args[0][0].value == "Private"
    assert er.async_get(hass).async_get(entity_id).name is None
    assert hass.states.get("calendar.personal").name == "Private"
    assert hass.states.get("todo.personal").name == "Private"


async def test_a_rename_of_a_read_only_calendar_stays_in_home_assistant(
    hass: HomeAssistant, principal: Mock
) -> None:
    await _setup_on_account(hass, {CONF_READ_ONLY: True})
    calendar = _renamed_by_the_server(principal)

    er.async_get(hass).async_update_entity("calendar.personal", name="Private")
    await hass.async_block_till_done()

    calendar.set_properties.assert_not_called()
    assert er.async_get(hass).async_get("calendar.personal").name == "Private"


async def test_a_rename_the_server_refuses_stays_in_home_assistant(
    hass: HomeAssistant, principal: Mock, caplog: pytest.LogCaptureFixture
) -> None:
    await _setup_on_account(hass)
    principal.calendars.return_value[0].set_properties.side_effect = OSError("boom")

    er.async_get(hass).async_update_entity("calendar.personal", name="Private")
    await hass.async_block_till_done()

    assert er.async_get(hass).async_get("calendar.personal").name == "Private"
    assert principal.calendars.call_count == 1
    assert "Could not rename Personal on the server" in caplog.text


async def test_a_name_given_before_the_update_is_not_written_to_the_server(
    hass: HomeAssistant, principal: Mock
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="iven", data=ENTRY_DATA, unique_id="x")
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "calendar",
        DOMAIN,
        f"{entry.entry_id}-/remote.php/dav/Personal",
        suggested_object_id="personal",
        config_entry=entry,
    )
    er.async_get(hass).async_update_entity("calendar.personal", name="My calendar")

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    principal.calendars.return_value[0].set_properties.assert_not_called()
    assert hass.states.get("calendar.personal").name == "My calendar"


async def test_a_selection_stored_by_name_survives_a_rename_on_the_server(
    hass: HomeAssistant, principal: Mock
) -> None:
    entry = await _setup_on_account(hass, {CONF_CALENDARS: ["Personal"]})
    assert entry.options[CONF_CALENDARS] == ["/remote.php/dav/Personal"]

    _rename_on_server(principal, "Private")
    await _next_poll(hass)

    assert er.async_get(hass).async_get("calendar.personal") is not None
    assert hass.states.get("calendar.personal").name == "Private"


async def test_an_override_stored_by_name_is_keyed_by_url(
    hass: HomeAssistant, principal: Mock
) -> None:
    entry = await _setup_on_account(
        hass, {CONF_CALENDAR_OPTIONS: {"Personal": {CONF_READ_ONLY: True}}}
    )

    assert entry.options[CONF_CALENDAR_OPTIONS] == {
        "/remote.php/dav/Personal": {CONF_READ_ONLY: True}
    }


async def test_a_calendar_deleted_on_the_server_reloads_the_entry(
    hass: HomeAssistant, principal: Mock
) -> None:
    await _setup_on_account(hass)

    principal.calendars.return_value = []
    _home_set(principal)
    await _next_poll(hass)

    assert principal.calendars.call_count == 2


async def test_an_entry_from_a_later_major_version_is_not_migrated(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="x", version=2)
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)

    assert entry.state is ConfigEntryState.MIGRATION_ERROR


async def test_a_second_spelling_of_one_account_keeps_its_own_key(
    hass: HomeAssistant,
) -> None:
    held = MockConfigEntry(
        domain=DOMAIN,
        data=ENTRY_DATA,
        unique_id="https://cloud.example.com/remote.php/dav#iven",
        minor_version=2,
    )
    held.add_to_hass(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**ENTRY_DATA, CONF_URL: "https://Cloud.example.com:443/remote.php/dav/"},
        unique_id="https://Cloud.example.com:443/remote.php/dav/#iven",
        minor_version=1,
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)

    assert entry.unique_id == "https://Cloud.example.com:443/remote.php/dav/#iven"
    assert entry.minor_version == 2


async def test_a_rename_saved_with_a_new_entity_id_reaches_the_server(
    hass: HomeAssistant, principal: Mock
) -> None:
    # Core re-adds the entity instead of reporting the update.
    await _setup_on_account(hass)
    calendar = _renamed_by_the_server(principal)

    er.async_get(hass).async_update_entity(
        "calendar.personal", name="Private", new_entity_id="calendar.private"
    )
    await hass.async_block_till_done()
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    calendar.set_properties.assert_called_once()
    assert calendar.set_properties.call_args.args[0][0].value == "Private"


async def test_a_401_on_the_entered_url_still_tries_the_bootstrap(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="iven", data=ENTRY_DATA, unique_id="x")
    entry.add_to_hass(hass)
    listed = Mock()
    listed.calendars.return_value = [_calendar("Personal")]
    refusals = iter([AuthorizationError(reason="Unauthorized")])

    def principal() -> Mock:
        if (refusal := next(refusals, None)) is not None:
            raise refusal
        return listed

    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.side_effect = principal
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED


async def test_a_color_poll_turned_away_asks_for_a_new_password(
    hass: HomeAssistant,
) -> None:
    entry = await _setup(hass)
    colors = entry.runtime_data.colors
    colors.client.principal.side_effect = AuthorizationError(reason="Unauthorized")

    await colors.async_refresh()
    await hass.async_block_till_done()

    assert any(
        flow["context"]["source"] == "reauth"
        for flow in hass.config_entries.flow.async_progress()
    )


async def test_an_entry_left_by_a_later_minor_version_is_not_migrated_back(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, unique_id="kept", minor_version=3
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)

    assert entry.unique_id == "kept"
    assert entry.minor_version == 3
