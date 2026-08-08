"""To-do platform for the CalDAV integration."""

from __future__ import annotations

from functools import partial
from typing import Any

from homeassistant.components.todo import (
    TodoItem,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import create_todo, delete_todos, reorder_todos, update_todo
from .const import DOMAIN
from .coordinator import (
    TODO_STATUS_INV,
    HaCaldavConfigEntry,
    ManagedCalendar,
    todo_unique_id,
)
from .entity import HaCaldavEntity

# Service calls only; the to-do panel, reordering included, comes in over the
# websocket and never reaches it. What actually keeps two writes off the same
# object is the per-collection lock in HaCaldavEntity.async_write.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HaCaldavConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one to-do list per calendar that holds to-do items."""
    async_add_entities(
        HaCaldavTodoListEntity(managed, entry)
        for managed in entry.runtime_data.calendars
        if managed.capability.supports_todos
    )


class HaCaldavTodoListEntity(HaCaldavEntity, TodoListEntity):
    """The VTODO items of a CalDAV calendar."""

    _half = "todos"

    def __init__(
        self,
        managed: ManagedCalendar,
        entry: HaCaldavConfigEntry,
    ) -> None:
        """Initialize the to-do list entity."""
        super().__init__(managed, entry)
        self._attr_supported_features = (
            TodoListEntityFeature.CREATE_TODO_ITEM
            | TodoListEntityFeature.UPDATE_TODO_ITEM
            | TodoListEntityFeature.DELETE_TODO_ITEM
            | TodoListEntityFeature.MOVE_TODO_ITEM
            | TodoListEntityFeature.SET_DUE_DATE_ON_ITEM
            | TodoListEntityFeature.SET_DUE_DATETIME_ON_ITEM
            | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
            if managed.writable
            else TodoListEntityFeature(0)
        )
        self._attr_unique_id = todo_unique_id(entry.entry_id, managed.calendar.url)

    @property
    def todo_items(self) -> list[TodoItem] | None:
        """Return the to-do items."""
        return self.coordinator.data.todos if self.coordinator.data else None

    async def async_create_todo_item(self, item: TodoItem) -> None:
        """Add an item to the list."""
        await self.async_write(
            partial(create_todo, self.calendar, _item_data(item)), "create"
        )

    async def async_update_todo_item(self, item: TodoItem) -> None:
        """Update an item on the list."""
        await self.async_write(
            partial(
                update_todo,
                self.calendar,
                item.uid,
                _item_data(item),
                self.coordinator.todo_etags.get(item.uid or ""),
            ),
            "update",
            forget=("todo_etags", (item.uid or "",)),
        )

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        """Delete items from the list."""
        await self.async_write(
            partial(
                delete_todos, self.calendar, uids, dict(self.coordinator.todo_etags)
            ),
            "delete",
            forget=("todo_etags", tuple(uids)),
        )

    async def async_move_todo_item(
        self, uid: str, previous_uid: str | None = None
    ) -> None:
        """Move an item behind another one, or to the front of the list."""
        items = self.coordinator.data.todos if self.coordinator.data else []
        order = [item.uid for item in items if item.uid]
        for wanted in (uid, previous_uid):
            if wanted is not None and wanted not in order:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="unknown_todo_item",
                    translation_placeholders={"uid": wanted},
                )
        if previous_uid == uid:
            return
        order.remove(uid)
        order.insert(0 if previous_uid is None else order.index(previous_uid) + 1, uid)
        await self.async_write(
            partial(reorder_todos, self.calendar, order),
            "reorder",
            # The moved item is written, and so is every other one whenever the
            # list has to be renumbered, which is the first drag of any list the
            # server never numbered. Their etags are stale either way, and the
            # refresh behind this is debounced, so the next tick of one of them
            # would be refused as a conflict the user made themselves.
            forget=("todo_etags", tuple(order)),
        )


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
