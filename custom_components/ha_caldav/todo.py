"""To-do platform for the CalDAV integration."""

from __future__ import annotations

from datetime import timedelta
from functools import partial
from typing import Any

import caldav
from caldav.lib.error import DAVError, NotFoundError
from homeassistant.components.todo import (
    TodoItem,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from requests import ConnectionError as RequestsConnectionError, Timeout

from . import HaCaldavConfigEntry
from .api import create_todo, delete_todo, update_todo
from .calendar import fetch_calendars
from .const import (
    CONF_CALENDARS,
    CONF_READ_ONLY,
    DEFAULT_READ_ONLY,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import TODO_STATUS_INV, HaCaldavTodoCoordinator

WRITE_ERRORS = (RequestsConnectionError, Timeout, DAVError, ValueError)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HaCaldavConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one to-do list per selected calendar."""
    client = entry.runtime_data
    selected = entry.options.get(CONF_CALENDARS)
    read_only = entry.options.get(CONF_READ_ONLY, DEFAULT_READ_ONLY)
    scan_interval = timedelta(
        minutes=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )

    calendars = await hass.async_add_executor_job(fetch_calendars, client)

    entities: list[HaCaldavTodoListEntity] = []
    for calendar in calendars:
        if selected and calendar.name not in selected:
            continue
        coordinator = HaCaldavTodoCoordinator(hass, entry, calendar, scan_interval)
        await coordinator.async_config_entry_first_refresh()
        entities.append(HaCaldavTodoListEntity(coordinator, entry, calendar, read_only))
    async_add_entities(entities)


class HaCaldavTodoListEntity(
    CoordinatorEntity[HaCaldavTodoCoordinator], TodoListEntity
):
    """The VTODO items of a CalDAV calendar."""

    _attr_has_entity_name = True
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
        | TodoListEntityFeature.SET_DUE_DATE_ON_ITEM
        | TodoListEntityFeature.SET_DUE_DATETIME_ON_ITEM
        | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
    )

    def __init__(
        self,
        coordinator: HaCaldavTodoCoordinator,
        entry: HaCaldavConfigEntry,
        calendar: caldav.Calendar,
        read_only: bool = False,
    ) -> None:
        """Initialize the to-do list entity."""
        super().__init__(coordinator)
        if read_only:
            self._attr_supported_features = TodoListEntityFeature(0)
        self.calendar = calendar
        self._attr_name = calendar.name or "CalDAV"
        self._attr_unique_id = f"{entry.entry_id}-{calendar.url}-todo"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            entry_type=DeviceEntryType.SERVICE,
            name=entry.title,
        )

    @property
    def todo_items(self) -> list[TodoItem] | None:
        """Return the to-do items."""
        return self.coordinator.data

    async def async_create_todo_item(self, item: TodoItem) -> None:
        """Add an item to the list."""
        await self._write(
            partial(create_todo, self.calendar, _item_data(item)), "create"
        )

    async def async_update_todo_item(self, item: TodoItem) -> None:
        """Update an item on the list."""
        await self._write(
            partial(update_todo, self.calendar, item.uid, _item_data(item)), "update"
        )

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        """Delete items from the list."""
        for uid in uids:
            await self._write(partial(delete_todo, self.calendar, uid), "delete")

    async def _write(self, job: partial[None], action: str) -> None:
        try:
            await self.hass.async_add_executor_job(job)
        except NotFoundError as err:
            raise HomeAssistantError(
                f"To-do item not found on the server: {err}"
            ) from err
        except WRITE_ERRORS as err:
            raise HomeAssistantError(f"CalDAV {action} error: {err}") from err
        await self.coordinator.async_request_refresh()


def _item_data(item: TodoItem) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if item.summary:
        data["summary"] = item.summary
    if item.status:
        data["status"] = TODO_STATUS_INV.get(item.status, "NEEDS-ACTION")
    if item.due:
        data["due"] = item.due
    if item.description:
        data["description"] = item.description
    return data
