"""Tests for the account setup, the shared poller and the repair issues."""

from unittest.mock import Mock, patch

from caldav.davclient import requests
from caldav.lib.error import AuthorizationError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_TIMEOUT,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.entity_component import EntityComponent
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_caldav import async_migrate_entry
from custom_components.ha_caldav.const import (
    CONF_ADVANCED,
    CONF_CALENDARS,
    DOMAIN,
    ISSUE_BUILTIN_CALDAV,
    ISSUE_NO_SYNC_COLLECTION,
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

    calendar = _entity(hass, "calendar", "calendar.iven_personal")
    todo = _entity(hass, "todo", "todo.iven_personal")

    # Both live in the same collection under one sync token; polling twice
    # would double the work for nothing.
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

    # A warning nobody can act on any more has to go away by itself.
    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_entities_for_a_component_the_calendar_lost_are_removed(
    hass: HomeAssistant,
) -> None:
    from custom_components.ha_caldav.capability import Capability

    entry = await _setup(hass)
    registry = er.async_get(hass)
    assert registry.async_get("todo.iven_personal") is not None

    # The server now says the calendar only holds events.
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

    assert registry.async_get("todo.iven_personal") is None
    assert registry.async_get("calendar.iven_personal") is not None


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

    # Deleting the issue would drop the record of the dismissal, and changing
    # any option reloads the entry.
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
    entity = _entity(hass, "calendar", "calendar.iven_personal")
    entity.coordinator.data.extras = {"url": "https://meet.example.com/x"}

    assert entity.extra_state_attributes["url"] == "https://meet.example.com/x"


async def _connect_with(hass: HomeAssistant, error, url: str | None = None):
    """Set up an entry whose calendar listing raises, and return the entry."""
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
    # Both candidate urls were tried, and each session handed back.
    assert client.return_value.close.call_count == 2


async def test_an_unreachable_server_is_retried(hass: HomeAssistant) -> None:
    entry, _ = await _connect_with(hass, requests.ConnectionError("boom"))

    assert entry.state is ConfigEntryState.SETUP_RETRY


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

    # Once per candidate url, or the pooled sockets leak on every retry. That
    # it happens in the executor is not observable here: the test harness runs
    # a Mock handed to async_add_executor_job on the loop anyway.
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

    # Setting up "successfully" here would leave dead entities behind.
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

        # SETUP_ERROR would never be retried, so the entry would stay dead
        # until someone reloaded it by hand.
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

    # Written before the selection keyed on the url.
    assert [item.name for item in entry.runtime_data.calendars] == ["Personal"]


async def _setup_with(
    hass: HomeAssistant, calendars: list, options: dict | None = None
):
    """Set an entry up against a given calendar list and options."""
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
    # A brand-new account has none yet. Treating that as "nothing could be
    # read" would leave the entry retrying forever instead of showing an
    # account waiting for its first calendar.
    entry = await _setup_with(hass, [])

    assert entry.state is ConfigEntryState.LOADED


async def test_an_entry_with_an_empty_selection_falls_back_to_every_calendar(
    hass: HomeAssistant,
) -> None:
    # The options form refuses an empty selection, so this shape only reaches
    # setup from a hand-edited entry. Loading nothing there would look like a
    # broken integration; the form is where the choice is made.
    await _setup_with(hass, [_calendar("Personal")], options={CONF_CALENDARS: []})

    assert hass.states.get("calendar.iven_personal") is not None


async def test_an_entry_keyed_before_normalization_is_rekeyed(
    hass: HomeAssistant,
) -> None:
    """It could otherwise be set up a second time under its other spelling,
    doubling every calendar and to-do list on the account."""
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
    """Reload an entry against a different answer from the account."""
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = calendars
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()


async def test_a_calendar_missing_from_one_answer_keeps_its_entities(
    hass: HomeAssistant,
) -> None:
    """A server having a bad minute is not a decision to stop tracking a
    calendar, and answering it by deleting the registry record takes the
    recorder history, the automations, the dashboard cards and the name
    overrides that point at the entity with it, irreversibly and while setup
    still reports success."""
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
    assert "calendar.iven_work" in before

    await _reload_with(hass, entry, [personal])

    after = {
        e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert after == before


async def test_a_calendar_the_user_unticked_loses_its_entities(
    hass: HomeAssistant,
) -> None:
    # The account still lists it, so the selection is the only thing leaving it
    # out: that is a decision, and the entities would otherwise stay in the
    # registry as permanently unavailable.
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
    assert kept == {"calendar.iven_personal", "todo.iven_personal"}


async def test_the_migration_is_reachable_at_all(hass: HomeAssistant) -> None:
    """Home Assistant compares the stored version against the handler's and
    returns before loading the component when they agree, so a migration
    written for entries that already carry the current number never runs and
    the account it was written for is set up twice."""
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
    # Keyed on the url as it stood, the reconfigure step renamed every entity
    # to _2 the first time the account moved from http to https.
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
    """It belongs in reauth, not in the retry loop: the principal lookup went
    through, so nothing else will notice, and the entry would retry forever
    without ever prompting for the new password."""
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
    """The built-in integration is set up on its own, so the one spelling that
    would not warn is the one where the two were typed differently — which is
    most of them, and the user sees every calendar and to-do list twice."""
    builtin = MockConfigEntry(
        domain="caldav",
        data={
            CONF_URL: "https://cloud.example.com/remote.php/dav",
            CONF_USERNAME: "iven",
            CONF_PASSWORD: "secret",
        },
    )
    builtin.add_to_hass(hass)
    # Ours needs normalizing too, or the comparison would come out right
    # without it and prove nothing.
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
    """Decided against what the account listed, not against what was loaded out
    of it: those differ exactly when a selected calendar is missing from one
    answer, and deleting its record then is irreversible."""
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

    # The server answers with Personal alone this time.
    _async_prune_deselected(hass, entry, [personal], registry)

    after = {
        record.entity_id
        for record in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert after == before


async def test_the_migration_tells_the_two_halves_apart_by_platform(
    hass: HomeAssistant,
) -> None:
    """A collection url may itself end in "-todo", and calendar_key drops a
    trailing slash and a query string, so reading the suffix off the key cannot
    tell the two entities apart. Where both mapped onto one key the second
    re-key was refused and that entity stayed unavailable for good, with
    minor_version already bumped so nothing would try again."""
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
    """An entity registered under this entry need not be one of ours: a helper
    or a template built on the account carries a key of its own shape, and
    rebuilding one would point it at a calendar."""
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
    """Two urls differing only in what the normalization drops land on one key.
    Raising there would fail the whole setup for an account whose other
    calendars are fine, and every entity on it would go with them."""
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
    # Left where it was, and still in the registry for the user to see.
    assert registry.async_get(second.entity_id).unique_id == f"{entry.entry_id}-{url}/"


async def test_the_timeout_option_is_what_a_request_gets(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="iven",
        data=ENTRY_DATA,
        options={CONF_ADVANCED: {CONF_TIMEOUT: 90}},
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
