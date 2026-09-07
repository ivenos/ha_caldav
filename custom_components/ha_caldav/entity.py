"""The entity base both platforms share."""

from __future__ import annotations

from functools import partial

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HaCaldavConfigEntry, HaCaldavCoordinator, ManagedCalendar
from .errors import as_reported


class HaCaldavEntity(CoordinatorEntity[HaCaldavCoordinator]):
    """One entity for one calendar, under the device of its account."""

    _attr_has_entity_name = True
    # Which half of the collection this entity reads.
    _half: str

    @property
    def available(self) -> bool:
        """Whether this entity's own half of the collection is being read."""
        return super().available and not self.coordinator.halves[self._half].dead

    def __init__(self, managed: ManagedCalendar, entry: HaCaldavConfigEntry) -> None:
        """Initialize the entity."""
        super().__init__(managed.coordinator)
        self.managed = managed
        self.calendar = managed.calendar
        self._attr_name = managed.name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            entry_type=DeviceEntryType.SERVICE,
            name=entry.title,
        )

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
