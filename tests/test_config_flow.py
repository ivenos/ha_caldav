from contextlib import suppress
import json
from pathlib import Path
import ssl
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
from homeassistant.data_entry_flow import FlowResultType, section
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import urllib3

from custom_components.ha_caldav.config_flow import _labels
from custom_components.ha_caldav.connection import HostLockedSession, distinctive
from custom_components.ha_caldav.const import (
    CONF_BUSY_ONLY,
    CONF_CA_BUNDLE,
    CONF_CALENDAR_OPTIONS,
    CONF_CALENDARS,
    CONF_CLIENT_CERT,
    CONF_CLIENT_KEY,
    CONF_DAYS,
    CONF_INCLUDE_ALL_DAY,
    CONF_PRINCIPAL,
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
PRINCIPAL = "https://cloud.example.com/remote.php/dav/principals/users/iven/"


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
        client.return_value.principal.return_value.url = PRINCIPAL
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "iven@cloud.example.com"
    assert result["data"] == {**USER_INPUT, CONF_PRINCIPAL: PRINCIPAL}
    assert client.call_args.kwargs["timeout"] == DEFAULT_TIMEOUT


@pytest.mark.parametrize(
    ("side_effect", "expected"),
    [
        (AuthorizationError(reason="Unauthorized"), "invalid_auth"),
        (AuthorizationError(reason="Forbidden"), "cannot_connect"),
        (requests.ConnectionError(), "cannot_connect"),
        (requests.exceptions.SSLError(), "certificate_failed"),
        (requests.exceptions.InvalidURL(), "cannot_connect"),
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
                CONF_BUSY_ONLY: True,
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
        CONF_BUSY_ONLY: True,
        CONF_READ_ONLY: True,
        CONF_TIMEOUT: DEFAULT_TIMEOUT,
    }


async def test_options_flow_keeps_selection_while_unreachable(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_entry(
        hass, options={CONF_CALENDARS: ["/remote.php/dav/Personal"]}
    )
    principal = entry.runtime_data.client.principal
    principal.reset_mock()
    principal.side_effect = requests.ConnectionError()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )
    assert result["type"] is FlowResultType.FORM
    assert CONF_CALENDARS not in result["data_schema"].schema

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
    assert entry.options[CONF_CALENDARS] == ["/remote.php/dav/Personal"]
    assert principal.call_count == 1
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
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data[CONF_PASSWORD] == "secret"


async def test_reauth_tries_the_password_just_entered(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    with (
        patch("custom_components.ha_caldav.config_flow.caldav.DAVClient") as client,
        patch("custom_components.ha_caldav.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new-secret"}
        )
        await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    assert [call.kwargs["password"] for call in client.call_args_list] == ["new-secret"]


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
            result["flow_id"],
            {**USER_INPUT, "certificates": {CONF_CLIENT_CERT: "", CONF_CA_BUNDLE: ""}},
        )

    assert CONF_CLIENT_CERT not in result["data"]
    assert CONF_CA_BUNDLE not in result["data"]


async def test_tls_paths_are_stored_when_given(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    certificate, key = tmp_path / "client.pem", tmp_path / "client.key"
    certificate.write_text("cert")
    key.write_text("key")
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
                "certificates": {
                    CONF_CLIENT_CERT: str(certificate),
                    CONF_CLIENT_KEY: str(key),
                },
            },
        )

    assert result["data"][CONF_CLIENT_CERT] == str(certificate)
    assert client.call_args.kwargs["ssl_cert"] == (str(certificate), str(key))


async def test_reconfigure_updates_the_connection(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert CONF_USERNAME not in result["data_schema"].schema
    password = next(key for key in result["data_schema"].schema if key == CONF_PASSWORD)
    assert "suggested_value" not in (password.description or {})

    with (
        patch("custom_components.ha_caldav.config_flow.caldav.DAVClient"),
        patch("custom_components.ha_caldav.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_URL: USER_INPUT[CONF_URL], CONF_VERIFY_SSL: False},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_VERIFY_SSL] is False
    assert entry.data[CONF_PASSWORD] == "secret"


async def test_a_move_to_a_new_server_can_bring_a_new_password(
    hass: HomeAssistant,
) -> None:
    """Reauth would test the new password against the old url."""
    entry = _entry()
    entry.add_to_hass(hass)

    def server(url, username, password, **_kwargs):
        client = Mock()
        if password != "new":
            client.principal.side_effect = AuthorizationError(reason="Unauthorized")
        return client

    result = await entry.start_reconfigure_flow(hass)
    with (
        patch(
            "custom_components.ha_caldav.config_flow.caldav.DAVClient",
            side_effect=server,
        ),
        patch("custom_components.ha_caldav.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_URL: "https://new.example.com/dav", CONF_PASSWORD: "new"},
        )
        await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_URL] == "https://new.example.com/dav"
    assert entry.data[CONF_PASSWORD] == "new"


async def test_reconfigure_moves_the_account_to_a_new_url(
    hass: HomeAssistant,
) -> None:
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
        await hass.async_block_till_done()

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
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_one_account_spelled_two_ways_is_not_set_up_twice(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    with patch("custom_components.ha_caldav.config_flow.caldav.DAVClient"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            # RFC 3986 makes scheme and host case-insensitive and keeps the path
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
            result["flow_id"],
            {CONF_URL: USER_INPUT[CONF_URL], "certificates": {CONF_CLIENT_CERT: ""}},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert CONF_CLIENT_CERT not in entry.data


async def test_a_bootstrapped_account_can_be_reconfigured(
    hass: HomeAssistant,
) -> None:
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
        await hass.async_block_till_done()
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
        await hass.async_block_till_done()

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
                CONF_BUSY_ONLY: True,
                CONF_READ_ONLY: True,
                "reset": False,
            },
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_DAYS] == 7
    assert entry.options[CONF_CALENDAR_OPTIONS] == {
        "/remote.php/dav/Work": {
            CONF_DAYS: 30,
            CONF_INCLUDE_ALL_DAY: False,
            CONF_BUSY_ONLY: True,
            CONF_READ_ONLY: True,
        }
    }


async def test_two_calendars_of_one_name_stay_apart_in_the_picker(
    hass: HomeAssistant,
) -> None:
    """A shared calendar keeps its owner's name."""
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


async def test_a_calendar_set_back_to_the_account_values_drops_its_override(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_entry(
        hass,
        options={
            CONF_DAYS: 7,
            CONF_CALENDAR_OPTIONS: {"/remote.php/dav/Work": {CONF_DAYS: 30}},
        },
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
                CONF_DAYS: 7,
                CONF_INCLUDE_ALL_DAY: True,
                CONF_READ_ONLY: False,
                "reset": False,
            },
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_CALENDAR_OPTIONS] == {}


async def test_a_per_calendar_override_reaches_the_coordinator(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_entry(
        hass,
        options={
            CONF_DAYS: 7,
            CONF_CALENDAR_OPTIONS: {
                "/remote.php/dav/Work": {CONF_DAYS: 30, CONF_READ_ONLY: True}
            },
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

    # caldav logs the url it was handed before stripping those itself.
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

    # Some servers take the token in the username and want no password.
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PASSWORD] == ""


STRINGS = json.loads(
    (Path(__file__).parents[1] / "custom_components/ha_caldav/strings.json").read_text(
        encoding="utf-8"
    )
)


def _fields(form) -> set[str]:
    return {str(marker) for marker in form["data_schema"].schema}


async def test_every_option_has_a_label_and_a_description(hass: HomeAssistant) -> None:
    entry = await _setup_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    account = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "pick"}
    )
    calendar = await hass.config_entries.options.async_configure(
        result["flow_id"], {"calendar": "/remote.php/dav/Work"}
    )

    for form in (account, calendar):
        step = STRINGS["options"]["step"][form["step_id"]]
        assert _fields(form) <= step["data"].keys(), form["step_id"]
        assert _fields(form) <= step["data_description"].keys(), form["step_id"]


def test_every_account_option_has_a_row_in_the_readme() -> None:
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    labels = STRINGS["options"]["step"]["account"]["data"].values()

    assert [label for label in labels if f"\n| {label} |" not in readme] == []


async def test_the_options_dialog_lists_the_calendars_once(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_entry(hass)
    principal = entry.runtime_data.client.principal
    principal.reset_mock()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CALENDARS: []}
    )

    assert principal.call_count == 1


async def test_the_options_form_refuses_an_empty_calendar_selection(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CALENDARS: []}
    )

    # Home Assistant hands a voluptuous message to the frontend verbatim and in English.
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_CALENDARS: "no_calendars"}


async def test_the_options_form_keeps_a_selection_stored_by_name(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_entry(hass, options={CONF_CALENDARS: ["Personal"]})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )

    default = next(
        key.default() for key in result["data_schema"].schema if key == CONF_CALENDARS
    )
    assert default == ["/remote.php/dav/Personal"]


def test_a_path_that_merely_ends_like_another_is_told_apart_by_its_last_part() -> None:
    assert distinctive("/dav/Personal", ["/dav/MyPersonal"]) == "Personal"


async def test_the_account_form_tells_two_calendars_of_one_name_apart(
    hass: HomeAssistant,
) -> None:
    """A calendar shared with the account keeps its owner's name."""
    entry = await _setup_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with patch(
        "custom_components.ha_caldav.config_flow._calendar_choices",
        return_value={
            "/remote.php/dav/Personal": "Personal",
            "/remote.php/dav/shared/Personal": "Personal",
        },
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "account"}
        )

    field = next(
        value
        for key, value in result["data_schema"].schema.items()
        if key == CONF_CALENDARS
    )
    assert sorted(field.options.values()) == [
        "Personal (dav/Personal)",
        "Personal (shared/Personal)",
    ]


async def test_an_empty_selection_from_an_older_version_opens_as_every_calendar(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_entry(hass, options={CONF_CALENDARS: []})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )

    default = next(
        key.default() for key in result["data_schema"].schema if key == CONF_CALENDARS
    )
    assert default == ["/remote.php/dav/Personal", "/remote.php/dav/Work"]


async def test_a_401_on_the_entered_url_still_tries_the_bootstrap(
    hass: HomeAssistant,
) -> None:
    """A bare host whose root sits behind another auth realm answers 401 while the
    RFC 6764 candidate behind it works."""
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


async def test_a_401_outranks_a_bootstrap_that_does_not_answer(
    hass: HomeAssistant,
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    refused, silent = Mock(), Mock()
    refused.principal.side_effect = AuthorizationError(reason="Unauthorized")
    silent.principal.side_effect = requests.ConnectTimeout()

    with patch(
        "custom_components.ha_caldav.config_flow.caldav.DAVClient",
        side_effect=[refused, silent],
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_URL: "cloud.example.com"}
        )

    assert result["errors"] == {"base": "invalid_auth"}


async def test_a_calendar_one_listing_left_out_stays_selected(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_entry(
        hass,
        options={CONF_CALENDARS: ["/remote.php/dav/Personal", "/remote.php/dav/Work"]},
    )
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
    """An entry that never loaded has no runtime_data."""
    entry = MockConfigEntry(
        domain=DOMAIN, title="iven", data=USER_INPUT, options={}, unique_id="x"
    )
    entry.add_to_hass(hass)
    assert not hasattr(entry, "runtime_data")

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM


def test_two_calendars_whose_paths_nest_still_get_a_label_each() -> None:
    labels = _labels(
        {"/personal": "Personal", "/remote.php/dav/iven/personal": "Personal"}
    )

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
        # Reauth reloads the entry, which builds another client.
        patch("custom_components.ha_caldav.async_setup_entry", return_value=True),
    ):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new"}
        )
        await hass.async_block_till_done()

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
        await hass.async_block_till_done()

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

    schema = result["data_schema"].schema
    timeout = next(key for key in schema if key == CONF_TIMEOUT)

    assert timeout.default() == 90
    config = schema[timeout].config
    assert (config["min"], config["max"]) == (5, 120)


async def test_inputs_are_trimmed(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with (
        patch("custom_components.ha_caldav.config_flow.caldav.DAVClient"),
        patch("custom_components.ha_caldav.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                **USER_INPUT,
                CONF_URL: f"{USER_INPUT[CONF_URL]} ",
                CONF_USERNAME: " iven",
            },
        )

    assert result["data"][CONF_URL] == USER_INPUT[CONF_URL]
    assert result["data"][CONF_USERNAME] == "iven"


async def test_a_password_keeps_its_spaces(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with (
        patch("custom_components.ha_caldav.config_flow.caldav.DAVClient"),
        patch("custom_components.ha_caldav.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_PASSWORD: " secret "}
        )

    assert result["data"][CONF_PASSWORD] == " secret "


async def test_the_certificate_paths_sit_in_a_collapsed_section(
    hass: HomeAssistant,
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    schema = result["data_schema"].schema

    assert CONF_CLIENT_CERT not in schema
    certificates = schema["certificates"]
    assert isinstance(certificates, section)
    assert certificates.options["collapsed"] is True
    assert CONF_CLIENT_CERT in certificates.schema.schema


async def test_reconfigure_follows_the_host_in_an_untouched_title(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="iven@cloud.example.com",
        data=USER_INPUT,
        unique_id=f"{USER_INPUT[CONF_URL]}#{USER_INPUT[CONF_USERNAME]}",
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    with (
        patch("custom_components.ha_caldav.config_flow.caldav.DAVClient"),
        patch("custom_components.ha_caldav.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: "https://new.example.com/dav"}
        )
        await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    assert entry.title == "iven@new.example.com"


async def test_reconfigure_keeps_a_title_the_user_chose(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Work",
        data=USER_INPUT,
        unique_id=f"{USER_INPUT[CONF_URL]}#{USER_INPUT[CONF_USERNAME]}",
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    with (
        patch("custom_components.ha_caldav.config_flow.caldav.DAVClient"),
        patch("custom_components.ha_caldav.async_setup_entry", return_value=True),
    ):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: "https://new.example.com/dav"}
        )
        await hass.async_block_till_done()

    assert entry.title == "Work"


@pytest.mark.parametrize(
    "url",
    [
        "https://[fd00::1/dav",
        "webcal://cloud.example.com/",
        "https://host:99999/",
        "https://host:0/",
    ],
)
async def test_a_url_nothing_can_connect_to_is_named_as_such(
    hass: HomeAssistant, url: str
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    with patch("custom_components.ha_caldav.config_flow.caldav.DAVClient") as client:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_URL: url}
        )

    assert result["errors"] == {"base": "invalid_url"}
    client.assert_not_called()


async def test_a_certificate_path_that_leads_nowhere_is_named_as_such(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    with patch("custom_components.ha_caldav.config_flow.caldav.DAVClient") as client:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {**USER_INPUT, "certificates": {CONF_CA_BUNDLE: str(tmp_path / "ca.pem")}},
        )

    assert result["errors"] == {"base": "certificate_not_found"}
    client.assert_not_called()


async def test_the_url_a_server_moved_the_account_to_is_the_one_stored(
    hass: HomeAssistant,
) -> None:
    moved = "https://cloud.example.com/remote.php/dav/"
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    with (
        patch("custom_components.ha_caldav.config_flow.caldav.DAVClient"),
        patch.object(HostLockedSession, "moved_to", moved),
        patch("custom_components.ha_caldav.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["data"][CONF_URL] == moved


async def test_ticking_every_calendar_again_follows_the_server_again(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_entry(
        hass, options={CONF_CALENDARS: ["/remote.php/dav/Personal"]}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )

    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = []
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_CALENDARS: ["/remote.php/dav/Personal", "/remote.php/dav/Work"],
                CONF_SCAN_INTERVAL: 15,
                CONF_DAYS: 7,
                CONF_INCLUDE_ALL_DAY: True,
                CONF_READ_ONLY: False,
            },
        )
        await hass.async_block_till_done()

    assert CONF_CALENDARS not in entry.options


async def test_a_host_that_never_answers_is_not_asked_twice(
    hass: HomeAssistant, bootstrap_probe
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    with patch("custom_components.ha_caldav.config_flow.caldav.DAVClient") as client:
        client.return_value.principal.side_effect = requests.ConnectTimeout()
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_URL: "https://cloud.example.com"}
        )

    assert result["errors"] == {"base": "cannot_connect"}
    assert client.call_count == 1
    bootstrap_probe.assert_not_called()


async def test_a_bare_host_answering_html_logs_no_error_once_the_bootstrap_works(
    hass: HomeAssistant, caplog
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    with (
        patch("custom_components.ha_caldav.config_flow.caldav.DAVClient") as client,
        patch("custom_components.ha_caldav.async_setup_entry", return_value=True),
    ):
        client.return_value.principal.side_effect = [TypeError("html"), Mock()]
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_URL: "https://cloud.example.com"}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert not [record for record in caplog.records if record.levelname == "ERROR"]


async def test_an_unexpected_answer_from_every_candidate_is_logged(
    hass: HomeAssistant, caplog
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    with patch("custom_components.ha_caldav.config_flow.caldav.DAVClient") as client:
        client.return_value.principal.side_effect = TypeError("html")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_URL: "https://cloud.example.com"}
        )

    assert result["errors"] == {"base": "unknown"}
    assert any(
        "Unexpected error" in record.getMessage()
        and record.exc_info is not None
        and isinstance(record.exc_info[1], TypeError)
        for record in caplog.records
        if record.levelname == "ERROR"
    )


WEB_PAGE = b"<!DOCTYPE html><html><head><title>Login</title></head></html>"


def _web_page():
    """Answer every request like the web interface of a groupware: 200 with html."""

    def send(self, request, **kwargs):
        reply = requests.Response()
        reply.status_code = 200
        reply.reason = "OK"
        reply.headers["Content-Type"] = "text/html; charset=utf-8"
        reply._content = WEB_PAGE
        reply.url = request.url
        reply.request = request
        return reply

    return patch.object(requests.adapters.HTTPAdapter, "send", send)


async def test_a_web_page_is_not_set_up_as_an_account(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with _web_page():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_URL: "https://mail.example.com/SOGo/",
                CONF_USERNAME: "jane",
                CONF_PASSWORD: "secret",
                CONF_VERIFY_SSL: True,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.FORM
    assert hass.config_entries.async_entries(DOMAIN) == []


def _suggested(result) -> dict:
    return {
        str(key): (key.description or {}).get("suggested_value")
        for key in result["data_schema"].schema
    }


async def test_the_form_keeps_what_was_entered_after_an_error(
    hass: HomeAssistant,
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch("custom_components.ha_caldav.config_flow.caldav.DAVClient") as client:
        client.return_value.principal.side_effect = requests.ConnectionError()
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                **USER_INPUT,
                CONF_URL: "https://iven:secret@cloud.example.com/remote.php/dav",
                CONF_VERIFY_SSL: False,
            },
        )

    assert result["errors"] == {"base": "cannot_connect"}
    suggested = _suggested(result)
    assert suggested[CONF_URL] == USER_INPUT[CONF_URL]
    assert suggested[CONF_USERNAME] == "iven"
    assert suggested[CONF_VERIFY_SSL] is False
    assert suggested[CONF_PASSWORD] is None


async def test_reconfigure_keeps_the_url_typed_after_an_error(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    with patch("custom_components.ha_caldav.config_flow.caldav.DAVClient") as client:
        client.return_value.principal.side_effect = requests.ConnectionError()
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_URL: "https://new.example.com/dav", CONF_PASSWORD: "new"},
        )

    assert result["errors"] == {"base": "cannot_connect"}
    assert _suggested(result)[CONF_URL] == "https://new.example.com/dav"
    assert _suggested(result)[CONF_PASSWORD] is None


@pytest.mark.parametrize(
    "url",
    [
        "https://cloud.example.com/remote.php/dav/calendars/iven/",
        "https://cloud.example.com/remote.php/dav/calendars/iven/personal/",
    ],
)
async def test_an_account_entered_by_another_of_its_urls_is_not_set_up_twice(
    hass: HomeAssistant, url: str
) -> None:
    MockConfigEntry(
        domain=DOMAIN,
        data={
            **USER_INPUT,
            CONF_PRINCIPAL: "https://Cloud.Example.com:443"
            "/remote.php/dav/principals/users/iven",
        },
        unique_id=f"{USER_INPUT[CONF_URL]}#{USER_INPUT[CONF_USERNAME]}",
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with (
        patch("custom_components.ha_caldav.config_flow.caldav.DAVClient") as client,
        patch("custom_components.ha_caldav.async_setup_entry", return_value=True),
    ):
        client.return_value.principal.return_value.url = PRINCIPAL
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_URL: url}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_reconfigure_refuses_to_point_an_entry_at_another_entrys_account(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**USER_INPUT, CONF_URL: "https://old.example.com/dav"},
        unique_id=f"https://old.example.com/dav#{USER_INPUT[CONF_USERNAME]}",
    )
    entry.add_to_hass(hass)
    MockConfigEntry(
        domain=DOMAIN,
        data={**USER_INPUT, CONF_PRINCIPAL: PRINCIPAL},
        unique_id=f"{USER_INPUT[CONF_URL]}#{USER_INPUT[CONF_USERNAME]}",
    ).add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    with (
        patch("custom_components.ha_caldav.config_flow.caldav.DAVClient") as client,
        patch("custom_components.ha_caldav.async_setup_entry", return_value=True),
    ):
        client.return_value.principal.return_value.url = PRINCIPAL
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_URL: "https://cloud.example.com/remote.php/dav/calendars/iven/"},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_URL] == "https://old.example.com/dav"


def _plain_http_answer() -> requests.exceptions.SSLError:
    """Return the TLS failure requests raises when a server answers in plain HTTP."""
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = ssl.create_default_context().wrap_bio(
        incoming, outgoing, server_hostname="dav.example.com"
    )
    with suppress(ssl.SSLWantReadError):
        tls.do_handshake()
    incoming.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
    try:
        tls.do_handshake()
    except ssl.SSLError as err:
        refused = err
    return requests.exceptions.SSLError(
        urllib3.exceptions.MaxRetryError(
            None, "/", reason=urllib3.exceptions.SSLError(refused)
        )
    )


async def test_a_plain_http_server_entered_without_a_scheme_is_named_as_such(
    hass: HomeAssistant,
) -> None:
    """Falling back to http on its own would send the password unencrypted."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch("custom_components.ha_caldav.config_flow.caldav.DAVClient") as client:
        client.return_value.principal.side_effect = _plain_http_answer()
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {**USER_INPUT, CONF_URL: "dav.example.com:5232", CONF_VERIFY_SSL: False},
        )

    assert result["errors"] == {"base": "no_tls"}
    assert client.call_args_list[0].args[0] == "https://dav.example.com:5232"


def _throttling(self, request, **kwargs):
    reply = requests.Response()
    reply.status_code = 429
    reply.reason = "Too Many Requests"
    reply._content = b""
    reply.url = request.url
    reply.request = request
    return reply


async def test_a_server_blocking_logins_is_named_as_such(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch.object(requests.adapters.HTTPAdapter, "send", _throttling):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["errors"] == {"base": "too_many_attempts"}


async def test_reauth_names_a_server_blocking_logins(hass: HomeAssistant) -> None:
    """Nextcloud answers 429 to the right password too once it blocks the address."""
    entry = _entry()
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    with patch.object(requests.adapters.HTTPAdapter, "send", _throttling):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "right"}
        )

    assert result["errors"] == {"base": "too_many_attempts"}
    assert entry.data[CONF_PASSWORD] == "secret"
