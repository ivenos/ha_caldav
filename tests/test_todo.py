"""Tests for the CalDAV to-do list entity."""

from unittest.mock import Mock, patch

from caldav.lib.error import DAVError
from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntityFeature,
)
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_component import EntityComponent
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_caldav.const import CONF_READ_ONLY, DOMAIN

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


async def _setup(hass: HomeAssistant, options: dict | None = None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="iven",
        data=ENTRY_DATA,
        options=options or {},
        unique_id="x",
    )
    entry.add_to_hass(hass)
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = [
            _calendar("Personal")
        ]
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _entity(hass: HomeAssistant):
    component: EntityComponent = hass.data["entity_components"]["todo"]
    return component.get_entity("todo.iven_personal")


async def test_todo_list_per_calendar_with_full_crud(hass: HomeAssistant) -> None:
    await _setup(hass)

    state = hass.states.get("todo.iven_personal")
    assert state is not None

    features = state.attributes["supported_features"]
    assert features & TodoListEntityFeature.CREATE_TODO_ITEM
    assert features & TodoListEntityFeature.UPDATE_TODO_ITEM
    assert features & TodoListEntityFeature.DELETE_TODO_ITEM
    assert features & TodoListEntityFeature.SET_DUE_DATE_ON_ITEM
    assert features & TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM


async def test_read_only_hides_write_features(hass: HomeAssistant) -> None:
    await _setup(hass, options={CONF_READ_ONLY: True})

    assert hass.states.get("todo.iven_personal").attributes["supported_features"] == 0


async def test_create_forwards_mapped_status(hass: HomeAssistant) -> None:
    await _setup(hass)
    entity = _entity(hass)

    with patch("custom_components.ha_caldav.todo.create_todo") as create:
        await entity.async_create_todo_item(
            TodoItem(summary="Buy milk", status=TodoItemStatus.COMPLETED)
        )

    assert create.call_args.args[1]["summary"] == "Buy milk"
    assert create.call_args.args[1]["status"] == "COMPLETED"


async def test_delete_forwards_every_uid(hass: HomeAssistant) -> None:
    await _setup(hass)
    entity = _entity(hass)

    with patch("custom_components.ha_caldav.todo.delete_todo") as delete:
        await entity.async_delete_todo_items(["uid-1", "uid-2"])

    assert [call.args[1] for call in delete.call_args_list] == ["uid-1", "uid-2"]


async def test_server_error_becomes_home_assistant_error(hass: HomeAssistant) -> None:
    await _setup(hass)
    entity = _entity(hass)

    with (
        patch(
            "custom_components.ha_caldav.todo.delete_todo",
            side_effect=DAVError("boom"),
        ),
        pytest.raises(HomeAssistantError, match="CalDAV delete error"),
    ):
        await entity.async_delete_todo_items(["uid-1"])
