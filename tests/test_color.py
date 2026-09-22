from datetime import timedelta
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from caldav.davclient import DAVResponse
from caldav.elements import ical
from conftest import propfind_answer
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
from custom_components.ha_caldav.color import (
    Collection,
    fetch_collections,
    normalize_color,
)
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
        # Apple and older Nextcloud append alpha, which Home Assistant refuses.
        ("#711A76FF", "#711a76"),
        ("#FF000080", "#ff0000"),
        ("#F00", "#ff0000"),
        ("#f00a", "#ff0000"),
        ("  #00679e  ", "#00679e"),
        ("00679e", "#00679e"),
        ("#12345", None),
        ("#1234567", None),
        ("#zzzzzz", None),
        # Home Assistant validates what registration writes, not what a poll does.
        ("#00679z", None),
        ("red", "#ff0000"),
        ("Navy", "#000080"),
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
    client = Mock()
    principal = client.principal.return_value
    principal.calendars.return_value = calendars or []
    home = principal.calendar_home_set
    home.url = "https://cloud.example.com/dav/"
    home.get_properties.return_value = propfind_answer(
        {href: _props(value) for href, value in (colors or {}).items()}
    )
    return client


def test_fetch_collections_keys_by_path_and_reports_the_colorless() -> None:
    client = _client(
        {
            "/remote.php/dav/calendars/iven/personal/": "#00679E",
            "/remote.php/dav/calendars/iven/work/": "#FF000080",
            "/remote.php/dav/calendars/iven/": None,
            "/remote.php/dav/calendars/iven/broken/": "nope",
        }
    )

    assert fetch_collections(client) == {
        "/remote.php/dav/calendars/iven/personal": Collection("#00679e", None),
        "/remote.php/dav/calendars/iven/work": Collection("#ff0000", None),
        "/remote.php/dav/calendars/iven": Collection(None, None),
        "/remote.php/dav/calendars/iven/broken": Collection(None, None),
    }


def _dav_response(body: str) -> DAVResponse:
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
      <d:prop><i:calendar-color>#E9D859</i:calendar-color><d:displayname/></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/calendars/iven/personal/</d:href>
    <d:propstat>
      <d:prop>
        <i:calendar-color symbolic-color="blue">#00679EFF</i:calendar-color>
        <d:displayname>Iven</d:displayname>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
</d:multistatus>"""


def test_fetch_collections_reads_the_unparsed_response() -> None:
    # Only the unparsed form of a caldav response carries a row per calendar.
    client = Mock()
    home = client.principal.return_value.calendar_home_set
    home.get_properties.side_effect = lambda props, depth=0, parse_response_xml=True: (
        _dav_response(MULTISTATUS) if not parse_response_xml else {}
    )

    # caldav resolves the encoding on an href exactly once.
    assert fetch_collections(client) == {
        "/remote.php/dav/calendars/iven": Collection(None, None),
        "/remote.php/dav/calendars/iven/a%20b": Collection("#e9d859", None),
        "/remote.php/dav/calendars/iven/personal": Collection("#00679e", "Iven"),
    }


def test_fetch_collections_keeps_a_literal_percent_sequence() -> None:
    client = _client({"/dav/a%2520b/": "#00679e"})

    collections = fetch_collections(client)

    assert collections == {"/dav/a%20b": Collection("#00679e", None)}
    assert calendar_key("https://cloud.example.com/dav/a%2520b/") in collections


def test_fetch_collections_asks_once_for_every_calendar() -> None:
    client = _client({"/dav/personal/": "#00679e"})

    fetch_collections(client)

    home = client.principal.return_value.calendar_home_set
    assert home.get_properties.call_count == 1
    assert home.get_properties.call_args.kwargs["depth"] == 1


# caldav raises AssertionError on a multistatus it does not expect.
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
    assert coordinator.data == {"/dav/personal": Collection("#00679e", None)}

    client.principal.return_value.calendar_home_set.get_properties.side_effect = failure
    await coordinator.async_refresh()

    assert coordinator.data == {"/dav/personal": Collection("#00679e", None)}


@pytest.mark.parametrize("failure", FAILURES)
async def test_failed_first_fetch_is_not_an_empty_result(
    hass: HomeAssistant, failure
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="x")
    entry.add_to_hass(hass)
    client = _client()
    client.principal.return_value.calendar_home_set.get_properties.side_effect = failure
    coordinator = HaCaldavColorCoordinator(hass, entry, client, timedelta(minutes=15))

    await coordinator.async_refresh()

    assert coordinator.data is None
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


def _options(hass: HomeAssistant, entity_id: str = "calendar.personal") -> dict:
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
    entry = MockConfigEntry(domain=DOMAIN, title="iven", data=ENTRY_DATA, unique_id="x")
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    existing = registry.async_get_or_create(
        "calendar",
        DOMAIN,
        f"{entry.entry_id}-{PERSONAL_URL}",
        suggested_object_id="personal",
        config_entry=entry,
    )
    for domain, values in (options or {}).items():
        registry.async_update_entity_options(existing.entity_id, domain, values)
    return entry


async def test_color_reaches_an_account_that_predates_the_feature(
    hass: HomeAssistant,
) -> None:
    # initial_color never runs for an entity that is already registered.
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
    return hass.data["entity_components"]["calendar"].get_entity("calendar.personal")


async def _recolor(hass: HomeAssistant, color: str | None) -> None:
    entity = _entity(hass)
    home = entity.colors.client.principal.return_value.calendar_home_set
    home.get_properties.return_value = propfind_answer({PERSONAL_HREF: _props(color)})
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
    er.async_get(hass).async_update_entity_options(
        "calendar.personal", "calendar", None
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
    await _setup(hass, {PERSONAL_HREF: "#00679e"})
    er.async_get(hass).async_update_entity_options(
        "calendar.personal", "calendar", None
    )

    await _recolor(hass, None)
    await _recolor(hass, "#123456")

    assert "calendar" not in _options(hass)


async def test_cleared_color_survives_a_failed_poll(hass: HomeAssistant) -> None:
    await _setup(hass, {PERSONAL_HREF: "#00679e"})
    er.async_get(hass).async_update_entity_options(
        "calendar.personal", "calendar", None
    )
    entity = _entity(hass)
    home = entity.colors.client.principal.return_value.calendar_home_set
    home.get_properties.side_effect = OSError("boom")
    await entity.colors.async_refresh()
    home.get_properties.side_effect = None

    await _recolor(hass, "#00679e")

    assert "calendar" not in _options(hass)


async def test_hand_picked_color_stays_protected_once_it_matches_the_server(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, {PERSONAL_HREF: "#00679e"})
    registry = er.async_get(hass)
    registry.async_update_entity_options(
        "calendar.personal", "calendar", {"color": "#abcdef"}
    )
    await _recolor(hass, "#00679e")
    registry.async_update_entity_options(
        "calendar.personal", "calendar", {"color": "#00679e"}
    )
    await _recolor(hass, "#00679e")

    await _recolor(hass, "#123456")

    assert _options(hass)["calendar"]["color"] == "#00679e"


async def test_color_picked_and_changed_again_between_polls_is_caught(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, {PERSONAL_HREF: "#00679e"})
    registry = er.async_get(hass)
    for color in ("#abcdef", "#00679e"):
        registry.async_update_entity_options(
            "calendar.personal", "calendar", {"color": color}
        )
        await hass.async_block_till_done()

    await _recolor(hass, "#123456")

    assert _options(hass)["calendar"]["color"] == "#00679e"


WORK_PATH = "/remote.php/dav/calendars/iven/work"


async def test_each_calendar_gets_its_own_color(hass: HomeAssistant) -> None:
    await _setup(
        hass,
        {PERSONAL_HREF: "#00679e", f"{WORK_PATH}/": "#e9d859"},
        calendars=[
            _calendar("Personal", PERSONAL_URL),
            _calendar("Work", f"https://cloud.example.com{WORK_PATH}/"),
        ],
    )

    assert _options(hass)["calendar"]["color"] == "#00679e"
    assert _options(hass, "calendar.work")["calendar"]["color"] == "#e9d859"


async def test_color_is_polled_without_anyone_asking(hass: HomeAssistant) -> None:
    await _setup(hass, {PERSONAL_HREF: "#00679e"})
    home = _entity(hass).colors.client.principal.return_value.calendar_home_set
    home.get_properties.return_value = propfind_answer(
        {PERSONAL_HREF: _props("#123456")}
    )

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=16))
    # The scheduled poll is a background task.
    await hass.async_block_till_done(wait_background_tasks=True)

    assert _options(hass)["calendar"]["color"] == "#123456"


async def test_two_server_side_changes_in_a_row_both_land(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, {PERSONAL_HREF: "#00679e"})

    await _recolor(hass, "#123456")
    await _recolor(hass, "#abcdef")

    assert _options(hass)["calendar"]["color"] == "#abcdef"


async def _reload(hass: HomeAssistant, entry: MockConfigEntry, color: str) -> None:
    await hass.config_entries.async_unload(entry.entry_id)
    await _setup(hass, {PERSONAL_HREF: color}, entry)


async def test_hand_picked_color_survives_a_restart(hass: HomeAssistant) -> None:
    entry = await _setup(hass, {PERSONAL_HREF: "#00679e"})
    er.async_get(hass).async_update_entity_options(
        "calendar.personal", "calendar", {"color": "#abcdef"}
    )
    await hass.async_block_till_done()

    await _reload(hass, entry, "#00679e")
    await _recolor(hass, "#123456")

    assert _options(hass)["calendar"]["color"] == "#abcdef"


async def test_color_picked_while_unloaded_survives(hass: HomeAssistant) -> None:
    entry = await _setup(hass, {PERSONAL_HREF: "#00679e"})
    await hass.config_entries.async_unload(entry.entry_id)
    er.async_get(hass).async_update_entity_options(
        "calendar.personal", "calendar", {"color": "#abcdef"}
    )

    await _setup(hass, {PERSONAL_HREF: "#00679e"}, entry)
    await _recolor(hass, "#123456")

    assert _options(hass)["calendar"]["color"] == "#abcdef"


async def test_pick_matching_the_server_survives_a_restart(hass: HomeAssistant) -> None:
    entry = await _setup(hass, {PERSONAL_HREF: "#00679e"})
    registry = er.async_get(hass)
    for color in ("#abcdef", "#00679e"):
        registry.async_update_entity_options(
            "calendar.personal", "calendar", {"color": color}
        )
        await hass.async_block_till_done()

    await _reload(hass, entry, "#00679e")
    await _recolor(hass, "#123456")

    assert _options(hass)["calendar"]["color"] == "#00679e"


async def test_unrelated_registry_edit_is_not_a_pick(hass: HomeAssistant) -> None:
    await _setup(hass, {PERSONAL_HREF: "#00679e"})
    er.async_get(hass).async_update_entity("calendar.personal", icon="mdi:calendar")
    await hass.async_block_till_done()

    await _recolor(hass, "#123456")

    assert _options(hass)["calendar"]["color"] == "#123456"


async def test_clearing_without_a_record_survives(hass: HomeAssistant) -> None:
    entry = _register_existing(hass, {"calendar": {"color": "#abcdef"}})
    calendars = [_calendar("Personal", PERSONAL_URL)]
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value = _client({}, calendars)
        home = client.return_value.principal.return_value.calendar_home_set
        home.get_properties.side_effect = OSError("boom")
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        er.async_get(hass).async_update_entity_options(
            "calendar.personal", "calendar", None
        )
        await hass.async_block_till_done()
        home.get_properties.side_effect = None

    await _recolor(hass, "#00679e")

    assert "calendar" not in _options(hass)


async def test_calendar_the_server_did_not_report_keeps_its_color(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, {PERSONAL_HREF: "#00679e"})
    entity = _entity(hass)
    home = entity.colors.client.principal.return_value.calendar_home_set
    home.get_properties.return_value = propfind_answer(
        {"/somewhere/else/": _props("#123456")}
    )

    await entity.colors.async_refresh()
    await hass.async_block_till_done()

    assert _options(hass)["calendar"]["color"] == "#00679e"


async def test_overridden_calendar_stops_writing(hass: HomeAssistant) -> None:
    await _setup(hass, {PERSONAL_HREF: "#00679e"})
    registry = er.async_get(hass)
    registry.async_update_entity_options(
        "calendar.personal", "calendar", {"color": "#abcdef"}
    )
    await hass.async_block_till_done()
    writes = []
    hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, writes.append)

    await _recolor(hass, "#123456")

    assert writes == []


async def test_a_record_missing_a_key_is_tolerated(hass: HomeAssistant) -> None:
    entry = _register_existing(
        hass,
        {"calendar": {"color": "#00679e"}, COLOR_STATE: {"color": "#00679e"}},
    )

    await _setup(hass, {PERSONAL_HREF: "#123456"}, entry)

    assert _options(hass)["calendar"]["color"] == "#123456"


async def test_unchanged_color_touches_nothing(hass: HomeAssistant) -> None:
    await _setup(hass, {PERSONAL_HREF: "#00679e"})
    writes = []
    hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, writes.append)

    await _recolor(hass, "#00679e")

    assert writes == []


async def test_color_cleared_while_unloaded_survives(hass: HomeAssistant) -> None:
    entry = await _setup(hass, {PERSONAL_HREF: "#00679e"})
    await hass.config_entries.async_unload(entry.entry_id)
    er.async_get(hass).async_update_entity_options(
        "calendar.personal", "calendar", None
    )

    await _setup(hass, {PERSONAL_HREF: "#00679e"}, entry)
    await _recolor(hass, "#123456")

    assert "calendar" not in _options(hass)


async def test_cleared_color_survives_a_restart(hass: HomeAssistant) -> None:
    entry = await _setup(hass, {PERSONAL_HREF: "#00679e"})
    er.async_get(hass).async_update_entity_options(
        "calendar.personal", "calendar", None
    )
    await hass.async_block_till_done()

    await _reload(hass, entry, "#00679e")
    await _recolor(hass, "#123456")

    assert "calendar" not in _options(hass)


async def test_startup_failure_keeps_the_visible_color(hass: HomeAssistant) -> None:
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

    er.async_get(hass).async_update_entity("calendar.personal", disabled_by=None)
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

    assert hass.states.get("calendar.personal") is not None
    assert "color" not in _options(hass).get("calendar", {})


async def test_a_failed_color_read_names_no_url_and_no_body(
    hass: HomeAssistant,
) -> None:
    """UpdateFailed is logged at error level, and caldav's message carries the url
    it was reading and the whole response body."""
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
    await _setup(hass, {PERSONAL_HREF: "#00679e"})
    er.async_get(hass).async_update_entity_options(
        "calendar.personal", "calendar", {"color": "#abcdef"}
    )
    await hass.async_block_till_done()
    assert _options(hass)[COLOR_STATE]["override"] is True

    _entity(hass).async_follow_server_color()
    await hass.async_block_till_done()

    assert _options(hass)[COLOR_STATE]["override"] is False

    await _recolor(hass, "#cccccc")

    assert _options(hass)["calendar"]["color"] == "#cccccc"
    assert _options(hass)[COLOR_STATE] == {"color": "#cccccc", "override": False}


def test_fetch_collections_keys_an_absolute_href_by_its_path() -> None:
    client = _client({"https://cloud.example.com/dav/a%2520b/": "#00679e"})

    assert fetch_collections(client) == {"/dav/a%20b": Collection("#00679e", None)}


def test_a_color_refused_on_one_calendar_leaves_the_others_theirs() -> None:
    client = Mock()
    home = client.principal.return_value.calendar_home_set
    home.url = "https://cloud.example.com/dav/"
    home.get_properties.return_value = Mock(
        tree=ET.fromstring(
            '<d:multistatus xmlns:d="DAV:" xmlns:ic="http://apple.com/ns/ical/">'
            "<d:response><d:href>/dav/personal/</d:href><d:propstat><d:prop>"
            "<ic:calendar-color>#00679e</ic:calendar-color></d:prop>"
            "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
            "<d:response><d:href>/dav/shared/</d:href><d:propstat><d:prop>"
            "<ic:calendar-color/></d:prop>"
            "<d:status>HTTP/1.1 403 Forbidden</d:status></d:propstat></d:response>"
            "</d:multistatus>"
        )
    )

    assert fetch_collections(client) == {
        "/dav/personal": Collection("#00679e", None),
        "/dav/shared": Collection(None, None),
    }
