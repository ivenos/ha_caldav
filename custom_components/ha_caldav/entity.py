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

    # Names stay scoped to the account device, so two accounts can both expose
    # a calendar called "Personal" without colliding.
    _attr_has_entity_name = True

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
        """Run a write in the executor and refresh once it lands.

        Shared by both platforms and by the services, so a retry, a debounce or
        a new error type is added in one place rather than three.

        An etag the write invalidated is dropped before that refresh, never
        after: the refresh reads the new one back, and dropping it afterwards
        would leave the next edit of the same object with nothing to check
        against and overwrite whatever landed in between.

        The cache is named rather than handed over, because a poll landing
        while the write is on the wire replaces the dict rather than emptying
        it. Dropping the key from the one captured beforehand would leave the
        pre-write etag in the live cache and refuse the next edit of the same
        object over a conflict that never happened.

        One write at a time per collection. PARALLEL_UPDATES only reaches the
        service path, and the calendar and to-do panels call their entity
        directly over the websocket, so two edits of one series read the same
        object, both passed the etag check against the same value, and the
        second PUT dropped what the first had written with nothing reported.
        """
        async with self.managed.write_lock:
            try:
                await self.hass.async_add_executor_job(job)
            except Exception as err:
                # Broader than the network and value errors, because caldav
                # asserts its way out of a response it did not expect and comes
                # out of a captive portal's html with a TypeError. Unwrapped,
                # those reach the frontend as "Unknown error" with a traceback
                # in the log, and as_reported names only the type, so nothing
                # new is disclosed.
                raise as_reported(err, action) from err
            if forget is not None:
                name, keys = forget
                # Under the lock a poll merges into: without it, a merge that
                # read its etags before the PUT landed can put the stale one
                # back after the pop, and the next edit of the same object is
                # refused over a conflict the user caused themselves.
                with self.coordinator.etag_lock:
                    cache = getattr(self.coordinator, name)
                    for key in keys:
                        cache.pop(key, None)
        await self.coordinator.async_request_refresh()
