"""Services for the things CalDAV can do and the calendar platform cannot."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from functools import partial
from typing import TYPE_CHECKING, Any

from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError, Unauthorized, UnknownUser
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .api import (
    create_calendar,
    delete_calendar,
    export_ics,
    import_ics,
    move_event,
    respond_to_invitation,
    set_calendar_color,
)
from .color import normalize_color
from .connection import calendar_key
from .const import (
    ATTR_ALARMS,
    ATTR_ATTENDEES,
    ATTR_CATEGORIES,
    ATTR_CLASSIFICATION,
    ATTR_COLOR,
    ATTR_COMPONENTS,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_EVENT_STATUS,
    ATTR_FIELD,
    ATTR_ICS,
    ATTR_KEEP_ORIGINAL,
    ATTR_NAME,
    ATTR_ORGANIZER,
    ATTR_PRIORITY,
    ATTR_RESPONSE,
    ATTR_TARGET_ENTITY_ID,
    ATTR_TEXT,
    ATTR_TRANSPARENCY,
    ATTR_URL,
    COMPONENT_EVENT,
    COMPONENT_TODO,
    CONF_CALENDARS,
    DOMAIN,
    EVENT_CLASSIFICATIONS,
    EVENT_STATUSES,
    EVENT_TRANSPARENCIES,
    PARTSTAT_BY_RESPONSE,
    RANGE_THIS_AND_FUTURE,
    SEARCH_FIELDS,
    SERVICE_CREATE_CALENDAR,
    SERVICE_CREATE_EVENT,
    SERVICE_DELETE_CALENDAR,
    SERVICE_EXPORT_ICS,
    SERVICE_GET_FREE_BUSY,
    SERVICE_IMPORT_ICS,
    SERVICE_MOVE_EVENT,
    SERVICE_RESPOND_TO_INVITATION,
    SERVICE_SEARCH_EVENTS,
    SERVICE_SET_CALENDAR_COLOR,
    SERVICE_UPDATE_EVENT,
)
from .coordinator import (
    HaCaldavConfigEntry,
    ManagedCalendar,
    calendar_unique_id,
    component_of,
    to_event,
    todo_unique_id,
)
from .errors import as_reported
from .event import read_extras

if TYPE_CHECKING:
    from .calendar import HaCaldavCalendarEntity


def _local_datetime(value: Any) -> datetime:
    """Return an aware datetime in Home Assistant's own timezone.

    The datetime selector sends a naive value, which caldav would resolve
    against the OS timezone.
    """
    parsed = cv.datetime(value)
    return parsed if parsed.tzinfo is not None else dt_util.as_local(parsed)


def _date_only(value: Any) -> date:
    """Return a plain date; cv.date lets a datetime through unchanged."""
    if isinstance(value, datetime):
        raise vol.Invalid("expected a date without a time")
    return cv.date(value)


def _named(values: tuple[str, ...]) -> Any:
    """Return a field taking an RFC 5545 name in either spelling.

    hassfest holds selector option keys to [a-z0-9-_]+.
    """
    return vol.All(vol.Lower, vol.In(tuple(v.lower() for v in values)), vol.Upper)


EXTRA_FIELDS = {
    vol.Optional(ATTR_URL): cv.string,
    vol.Optional(ATTR_EVENT_STATUS): _named(EVENT_STATUSES),
    vol.Optional(ATTR_TRANSPARENCY): _named(EVENT_TRANSPARENCIES),
    vol.Optional(ATTR_CLASSIFICATION): _named(EVENT_CLASSIFICATIONS),
    vol.Optional(ATTR_PRIORITY): vol.All(vol.Coerce(int), vol.Range(min=0, max=9)),
    vol.Optional(ATTR_CATEGORIES): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional(ATTR_ORGANIZER): cv.string,
    vol.Optional(ATTR_ATTENDEES): vol.All(
        cv.ensure_list,
        [
            vol.Any(
                cv.string,
                vol.Schema(
                    {
                        vol.Required("email"): cv.string,
                        vol.Optional("name"): cv.string,
                        vol.Optional("role"): cv.string,
                        vol.Optional("status"): cv.string,
                        vol.Optional("rsvp"): cv.boolean,
                    }
                ),
            )
        ],
    ),
    vol.Optional(ATTR_ALARMS): vol.All(
        cv.ensure_list,
        [
            vol.Any(
                vol.Coerce(int),
                vol.Schema(
                    {
                        vol.Required("minutes_before"): vol.Coerce(int),
                        vol.Optional("action"): vol.In(("DISPLAY", "AUDIO")),
                        vol.Optional("related"): vol.In(("START", "END")),
                        vol.Optional("description"): cv.string,
                    }
                ),
            )
        ],
    ),
}

SEARCH_SCHEMA = {
    vol.Required(ATTR_TEXT): cv.string,
    vol.Optional(ATTR_FIELD, default="summary"): vol.In(SEARCH_FIELDS),
    vol.Optional("start"): _local_datetime,
    vol.Optional("end"): _local_datetime,
}

FREE_BUSY_SCHEMA = {
    vol.Required("start"): _local_datetime,
    vol.Required("end"): _local_datetime,
}

# A field each: the datetime picker always sends a time, and RFC 5545 writes
# an all-day event as a DATE.
SPAN_FIELDS = {
    vol.Optional("start_date_time"): _local_datetime,
    vol.Optional("end_date_time"): _local_datetime,
    vol.Optional("start_date"): _date_only,
    vol.Optional("end_date"): _date_only,
}

_ONE_START = cv.has_at_most_one_key("start_date_time", "start_date")
_ONE_END = cv.has_at_most_one_key("end_date_time", "end_date")


def _range_needs_occurrence(value: dict[str, Any]) -> dict[str, Any]:
    """Refuse a recurrence range with no occurrence for it to start at."""
    if "recurrence_range" in value and "recurrence_id" not in value:
        raise vol.Invalid("recurrence_range needs recurrence_id")
    return value


CREATE_EVENT_SCHEMA = vol.All(
    cv.make_entity_service_schema(
        {
            vol.Required("summary"): cv.string,
            **SPAN_FIELDS,
            vol.Optional("description"): cv.string,
            vol.Optional("location"): cv.string,
            vol.Optional("rrule"): cv.string,
            **EXTRA_FIELDS,
        }
    ),
    _ONE_START,
    _ONE_END,
    cv.has_at_least_one_key("start_date_time", "start_date"),
    cv.has_at_least_one_key("end_date_time", "end_date"),
)

UPDATE_EVENT_SCHEMA = vol.All(
    cv.make_entity_service_schema(
        {
            vol.Required("uid"): cv.string,
            vol.Optional("recurrence_id"): cv.string,
            vol.Optional("recurrence_range"): _named((RANGE_THIS_AND_FUTURE,)),
            vol.Optional("summary"): cv.string,
            **SPAN_FIELDS,
            vol.Optional("description"): cv.string,
            vol.Optional("location"): cv.string,
            vol.Optional("rrule"): cv.string,
            **EXTRA_FIELDS,
        }
    ),
    _ONE_START,
    _ONE_END,
    _range_needs_occurrence,
    # A PUT naming nothing still moves SEQUENCE, which a scheduling server
    # announces to every attendee.
    cv.has_at_least_one_key(
        "summary",
        "description",
        "location",
        "rrule",
        "start_date_time",
        "end_date_time",
        "start_date",
        "end_date",
        *(str(key) for key in EXTRA_FIELDS),
    ),
)

MOVE_EVENT_SCHEMA = {
    vol.Required("uid"): cv.string,
    vol.Required(ATTR_TARGET_ENTITY_ID): cv.entity_id,
    vol.Optional(ATTR_KEEP_ORIGINAL, default=False): cv.boolean,
}

IMPORT_SCHEMA = {vol.Required(ATTR_ICS): cv.string}

EXPORT_SCHEMA = {vol.Optional("uid"): cv.string}


def _hex_color(value: Any) -> str:
    """Return a color in the one shape the read path recognises again."""
    if (color := normalize_color(cv.string(value))) is None:
        raise vol.Invalid("expected a hex color such as #00679e")
    return color


COLOR_SCHEMA = {vol.Required(ATTR_COLOR): _hex_color}

INVITATION_SCHEMA = {
    vol.Required("uid"): cv.string,
    vol.Required(ATTR_RESPONSE): vol.In(tuple(PARTSTAT_BY_RESPONSE)),
}

CREATE_CALENDAR_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_NAME): cv.string,
        vol.Optional(ATTR_COMPONENTS): vol.All(
            cv.ensure_list, [_named((COMPONENT_EVENT, COMPONENT_TODO))]
        ),
    }
)

DELETE_CALENDAR_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_NAME): cv.string,
    }
)


def async_register_services(hass: HomeAssistant) -> None:
    """Register the services that act on an account rather than a calendar."""
    # Admin only: a plain service gets no permission check at all.
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_CREATE_CALENDAR,
        partial(_async_create_calendar, hass),
        schema=CREATE_CALENDAR_SCHEMA,
    )
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_DELETE_CALENDAR,
        partial(_async_delete_calendar, hass),
        schema=DELETE_CALENDAR_SCHEMA,
    )


def async_register_entity_services(platform: EntityPlatform) -> None:
    """Register the services that target one calendar entity."""
    platform.async_register_entity_service(
        SERVICE_SEARCH_EVENTS,
        SEARCH_SCHEMA,
        _async_search_events,
        supports_response=SupportsResponse.ONLY,
    )
    platform.async_register_entity_service(
        SERVICE_GET_FREE_BUSY,
        FREE_BUSY_SCHEMA,
        _async_get_free_busy,
        supports_response=SupportsResponse.ONLY,
    )
    # Not feature-gated: _require_writable names the option the user set.
    platform.async_register_entity_service(
        SERVICE_CREATE_EVENT, CREATE_EVENT_SCHEMA, _async_create_event
    )
    platform.async_register_entity_service(
        SERVICE_UPDATE_EVENT, UPDATE_EVENT_SCHEMA, _async_update_event
    )
    platform.async_register_entity_service(
        SERVICE_MOVE_EVENT, MOVE_EVENT_SCHEMA, _async_move_event
    )
    platform.async_register_entity_service(
        SERVICE_IMPORT_ICS, IMPORT_SCHEMA, _async_import_ics
    )
    platform.async_register_entity_service(
        SERVICE_EXPORT_ICS,
        EXPORT_SCHEMA,
        _async_export_ics,
        supports_response=SupportsResponse.ONLY,
    )
    platform.async_register_entity_service(
        SERVICE_SET_CALENDAR_COLOR, COLOR_SCHEMA, _async_set_color
    )
    platform.async_register_entity_service(
        SERVICE_RESPOND_TO_INVITATION, INVITATION_SCHEMA, _async_respond
    )


def _require_writable(entity: HaCaldavCalendarEntity) -> None:
    """Refuse a write the entity's own controls would not offer either."""
    if not entity.managed.writable:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="read_only",
            translation_placeholders={"name": entity.managed.name},
        )


async def _async_search_events(
    entity: HaCaldavCalendarEntity, call: ServiceCall
) -> ServiceResponse:
    """Search the calendar server-side and return what it matched."""
    criteria: dict[str, Any] = {call.data[ATTR_FIELD]: call.data[ATTR_TEXT]}
    if start := call.data.get("start"):
        criteria["start"] = start
    if end := call.data.get("end"):
        criteria["end"] = end
    _check_window(criteria.get("start"), criteria.get("end"))
    # Searched and parsed in one executor job: vobject parses on first access.
    events = await _async_account_job(
        entity.hass,
        partial(_found_events, entity.calendar, criteria),
        SERVICE_SEARCH_EVENTS,
    )
    return {"events": events}


def _found_events(calendar: Any, criteria: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the matches of a server-side search, mapped. Blocking."""
    events = []
    for item in calendar.search(event=True, **criteria):
        vevent = component_of(item, "vevent")
        if vevent is None or (event := to_event(vevent)) is None:
            continue
        events.append(
            {
                "uid": event.uid,
                "summary": event.summary,
                "start": event.start.isoformat(),
                "end": event.end.isoformat(),
                "description": event.description,
                "location": event.location,
                **read_extras(vevent),
            }
        )
    return events


async def _async_get_free_busy(
    entity: HaCaldavCalendarEntity, call: ServiceCall
) -> ServiceResponse:
    """Return the busy periods the server reports for a window."""
    _check_window(call.data["start"], call.data["end"])
    report = await _async_account_job(
        entity.hass,
        partial(entity.calendar.freebusy_request, call.data["start"], call.data["end"]),
        SERVICE_GET_FREE_BUSY,
    )
    return {"periods": _periods(report)}


def _periods(report: Any) -> list[dict[str, str]]:
    """Flatten the FREEBUSY lines of a VFREEBUSY into start/end pairs.

    A period's second half is an end or a duration, and icalendar hands back
    a bare value for one period and a list for several.
    """
    try:
        found = list(report.icalendar_instance.walk("VFREEBUSY"))
    except Exception as err:
        # "Nothing is busy" is the wrong answer for a report we could not read.
        raise as_reported(err, SERVICE_GET_FREE_BUSY) from err
    periods = []
    for component in found:
        entries = component.get("FREEBUSY")
        if entries is None:
            continue
        for entry in entries if isinstance(entries, list) else [entries]:
            value = getattr(entry, "dt", None)
            if not isinstance(value, tuple) or len(value) != 2:
                continue
            start, second = value
            end = second if isinstance(second, datetime) else start + second
            periods.append({"start": start.isoformat(), "end": end.isoformat()})
    return periods


async def _async_create_event(
    entity: HaCaldavCalendarEntity, call: ServiceCall
) -> None:
    """Create an event with the full set of properties."""
    _require_writable(entity)
    data = _event_fields(call.data)
    start, end = _span(call.data)
    _check_span(start, end)
    data["dtstart"] = start
    data["dtend"] = end
    await entity.async_create_full_event(data)


async def _async_update_event(
    entity: HaCaldavCalendarEntity, call: ServiceCall
) -> None:
    """Change any subset of an event's properties."""
    _require_writable(entity)
    data = _event_fields(call.data)
    start, end = _span(call.data)
    if start is not None and end is not None:
        _check_span(start, end)
    if start is not None:
        data["dtstart"] = start
    if end is not None:
        data["dtend"] = end
    await entity.async_update_full_event(
        call.data["uid"],
        data,
        call.data.get("recurrence_id"),
        call.data.get("recurrence_range"),
    )


def _span(data: Mapping[str, Any]) -> tuple[Any, Any]:
    """Return the start and end of a call, whichever pair of fields it used."""
    return (
        data.get("start_date_time", data.get("start_date")),
        data.get("end_date_time", data.get("end_date")),
    )


def _check_window(start: Any, end: Any) -> None:
    """Refuse a window that ends before it starts.

    A server answers one with nothing, which a free/busy caller reads as free.
    """
    if start is not None and end is not None and end < start:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="end_before_start"
        )


def _check_span(start: Any, end: Any) -> None:
    """Refuse a date paired with a datetime, and an end not after its start.

    RFC 5545 requires DTSTART and DTEND to share a value type, and 3.6.1
    wants a DATE end strictly later.
    """
    if isinstance(start, datetime) != isinstance(end, datetime):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="mixed_time_types"
        )
    if end <= start:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="end_before_start"
        )


def _event_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Return the event fields present in a call, extras included."""
    fields: dict[str, Any] = {}
    for key in ("summary", "description", "location", "rrule"):
        if key in data:
            fields[key] = data[key]
    for key in EXTRA_FIELDS:
        name = str(key)
        if name in data:
            fields[name] = data[name]
    return fields


async def _async_move_event(entity: HaCaldavCalendarEntity, call: ServiceCall) -> None:
    """Move an event to another calendar, on this account or another one."""
    _require_writable(entity)
    await _async_check_control(entity, call, call.data[ATTR_TARGET_ENTITY_ID])
    target = _managed_target(entity, call.data[ATTR_TARGET_ENTITY_ID])
    # By identity: two accounts on different servers may share a path, and on
    # Nextcloud every account has /personal/.
    if target is entity.managed:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="same_calendar",
            translation_placeholders={"name": target.name},
        )
    await _async_write(
        entity,
        partial(
            move_event,
            entity.calendar,
            target.calendar,
            call.data["uid"],
            call.data[ATTR_KEEP_ORIGINAL],
        ),
        SERVICE_MOVE_EVENT,
    )
    await target.coordinator.async_request_refresh()


async def _async_check_control(
    entity: HaCaldavCalendarEntity, call: ServiceCall, entity_id: str
) -> None:
    """Refuse a caller who may not control the entity being written to.

    Core checks this for the service target only, not for an entity named in
    a field.
    """
    if (user_id := call.context.user_id) is None:
        return
    user = await entity.hass.auth.async_get_user(user_id)
    if user is None:
        raise UnknownUser(
            context=call.context, entity_id=entity_id, permission=POLICY_CONTROL
        )
    if not user.permissions.check_entity(entity_id, POLICY_CONTROL):
        raise Unauthorized(
            context=call.context, entity_id=entity_id, permission=POLICY_CONTROL
        )


def _managed_target(entity: HaCaldavCalendarEntity, entity_id: str) -> ManagedCalendar:
    """Return the managed calendar behind a target entity id."""
    registry = er.async_get(entity.hass)
    record = registry.async_get(entity_id)
    if record is None or record.platform != DOMAIN:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_target",
            translation_placeholders={"entity_id": entity_id},
        )
    entry = entity.hass.config_entries.async_get_entry(record.config_entry_id or "")
    if entry is None or entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_target",
            translation_placeholders={"entity_id": entity_id},
        )
    for managed in entry.runtime_data.calendars:
        if calendar_unique_id(entry.entry_id, managed.calendar.url) != record.unique_id:
            continue
        if not managed.writable:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="read_only",
                translation_placeholders={"name": managed.name},
            )
        return managed
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="unknown_target",
        translation_placeholders={"entity_id": entity_id},
    )


async def _async_import_ics(entity: HaCaldavCalendarEntity, call: ServiceCall) -> None:
    _require_writable(entity)
    await _async_write(
        entity,
        partial(import_ics, entity.calendar, call.data[ATTR_ICS]),
        SERVICE_IMPORT_ICS,
    )


async def _async_export_ics(
    entity: HaCaldavCalendarEntity, call: ServiceCall
) -> ServiceResponse:
    ics = await _async_account_job(
        entity.hass,
        partial(export_ics, entity.calendar, call.data.get("uid")),
        SERVICE_EXPORT_ICS,
    )
    return {"ics": ics}


async def _async_set_color(entity: HaCaldavCalendarEntity, call: ServiceCall) -> None:
    _require_writable(entity)
    await _async_write(
        entity,
        partial(set_calendar_color, entity.calendar, call.data[ATTR_COLOR]),
        SERVICE_SET_CALENDAR_COLOR,
    )
    # Pushing a color releases the hold a local pick puts on the sync.
    entity.async_follow_server_color()
    await entity.colors.async_request_refresh()


async def _async_respond(entity: HaCaldavCalendarEntity, call: ServiceCall) -> None:
    _require_writable(entity)
    addresses = entity.runtime_data.address_set
    if not addresses:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_scheduling"
        )
    await _async_write(
        entity,
        partial(
            respond_to_invitation,
            entity.calendar,
            call.data["uid"],
            PARTSTAT_BY_RESPONSE[call.data[ATTR_RESPONSE]],
            addresses,
        ),
        SERVICE_RESPOND_TO_INVITATION,
        # The reply is a PUT, so the etag held for this event is stale.
        forget=("etags", (call.data["uid"],)),
    )


async def _async_create_calendar(hass: HomeAssistant, call: ServiceCall) -> None:
    entry = _loaded_entry(hass, call.data[ATTR_CONFIG_ENTRY_ID])
    name = call.data[ATTR_NAME]
    await _async_account_job(
        hass,
        partial(
            create_calendar,
            entry.runtime_data.client,
            name,
            call.data.get(ATTR_COMPONENTS),
        ),
        SERVICE_CREATE_CALENDAR,
    )
    # By name: the url of the new collection is not known until the account
    # is listed again, and _is_selected accepts either shape.
    selected = entry.options.get(CONF_CALENDARS)
    if selected is not None and name not in selected:
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_CALENDARS: [*selected, name]}
        )
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_delete_calendar(hass: HomeAssistant, call: ServiceCall) -> None:
    entry = _loaded_entry(hass, call.data[ATTR_CONFIG_ENTRY_ID])
    name = call.data[ATTR_NAME]
    matches = [item for item in entry.runtime_data.calendars if item.name == name]
    if not matches:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_calendar",
            translation_placeholders={"name": name},
        )
    if len(matches) > 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="ambiguous_calendar",
            translation_placeholders={"name": name},
        )
    managed = matches[0]
    await _async_account_job(
        hass, partial(delete_calendar, managed.calendar), SERVICE_DELETE_CALENDAR
    )
    _async_forget_entities(hass, entry, managed.calendar.url)
    # Both shapes: the flow writes keys and create_calendar writes a name.
    gone = {name, calendar_key(managed.calendar.url)}
    selected = entry.options.get(CONF_CALENDARS)
    if selected is not None and gone & set(selected):
        hass.config_entries.async_update_entry(
            entry,
            options={
                **entry.options,
                CONF_CALENDARS: [item for item in selected if item not in gone],
            },
        )
    await hass.config_entries.async_reload(entry.entry_id)


def _async_forget_entities(
    hass: HomeAssistant, entry: HaCaldavConfigEntry, url: Any
) -> None:
    """Remove the entities of a calendar that no longer exists."""
    registry = er.async_get(hass)
    for unique_id in (
        calendar_unique_id(entry.entry_id, url),
        todo_unique_id(entry.entry_id, url),
    ):
        for domain in ("calendar", "todo"):
            if entity_id := registry.async_get_entity_id(domain, DOMAIN, unique_id):
                registry.async_remove(entity_id)


def _loaded_entry(hass: HomeAssistant, entry_id: str) -> HaCaldavConfigEntry:
    entry = hass.config_entries.async_get_entry(entry_id)
    if (
        entry is None
        or entry.domain != DOMAIN
        or entry.state is not ConfigEntryState.LOADED
    ):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_account",
            translation_placeholders={"config_entry_id": entry_id},
        )
    return entry


async def _async_account_job(hass: HomeAssistant, job: partial, action: str) -> Any:
    try:
        return await hass.async_add_executor_job(job)
    except Exception as err:
        # As broad as the entity write path, and for the same reason.
        raise as_reported(err, action) from err


async def _async_write(
    entity: HaCaldavCalendarEntity,
    job: partial,
    action: str,
    forget: tuple[str, tuple[str, ...]] | None = None,
) -> None:
    """Run a write against the entity's own calendar, as the platform would."""
    await entity.async_write(job, action, forget=forget)
