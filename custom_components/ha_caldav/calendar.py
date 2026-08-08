"""Calendar platform for the CalDAV integration."""

from __future__ import annotations

from datetime import datetime
from functools import partial
from typing import Any

from homeassistant.components.calendar import (
    DOMAIN as CALENDAR_DOMAIN,
    CalendarEntity,
    CalendarEntityFeature,
    CalendarEvent,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import (
    AddConfigEntryEntitiesCallback,
    async_get_current_platform,
)

from .api import create_event
from .connection import calendar_key
from .const import DOMAIN, EVENT_ATTRIBUTES, RANGE_THIS_AND_FUTURE
from .coordinator import (
    HaCaldavConfigEntry,
    HaCaldavRuntimeData,
    ManagedCalendar,
    calendar_unique_id,
)
from .entity import HaCaldavEntity
from .errors import as_reported
from .recurrence import delete_event, update_event
from .services import async_register_entity_services

# Home Assistant owns the visible color under its own domain, so the record of
# what we took from the server needs a namespace of its own.
COLOR_STATE = f"{DOMAIN}.private"

# Home Assistant honours this for service calls only. The panel edits its way
# in over the websocket and never reaches it, which is why the writes are also
# serialized per collection in HaCaldavEntity.async_write; this stays for the
# service path, where it queues at the platform rather than per calendar.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HaCaldavConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one calendar entity per calendar that holds events."""
    data = entry.runtime_data
    async_register_entity_services(async_get_current_platform())
    async_add_entities(
        HaCaldavCalendarEntity(data, managed, entry)
        for managed in data.calendars
        if managed.capability.supports_events
    )


class HaCaldavCalendarEntity(HaCaldavEntity, CalendarEntity):
    """A CalDAV calendar with full create, update and delete support."""

    _half = "events"
    # Templatable, but not worth keeping for the whole recorder retention.
    _unrecorded_attributes = frozenset(EVENT_ATTRIBUTES)

    def __init__(
        self,
        data: HaCaldavRuntimeData,
        managed: ManagedCalendar,
        entry: HaCaldavConfigEntry,
    ) -> None:
        """Initialize the calendar entity."""
        super().__init__(managed, entry)
        self.runtime_data = data
        self.colors = data.colors
        self._attr_supported_features = (
            CalendarEntityFeature.CREATE_EVENT
            | CalendarEntityFeature.UPDATE_EVENT
            | CalendarEntityFeature.DELETE_EVENT
            if managed.writable
            else CalendarEntityFeature(0)
        )
        self._color_key = calendar_key(managed.calendar.url)
        self._attr_initial_color = self._server_color()
        self._written_color: str | None = None
        self._picked = False
        self._attr_unique_id = calendar_unique_id(entry.entry_id, managed.calendar.url)

    def get_initial_entity_options(self) -> er.EntityOptionsType | None:
        """Give a newly registered entity the server color, and say we set it.

        Core only calls this on registration, so recording our own state here is
        what keeps the first sync from reading its own color as a user's pick.
        """
        options = dict(super().get_initial_entity_options() or {})
        options[COLOR_STATE] = {"color": self.initial_color, "override": False}
        return options

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

        Two changes between polls can land back on the server's own color.
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

        Clearing one counts as a choice. A calendar the server did not report
        on is left alone.
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

    @callback
    def async_follow_server_color(self) -> None:
        """Adopt the server color again after we ourselves wrote one."""
        self._picked = False
        if (entry := self.registry_entry) is None:
            return
        current = entry.options.get(CALENDAR_DOMAIN, {}).get("color")
        # Both sides re-baselined on the color the entity shows now. Keeping
        # the recorded one would have the registry watcher read the user's own
        # earlier pick as a fresh one and put the override straight back, so
        # the calendar would never follow the server again.
        self._written_color = current
        if entry.options.get(COLOR_STATE) is not None:
            er.async_get(self.hass).async_update_entity_options(
                self.entity_id, COLOR_STATE, {"color": current, "override": False}
            )

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming event."""
        return self.coordinator.data.next_event if self.coordinator.data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the properties of the upcoming event HA has no field for."""
        return dict(self.coordinator.data.extras) if self.coordinator.data else {}

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Return events in a date range.

        Everything the caller catches is a HomeAssistantError: the panel drops
        a subscription that raises anything else and then waits forever, and
        the REST view answers a plain-text traceback instead of json.
        """
        try:
            return await self.coordinator.async_get_events(hass, start_date, end_date)
        except Exception as err:
            raise as_reported(err, "read") from err

    async def async_create_event(self, **kwargs: Any) -> None:
        """Create a new event."""
        await self.async_write(
            partial(create_event, self.calendar, _item_data(kwargs)), "create"
        )

    @property
    def _own_address(self) -> str | None:
        """Return the account's own calendar user address, if the server has one.

        Written as ORGANIZER whenever attendees are set and the object carries
        none: RFC 5546 3 requires it, and sabre/dav answers 500 on deleting an
        object that lists attendees without one, which leaves the event
        impossible to remove at all.
        """
        addresses = self.runtime_data.address_set
        return addresses[0] if addresses else None

    async def async_create_full_event(self, data: dict[str, Any]) -> None:
        """Create an event including the properties only a service can set."""
        await self.async_write(
            partial(create_event, self.calendar, data, self._own_address), "create"
        )

    async def async_update_event(
        self,
        uid: str,
        event: dict[str, Any],
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Update a series, a single occurrence, or an occurrence onwards."""
        await self.async_update_full_event(
            uid, _item_data(event), recurrence_id, recurrence_range
        )

    async def async_update_full_event(
        self,
        uid: str,
        data: dict[str, Any],
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Update an event from an already-mapped field set."""
        await self.async_write(
            partial(
                update_event,
                self.calendar,
                uid,
                data,
                recurrence_id,
                recurrence_range == RANGE_THIS_AND_FUTURE,
                expected_etag=self.coordinator.etags.get(uid),
                own_address=self._own_address,
            ),
            "update",
            forget=("etags", (uid,)),
        )

    async def async_delete_event(
        self,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Delete a series, a single occurrence, or an occurrence onwards."""
        await self.async_write(
            partial(
                delete_event,
                self.calendar,
                uid,
                recurrence_id,
                recurrence_range == RANGE_THIS_AND_FUTURE,
                expected_etag=self.coordinator.etags.get(uid),
            ),
            "delete",
            forget=("etags", (uid,)),
        )


def _item_data(fields: dict[str, Any]) -> dict[str, Any]:
    """Map the platform's event fields, naming every one of them.

    Home Assistant always sends the whole event, so a field it left out is one
    the user cleared, and :func:`.recurrence._apply` only clears what it is
    given. An absent rrule is the exception: expand strips it from the event
    the frontend echoes back, and dropping the recurrence is never meant.
    """
    data: dict[str, Any] = {
        "summary": fields["summary"],
        "dtstart": fields["dtstart"],
        "dtend": fields["dtend"],
        "description": fields.get("description") or None,
        "location": fields.get("location") or None,
    }
    if rrule := fields.get("rrule"):
        data["rrule"] = rrule
    return data
