"""Tests for the CalDAV config, reauth and options flow."""

from unittest.mock import Mock, patch

from caldav.lib.error import AuthorizationError
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import requests

from custom_components.ha_caldav.const import (
    CONF_CALENDARS,
    CONF_DAYS,
    CONF_INCLUDE_ALL_DAY,
    CONF_READ_ONLY,
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
        patch("custom_components.ha_caldav.config_flow.caldav.DAVClient"),
        patch("custom_components.ha_caldav.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "iven"
    assert result["data"] == USER_INPUT


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
    assert result["type"] is FlowResultType.FORM

    # Saving triggers a reload, which builds a fresh client.
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = []
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_CALENDARS: ["Work"],
                CONF_SCAN_INTERVAL: 30,
                CONF_DAYS: 14,
                CONF_INCLUDE_ALL_DAY: False,
                CONF_READ_ONLY: True,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_CALENDARS: ["Work"],
        CONF_SCAN_INTERVAL: 30,
        CONF_DAYS: 14,
        CONF_INCLUDE_ALL_DAY: False,
        CONF_READ_ONLY: True,
    }


async def test_options_flow_keeps_selection_while_unreachable(
    hass: HomeAssistant,
) -> None:
    # With the server down the calendars field is left out of the form; saving
    # the other options must not wipe the stored selection.
    entry = await _setup_entry(hass, options={CONF_CALENDARS: ["Personal"]})
    entry.runtime_data.principal.side_effect = requests.ConnectionError()

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
