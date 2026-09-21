"""The entity base both platforms share."""

from __future__ import annotations

from functools import partial
import logging

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import set_calendar_name
from .coordinator import HaCaldavCoordinator, ManagedCalendar
from .errors import as_reported

_LOGGER = logging.getLogger(__name__)


class HaCaldavEntity(CoordinatorEntity[HaCaldavCoordinator]):
    """One entity for one calendar."""

    _attr_has_entity_name = True
    # Which half of the collection this entity reads.
    _half: str
    _name_in_registry: str | None = None

    @property
    def available(self) -> bool:
        """Whether this entity's own half of the collection is being read."""
        return super().available and not self.coordinator.halves[self._half].dead

    def __init__(self, managed: ManagedCalendar) -> None:
        """Initialize the entity."""
        super().__init__(managed.coordinator)
        self.managed = managed
        self.calendar = managed.calendar
        self._attr_name = managed.name

    async def async_added_to_hass(self) -> None:
        """Note the name the registry holds, to tell a rename from it."""
        if entry := self.registry_entry:
            self._name_in_registry = entry.name
        await super().async_added_to_hass()

    @callback
    def async_registry_entry_updated(self) -> None:
        """Carry a name given in Home Assistant over to the server."""
        entry = self.registry_entry
        if entry is None or entry.name == self._name_in_registry:
            return
        self._name_in_registry = entry.name
        if entry.name is not None and self.managed.writable:
            self.coordinator.config_entry.async_create_task(
                self.hass, self._async_rename(entry.name)
            )

    async def _async_rename(self, name: str) -> None:
        """Rename the calendar on the server, then take the name from there."""
        if name != self.managed.name:
            try:
                await self.hass.async_add_executor_job(
                    set_calendar_name, self.calendar, name
                )
            except Exception as err:  # noqa: BLE001
                # Nobody to raise to: the rename came in through the registry.
                _LOGGER.warning(
                    "Could not rename %s on the server, so %s stays local: %s",
                    self.managed.name,
                    name,
                    type(err).__name__,
                )
                return
            self.hass.config_entries.async_schedule_reload(
                self.coordinator.config_entry.entry_id
            )
        er.async_get(self.hass).async_update_entity(self.entity_id, name=None)

    async def async_write(
        self,
        job: partial[None],
        action: str,
        forget: tuple[str, tuple[str, ...]] | None = None,
    ) -> None:
        """Run a write in the executor, one per collection at a time, then refresh.

        The panels call the entity over the websocket, past PARALLEL_UPDATES.
        Etags the write invalidated are dropped before the refresh reads the
        new ones back.
        """
        async with self.managed.write_lock:
            try:
                await self.hass.async_add_executor_job(job)
            except Exception as err:
                # caldav asserts on an unexpected response and raises TypeError
                # on html; as_reported names only the type.
                raise as_reported(err, action) from err
            if forget is not None:
                self.coordinator.forget_etags(*forget)
        await self.coordinator.async_request_refresh()
