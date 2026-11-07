"""Event platform for the CalDAV integration, one reminder entity per calendar."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import HaCaldavConfigEntry, ManagedCalendar, reminder_unique_id
from .entity import RemindingEntity

PARALLEL_UPDATES = 0

EVENT_TYPES = {"events": "event", "todos": "todo"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HaCaldavConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one reminder entity per calendar that holds events or to-dos."""
    async_add_entities(
        HaCaldavReminderEntity(managed, entry, halves)
        for managed in entry.runtime_data.calendars
        if (halves := reminder_halves(managed))
    )


def reminder_halves(managed: ManagedCalendar) -> tuple[str, ...]:
    """Return the halves of a calendar whose alarms a reminder entity tells of."""
    return tuple(
        name for name, half in managed.coordinator.halves.items() if half.supported
    )


class HaCaldavReminderEntity(RemindingEntity, EventEntity):
    """The alarms of a calendar's events and to-dos, as they come due."""

    _attr_translation_key = "reminder"

    def __init__(
        self,
        managed: ManagedCalendar,
        entry: HaCaldavConfigEntry,
        halves: tuple[str, ...],
    ) -> None:
        """Initialize the reminder entity."""
        super().__init__(managed)
        self._halves = halves
        self._attr_event_types = [EVENT_TYPES[half] for half in halves]
        self._attr_translation_placeholders = {"calendar": managed.name}
        self._attr_unique_id = reminder_unique_id(entry.entry_id, managed.calendar.url)

    @property
    def available(self) -> bool:
        """Whether any half it tells of is still being read."""
        return super().available and not all(
            self.coordinator.halves[half].dead for half in self._halves
        )

    @callback
    def _async_fire(self, half: str, data: dict[str, Any]) -> None:
        self._trigger_event(EVENT_TYPES[half], data)
        self.async_write_ha_state()
