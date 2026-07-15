"""Calendar platform for the CalDAV integration."""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import partial
from typing import Any

import caldav
from caldav.lib.error import DAVError, NotFoundError
from homeassistant.components.calendar import (
    CalendarEntity,
    CalendarEntityFeature,
    CalendarEvent,
)
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from requests import ConnectionError as RequestsConnectionError, Timeout

from . import HaCaldavConfigEntry
from .api import create_event, delete_event, update_event
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
from .coordinator import HaCaldavCoordinator

WRITE_ERRORS = (RequestsConnectionError, Timeout, DAVError, ValueError)


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

    entities: list[HaCaldavCalendarEntity] = []
    for calendar in calendars:
        if selected and calendar.name not in selected:
            continue
        coordinator = HaCaldavCoordinator(
            hass, entry, calendar, days, include_all_day, scan_interval
        )
        await coordinator.async_config_entry_first_refresh()
        entities.append(HaCaldavCalendarEntity(coordinator, entry, calendar, read_only))
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
        entry: HaCaldavConfigEntry,
        calendar: caldav.Calendar,
        read_only: bool = False,
    ) -> None:
        """Initialize the calendar entity."""
        super().__init__(coordinator)
        if read_only:
            self._attr_supported_features = CalendarEntityFeature(0)
        self.calendar = calendar
        self._attr_name = calendar.name or "CalDAV"
        self._attr_unique_id = f"{entry.entry_id}-{calendar.url}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            entry_type=DeviceEntryType.SERVICE,
            name=entry.title,
        )

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
            ),
            "update",
        )

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
            ),
            "delete",
        )

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
