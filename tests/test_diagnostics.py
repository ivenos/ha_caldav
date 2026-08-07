"""Tests for the CalDAV diagnostics."""

import json
from unittest.mock import Mock

from caldav.elements import ical
from caldav.lib.error import DAVError
from homeassistant.components.diagnostics import REDACTED
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_caldav.const import (
    CONF_CALENDAR_OPTIONS,
    CONF_CALENDARS,
    DOMAIN,
)
from custom_components.ha_caldav.coordinator import HaCaldavRuntimeData, ManagedCalendar
from custom_components.ha_caldav.diagnostics import async_get_config_entry_diagnostics

ENTRY_DATA = {
    CONF_URL: "https://cloud.example.com/remote.php/dav",
    CONF_USERNAME: "iven",
    CONF_PASSWORD: "secret",
    CONF_VERIFY_SSL: True,
}


CALENDARS = "https://cloud.example.com/remote.php/dav/calendars/iven"


def _calendar(name: str, components: list[str]) -> Mock:
    calendar = Mock()
    calendar.name = name
    calendar.url = f"{CALENDARS}/{name.lower()}/"
    calendar.get_supported_components.return_value = components
    return calendar


def _runtime(
    client: Mock, calendars: list[ManagedCalendar] | None = None
) -> HaCaldavRuntimeData:
    """Wrap a client the way a loaded entry holds it."""
    return HaCaldavRuntimeData(
        client=client, colors=Mock(), calendars=calendars or [], address_set=[]
    )


def _client(calendars: list[Mock], colors: dict[str, str] | None = None) -> Mock:
    client = Mock()
    client.principal.return_value.calendars.return_value = calendars
    home = client.principal.return_value.calendar_home_set
    home.get_properties.return_value.find_objects_and_props.return_value = {
        href: {ical.CalendarColor.tag: Mock(text=value)}
        for href, value in (colors or {}).items()
    }
    return client


async def test_diagnostics_redacts_secrets_and_lists_calendars(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, options={"days": 7}, unique_id="x"
    )
    entry.add_to_hass(hass)
    entry.runtime_data = _runtime(
        _client(
            [_calendar("Personal", ["VEVENT", "VTODO"]), _calendar("Work", ["VEVENT"])],
            {"/remote.php/dav/calendars/iven/personal/": "#00679E"},
        )
    )

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["data"][CONF_PASSWORD] == REDACTED
    assert diag["data"][CONF_URL] == REDACTED
    assert diag["data"][CONF_USERNAME] == REDACTED
    assert diag["data"][CONF_VERIFY_SSL] is True
    assert diag["options"] == {"days": 7}
    assert diag["calendars"] == [
        {
            "name": "Personal",
            "color": "#00679e",
            "components": ["VEVENT", "VTODO"],
        },
        {"name": "Work", "color": None, "components": ["VEVENT"]},
    ]
    assert "cloud.example.com" not in json.dumps(diag)


async def test_diagnostics_error_reports_type_not_url(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="y")
    entry.add_to_hass(hass)
    client = Mock()
    client.principal.side_effect = DAVError(
        "500 Server Error for https://cloud.example.com/remote.php/dav/calendars/iven/"
    )
    entry.runtime_data = _runtime(client)

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["calendars"] == {"error": "DAVError"}
    # The server URL from the error text must not leak into the document.
    assert "cloud.example.com" not in json.dumps(diag)


async def test_diagnostics_handles_unloaded_entry(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="z")
    entry.add_to_hass(hass)

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["calendars"] == {"error": "entry not loaded"}
    assert diag["data"][CONF_PASSWORD] == REDACTED


async def test_diagnostics_reports_per_calendar_component_error(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="c")
    entry.add_to_hass(hass)
    bad = _calendar("Broken", [])
    bad.get_supported_components.side_effect = DAVError(
        "500 Server Error for https://cloud.example.com/remote.php/dav/broken/"
    )
    entry.runtime_data = _runtime(_client([_calendar("Personal", ["VEVENT"]), bad]))

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["calendars"][0] == {
        "name": "Personal",
        "color": None,
        "components": ["VEVENT"],
    }
    assert diag["calendars"][1] == {
        "name": "Broken",
        "color": None,
        "components_error": "DAVError",
    }
    assert "cloud.example.com" not in json.dumps(diag)


async def test_diagnostics_survives_a_failed_color_lookup(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="d")
    entry.add_to_hass(hass)
    client = _client([_calendar("Personal", ["VEVENT"])])
    # caldav asserts its way out of a multistatus it does not expect.
    client.principal.return_value.calendar_home_set.get_properties.side_effect = (
        AssertionError("weird xml")
    )
    entry.runtime_data = _runtime(client)

    diag = await async_get_config_entry_diagnostics(hass, entry)

    # A lookup that failed must not read as a calendar without a color.
    assert diag["calendars"] == [
        {"name": "Personal", "color_error": "AssertionError", "components": ["VEVENT"]}
    ]
    assert "cloud.example.com" not in json.dumps(diag)


async def test_diagnostics_keeps_the_account_out_of_the_calendar_options(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=ENTRY_DATA,
        options={
            "calendar_options": {
                "/remote.php/dav/calendars/iven/therapie": {"read_only": True}
            }
        },
        unique_id="z",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = _runtime(_client([]))

    diag = await async_get_config_entry_diagnostics(hass, entry)

    # The key is the collection path, which on every mainstream server spells
    # out the account the redaction above strips from entry.data.
    assert diag["options"]["calendar_options"] == {"therapie": {"read_only": True}}
    assert "iven" not in json.dumps(diag)


async def test_diagnostics_survives_a_server_answering_with_nonsense(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="w")
    entry.add_to_hass(hass)
    client = Mock()
    # caldav comes out of a 207 whose body is not xml with a bare TypeError,
    # which is not a network error; letting it escape 500s the download.
    client.principal.side_effect = TypeError("argument of type 'NoneType'")
    entry.runtime_data = _runtime(client)

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["calendars"] == {"error": "TypeError"}


async def test_diagnostics_reports_an_unmanaged_calendar_that_answers_oddly(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="v")
    entry.add_to_hass(hass)
    deselected = _calendar("Shared", [])
    deselected.get_supported_components.side_effect = AssertionError("bad multistatus")
    entry.runtime_data = _runtime(_client([deselected]))

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["calendars"][0]["components_error"] == "AssertionError"


async def test_the_selected_calendars_carry_no_account_name(
    hass: HomeAssistant,
) -> None:
    """The selection is stored as full collection paths.

    On most servers those spell out the account name, which is the one thing
    the redaction above exists to hide, in a file the issue template asks
    people to attach to a public report.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=ENTRY_DATA,
        options={
            CONF_CALENDARS: [
                "/remote.php/dav/calendars/iven/personal",
                "/remote.php/dav/calendars/iven/therapy",
            ],
            CONF_CALENDAR_OPTIONS: {
                "/remote.php/dav/calendars/iven/personal": {"days": 30}
            },
        },
        unique_id="x",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = _runtime(_client([], {}))

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["options"][CONF_CALENDARS] == ["personal", "therapy"]
    assert diag["options"][CONF_CALENDAR_OPTIONS] == {"personal": {"days": 30}}
    assert "iven" not in json.dumps(diag)
