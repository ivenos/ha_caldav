"""Data update coordinator for the CalDAV integration."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import partial
import logging
from typing import Any

import caldav
from homeassistant.components.calendar import CalendarEvent
from homeassistant.components.todo import TodoItem, TodoItemStatus
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
import homeassistant.util.dt as dt_util

from . import HaCaldavConfigEntry

_LOGGER = logging.getLogger(__name__)

# CalDAV knows four states, Home Assistant two. Cancelled items are folded into
# completed because they are equally "not outstanding".
TODO_STATUS = {
    "NEEDS-ACTION": TodoItemStatus.NEEDS_ACTION,
    "IN-PROCESS": TodoItemStatus.NEEDS_ACTION,
    "COMPLETED": TodoItemStatus.COMPLETED,
    "CANCELLED": TodoItemStatus.COMPLETED,
}
TODO_STATUS_INV = {
    TodoItemStatus.NEEDS_ACTION: "NEEDS-ACTION",
    TodoItemStatus.COMPLETED: "COMPLETED",
}


class HaCaldavCoordinator(DataUpdateCoordinator[CalendarEvent | None]):
    """Fetch calendar data and expose the next upcoming event."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: HaCaldavConfigEntry,
        calendar: caldav.Calendar,
        days: int,
        include_all_day: bool,
        scan_interval: timedelta,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=calendar.name or "CalDAV",
            update_interval=scan_interval,
            config_entry=entry,
        )
        self.calendar = calendar
        self.days = days
        self.include_all_day = include_all_day

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Return all events between two dates."""
        results = await hass.async_add_executor_job(
            partial(
                self.calendar.search,
                start=start_date,
                end=end_date,
                event=True,
                expand=True,
            )
        )
        return [
            to_event(item.vobject_instance.vevent)
            for item in results
            if hasattr(item.vobject_instance, "vevent")
        ]

    async def _async_update_data(self) -> CalendarEvent | None:
        """Return the next upcoming event within the configured window."""
        start = dt_util.start_of_local_day()
        end = start + timedelta(days=self.days)
        results = await self.hass.async_add_executor_job(
            partial(self.calendar.search, start=start, end=end, event=True, expand=True)
        )
        # The server is not required to return results in any order.
        vevents = sorted(
            (
                item.vobject_instance.vevent
                for item in results
                if hasattr(item.vobject_instance, "vevent")
            ),
            key=sort_key,
        )
        vevent = next(
            (
                vevent
                for vevent in vevents
                if (self.include_all_day or not is_all_day(vevent))
                and not is_over(vevent)
            ),
            None,
        )
        return to_event(vevent) if vevent is not None else None


class HaCaldavTodoCoordinator(DataUpdateCoordinator[list[TodoItem]]):
    """Fetch the to-do items of a single calendar."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: HaCaldavConfigEntry,
        calendar: caldav.Calendar,
        scan_interval: timedelta,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{calendar.name or 'CalDAV'} todo",
            update_interval=scan_interval,
            config_entry=entry,
        )
        self.calendar = calendar

    async def _async_update_data(self) -> list[TodoItem]:
        """Return every to-do item, including the completed ones."""
        results = await self.hass.async_add_executor_job(
            partial(self.calendar.search, todo=True, include_completed=True)
        )
        items = []
        for resource in results:
            if not hasattr(resource.vobject_instance, "vtodo"):
                continue
            if (item := to_todo(resource.vobject_instance.vtodo)) is not None:
                items.append(item)
        return items


def to_todo(vtodo: Any) -> TodoItem | None:
    """Map a vobject VTODO onto a Home Assistant to-do item.

    Items without a uid or summary are skipped: Home Assistant needs both to
    address and render them.
    """
    uid = get_attr_value(vtodo, "uid")
    summary = get_attr_value(vtodo, "summary")
    if uid is None or summary is None:
        return None
    due: date | datetime | None = None
    if (value := get_attr_value(vtodo, "due")) is not None:
        due = dt_util.as_local(value) if isinstance(value, datetime) else value
    return TodoItem(
        uid=uid,
        summary=summary,
        status=TODO_STATUS.get(
            get_attr_value(vtodo, "status") or "", TodoItemStatus.NEEDS_ACTION
        ),
        due=due,
        description=get_attr_value(vtodo, "description"),
    )


def get_attr_value(obj: Any, attribute: str) -> Any | None:
    """Return the value of a vobject attribute or None if absent."""
    if (value := getattr(obj, attribute, None)) is not None:
        return value.value
    return None


def get_end_date(vevent: Any) -> datetime | date:
    """Return the end date derived from dtend, duration or a default day."""
    if hasattr(vevent, "dtend"):
        end = vevent.dtend.value
    elif hasattr(vevent, "duration"):
        end = vevent.dtstart.value + vevent.duration.value
    else:
        end = vevent.dtstart.value + timedelta(days=1)
    if not isinstance(end, datetime) and end == vevent.dtstart.value:
        end += timedelta(days=1)
    return end


def to_local(value: datetime | date) -> datetime | date:
    """Return datetimes as local time and leave dates unchanged."""
    if isinstance(value, datetime):
        return dt_util.as_local(value)
    return value


def sort_key(vevent: Any) -> datetime:
    """Return the start as an aware datetime so mixed types sort together."""
    start = vevent.dtstart.value
    if not isinstance(start, datetime):
        start = datetime.combine(start, time.min)
    if start.tzinfo is None:
        start = start.replace(tzinfo=dt_util.get_default_time_zone())
    return start


def is_all_day(vevent: Any) -> bool:
    """Return whether the event covers a whole day."""
    return not isinstance(vevent.dtstart.value, datetime)


def is_over(vevent: Any) -> bool:
    """Return whether the event has already ended.

    An all-day event ends on a date, which cannot be compared against a
    datetime, so the comparison is made per type.
    """
    end = to_local(get_end_date(vevent))
    if isinstance(end, datetime):
        return dt_util.now() >= end
    return dt_util.now().date() >= end


def to_event(vevent: Any) -> CalendarEvent:
    """Map a vobject VEVENT onto a Home Assistant calendar event."""
    return CalendarEvent(
        summary=get_attr_value(vevent, "summary") or "",
        start=to_local(vevent.dtstart.value),
        end=to_local(get_end_date(vevent)),
        location=get_attr_value(vevent, "location"),
        description=get_attr_value(vevent, "description"),
        uid=get_attr_value(vevent, "uid"),
        recurrence_id=(
            str(value)
            if (value := get_attr_value(vevent, "recurrence_id")) is not None
            else None
        ),
    )
