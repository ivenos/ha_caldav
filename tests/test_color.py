"""Tests for calendar colors taken from the CalDAV server."""

from datetime import timedelta
from unittest.mock import Mock, patch

from caldav.davclient import DAVResponse
from caldav.elements import ical
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
import requests

from custom_components.ha_caldav.calendar import COLOR_STATE
from custom_components.ha_caldav.color import fetch_colors, normalize_color
from custom_components.ha_caldav.connection import calendar_key
from custom_components.ha_caldav.const import DOMAIN
from custom_components.ha_caldav.coordinator import HaCaldavColorCoordinator

ENTRY_DATA = {
    CONF_URL: "https://cloud.example.com/remote.php/dav",
    CONF_USERNAME: "iven",
    CONF_PASSWORD: "secret",
    CONF_VERIFY_SSL: True,
}
PERSONAL_PATH = "/remote.php/dav/calendars/iven/personal"
PERSONAL_URL = f"https://cloud.example.com{PERSONAL_PATH}/"
# caldav reduces every href to a path before we ever see it.
PERSONAL_HREF = f"{PERSONAL_PATH}/"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("#00679e", "#00679e"),
        ("#E9D859", "#e9d859"),
        # Alpha is what Apple and older Nextcloud append; Home Assistant would
        # throw the whole color away over it.
        ("#711A76FF", "#711a76"),
        ("#FF000080", "#ff0000"),
        ("#F00", "#ff0000"),
        ("#f00a", "#ff0000"),
        ("  #00679e  ", "#00679e"),
        ("00679e", "#00679e"),
        ("#12345", None),
        ("#1234567", None),
        ("#zzzzzz", None),
        # Rejected at the last digit, not the first: Home Assistant validates
        # what registration writes but not what a later poll does.
        ("#00679z", None),
        ("red", None),
        ("", None),
        (None, None),
        (0x00679E, None),
    ],
)
def test_normalize_color(value, expected) -> None:
    assert normalize_color(value) == expected


def _props(value: str | None) -> dict:
    """Mimic caldav: a calendar without a color answers 404 and drops the prop."""
    return {} if value is None else {ical.CalendarColor.tag: Mock(text=value)}


def _client(colors: dict[str, str | None] | None = None, calendars=None) -> Mock:
    """Build a caldav client whose home set answers a color PROPFIND."""
    client = Mock()
    principal = client.principal.return_value
    principal.calendars.return_value = calendars or []
    home = principal.calendar_home_set
    home.get_properties.return_value.find_objects_and_props.return_value = {
        href: _props(value) for href, value in (colors or {}).items()
    }
    return client


def test_fetch_colors_keys_by_path_and_reports_the_colorless() -> None:
    client = _client(
        {
            "/remote.php/dav/calendars/iven/personal/": "#00679E",
            "/remote.php/dav/calendars/iven/work/": "#FF000080",
            # The home set itself, and a calendar whose color is unusable.
            "/remote.php/dav/calendars/iven/": None,
            "/remote.php/dav/calendars/iven/broken/": "nope",
        }
    )

    # Every href answered for is a key: absent has to stay distinguishable from
    # colorless, or a lookup that went wrong would clear everyone's color.
    assert fetch_colors(client) == {
        "/remote.php/dav/calendars/iven/personal": "#00679e",
        "/remote.php/dav/calendars/iven/work": "#ff0000",
        "/remote.php/dav/calendars/iven": None,
        "/remote.php/dav/calendars/iven/broken": None,
    }


def _dav_response(body: str) -> DAVResponse:
    """Wrap canned multistatus XML the way caldav hands a response over."""
    raw = requests.Response()
    raw.status_code = 207
    raw.headers["Content-Type"] = "application/xml"
    raw._content = body.encode()
    return DAVResponse(raw)


MULTISTATUS = """<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:i="http://apple.com/ns/ical/">
  <d:response>
    <d:href>/remote.php/dav/calendars/iven/</d:href>
    <d:propstat><d:prop><i:calendar-color/></d:prop>
      <d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/calendars/iven/a%2520b/</d:href>
    <d:propstat>
      <d:prop><i:calendar-color>#E9D859</i:calendar-color></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/calendars/iven/personal/</d:href>
    <d:propstat>
      <d:prop>
        <i:calendar-color symbolic-color="blue">#00679EFF</i:calendar-color>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
</d:multistatus>"""


def test_fetch_colors_reads_the_unparsed_response() -> None:
    # Run a real caldav response through it: only the unparsed form carries a
    # row per calendar, and asking for the parsed one leaves nothing to read.
    client = Mock()
    home = client.principal.return_value.calendar_home_set
    home.get_properties.side_effect = lambda props, depth=0, parse_response_xml=True: (
        _dav_response(MULTISTATUS) if not parse_response_xml else {}
    )

    # Alpha off, and Apple's symbolic-color attribute does not get in the way.
    # caldav resolves the encoding on an href exactly once, so the literal
    # percent sequence has to come back intact.
    assert fetch_colors(client) == {
        "/remote.php/dav/calendars/iven": None,
        "/remote.php/dav/calendars/iven/a%20b": "#e9d859",
        "/remote.php/dav/calendars/iven/personal": "#00679e",
    }


def test_fetch_colors_keeps_a_literal_percent_sequence() -> None:
    # caldav resolved the encoding on the href once already. Resolving it again
    # would key this under a path no calendar url ever reduces to.
    client = _client({"/dav/a%20b/": "#00679e"})

    colors = fetch_colors(client)

    assert colors == {"/dav/a%20b": "#00679e"}
    assert calendar_key("https://cloud.example.com/dav/a%2520b/") in colors


def test_fetch_colors_asks_once_for_every_calendar() -> None:
    client = _client({"/dav/personal/": "#00679e"})

    fetch_colors(client)

    home = client.principal.return_value.calendar_home_set
    assert home.get_properties.call_count == 1
    assert home.get_properties.call_args.kwargs["depth"] == 1


# AssertionError is what caldav raises on a multistatus it does not expect, so
# it has to be covered as squarely as a dead socket.
FAILURES = [OSError("boom"), AssertionError("weird xml")]


@pytest.mark.parametrize("failure", FAILURES)
async def test_failed_fetch_keeps_the_last_known_colors(
    hass: HomeAssistant, failure
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="x")
    entry.add_to_hass(hass)
    client = _client({"/dav/personal/": "#00679e"})
    coordinator = HaCaldavColorCoordinator(hass, entry, client, timedelta(minutes=15))
    await coordinator.async_refresh()
    assert coordinator.data == {"/dav/personal": "#00679e"}

    client.principal.return_value.calendar_home_set.get_properties.side_effect = failure
    await coordinator.async_refresh()

    assert coordinator.data == {"/dav/personal": "#00679e"}


@pytest.mark.parametrize("failure", FAILURES)
async def test_failed_first_fetch_is_not_an_empty_result(
    hass: HomeAssistant, failure
) -> None:
    # An empty map means the server reports no colors, which would clear them.
    # Never having read one has to stay distinguishable from that.
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="x")
    entry.add_to_hass(hass)
    client = _client()
    client.principal.return_value.calendar_home_set.get_properties.side_effect = failure
    coordinator = HaCaldavColorCoordinator(hass, entry, client, timedelta(minutes=15))

    await coordinator.async_refresh()

    assert coordinator.data is None
    # Colors are cosmetic: a server that cannot answer must not send the whole
    # account into reauth, and must not log a traceback on every poll either.
    assert isinstance(coordinator.last_exception, UpdateFailed)


def _calendar(name: str, url: str) -> Mock:
    calendar = Mock()
    calendar.name = name
    calendar.url = url
    calendar.search.return_value = []
    return calendar


async def _setup(
    hass: HomeAssistant,
    colors: dict[str, str | None],
    entry: MockConfigEntry | None = None,
    calendars: list[Mock] | None = None,
) -> MockConfigEntry:
    if entry is None:
        entry = MockConfigEntry(
            domain=DOMAIN, title="iven", data=ENTRY_DATA, unique_id="x"
        )
        entry.add_to_hass(hass)
    if calendars is None:
        calendars = [_calendar("Personal", PERSONAL_URL)]
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value = _client(colors, calendars)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _options(hass: HomeAssistant, entity_id: str = "calendar.iven_personal") -> dict:
    return dict(er.async_get(hass).async_get(entity_id).options)


async def test_new_entity_is_registered_with_the_server_color(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, {PERSONAL_HREF: "#00679e"})

    options = _options(hass)
    assert options["calendar"]["color"] == "#00679e"
    assert options[COLOR_STATE]["color"] == "#00679e"


async def test_calendar_without_a_color_gets_none(hass: HomeAssistant) -> None:
    await _setup(hass, {PERSONAL_HREF: None})

    assert "calendar" not in _options(hass)
    assert _options(hass)[COLOR_STATE] == {"color": None, "override": False}


def _register_existing(
    hass: HomeAssistant, options: dict | None = None
) -> MockConfigEntry:
    """Register the calendar entity the way an older install left it behind."""
    entry = MockConfigEntry(domain=DOMAIN, title="iven", data=ENTRY_DATA, unique_id="x")
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    existing = registry.async_get_or_create(
        "calendar",
        DOMAIN,
        f"{entry.entry_id}-{PERSONAL_URL}",
        suggested_object_id="iven_personal",
        config_entry=entry,
    )
    for domain, values in (options or {}).items():
        registry.async_update_entity_options(existing.entity_id, domain, values)
    return entry


async def test_color_reaches_an_account_that_predates_the_feature(
    hass: HomeAssistant,
) -> None:
    # initial_color never runs for an entity that is already registered, so
    # without the sync below these installs would stay colorless forever.
    entry = _register_existing(hass)

    await _setup(hass, {PERSONAL_HREF: "#00679e"}, entry)

    assert _options(hass)["calendar"]["color"] == "#00679e"


async def test_hand_picked_color_survives(hass: HomeAssistant) -> None:
    entry = _register_existing(hass, {"calendar": {"color": "#abcdef"}})

    await _setup(hass, {PERSONAL_HREF: "#00679e"}, entry)

    options = _options(hass)
    assert options["calendar"]["color"] == "#abcdef"
    assert options[COLOR_STATE] == {"color": "#00679e", "override": True}


def _entity(hass: HomeAssistant):
    return hass.data["entity_components"]["calendar"].get_entity(
        "calendar.iven_personal"
    )


async def _recolor(hass: HomeAssistant, color: str | None) -> None:
    """Change the color on the server and let the next poll pick it up."""
    entity = _entity(hass)
    home = entity.colors.client.principal.return_value.calendar_home_set
    home.get_properties.return_value.find_objects_and_props.return_value = {
        PERSONAL_HREF: _props(color)
    }
    await entity.colors.async_refresh()
    await hass.async_block_till_done()


async def test_server_side_change_propagates(hass: HomeAssistant) -> None:
    await _setup(hass, {PERSONAL_HREF: "#00679e"})

    await _recolor(hass, "#123456")

    options = _options(hass)
    assert options["calendar"]["color"] == "#123456"
    assert options[COLOR_STATE]["color"] == "#123456"


async def test_server_side_change_leaves_a_hand_picked_color_alone(
    hass: HomeAssistant,
) -> None:
    entry = _register_existing(hass, {"calendar": {"color": "#abcdef"}})
    await _setup(hass, {PERSONAL_HREF: "#00679e"}, entry)

    await _recolor(hass, "#123456")

    assert _options(hass)["calendar"]["color"] == "#abcdef"


async def test_cleared_color_stays_cleared(hass: HomeAssistant) -> None:
    await _setup(hass, {PERSONAL_HREF: "#00679e"})
    # Clearing the color in the UI is a choice too, and a later poll must not
    # quietly undo it.
    er.async_get(hass).async_update_entity_options(
        "calendar.iven_personal", "calendar", None
    )

    await _recolor(hass, "#00679e")
    assert "calendar" not in _options(hass)

    await _recolor(hass, "#123456")
    assert "calendar" not in _options(hass)


async def test_color_dropped_on_the_server_is_dropped_here(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, {PERSONAL_HREF: "#00679e"})

    await _recolor(hass, None)

    options = _options(hass)
    assert "calendar" not in options
    assert options[COLOR_STATE]["color"] is None


async def test_cleared_color_survives_the_server_dropping_its_own(
    hass: HomeAssistant,
) -> None:
    # Both sides end up without a color, which must not read as "never tracked"
    # and let the next server-side color back in.
    await _setup(hass, {PERSONAL_HREF: "#00679e"})
    er.async_get(hass).async_update_entity_options(
        "calendar.iven_personal", "calendar", None
    )

    await _recolor(hass, None)
    await _recolor(hass, "#123456")

    assert "calendar" not in _options(hass)


async def test_cleared_color_survives_a_failed_poll(hass: HomeAssistant) -> None:
    await _setup(hass, {PERSONAL_HREF: "#00679e"})
    er.async_get(hass).async_update_entity_options(
        "calendar.iven_personal", "calendar", None
    )
    entity = _entity(hass)
    home = entity.colors.client.principal.return_value.calendar_home_set
    home.get_properties.side_effect = OSError("boom")
    await entity.colors.async_refresh()
    home.get_properties.side_effect = None

    # Nothing changed on the server, so the cleared color has to stay cleared.
    await _recolor(hass, "#00679e")

    assert "calendar" not in _options(hass)


async def test_hand_picked_color_stays_protected_once_it_matches_the_server(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, {PERSONAL_HREF: "#00679e"})
    registry = er.async_get(hass)
    registry.async_update_entity_options(
        "calendar.iven_personal", "calendar", {"color": "#abcdef"}
    )
    await _recolor(hass, "#00679e")
    # Settling on the color the server happens to carry must not hand the
    # entity back to automatic tracking.
    registry.async_update_entity_options(
        "calendar.iven_personal", "calendar", {"color": "#00679e"}
    )
    await _recolor(hass, "#00679e")

    await _recolor(hass, "#123456")

    assert _options(hass)["calendar"]["color"] == "#00679e"


async def test_color_picked_and_changed_again_between_polls_is_caught(
    hass: HomeAssistant,
) -> None:
    # Landing back on the server's color leaves nothing for a poll to notice,
    # so the pick has to be seen as it happens.
    await _setup(hass, {PERSONAL_HREF: "#00679e"})
    registry = er.async_get(hass)
    for color in ("#abcdef", "#00679e"):
        registry.async_update_entity_options(
            "calendar.iven_personal", "calendar", {"color": color}
        )
        await hass.async_block_till_done()

    await _recolor(hass, "#123456")

    assert _options(hass)["calendar"]["color"] == "#00679e"


WORK_PATH = "/remote.php/dav/calendars/iven/work"


async def test_each_calendar_gets_its_own_color(hass: HomeAssistant) -> None:
    # With a single calendar any mix-up looks like a match.
    await _setup(
        hass,
        {PERSONAL_HREF: "#00679e", f"{WORK_PATH}/": "#e9d859"},
        calendars=[
            _calendar("Personal", PERSONAL_URL),
            _calendar("Work", f"https://cloud.example.com{WORK_PATH}/"),
        ],
    )

    assert _options(hass)["calendar"]["color"] == "#00679e"
    assert _options(hass, "calendar.iven_work")["calendar"]["color"] == "#e9d859"


async def test_color_is_polled_without_anyone_asking(hass: HomeAssistant) -> None:
    # Everything else here drives the coordinator by hand, which would still
    # pass if it never polled on its own.
    await _setup(hass, {PERSONAL_HREF: "#00679e"})
    home = _entity(hass).colors.client.principal.return_value.calendar_home_set
    home.get_properties.return_value.find_objects_and_props.return_value = {
        PERSONAL_HREF: _props("#123456")
    }

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=16))
    # The scheduled poll is a background task.
    await hass.async_block_till_done(wait_background_tasks=True)

    assert _options(hass)["calendar"]["color"] == "#123456"


async def test_two_server_side_changes_in_a_row_both_land(
    hass: HomeAssistant,
) -> None:
    # Our own write goes through the same registry the pick watcher listens on,
    # so a first change must not leave the entity looking hand-picked.
    await _setup(hass, {PERSONAL_HREF: "#00679e"})

    await _recolor(hass, "#123456")
    await _recolor(hass, "#abcdef")

    assert _options(hass)["calendar"]["color"] == "#abcdef"


async def _reload(hass: HomeAssistant, entry: MockConfigEntry, color: str) -> None:
    """Restart the entry, which drops everything not in the registry."""
    await hass.config_entries.async_unload(entry.entry_id)
    await _setup(hass, {PERSONAL_HREF: color}, entry)


async def test_hand_picked_color_survives_a_restart(hass: HomeAssistant) -> None:
    entry = await _setup(hass, {PERSONAL_HREF: "#00679e"})
    er.async_get(hass).async_update_entity_options(
        "calendar.iven_personal", "calendar", {"color": "#abcdef"}
    )
    await hass.async_block_till_done()

    # The in-memory note of the pick is gone after this; only the stored flag
    # can still tell that the color is the user's.
    await _reload(hass, entry, "#00679e")
    await _recolor(hass, "#123456")

    assert _options(hass)["calendar"]["color"] == "#abcdef"


async def test_color_picked_while_unloaded_survives(hass: HomeAssistant) -> None:
    entry = await _setup(hass, {PERSONAL_HREF: "#00679e"})
    await hass.config_entries.async_unload(entry.entry_id)
    # Nothing is watching the registry now, so the pick can only be noticed by
    # comparing against what we last wrote.
    er.async_get(hass).async_update_entity_options(
        "calendar.iven_personal", "calendar", {"color": "#abcdef"}
    )

    await _setup(hass, {PERSONAL_HREF: "#00679e"}, entry)
    await _recolor(hass, "#123456")

    assert _options(hass)["calendar"]["color"] == "#abcdef"


async def test_pick_matching_the_server_survives_a_restart(hass: HomeAssistant) -> None:
    entry = await _setup(hass, {PERSONAL_HREF: "#00679e"})
    registry = er.async_get(hass)
    for color in ("#abcdef", "#00679e"):
        registry.async_update_entity_options(
            "calendar.iven_personal", "calendar", {"color": color}
        )
        await hass.async_block_till_done()

    # What is stored now looks exactly like a color we wrote ourselves. Only
    # the flag still says otherwise, and it has to keep saying it.
    await _reload(hass, entry, "#00679e")
    await _recolor(hass, "#123456")

    assert _options(hass)["calendar"]["color"] == "#00679e"


async def test_unrelated_registry_edit_is_not_a_pick(hass: HomeAssistant) -> None:
    # Renaming an entity writes to the same registry the pick watcher listens
    # on, and must not read as the user choosing a color.
    await _setup(hass, {PERSONAL_HREF: "#00679e"})
    er.async_get(hass).async_update_entity("calendar.iven_personal", name="My calendar")
    await hass.async_block_till_done()

    await _recolor(hass, "#123456")

    assert _options(hass)["calendar"]["color"] == "#123456"


async def test_clearing_without_a_record_survives(hass: HomeAssistant) -> None:
    # No record yet, because the first fetch failed. Clearing the color now is
    # the only signal that it was a choice, and it has to outlive the recovery.
    entry = _register_existing(hass, {"calendar": {"color": "#abcdef"}})
    calendars = [_calendar("Personal", PERSONAL_URL)]
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value = _client({}, calendars)
        home = client.return_value.principal.return_value.calendar_home_set
        home.get_properties.side_effect = OSError("boom")
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        er.async_get(hass).async_update_entity_options(
            "calendar.iven_personal", "calendar", None
        )
        await hass.async_block_till_done()
        home.get_properties.side_effect = None

    await _recolor(hass, "#00679e")

    assert "calendar" not in _options(hass)


async def test_calendar_the_server_did_not_report_keeps_its_color(
    hass: HomeAssistant,
) -> None:
    # An href that stops matching is a lookup gone wrong, not a color someone
    # removed, and clearing it is the one thing the user cannot undo.
    await _setup(hass, {PERSONAL_HREF: "#00679e"})
    entity = _entity(hass)
    home = entity.colors.client.principal.return_value.calendar_home_set
    home.get_properties.return_value.find_objects_and_props.return_value = {
        "/somewhere/else/": _props("#123456")
    }

    await entity.colors.async_refresh()
    await hass.async_block_till_done()

    assert _options(hass)["calendar"]["color"] == "#00679e"


async def test_overridden_calendar_stops_writing(hass: HomeAssistant) -> None:
    await _setup(hass, {PERSONAL_HREF: "#00679e"})
    registry = er.async_get(hass)
    registry.async_update_entity_options(
        "calendar.iven_personal", "calendar", {"color": "#abcdef"}
    )
    await hass.async_block_till_done()
    writes = []
    hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, writes.append)

    await _recolor(hass, "#123456")

    assert writes == []


async def test_a_record_missing_a_key_is_tolerated(hass: HomeAssistant) -> None:
    # Nothing released ever wrote this namespace, but a hand-edited registry
    # must not put the poll into a crash loop.
    entry = _register_existing(
        hass,
        {"calendar": {"color": "#00679e"}, COLOR_STATE: {"color": "#00679e"}},
    )

    await _setup(hass, {PERSONAL_HREF: "#123456"}, entry)

    assert _options(hass)["calendar"]["color"] == "#123456"


async def test_unchanged_color_touches_nothing(hass: HomeAssistant) -> None:
    # This runs on every poll, so it has to stay a no-op when nothing moved.
    await _setup(hass, {PERSONAL_HREF: "#00679e"})
    writes = []
    hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, writes.append)

    await _recolor(hass, "#00679e")

    assert writes == []


async def test_color_cleared_while_unloaded_survives(hass: HomeAssistant) -> None:
    entry = await _setup(hass, {PERSONAL_HREF: "#00679e"})
    await hass.config_entries.async_unload(entry.entry_id)
    # Same as picking one while unloaded: nothing is watching, so an empty
    # color has to read as a choice from the stored state alone.
    er.async_get(hass).async_update_entity_options(
        "calendar.iven_personal", "calendar", None
    )

    await _setup(hass, {PERSONAL_HREF: "#00679e"}, entry)
    await _recolor(hass, "#123456")

    assert "calendar" not in _options(hass)


async def test_cleared_color_survives_a_restart(hass: HomeAssistant) -> None:
    entry = await _setup(hass, {PERSONAL_HREF: "#00679e"})
    er.async_get(hass).async_update_entity_options(
        "calendar.iven_personal", "calendar", None
    )
    await hass.async_block_till_done()

    await _reload(hass, entry, "#00679e")
    await _recolor(hass, "#123456")

    assert "calendar" not in _options(hass)


async def test_startup_failure_keeps_the_visible_color(hass: HomeAssistant) -> None:
    # A failed first fetch reports no colors at all. Taking that at face value
    # would strip every calendar until the next successful poll.
    entry = await _setup(hass, {PERSONAL_HREF: "#00679e"})
    await hass.config_entries.async_unload(entry.entry_id)

    calendars = [_calendar("Personal", PERSONAL_URL)]
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value = _client({}, calendars)
        home = client.return_value.principal.return_value.calendar_home_set
        home.get_properties.side_effect = OSError("boom")
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert _options(hass)["calendar"]["color"] == "#00679e"


async def test_entity_enabled_later_follows_the_server(hass: HomeAssistant) -> None:
    # Disabled, so nothing of ours ever ran for it: the color comes from
    # registration alone, and enabling it later still has to pick up a change.
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="iven",
        data=ENTRY_DATA,
        unique_id="x",
        pref_disable_new_entities=True,
    )
    entry.add_to_hass(hass)
    await _setup(hass, {PERSONAL_HREF: "#00679e"}, entry)
    assert _options(hass)["calendar"]["color"] == "#00679e"

    er.async_get(hass).async_update_entity("calendar.iven_personal", disabled_by=None)
    await _reload(hass, entry, "#123456")

    assert _options(hass)["calendar"]["color"] == "#123456"


async def test_setup_survives_a_server_that_cannot_answer(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="iven", data=ENTRY_DATA, unique_id="x")
    entry.add_to_hass(hass)
    calendars = [_calendar("Personal", PERSONAL_URL)]
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value = _client({}, calendars)
        home = client.return_value.principal.return_value.calendar_home_set
        home.get_properties.side_effect = OSError("boom")
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.get("calendar.iven_personal") is not None
    assert "color" not in _options(hass).get("calendar", {})


async def test_a_failed_colour_read_names_no_url_and_no_body(
    hass: HomeAssistant,
) -> None:
    """UpdateFailed is logged at error level, in the plain log.

    caldav's own message carries the url it was reading, the account name in
    it, and the whole response body it got back, which for a failing REPORT is
    calendar content.
    """
    from caldav.lib.error import PropfindError

    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="x")
    entry.add_to_hass(hass)
    client = _client()
    client.principal.return_value.calendar_home_set.get_properties.side_effect = (
        PropfindError(
            "403 Forbidden at 'https://cloud.example.com/remote.php/dav/iven/'"
            "\n\n<d:multistatus><d:href>/dav/iven/therapy/</d:href></d:multistatus>"
        )
    )
    coordinator = HaCaldavColorCoordinator(hass, entry, client, timedelta(minutes=15))

    await coordinator.async_refresh()

    reported = str(coordinator.last_exception)
    assert "cloud.example.com" not in reported
    assert "therapy" not in reported
    assert "PropfindError" in reported


async def test_writing_a_color_after_a_hand_pick_makes_the_calendar_follow_again(
    hass: HomeAssistant,
) -> None:
    """Left recorded against the old color, the registry watcher reads the
    user's earlier pick as a fresh one and puts the override straight back, so
    the service reports success and nothing on screen ever changes again."""
    await _setup(hass, {PERSONAL_HREF: "#00679e"})
    er.async_get(hass).async_update_entity_options(
        "calendar.iven_personal", "calendar", {"color": "#abcdef"}
    )
    await hass.async_block_till_done()
    assert _options(hass)[COLOR_STATE]["override"] is True

    _entity(hass).async_follow_server_color()
    await hass.async_block_till_done()

    assert _options(hass)[COLOR_STATE]["override"] is False

    await _recolor(hass, "#cccccc")

    assert _options(hass)["calendar"]["color"] == "#cccccc"
    assert _options(hass)[COLOR_STATE] == {"color": "#cccccc", "override": False}
