"""Calendar platform for the CalDAV integration."""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import partial
from typing import Any

import caldav
from caldav.lib.error import DAVError, NotFoundError
from homeassistant.components.calendar import (
    DOMAIN as CALENDAR_DOMAIN,
    CalendarEntity,
    CalendarEntityFeature,
    CalendarEvent,
)
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from requests import RequestException

from . import HaCaldavConfigEntry
from .api import create_event
from .color import calendar_key
from .const import (
    CONF_CALENDARS,
    CONF_DAYS,
    CONF_INCLUDE_ALL_DAY,
    CONF_READ_ONLY,
    DEFAULT_DAYS,
    DEFAULT_INCLUDE_ALL_DAY,
    DEFAULT_READ_ONLY,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    RANGE_THIS_AND_FUTURE,
)
from .coordinator import HaCaldavColorCoordinator, HaCaldavCoordinator
from .recurrence import delete_event, update_event

WRITE_ERRORS = (RequestException, DAVError, ValueError)

# Home Assistant owns the visible color under its own domain, so the record of
# what we took from the server needs a namespace of its own.
COLOR_STATE = f"{DOMAIN}.private"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HaCaldavConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one calendar entity per selected calendar."""
    client = entry.runtime_data
    days = entry.options.get(CONF_DAYS, DEFAULT_DAYS)
    include_all_day = entry.options.get(CONF_INCLUDE_ALL_DAY, DEFAULT_INCLUDE_ALL_DAY)
    selected = entry.options.get(CONF_CALENDARS)
    read_only = entry.options.get(CONF_READ_ONLY, DEFAULT_READ_ONLY)
    scan_interval = timedelta(
        minutes=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )

    calendars = await hass.async_add_executor_job(fetch_calendars, client)

    # Refreshed up front so the first sync of every entity has colors to work
    # with rather than having to wait a whole scan interval for them.
    colors = HaCaldavColorCoordinator(hass, entry, client, scan_interval)
    await colors.async_refresh()

    entities: list[HaCaldavCalendarEntity] = []
    for calendar in calendars:
        if selected and calendar.name not in selected:
            continue
        coordinator = HaCaldavCoordinator(
            hass, entry, calendar, days, include_all_day, scan_interval
        )
        await coordinator.async_config_entry_first_refresh()
        entities.append(
            HaCaldavCalendarEntity(coordinator, colors, entry, calendar, read_only)
        )
    async_add_entities(entities)


def fetch_calendars(client: caldav.DAVClient) -> list[caldav.Calendar]:
    """Return every calendar on the account."""
    return client.principal().calendars()


class HaCaldavCalendarEntity(CoordinatorEntity[HaCaldavCoordinator], CalendarEntity):
    """A CalDAV calendar with full create, update and delete support."""

    # Names stay scoped to the account device, so two accounts can both expose
    # a calendar called "Personal" without colliding.
    _attr_has_entity_name = True
    _attr_supported_features = (
        CalendarEntityFeature.CREATE_EVENT
        | CalendarEntityFeature.UPDATE_EVENT
        | CalendarEntityFeature.DELETE_EVENT
    )

    def __init__(
        self,
        coordinator: HaCaldavCoordinator,
        colors: HaCaldavColorCoordinator,
        entry: HaCaldavConfigEntry,
        calendar: caldav.Calendar,
        read_only: bool = False,
    ) -> None:
        """Initialize the calendar entity."""
        super().__init__(coordinator)
        if read_only:
            self._attr_supported_features = CalendarEntityFeature(0)
        self.calendar = calendar
        self.colors = colors
        self._color_key = calendar_key(calendar.url)
        self._written_color: str | None = None
        self._picked = False
        self._attr_name = calendar.name or "CalDAV"
        self._attr_unique_id = f"{entry.entry_id}-{calendar.url}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            entry_type=DeviceEntryType.SERVICE,
            name=entry.title,
        )

    async def async_added_to_hass(self) -> None:
        """Start following the color this calendar carries on the server."""
        # Before the base class, which is what subscribes to registry updates:
        # a hook running while this is unset would read as a color the user set.
        if entry := self.registry_entry:
            self._written_color = entry.options.get(CALENDAR_DOMAIN, {}).get("color")
        await super().async_added_to_hass()
        self.async_on_remove(self.colors.async_add_listener(self._async_sync_color))
        self._async_sync_color()

    @callback
    def async_registry_entry_updated(self) -> None:
        """Note a color the user picked the moment they pick it.

        Someone who changes the color twice between two polls can land back on
        the one the server carries, which the stored state alone cannot tell
        from never having touched it.
        """
        if (entry := self.registry_entry) is None:
            return
        if entry.options.get(CALENDAR_DOMAIN, {}).get("color") == self._written_color:
            return
        self._picked = True
        # Recorded right away rather than at the next poll, which a restart in
        # between would never let happen.
        state = entry.options.get(COLOR_STATE)
        if state is not None and not state.get("override"):
            er.async_get(self.hass).async_update_entity_options(
                self.entity_id, COLOR_STATE, {**state, "override": True}
            )

    @callback
    def _async_sync_color(self) -> None:
        """Carry a server-side color over to the entity setting.

        Clearing the color counts as a choice, and the flag that records one
        never clears. A calendar the server did not report on is left alone:
        that is a lookup that went wrong, not a color someone removed.
        """
        if (entry := self.registry_entry) is None or self.colors.data is None:
            return
        if self._color_key not in self.colors.data:
            return
        state = entry.options.get(COLOR_STATE)
        current = entry.options.get(CALENDAR_DOMAIN, {}).get("color")
        server = self._server_color()
        if state is None:
            # No record yet: an entity registered before we tracked colors, so
            # anything already showing is a color the user picked themselves.
            override = current is not None
        else:
            override = state.get("override") or current != state.get("color")
        override = override or self._picked

        registry = er.async_get(self.hass)
        # Both branches that do not return fall through to the record below: a
        # newly spotted pick has to be stored before the next poll.
        if override:
            if state is not None and state.get("override"):
                return
        elif state is not None and server == state.get("color"):
            return
        else:
            options = dict(entry.options.get(CALENDAR_DOMAIN, {}))
            if server is None:
                options.pop("color", None)
            else:
                options["color"] = server
            # Recorded before the write, which calls back into the watcher above.
            self._written_color = server
            registry.async_update_entity_options(
                self.entity_id, CALENDAR_DOMAIN, options or None
            )
        self.registry_entry = registry.async_update_entity_options(
            self.entity_id, COLOR_STATE, {"color": server, "override": override}
        )

    def _server_color(self) -> str | None:
        return (self.colors.data or {}).get(self._color_key)

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming event."""
        return self.coordinator.data

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Return events in a date range."""
        return await self.coordinator.async_get_events(hass, start_date, end_date)

    async def async_create_event(self, **kwargs: Any) -> None:
        """Create a new event."""
        await self._write(
            partial(create_event, self.calendar, _item_data(kwargs)), "create"
        )

    async def async_update_event(
        self,
        uid: str,
        event: dict[str, Any],
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Update a series, a single occurrence, or an occurrence onwards."""
        await self._write(
            partial(
                update_event,
                self.calendar,
                uid,
                _item_data(event),
                recurrence_id,
                recurrence_range == RANGE_THIS_AND_FUTURE,
                expected_etag=self.coordinator.etags.get(uid),
            ),
            "update",
        )
        # The write moved the server etag; drop the stale cache entry so a quick
        # follow-up edit is not flagged as a spurious conflict.
        self.coordinator.etags.pop(uid, None)

    async def async_delete_event(
        self,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Delete a series, a single occurrence, or an occurrence onwards."""
        await self._write(
            partial(
                delete_event,
                self.calendar,
                uid,
                recurrence_id,
                recurrence_range == RANGE_THIS_AND_FUTURE,
                expected_etag=self.coordinator.etags.get(uid),
            ),
            "delete",
        )
        self.coordinator.etags.pop(uid, None)

    async def _write(self, job: partial[None], action: str) -> None:
        try:
            await self.hass.async_add_executor_job(job)
        except NotFoundError as err:
            raise HomeAssistantError(f"Event not found on the server: {err}") from err
        except WRITE_ERRORS as err:
            raise HomeAssistantError(f"CalDAV {action} error: {err}") from err
        await self.coordinator.async_request_refresh()


def _item_data(fields: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {
        "summary": fields["summary"],
        "dtstart": fields["dtstart"],
        "dtend": fields["dtend"],
    }
    if description := fields.get("description"):
        data["description"] = description
    if location := fields.get("location"):
        data["location"] = location
    if rrule := fields.get("rrule"):
        data["rrule"] = rrule
    return data
