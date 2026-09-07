"""Tests for the CalDAV config, reauth and options flow."""

from unittest.mock import Mock, patch

from caldav.davclient import requests
from caldav.lib.error import AuthorizationError
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_TIMEOUT,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous_serialize

from custom_components.ha_caldav.config_flow import _labelled
from custom_components.ha_caldav.const import (
    CONF_CA_BUNDLE,
    CONF_CALENDAR_OPTIONS,
    CONF_CALENDARS,
    CONF_CLIENT_CERT,
    CONF_CLIENT_KEY,
    CONF_DAYS,
    CONF_INCLUDE_ALL_DAY,
    CONF_READ_ONLY,
    DEFAULT_TIMEOUT,
    DOMAIN,
)

USER_INPUT = {
    CONF_URL: "https://cloud.example.com/remote.php/dav",
    CONF_USERNAME: "iven",
    CONF_PASSWORD: "secret",
    CONF_VERIFY_SSL: True,
}


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data=USER_INPUT,
        unique_id=f"{USER_INPUT[CONF_URL]}#{USER_INPUT[CONF_USERNAME]}",
    )


async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    with (
        patch("custom_components.ha_caldav.config_flow.caldav.DAVClient") as client,
        patch("custom_components.ha_caldav.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "iven"
    assert result["data"] == USER_INPUT
    assert client.call_args.kwargs["timeout"] == DEFAULT_TIMEOUT


@pytest.mark.parametrize(
    ("side_effect", "expected"),
    [
        (AuthorizationError(reason="Unauthorized"), "invalid_auth"),
        (AuthorizationError(reason="Forbidden"), "cannot_connect"),
        (requests.ConnectionError(), "cannot_connect"),
        (Exception(), "unknown"),
    ],
)
async def test_user_flow_errors(
    hass: HomeAssistant, side_effect: Exception, expected: str
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch("custom_components.ha_caldav.config_flow.caldav.DAVClient") as client:
        client.return_value.principal.side_effect = side_effect
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}


async def test_user_flow_aborts_on_duplicate(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch("custom_components.ha_caldav.config_flow.caldav.DAVClient"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_password(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with (
        patch("custom_components.ha_caldav.config_flow.caldav.DAVClient"),
        patch("custom_components.ha_caldav.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new-secret"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-secret"
    assert entry.data[CONF_USERNAME] == "iven"


def _managed(entry, name: str):
    return next(item for item in entry.runtime_data.calendars if item.name == name)


def _dav_calendar(name: str) -> Mock:
    calendar = Mock()
    calendar.name = name
    calendar.url = f"https://cloud.example.com/remote.php/dav/{name}"
    calendar.search.return_value = []
    return calendar


async def _setup_entry(hass: HomeAssistant, options: dict | None = None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="iven",
        data=USER_INPUT,
        options=options or {},
        unique_id="x",
    )
    entry.add_to_hass(hass)
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = [
            _dav_calendar("Personal"),
            _dav_calendar("Work"),
        ]
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_options_flow_saves_the_selection(hass: HomeAssistant) -> None:
    entry = await _setup_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )
    assert result["type"] is FlowResultType.FORM

    # Saving triggers a reload, which builds a fresh client.
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = []
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_CALENDARS: ["/remote.php/dav/Work"],
                CONF_SCAN_INTERVAL: 30,
                CONF_DAYS: 14,
                CONF_INCLUDE_ALL_DAY: False,
                CONF_READ_ONLY: True,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_CALENDARS: ["/remote.php/dav/Work"],
        CONF_SCAN_INTERVAL: 30,
        CONF_DAYS: 14,
        CONF_INCLUDE_ALL_DAY: False,
        CONF_READ_ONLY: True,
        CONF_TIMEOUT: DEFAULT_TIMEOUT,
    }


async def test_options_flow_keeps_selection_while_unreachable(
    hass: HomeAssistant,
) -> None:
    # With the server down the calendars field is left out of the form; saving
    # the other options must not wipe the stored selection.
    entry = await _setup_entry(hass, options={CONF_CALENDARS: ["Personal"]})
    entry.runtime_data.client.principal.side_effect = requests.ConnectionError()

    # Without calendar names there is nothing to override per calendar, so the
    # flow skips the menu and shows the account form straight away.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = []
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_SCAN_INTERVAL: 5,
                CONF_DAYS: 3,
                CONF_INCLUDE_ALL_DAY: True,
                CONF_READ_ONLY: False,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_CALENDARS] == ["Personal"]
    assert entry.options[CONF_SCAN_INTERVAL] == 5


async def test_reauth_rejects_wrong_password(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    with patch("custom_components.ha_caldav.config_flow.caldav.DAVClient") as client:
        client.return_value.principal.side_effect = AuthorizationError(
            reason="Unauthorized"
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "still-wrong"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data[CONF_PASSWORD] == "secret"


async def test_bare_host_is_resolved_through_the_bootstrap_url(
    hass: HomeAssistant,
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    with (
        patch("custom_components.ha_caldav.config_flow.caldav.DAVClient") as client,
        patch("custom_components.ha_caldav.async_setup_entry", return_value=True),
    ):
        # The entered url answers with nothing; the RFC 6764 fallback does.
        client.return_value.principal.side_effect = [
            requests.ConnectionError(),
            Mock(),
        ]
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {**USER_INPUT, CONF_URL: "https://cloud.example.com"},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_URL] == "https://cloud.example.com/.well-known/caldav"


async def test_blank_tls_fields_are_not_stored(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    with (
        patch("custom_components.ha_caldav.config_flow.caldav.DAVClient"),
        patch("custom_components.ha_caldav.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_CLIENT_CERT: "", CONF_CA_BUNDLE: ""}
        )

    assert CONF_CLIENT_CERT not in result["data"]
    assert CONF_CA_BUNDLE not in result["data"]


async def test_tls_paths_are_stored_when_given(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    with (
        patch("custom_components.ha_caldav.config_flow.caldav.DAVClient") as client,
        patch("custom_components.ha_caldav.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                **USER_INPUT,
                CONF_CLIENT_CERT: "/etc/client.pem",
                CONF_CLIENT_KEY: "/etc/client.key",
            },
        )

    assert result["data"][CONF_CLIENT_CERT] == "/etc/client.pem"
    assert client.call_args.kwargs["ssl_cert"] == ("/etc/client.pem", "/etc/client.key")


async def test_reconfigure_updates_the_connection(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    # The account is fixed and the password belongs to reauth, so neither is
    # offered here and neither is sent to the browser.
    assert CONF_USERNAME not in result["data_schema"].schema
    assert CONF_PASSWORD not in result["data_schema"].schema

    with (
        patch("custom_components.ha_caldav.config_flow.caldav.DAVClient"),
        patch("custom_components.ha_caldav.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_URL: USER_INPUT[CONF_URL], CONF_VERIFY_SSL: False},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_VERIFY_SSL] is False
    assert entry.data[CONF_PASSWORD] == "secret"


async def test_reconfigure_moves_the_account_to_a_new_url(
    hass: HomeAssistant,
) -> None:
    """The url is what this step exists to change, and the entry is keyed on
    it, so the key moves with it rather than refusing the change."""
    entry = _entry()
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    with (
        patch("custom_components.ha_caldav.config_flow.caldav.DAVClient"),
        patch("custom_components.ha_caldav.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: "https://other.example.com/dav"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_URL] == "https://other.example.com/dav"
    assert (
        entry.unique_id == f"https://other.example.com/dav#{USER_INPUT[CONF_USERNAME]}"
    )


async def test_reconfigure_refuses_a_url_another_entry_already_holds(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    other = MockConfigEntry(
        domain=DOMAIN,
        data={**USER_INPUT, CONF_URL: "https://other.example.com/dav"},
        unique_id=f"https://other.example.com/dav#{USER_INPUT[CONF_USERNAME]}",
    )
    other.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    with patch("custom_components.ha_caldav.config_flow.caldav.DAVClient"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: "https://other.example.com/dav"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_one_account_spelled_two_ways_is_not_set_up_twice(
    hass: HomeAssistant,
) -> None:
    """A trailing slash or another case in the host is the same account, and
    setting it up again would double every calendar and to-do list."""
    entry = _entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    with patch("custom_components.ha_caldav.config_flow.caldav.DAVClient"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            # Scheme and host are case-insensitive and the trailing slash means
            # nothing; the path is left alone, because RFC 3986 keeps that one
            # case-sensitive.
            {**USER_INPUT, CONF_URL: "HTTPS://Cloud.Example.COM/remote.php/dav/"},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reconfigure_can_clear_a_client_certificate(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**USER_INPUT, CONF_CLIENT_CERT: "/etc/client.pem"},
        unique_id=f"{USER_INPUT[CONF_URL]}#{USER_INPUT[CONF_USERNAME]}",
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    with (
        patch("custom_components.ha_caldav.config_flow.caldav.DAVClient"),
        patch("custom_components.ha_caldav.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: USER_INPUT[CONF_URL], CONF_CLIENT_CERT: ""}
        )

    assert result["type"] is FlowResultType.ABORT
    # An expired certificate has to be removable, not merged back in.
    assert CONF_CLIENT_CERT not in entry.data


async def test_a_bootstrapped_account_can_be_reconfigured(
    hass: HomeAssistant,
) -> None:
    # Set up through the bare host name, so the stored url is the resolved one
    # while the entered one was shorter.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with (
        patch("custom_components.ha_caldav.config_flow.caldav.DAVClient") as client,
        patch("custom_components.ha_caldav.async_setup_entry", return_value=True),
    ):
        client.return_value.principal.side_effect = [requests.ConnectionError(), Mock()]
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_URL: "https://cloud.example.com"}
        )
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.data[CONF_URL].endswith("/.well-known/caldav")

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    with (
        patch("custom_components.ha_caldav.config_flow.caldav.DAVClient"),
        patch("custom_components.ha_caldav.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: entry.data[CONF_URL], CONF_VERIFY_SSL: False}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"


async def test_reconfigure_keeps_the_form_on_a_bad_connection(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    with patch("custom_components.ha_caldav.config_flow.caldav.DAVClient") as client:
        client.return_value.principal.side_effect = requests.ConnectionError()
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: USER_INPUT[CONF_URL]}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_per_calendar_options_only_store_the_override(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_entry(hass, options={CONF_DAYS: 7})

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "pick"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"calendar": "/remote.php/dav/Work"}
    )
    assert result["description_placeholders"] == {"calendar": "Work"}

    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = []
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_DAYS: 30,
                CONF_INCLUDE_ALL_DAY: False,
                CONF_READ_ONLY: True,
                "reset": False,
            },
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_DAYS] == 7
    # Keyed on the url, and holding only what differs from the account: the
    # fields left at the account values must keep following it.
    assert entry.options[CONF_CALENDAR_OPTIONS] == {
        "/remote.php/dav/Work": {
            CONF_DAYS: 30,
            CONF_INCLUDE_ALL_DAY: False,
            CONF_READ_ONLY: True,
        }
    }


async def test_two_calendars_of_one_name_stay_apart_in_the_picker(
    hass: HomeAssistant,
) -> None:
    """A shared calendar keeps its owner's name, so a name can arrive twice."""
    entry = MockConfigEntry(
        domain=DOMAIN, title="iven", data=USER_INPUT, options={}, unique_id="x"
    )
    entry.add_to_hass(hass)
    mine = _dav_calendar("Personal")
    shared = _dav_calendar("Personal")
    shared.url = "https://cloud.example.com/remote.php/dav/shared/Personal"
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = [
            mine,
            shared,
        ]
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "pick"}
    )
    choices = result["data_schema"].schema["calendar"].container
    assert set(choices) == {
        "/remote.php/dav/Personal",
        "/remote.php/dav/shared/Personal",
    }

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"calendar": "/remote.php/dav/shared/Personal"}
    )
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = []
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_DAYS: 7,
                CONF_INCLUDE_ALL_DAY: True,
                CONF_READ_ONLY: True,
                "reset": False,
            },
        )
        await hass.async_block_till_done()

    # The one that was picked, not the one the server happened to list first.
    assert entry.options[CONF_CALENDAR_OPTIONS] == {
        "/remote.php/dav/shared/Personal": {CONF_READ_ONLY: True}
    }


async def test_an_override_only_holds_the_fields_that_differ(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_entry(hass, options={CONF_DAYS: 7})

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "pick"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"calendar": "/remote.php/dav/Work"}
    )
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = []
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_DAYS: 7,
                CONF_INCLUDE_ALL_DAY: True,
                CONF_READ_ONLY: True,
                "reset": False,
            },
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_CALENDAR_OPTIONS] == {
        "/remote.php/dav/Work": {CONF_READ_ONLY: True}
    }


async def test_an_account_change_still_reaches_an_overridden_calendar(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_entry(
        hass,
        options={
            CONF_DAYS: 7,
            CONF_CALENDAR_OPTIONS: {"/remote.php/dav/Work": {CONF_READ_ONLY: True}},
        },
    )

    work = _managed(entry, "Work")
    assert work.read_only is True
    assert work.coordinator.days == 7


async def test_resetting_a_calendar_drops_its_override(hass: HomeAssistant) -> None:
    entry = await _setup_entry(
        hass,
        options={CONF_CALENDAR_OPTIONS: {"/remote.php/dav/Work": {CONF_DAYS: 30}}},
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "pick"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"calendar": "/remote.php/dav/Work"}
    )

    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = []
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_DAYS: 30,
                CONF_INCLUDE_ALL_DAY: True,
                CONF_READ_ONLY: False,
                "reset": True,
            },
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_CALENDAR_OPTIONS] == {}


async def test_a_per_calendar_override_reaches_the_coordinator(
    hass: HomeAssistant,
) -> None:
    # An entry written by the previous version keyed its overrides on the
    # display name; those have to keep working.
    entry = await _setup_entry(
        hass,
        options={
            CONF_DAYS: 7,
            CONF_CALENDAR_OPTIONS: {"Work": {CONF_DAYS: 30, CONF_READ_ONLY: True}},
        },
    )

    personal = _managed(entry, "Personal")
    work = _managed(entry, "Work")

    assert personal.coordinator.days == 7
    assert work.coordinator.days == 30
    assert personal.read_only is False
    assert work.read_only is True


async def test_credentials_typed_into_the_url_are_not_stored(
    hass: HomeAssistant,
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with (
        patch("custom_components.ha_caldav.config_flow.caldav.DAVClient"),
        patch("custom_components.ha_caldav.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {**USER_INPUT, CONF_URL: "https://alice:hunter2@cloud.example.com/dav"},
        )
        await hass.async_block_till_done()

    # caldav logs the url it was handed before stripping those itself, and the
    # form has its own fields for both.
    assert result["data"][CONF_URL] == "https://cloud.example.com/dav"
    assert "hunter2" not in str(result["data"][CONF_URL])


async def test_an_empty_password_survives_into_the_entry(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with (
        patch("custom_components.ha_caldav.config_flow.caldav.DAVClient"),
        patch("custom_components.ha_caldav.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_PASSWORD: ""}
        )
        await hass.async_block_till_done()

    # Only the three optional TLS paths may be dropped when blank: some servers
    # take the token in the username, and losing the key would KeyError.
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PASSWORD] == ""


async def test_the_options_dialog_lists_the_calendars_once(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_entry(hass)
    principal = entry.runtime_data.client.principal
    principal.reset_mock()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )

    # The menu decision and the form both need the list; asking twice is two
    # round-trips to the server for opening one dialog.
    assert principal.call_count == 1


async def test_the_options_form_refuses_an_empty_calendar_selection(
    hass: HomeAssistant,
) -> None:
    # Every box unticked leaves the account with no entities at all, which
    # reads as a broken integration rather than as a choice.
    entry = await _setup_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CALENDARS: []}
    )

    # Reported as a translated form error, not as the voluptuous message, which
    # Home Assistant hands to the frontend verbatim and in English however the
    # user has their language set.
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_CALENDARS: "no_calendars"}


async def test_the_options_form_keeps_a_selection_stored_by_name(
    hass: HomeAssistant,
) -> None:
    # A selection written before the switch to url keys named the calendar.
    # Dropping it here would silently untick every box on opening the dialog.
    entry = await _setup_entry(hass, options={CONF_CALENDARS: ["Personal"]})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )

    default = next(
        key.default() for key in result["data_schema"].schema if key == CONF_CALENDARS
    )
    assert default == ["/remote.php/dav/Personal"]


async def test_a_401_on_the_entered_url_still_tries_the_bootstrap(
    hass: HomeAssistant,
) -> None:
    """A bare host whose root sits behind another auth realm answers 401 while
    the RFC 6764 candidate behind it works, and giving up on the first one
    reports bad credentials for credentials that are fine."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    refused, accepted = Mock(), Mock()
    refused.principal.side_effect = AuthorizationError(reason="Unauthorized")

    with (
        patch(
            "custom_components.ha_caldav.config_flow.caldav.DAVClient",
            side_effect=[refused, accepted],
        ),
        patch("custom_components.ha_caldav.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_URL: "cloud.example.com"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_a_401_everywhere_is_still_reported_as_bad_credentials(
    hass: HomeAssistant,
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch("custom_components.ha_caldav.config_flow.caldav.DAVClient") as client:
        client.return_value.principal.side_effect = AuthorizationError(
            reason="Unauthorized"
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_URL: "cloud.example.com"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_a_calendar_one_listing_left_out_stays_selected(
    hass: HomeAssistant,
) -> None:
    """The form can only offer what one listing turned up, and what it does not
    offer cannot be ticked. Stored as submitted, a calendar the server left out
    of that listing reads as deselected on the next setup, and its entity goes
    from the registry with its history and everything pointing at it."""
    entry = await _setup_entry(
        hass,
        options={CONF_CALENDARS: ["/remote.php/dav/Personal", "/remote.php/dav/Work"]},
    )
    # The one bad minute: Work is missing from this listing only.
    entry.runtime_data.client.principal.return_value.calendars.return_value = [
        _dav_calendar("Personal")
    ]

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = []
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_CALENDARS: ["/remote.php/dav/Personal"],
                CONF_SCAN_INTERVAL: 30,
                CONF_DAYS: 14,
                CONF_INCLUDE_ALL_DAY: False,
                CONF_READ_ONLY: False,
            },
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_CALENDARS] == [
        "/remote.php/dav/Personal",
        "/remote.php/dav/Work",
    ]


async def test_an_account_tracking_everything_is_not_frozen_by_a_visit(
    hass: HomeAssistant,
) -> None:
    """An entry that never had a selection follows the server, and every box
    being ticked is what that looks like in the form. Written down it freezes,
    and a calendar made later is silently never loaded."""
    entry = await _setup_entry(hass)
    assert CONF_CALENDARS not in entry.options

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = []
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_CALENDARS: [
                    "/remote.php/dav/Personal",
                    "/remote.php/dav/Work",
                ],
                CONF_SCAN_INTERVAL: 30,
                CONF_DAYS: 14,
                CONF_INCLUDE_ALL_DAY: False,
                CONF_READ_ONLY: False,
            },
        )
        await hass.async_block_till_done()

    assert CONF_CALENDARS not in entry.options
    assert entry.options[CONF_SCAN_INTERVAL] == 30


async def test_every_probed_candidate_hands_its_connection_back(
    hass: HomeAssistant,
) -> None:
    """One flow walks several bootstrap candidates, and each keeps a pooled
    connection open until it is closed. Left to the garbage collector they pile
    up for as long as the user keeps retrying the form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    with patch(
        "custom_components.ha_caldav.config_flow.caldav.DAVClient"
    ) as client_class:
        client_class.return_value.principal.side_effect = requests.ConnectionError(
            "refused"
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_URL: "https://cloud.example.com"}
        )

    assert result["errors"] == {"base": "cannot_connect"}
    assert client_class.call_count > 1
    assert client_class.return_value.close.call_count == client_class.call_count


async def test_the_options_of_an_entry_that_never_loaded_still_open(
    hass: HomeAssistant,
) -> None:
    """An account whose server was down at startup has no runtime data, and the
    calendar list is read off it. Reaching for it there would leave the one
    dialog the user needs to fix the settings raising on open."""
    entry = MockConfigEntry(
        domain=DOMAIN, title="iven", data=USER_INPUT, options={}, unique_id="x"
    )
    entry.add_to_hass(hass)
    assert not hasattr(entry, "runtime_data")

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM


def test_two_calendars_whose_paths_nest_still_get_a_label_each() -> None:
    """A label is the shortest tail of the path no other calendar shares, and a
    path that is wholly the tail of another has none: every depth down to the
    whole key still matches the longer one. Without the fallback the loop runs
    out and the calendar comes back labelled with nothing to tell it apart."""
    nested = Mock(name="a")
    nested.name = "Personal"
    nested.calendar.url = "https://cloud.example.com/personal"
    outer = Mock(name="b")
    outer.name = "Personal"
    outer.calendar.url = "https://cloud.example.com/remote.php/dav/iven/personal"

    labels = _labelled([nested, outer])

    assert labels == {
        "/personal": "Personal (/personal)",
        "/remote.php/dav/iven/personal": "Personal (iven/personal)",
    }


async def test_reauth_asks_with_the_timeout_the_account_was_given(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=USER_INPUT,
        options={CONF_TIMEOUT: 90},
        unique_id=f"{USER_INPUT[CONF_URL]}#{USER_INPUT[CONF_USERNAME]}",
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    with (
        patch("custom_components.ha_caldav.config_flow.caldav.DAVClient") as client,
        # Or the reload that follows builds the client this assertion reads.
        patch("custom_components.ha_caldav.async_setup_entry", return_value=True),
    ):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new"}
        )

    assert client.call_args.kwargs["timeout"] == 90


async def test_reconfigure_asks_with_the_timeout_the_account_was_given(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=USER_INPUT,
        options={CONF_TIMEOUT: 90},
        unique_id=f"{USER_INPUT[CONF_URL]}#{USER_INPUT[CONF_USERNAME]}",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    with (
        patch("custom_components.ha_caldav.config_flow.caldav.DAVClient") as client,
        patch("custom_components.ha_caldav.async_setup_entry", return_value=True),
    ):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: USER_INPUT[CONF_URL], CONF_VERIFY_SSL: True}
        )

    assert client.call_args.kwargs["timeout"] == 90


async def test_a_timeout_set_in_the_options_reaches_the_client(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = []
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_SCAN_INTERVAL: 15,
                CONF_DAYS: 7,
                CONF_INCLUDE_ALL_DAY: True,
                CONF_READ_ONLY: False,
                CONF_TIMEOUT: 90,
            },
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_TIMEOUT] == 90
    assert client.call_args.kwargs["timeout"] == 90


async def test_the_account_form_opens_on_the_stored_timeout(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_entry(hass, options={CONF_TIMEOUT: 90})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )

    fields = voluptuous_serialize.convert(
        result["data_schema"], custom_serializer=cv.custom_serializer
    )
    timeout = next(item for item in fields if item["name"] == CONF_TIMEOUT)

    assert timeout["default"] == 90
    number = timeout["selector"]["number"]
    assert (number["min"], number["max"]) == (5, 120)
