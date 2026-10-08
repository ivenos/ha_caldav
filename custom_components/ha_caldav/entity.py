"""The entity base both platforms share."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging

from caldav.lib.error import NotFoundError
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later, async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import set_calendar_name
from .const import EVENT_REMINDER
from .coordinator import HaCaldavCoordinator, HaCaldavRuntimeData, ManagedCalendar
from .errors import Refused, as_reported

_LOGGER = logging.getLogger(__name__)

_LATE = timedelta(minutes=15)


class HaCaldavEntity(CoordinatorEntity[HaCaldavCoordinator]):
    """One entity for one calendar."""

    _attr_has_entity_name = True
    # Which half of the collection this entity reads.
    _half: str
    runtime_data: HaCaldavRuntimeData
    _name_in_registry: str | None = None
    _name_noted = False
    _reminding_since: datetime | None = None
    _next_reminder: CALLBACK_TYPE | None = None

    @property
    def addresses(self) -> list[str]:
        """Return the calendar user addresses of whoever owns this calendar."""
        if self.managed.addresses is not None:
            return self.managed.addresses
        return self.runtime_data.address_set

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
        self._reminded: set[tuple[object, ...]] = set()

    async def async_added_to_hass(self) -> None:
        """Note the name the registry holds, to tell a rename from it.

        Core re-adds the same entity when its entity_id changes, and a name
        saved along with it arrives that way instead of as an update. It is
        checked once added: a reload while core is still adding it leaves a copy.
        """
        readded = self._name_noted
        if not readded and (entry := self.registry_entry):
            self._name_in_registry = entry.name
        self._name_noted = True
        await super().async_added_to_hass()
        if readded:
            self.async_on_remove(
                async_call_later(self.hass, 0, self._async_recheck_name)
            )
        self._reminding_since = self._reminding_since or dt_util.utcnow()
        self.async_on_remove(self._async_stop_reminding)
        self._async_remind()

    @callback
    def _handle_coordinator_update(self) -> None:
        super()._handle_coordinator_update()
        self._async_remind()

    @callback
    def _async_stop_reminding(self) -> None:
        if self._next_reminder is not None:
            self._next_reminder()
            self._next_reminder = None

    @callback
    def _async_remind(self, _now: datetime | None = None) -> None:
        """Fire the reminders that came due and wake up for the next one.

        One found late still fires, as long as it came due within the last
        poll interval and after this entity started.
        """
        self._async_stop_reminding()
        now = dt_util.utcnow()
        earliest = now - (self.coordinator.update_interval or _LATE)
        if self._reminding_since is not None:
            earliest = max(earliest, self._reminding_since)
        upcoming: datetime | None = None
        known = set()
        for reminder in self.coordinator.reminders[self._half]:
            known.add(reminder.key)
            if reminder.at > now:
                upcoming = min(upcoming or reminder.at, reminder.at)
            elif reminder.at > earliest and reminder.key not in self._reminded:
                self._reminded.add(reminder.key)
                self.hass.bus.async_fire(
                    EVENT_REMINDER, {"entity_id": self.entity_id, **reminder.data}
                )
        self._reminded &= known
        if upcoming is not None:
            self._next_reminder = async_track_point_in_utc_time(
                self.hass, self._async_remind, upcoming
            )

    @callback
    def _async_recheck_name(self, _now: datetime) -> None:
        self.async_registry_entry_updated()

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
        renamed = name != self.managed.name
        if renamed:
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
        er.async_get(self.hass).async_update_entity(self.entity_id, name=None)
        if renamed:
            self.hass.config_entries.async_schedule_reload(
                self.coordinator.config_entry.entry_id
            )

    async def async_write(
        self,
        job: Callable[[], object],
        action: str,
        forget: tuple[str, tuple[str, ...]] | None = None,
    ) -> None:
        """Run a write in the executor, one per collection at a time, then refresh.

        The job and the refresh behind it both run under the lock, so what a
        job reads off the coordinator is past every earlier write. Core merges
        an update into the item it holds, which a debounced refresh leaves stale.
        """
        async with self.managed.write_lock:
            try:
                await self.hass.async_add_executor_job(job)
            except Exception as err:
                if _outdated(err):
                    await self.coordinator.async_refresh()
                # caldav asserts on an unexpected response and raises TypeError
                # on html; as_reported names only the type.
                raise as_reported(err, action) from err
            if forget is not None:
                self.coordinator.forget_etags(*forget)
            await self.coordinator.async_refresh()


def _outdated(err: Exception) -> bool:
    """Return whether a write failed on a state the server has moved past.

    Every retry would fail the same way until the next poll read it again.
    """
    if isinstance(err, Refused):
        return err.key == "etag_conflict"
    return isinstance(err, NotFoundError)
