"""Services for the things CalDAV can do and the calendar platform cannot."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime
from functools import partial
from typing import TYPE_CHECKING, Any

from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.components.calendar import (
    DOMAIN as CALENDAR_DOMAIN,
    CalendarEntity,
    CalendarEvent,
)
from homeassistant.components.todo import DOMAIN as TODO_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HassJob,
    HassJobType,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError, Unauthorized, UnknownUser
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.entity_platform import async_get_platforms
from homeassistant.helpers.service import (
    async_register_admin_service,
    async_register_platform_entity_service,
    entity_service_call,
)
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .api import (
    attendee_free_busy,
    busy_periods,
    create_calendar,
    delete_calendar,
    export_ics,
    import_ics,
    move_event,
    move_todo,
    set_calendar_color,
)
from .color import normalize_color
from .connection import calendar_key
from .const import (
    ALARM_ACTIONS,
    ATTR_ACCESS,
    ATTR_ALARMS,
    ATTR_ATTACHMENTS,
    ATTR_ATTENDEES,
    ATTR_CATEGORIES,
    ATTR_CLASSIFICATION,
    ATTR_COLOR,
    ATTR_COMPONENTS,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_EVENT_STATUS,
    ATTR_FIELD,
    ATTR_ICS,
    ATTR_ITEM,
    ATTR_KEEP_ORIGINAL,
    ATTR_NAME,
    ATTR_ORGANIZER,
    ATTR_PARENT,
    ATTR_PARENT_UID,
    ATTR_PERCENT_COMPLETE,
    ATTR_PRIORITY,
    ATTR_RENAME,
    ATTR_RESPONSE,
    ATTR_TARGET_ENTITY_ID,
    ATTR_TEXT,
    ATTR_TIME_ZONE,
    ATTR_TRANSPARENCY,
    ATTR_URL,
    ATTR_USER,
    COMPONENT_EVENT,
    COMPONENT_JOURNAL,
    COMPONENT_TODO,
    CONF_CALENDAR_OPTIONS,
    CONF_CALENDARS,
    CONF_READ_ONLY,
    DOMAIN,
    EVENT_CLASSIFICATIONS,
    EVENT_STATUSES,
    EVENT_TRANSPARENCIES,
    JOURNAL_SEARCH_FIELDS,
    JOURNAL_STATUSES,
    PARTSTAT_BY_RESPONSE,
    RANGE_THIS_AND_FUTURE,
    SEARCH_FIELDS,
    SERVICE_CREATE_CALENDAR,
    SERVICE_CREATE_EVENT,
    SERVICE_CREATE_JOURNAL,
    SERVICE_CREATE_TODO,
    SERVICE_DELETE_CALENDAR,
    SERVICE_DELETE_EVENT,
    SERVICE_DELETE_JOURNAL,
    SERVICE_EXPORT_ICS,
    SERVICE_GET_CALENDAR_SHARES,
    SERVICE_GET_FREE_BUSY,
    SERVICE_GET_INVITATIONS,
    SERVICE_IMPORT_ICS,
    SERVICE_MOVE_EVENT,
    SERVICE_MOVE_TODO,
    SERVICE_RESPOND_TO_INVITATION,
    SERVICE_SEARCH_EVENTS,
    SERVICE_SEARCH_JOURNALS,
    SERVICE_SEARCH_TODOS,
    SERVICE_SET_CALENDAR_COLOR,
    SERVICE_SHARE_CALENDAR,
    SERVICE_UNSHARE_CALENDAR,
    SERVICE_UPDATE_EVENT,
    SERVICE_UPDATE_JOURNAL,
    SERVICE_UPDATE_TODO,
    SHARE_ACCESS,
    TODO_SEARCH_FIELDS,
    TODO_STATUSES,
)
from .coordinator import (
    HaCaldavConfigEntry,
    ManagedCalendar,
    calendar_unique_id,
    components_of,
    master_of,
    occurrences,
    sort_order,
    to_event,
    to_todo,
    todo_unique_id,
)
from .errors import as_reported
from .event import awaits_reply, read_extras
from .journal import create_journal, delete_journal, read_journals, update_journal
from .options import account_settings
from .recurrence import respond_to_invitation
from .sharing import read_shares, share_calendar, unshare_calendar
from .task import read_todo_extras, status_of

if TYPE_CHECKING:
    from .calendar import HaCaldavCalendarEntity
    from .entity import HaCaldavEntity
    from .todo import HaCaldavTodoListEntity


def _local_datetime(value: Any) -> datetime:
    """Return an aware datetime in Home Assistant's own timezone.

    The datetime selector sends a naive value, which caldav would resolve
    against the OS timezone, and a template renders an offset no zone is
    named after.
    """
    return dt_util.as_local(cv.datetime(value))


def _placed(value: Any, zone: Any) -> Any:
    """Return a time in the zone a call names, or in Home Assistant's own."""
    if not isinstance(value, datetime) or zone is None:
        return dt_util.as_local(value) if isinstance(value, datetime) else value
    return (
        value.replace(tzinfo=zone) if value.tzinfo is None else value.astimezone(zone)
    )


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
                vol.All(
                    vol.Schema(
                        {
                            vol.Exclusive("minutes_before", "trigger"): vol.Coerce(int),
                            vol.Exclusive("at", "trigger"): _local_datetime,
                            vol.Optional("action"): _named(ALARM_ACTIONS),
                            vol.Optional("related"): _named(("START", "END")),
                            vol.Optional("description"): cv.string,
                            vol.Optional("summary"): cv.string,
                            vol.Optional("attendees"): vol.All(
                                cv.ensure_list, [cv.string]
                            ),
                        }
                    ),
                    cv.has_at_least_one_key("minutes_before", "at"),
                ),
            )
        ],
    ),
    vol.Optional(ATTR_ATTACHMENTS): vol.All(
        cv.ensure_list,
        [
            vol.Any(
                cv.string,
                vol.All(
                    vol.Schema(
                        {
                            vol.Optional("url"): cv.string,
                            vol.Optional("path"): cv.string,
                            vol.Optional("name"): cv.string,
                            vol.Optional("media_type"): cv.string,
                            vol.Optional("size"): vol.Coerce(int),
                        }
                    ),
                    cv.has_at_least_one_key("url", "path", "name"),
                ),
            )
        ],
    ),
}

SEARCH_SCHEMA = {
    vol.Optional(ATTR_TEXT): cv.string,
    vol.Optional(ATTR_FIELD, default="summary"): vol.In(SEARCH_FIELDS),
    vol.Optional("start"): _local_datetime,
    vol.Optional("end"): _local_datetime,
}

FREE_BUSY_SCHEMA = {
    vol.Required("start"): _local_datetime,
    vol.Required("end"): _local_datetime,
    vol.Optional(ATTR_ATTENDEES): vol.All(cv.ensure_list, [cv.string]),
}

INVITATIONS_SCHEMA = {
    vol.Optional("start"): _local_datetime,
    vol.Optional("end"): _local_datetime,
}

# A field each: the datetime picker always sends a time, and RFC 5545 writes
# an all-day event as a DATE.
SPAN_FIELDS = {
    vol.Optional("start_date_time"): cv.datetime,
    vol.Optional("end_date_time"): cv.datetime,
    vol.Optional("start_date"): _date_only,
    vol.Optional("end_date"): _date_only,
    vol.Optional(ATTR_TIME_ZONE): cv.string,
}

_ONE_START = cv.has_at_most_one_key("start_date_time", "start_date")
_ONE_END = cv.has_at_most_one_key("end_date_time", "end_date")


def _blank_as_none(validator: Any) -> Any:
    """Return a field that reads as left out when a template renders it empty."""

    def validate(value: Any) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return validator(value)

    return validate


def _range_needs_occurrence(value: dict[str, Any]) -> dict[str, Any]:
    """Refuse a recurrence range with no occurrence for it to start at."""
    if value.get("recurrence_range") and value.get("recurrence_id") is None:
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
            vol.Optional("recurrence_id"): _blank_as_none(cv.string),
            vol.Optional("recurrence_range"): _blank_as_none(
                _named((RANGE_THIS_AND_FUTURE,))
            ),
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
        ATTR_TIME_ZONE,
        *(str(key) for key in EXTRA_FIELDS),
    ),
)

DELETE_EVENT_SCHEMA = vol.All(
    cv.make_entity_service_schema(
        {
            vol.Required("uid"): cv.string,
            vol.Optional("recurrence_id"): _blank_as_none(cv.string),
            vol.Optional("recurrence_range"): _blank_as_none(
                _named((RANGE_THIS_AND_FUTURE,))
            ),
        }
    ),
    _range_needs_occurrence,
)

MOVE_EVENT_SCHEMA = {
    vol.Required("uid"): cv.string,
    vol.Required(ATTR_TARGET_ENTITY_ID): cv.entity_id,
    vol.Optional(ATTR_KEEP_ORIGINAL, default=False): cv.boolean,
}

# Core's own to-do actions spell the two-word statuses with an underscore.
_TODO_STATUS = vol.All(
    vol.Lower,
    lambda value: value.replace("_", "-"),
    vol.In(tuple(status.lower() for status in TODO_STATUSES)),
    vol.Upper,
)

# The names todo.add_item and todo.update_item give these fields, so a call
# to either carries over.
TODO_FIELDS = {
    vol.Optional("due_date"): _blank_as_none(_date_only),
    vol.Optional("due_datetime"): _blank_as_none(_local_datetime),
    vol.Optional("start_date"): _blank_as_none(_date_only),
    vol.Optional("start_datetime"): _blank_as_none(_local_datetime),
    vol.Optional("description"): _blank_as_none(cv.string),
    vol.Optional(ATTR_PARENT): _blank_as_none(cv.string),
    vol.Optional("rrule"): _blank_as_none(cv.string),
    vol.Optional("status"): _TODO_STATUS,
    vol.Optional(ATTR_PERCENT_COMPLETE): vol.All(
        vol.Coerce(int), vol.Range(min=0, max=100)
    ),
    vol.Optional("location"): _blank_as_none(cv.string),
    **{
        key: validator
        for key, validator in EXTRA_FIELDS.items()
        if str(key) not in (ATTR_EVENT_STATUS, ATTR_TRANSPARENCY)
    },
}

_ONE_DUE = cv.has_at_most_one_key("due_date", "due_datetime")
_ONE_TODO_START = cv.has_at_most_one_key("start_date", "start_datetime")

CREATE_TODO_SCHEMA = vol.All(
    cv.make_entity_service_schema(
        {vol.Required(ATTR_ITEM): vol.All(cv.string, vol.Length(min=1)), **TODO_FIELDS}
    ),
    _ONE_DUE,
    _ONE_TODO_START,
)

UPDATE_TODO_SCHEMA = vol.All(
    cv.make_entity_service_schema(
        {
            vol.Required(ATTR_ITEM): vol.All(cv.string, vol.Length(min=1)),
            vol.Optional(ATTR_RENAME): vol.All(cv.string, vol.Length(min=1)),
            **TODO_FIELDS,
        }
    ),
    _ONE_DUE,
    _ONE_TODO_START,
    cv.has_at_least_one_key(ATTR_RENAME, *(str(key) for key in TODO_FIELDS)),
)

SEARCH_TODOS_SCHEMA = {
    vol.Optional(ATTR_TEXT): cv.string,
    vol.Optional(ATTR_FIELD, default="summary"): vol.In(TODO_SEARCH_FIELDS),
    vol.Optional("status"): vol.All(cv.ensure_list, [_TODO_STATUS]),
}

MOVE_TODO_SCHEMA = {
    vol.Required(ATTR_ITEM): vol.All(cv.string, vol.Length(min=1)),
    vol.Required(ATTR_TARGET_ENTITY_ID): cv.entity_id,
    vol.Optional(ATTR_KEEP_ORIGINAL, default=False): cv.boolean,
}

JOURNAL_FIELDS = {
    vol.Optional("description"): _blank_as_none(cv.string),
    vol.Optional("start_date"): _blank_as_none(_date_only),
    vol.Optional("start_date_time"): _blank_as_none(_local_datetime),
    vol.Optional(ATTR_EVENT_STATUS): _named(JOURNAL_STATUSES),
    **{
        key: validator
        for key, validator in EXTRA_FIELDS.items()
        if str(key)
        in (ATTR_URL, ATTR_CLASSIFICATION, ATTR_CATEGORIES, ATTR_ATTACHMENTS)
    },
}

_ONE_JOURNAL_START = cv.has_at_most_one_key("start_date", "start_date_time")
_NAMED = vol.All(cv.string, vol.Length(min=1))

CREATE_JOURNAL_SCHEMA = vol.All(
    cv.make_entity_service_schema({vol.Required("summary"): _NAMED, **JOURNAL_FIELDS}),
    _ONE_JOURNAL_START,
)

UPDATE_JOURNAL_SCHEMA = vol.All(
    cv.make_entity_service_schema(
        {
            vol.Required("uid"): cv.string,
            vol.Optional("summary"): _NAMED,
            **JOURNAL_FIELDS,
        }
    ),
    _ONE_JOURNAL_START,
    cv.has_at_least_one_key("summary", *(str(key) for key in JOURNAL_FIELDS)),
)

DELETE_JOURNAL_SCHEMA = {vol.Required("uid"): cv.string}

SEARCH_JOURNALS_SCHEMA = {
    vol.Optional(ATTR_TEXT): cv.string,
    vol.Optional(ATTR_FIELD, default="summary"): vol.In(JOURNAL_SEARCH_FIELDS),
    vol.Optional("start"): _local_datetime,
    vol.Optional("end"): _local_datetime,
}

SHARE_SCHEMA = {
    vol.Required(ATTR_USER): _NAMED,
    vol.Optional(ATTR_ACCESS, default="read"): vol.In(SHARE_ACCESS),
}

UNSHARE_SCHEMA = {vol.Required(ATTR_USER): _NAMED}

IMPORT_SCHEMA = {vol.Required(ATTR_ICS): cv.string}

EXPORT_SCHEMA = {vol.Optional("uid"): cv.string}


def _hex_color(value: Any) -> str:
    """Return a color in the one shape the read path recognizes again."""
    if (color := normalize_color(cv.string(value))) is None:
        raise vol.Invalid("expected a hex color such as #00679e")
    return color


COLOR_SCHEMA = {vol.Required(ATTR_COLOR): _hex_color}

INVITATION_SCHEMA = {
    vol.Required("uid"): cv.string,
    vol.Optional("recurrence_id"): _blank_as_none(cv.string),
    vol.Required(ATTR_RESPONSE): vol.In(tuple(PARTSTAT_BY_RESPONSE)),
}

CREATE_CALENDAR_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_NAME): cv.string,
        vol.Optional(ATTR_COMPONENTS): vol.All(
            cv.ensure_list,
            [_named((COMPONENT_EVENT, COMPONENT_TODO, COMPONENT_JOURNAL))],
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
    """Register the services, up front so they exist while an entry retries."""
    ONLY, NONE = SupportsResponse.ONLY, SupportsResponse.NONE
    cal, todo = CALENDAR_DOMAIN, TODO_DOMAIN
    # Not feature-gated: _require_writable names the option the user set.
    for name, schema, func, response, domain in (
        (SERVICE_SEARCH_EVENTS, SEARCH_SCHEMA, _async_search_events, ONLY, cal),
        (SERVICE_GET_FREE_BUSY, FREE_BUSY_SCHEMA, _async_get_free_busy, ONLY, cal),
        (
            SERVICE_GET_INVITATIONS,
            INVITATIONS_SCHEMA,
            _async_get_invitations,
            ONLY,
            cal,
        ),
        (SERVICE_CREATE_EVENT, CREATE_EVENT_SCHEMA, _async_create_event, NONE, cal),
        (SERVICE_UPDATE_EVENT, UPDATE_EVENT_SCHEMA, _async_update_event, NONE, cal),
        (SERVICE_DELETE_EVENT, DELETE_EVENT_SCHEMA, _async_delete_event, NONE, cal),
        (SERVICE_MOVE_EVENT, MOVE_EVENT_SCHEMA, _async_move_event, NONE, cal),
        (SERVICE_RESPOND_TO_INVITATION, INVITATION_SCHEMA, _async_respond, NONE, cal),
        (SERVICE_CREATE_TODO, CREATE_TODO_SCHEMA, _async_create_todo, NONE, todo),
        (SERVICE_UPDATE_TODO, UPDATE_TODO_SCHEMA, _async_update_todo, NONE, todo),
        (SERVICE_SEARCH_TODOS, SEARCH_TODOS_SCHEMA, _async_search_todos, ONLY, todo),
        (SERVICE_MOVE_TODO, MOVE_TODO_SCHEMA, _async_move_todo, NONE, todo),
    ):
        async_register_platform_entity_service(
            hass,
            DOMAIN,
            name,
            entity_domain=domain,
            func=func,
            schema=schema,
            supports_response=response,
        )
    for name, schema, func, response in (
        (SERVICE_IMPORT_ICS, IMPORT_SCHEMA, _async_import_ics, NONE),
        (SERVICE_EXPORT_ICS, EXPORT_SCHEMA, _async_export_ics, ONLY),
        (SERVICE_SET_CALENDAR_COLOR, COLOR_SCHEMA, _async_set_color, NONE),
        (SERVICE_CREATE_JOURNAL, CREATE_JOURNAL_SCHEMA, _async_create_journal, NONE),
        (SERVICE_UPDATE_JOURNAL, UPDATE_JOURNAL_SCHEMA, _async_update_journal, NONE),
        (SERVICE_DELETE_JOURNAL, DELETE_JOURNAL_SCHEMA, _async_delete_journal, NONE),
        (SERVICE_SEARCH_JOURNALS, SEARCH_JOURNALS_SCHEMA, _async_search_journals, ONLY),
        (SERVICE_SHARE_CALENDAR, SHARE_SCHEMA, _async_share_calendar, NONE),
        (SERVICE_UNSHARE_CALENDAR, UNSHARE_SCHEMA, _async_unshare_calendar, NONE),
        (SERVICE_GET_CALENDAR_SHARES, {}, _async_get_shares, ONLY),
    ):
        # The platform helper binds an action to the entities of one domain.
        hass.services.async_register(
            DOMAIN,
            name,
            partial(
                entity_service_call,
                hass,
                partial(_collection_entities, hass),
                HassJob(func),
            ),
            schema=schema
            if isinstance(schema, vol.All)
            else cv.make_entity_service_schema(schema),
            supports_response=response,
            job_type=HassJobType.Coroutinefunction,
        )
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


def _collection_entities(hass: HomeAssistant) -> dict[str, Any]:
    """Return the entity each collection is addressed by as a whole.

    Its calendar, or its to-do list where it holds no events. With both
    taken, a call naming an area or a label would run twice on one collection.
    """
    found: dict[str, Any] = {}
    for platform in async_get_platforms(hass, DOMAIN):
        for entity_id, entity in platform.entities.items():
            if (
                platform.domain == CALENDAR_DOMAIN
                or not entity.managed.capability.shows_calendar
            ):
                found[entity_id] = entity
    return found


def _require_writable(entity: HaCaldavEntity) -> None:
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
    criteria: dict[str, Any] = {}
    if text := call.data.get(ATTR_TEXT):
        criteria[call.data[ATTR_FIELD]] = text
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
    """Return the matches of a server-side search, mapped. Blocking.

    Within a window a series answers with its occurrences there, otherwise
    with its master.
    """
    window = "start" in criteria and "end" in criteria
    events = []
    for item in calendar.search(event=True, **criteria):
        vevents = (
            occurrences(item, criteria["start"], criteria["end"])
            if window
            else [master_of(item)]
        )
        for vevent in vevents:
            if vevent is not None and (event := to_event(vevent)) is not None:
                events.append(_event_entry(vevent, event))
    return events


def _event_entry(vevent: Any, event: CalendarEvent) -> dict[str, Any]:
    return {
        "uid": event.uid,
        "recurrence_id": event.recurrence_id,
        "summary": event.summary,
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
        "description": event.description,
        "location": event.location,
        **read_extras(vevent),
    }


async def _async_get_invitations(
    entity: HaCaldavCalendarEntity, call: ServiceCall
) -> ServiceResponse:
    """Return the events on a calendar that still wait for the account's answer."""
    if not (addresses := entity.addresses):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_scheduling"
        )
    start = call.data.get("start") or dt_util.now()
    _check_window(start, call.data.get("end"))
    invitations = await _async_account_job(
        entity.hass,
        partial(
            _found_invitations, entity.calendar, addresses, start, call.data.get("end")
        ),
        SERVICE_GET_INVITATIONS,
    )
    return {"invitations": invitations}


def _found_invitations(
    calendar: Any, addresses: list[str], start: datetime, end: datetime | None
) -> list[dict[str, Any]]:
    """Return the unanswered invitations from a moment on, mapped. Blocking.

    A series answers with its master, and with each exception that waits for
    an answer of its own.
    """
    window = {"start": start} if end is None else {"start": start, "end": end}
    invitations = []
    for item in calendar.search(event=True, **window):
        for vevent in components_of(item, "vevent"):
            if not awaits_reply(vevent, addresses):
                continue
            if (event := to_event(vevent)) is None:
                continue
            if event.recurrence_id and event.end_datetime_local <= start:
                continue
            invitations.append(_event_entry(vevent, event))
    return invitations


async def _async_get_free_busy(
    entity: HaCaldavCalendarEntity, call: ServiceCall
) -> ServiceResponse:
    """Return the busy periods the server reports for a window."""
    start, end = call.data["start"], call.data["end"]
    _check_window(start, end)
    attendees = call.data.get(ATTR_ATTENDEES)
    addresses = entity.runtime_data.address_set
    if attendees and not addresses:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_scheduling"
        )
    report = await _async_account_job(
        entity.hass,
        partial(entity.calendar.freebusy_request, start, end),
        SERVICE_GET_FREE_BUSY,
    )
    try:
        answer: dict[str, Any] = {"periods": busy_periods(report.icalendar_instance)}
    except Exception as err:
        # "Nothing is busy" is the wrong answer for a report we could not read.
        raise as_reported(err, SERVICE_GET_FREE_BUSY) from err
    if attendees:
        answer[ATTR_ATTENDEES] = await _async_account_job(
            entity.hass,
            partial(
                attendee_free_busy,
                entity.runtime_data.client,
                start,
                end,
                attendees,
                addresses,
            ),
            SERVICE_GET_FREE_BUSY,
        )
    return answer


async def _async_create_event(
    entity: HaCaldavCalendarEntity, call: ServiceCall
) -> None:
    """Create an event with the full set of properties."""
    _require_writable(entity)
    data = _event_fields(entity.hass, call.data)
    start, end = _span(call.data, await _async_zone(call.data))
    _check_span(start, end)
    data["dtstart"] = start
    data["dtend"] = end
    await entity.async_create_full_event(data)


async def _async_update_event(
    entity: HaCaldavCalendarEntity, call: ServiceCall
) -> None:
    """Change any subset of an event's properties."""
    _require_writable(entity)
    data = _event_fields(entity.hass, call.data)
    zone = await _async_zone(call.data)
    start, end = _span(call.data, zone)
    if start is not None and end is not None:
        _check_span(start, end)
    if start is not None:
        data["dtstart"] = start
    if end is not None:
        data["dtend"] = end
    if zone is not None:
        data[ATTR_TIME_ZONE] = zone
    await entity.async_update_full_event(
        call.data["uid"],
        data,
        call.data.get("recurrence_id"),
        call.data.get("recurrence_range"),
    )


async def _async_delete_event(
    entity: HaCaldavCalendarEntity, call: ServiceCall
) -> None:
    """Delete an event, one occurrence of it, or an occurrence onwards."""
    _require_writable(entity)
    await entity.async_delete_event(
        call.data["uid"],
        call.data.get("recurrence_id"),
        call.data.get("recurrence_range"),
        as_shown=False,
    )


async def _async_zone(data: Mapping[str, Any]) -> Any:
    """Return the zone a call names, loaded off the event loop."""
    if (name := data.get(ATTR_TIME_ZONE)) is None:
        return None
    if (zone := await dt_util.async_get_time_zone(name)) is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_time_zone",
            translation_placeholders={"time_zone": name},
        )
    return zone


def _span(data: Mapping[str, Any], zone: Any) -> tuple[Any, Any]:
    """Return the start and end of a call, whichever pair of fields it used."""
    return (
        _placed(data.get("start_date_time", data.get("start_date")), zone),
        _placed(data.get("end_date_time", data.get("end_date")), zone),
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


_CLEARABLE = ("description", "location", ATTR_URL, ATTR_ORGANIZER)


def _check_paths(hass: HomeAssistant, fields: Mapping[str, Any]) -> None:
    """Refuse to attach a file from outside the folders Home Assistant may read."""
    for attachment in fields.get(ATTR_ATTACHMENTS) or []:
        path = attachment.get("path") if isinstance(attachment, dict) else None
        if path and not hass.config.is_allowed_path(path):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="path_not_allowed",
                translation_placeholders={"path": path},
            )


def _event_fields(hass: HomeAssistant, data: Mapping[str, Any]) -> dict[str, Any]:
    """Return the event fields present in a call, extras included.

    An empty text clears its field: it is what a template renders for nothing.
    """
    _check_paths(hass, data)
    fields: dict[str, Any] = {}
    for key in ("summary", "description", "location", "rrule"):
        if key in data:
            fields[key] = data[key]
    for key in EXTRA_FIELDS:
        name = str(key)
        if name in data:
            fields[name] = data[name]
    for name in _CLEARABLE:
        if fields.get(name) == "":
            fields[name] = None
    return fields


async def _async_move_event(entity: HaCaldavCalendarEntity, call: ServiceCall) -> None:
    """Move an event to another calendar, on this account or another one."""
    if not call.data[ATTR_KEEP_ORIGINAL]:
        _require_writable(entity)
    await _async_check_control(entity, call, call.data[ATTR_TARGET_ENTITY_ID])
    target = _managed_target(
        entity, call.data[ATTR_TARGET_ENTITY_ID], calendar_unique_id, "unknown_target"
    )
    # By identity: two accounts on different servers may share a path, and on
    # Nextcloud every account has /personal/.
    if target is entity.managed:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="same_calendar",
            translation_placeholders={"name": target.name},
        )
    await entity.async_write(
        partial(
            move_event,
            entity.calendar,
            target.calendar,
            call.data["uid"],
            call.data[ATTR_KEEP_ORIGINAL],
        ),
        SERVICE_MOVE_EVENT,
    )
    await target.coordinator.async_refresh()


async def _async_check_control(
    entity: HaCaldavEntity, call: ServiceCall, entity_id: str
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


def _managed_target(
    entity: HaCaldavEntity,
    entity_id: str,
    unique_id: Callable[[str, object], str],
    unknown: str,
) -> ManagedCalendar:
    """Return the managed calendar behind a target entity id.

    The unique id says which of its two entities the target has to be.
    """
    registry = er.async_get(entity.hass)
    record = registry.async_get(entity_id)
    if record is None or record.platform != DOMAIN:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=unknown,
            translation_placeholders={"entity_id": entity_id},
        )
    entry = entity.hass.config_entries.async_get_entry(record.config_entry_id or "")
    if entry is None or entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=unknown,
            translation_placeholders={"entity_id": entity_id},
        )
    for managed in entry.runtime_data.calendars:
        if unique_id(entry.entry_id, managed.calendar.url) != record.unique_id:
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
        translation_key=unknown,
        translation_placeholders={"entity_id": entity_id},
    )


async def _async_create_todo(entity: HaCaldavTodoListEntity, call: ServiceCall) -> None:
    """Create a to-do item with the full set of properties."""
    _require_writable(entity)
    data = _todo_fields(entity, call.data)
    data["summary"] = call.data[ATTR_ITEM]
    await entity.async_create_full_item(
        {key: value for key, value in data.items() if value is not None}
    )


async def _async_update_todo(entity: HaCaldavTodoListEntity, call: ServiceCall) -> None:
    """Change any subset of a to-do item's properties."""
    _require_writable(entity)
    data = _todo_fields(entity, call.data)
    if ATTR_RENAME in call.data:
        data["summary"] = call.data[ATTR_RENAME]
    await entity.async_update_full_item(_item_uid(entity, call.data[ATTR_ITEM]), data)


def _item_uid(entity: HaCaldavTodoListEntity, item: str) -> str:
    """Return the uid of an item named by uid or by summary, as core names one.

    A name the list does not show is taken for the uid of an item it has
    yet to read.
    """
    items = entity.todo_items or []
    if any(found.uid == item for found in items):
        return item
    return next(
        (found.uid for found in items if found.summary == item and found.uid), item
    )


_TODO_DATES = {
    "due_date": "due",
    "due_datetime": "due",
    "start_date": "start",
    "start_datetime": "start",
}


def _todo_fields(
    entity: HaCaldavTodoListEntity, data: Mapping[str, Any]
) -> dict[str, Any]:
    """Return the to-do fields present in a call, as the write path names them.

    A field given as nothing clears it.
    """
    _check_paths(entity.hass, data)
    fields: dict[str, Any] = {}
    for key in TODO_FIELDS:
        name = str(key)
        if name not in data:
            continue
        if name == ATTR_PARENT:
            parent = data[name]
            fields[ATTR_PARENT_UID] = parent and _item_uid(entity, parent)
        else:
            fields[_TODO_DATES.get(name, name)] = data[name]
    for name in (ATTR_URL, ATTR_ORGANIZER):
        if fields.get(name) == "":
            fields[name] = None
    return fields


async def _async_search_todos(
    entity: HaCaldavTodoListEntity, call: ServiceCall
) -> ServiceResponse:
    """Return the items of the list with everything stored on them."""
    todos = await _async_account_job(
        entity.hass,
        partial(
            _found_todos,
            entity.calendar,
            call.data.get(ATTR_TEXT),
            call.data[ATTR_FIELD],
            call.data.get("status"),
        ),
        SERVICE_SEARCH_TODOS,
    )
    return {"todos": todos}


def _found_todos(
    calendar: Any, text: str | None, field: str, statuses: list[str] | None
) -> list[dict[str, Any]]:
    """Return the items of a list in the order it shows them, mapped. Blocking.

    Read whole and filtered here, the way the list itself is read.
    """
    found = []
    for item in calendar.search(todo=True, include_completed=True):
        vtodo = master_of(item, "vtodo")
        if vtodo is None or (shown := to_todo(vtodo)) is None:
            continue
        status = status_of(vtodo)
        if statuses and status not in statuses:
            continue
        todo: dict[str, Any] = {
            "uid": shown.uid,
            "summary": shown.summary,
            # Spelled the way todo.get_items spells it.
            "status": status.lower().replace("-", "_"),
        }
        for key, value in (("due", shown.due), ("completed", shown.completed)):
            if value is not None:
                todo[key] = value.isoformat()
        if shown.description:
            todo["description"] = shown.description
        todo.update(read_todo_extras(vtodo))
        if text and not _matches(todo, field, text):
            continue
        found.append((sort_order(vtodo), todo))
    return [todo for _, todo in sorted(found, key=lambda pair: pair[0])]


def _matches(todo: dict[str, Any], field: str, text: str) -> bool:
    if field == "uid":
        return todo["uid"] == text
    values = (
        todo.get(ATTR_CATEGORIES, [])
        if field == "category"
        else [todo.get(field) or ""]
    )
    return any(text.casefold() in str(value).casefold() for value in values)


async def _async_move_todo(entity: HaCaldavTodoListEntity, call: ServiceCall) -> None:
    """Move a to-do and its subtasks to another list, on this account or another."""
    if not call.data[ATTR_KEEP_ORIGINAL]:
        _require_writable(entity)
    await _async_check_control(entity, call, call.data[ATTR_TARGET_ENTITY_ID])
    target = _managed_target(
        entity, call.data[ATTR_TARGET_ENTITY_ID], todo_unique_id, "unknown_todo_list"
    )
    if target is entity.managed:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="same_todo_list",
            translation_placeholders={"name": target.name},
        )
    await entity.async_write(
        partial(
            move_todo,
            entity.calendar,
            target.calendar,
            _item_uid(entity, call.data[ATTR_ITEM]),
            call.data[ATTR_KEEP_ORIGINAL],
        ),
        SERVICE_MOVE_TODO,
        forget=entity.every_etag(),
    )
    await target.coordinator.async_refresh()


def _require_journals(entity: HaCaldavEntity) -> None:
    """Refuse a journal action on a calendar that holds no journal entries."""
    if not entity.managed.capability.supports_journals:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_journals",
            translation_placeholders={"name": entity.managed.name},
        )


def _journal_fields(hass: HomeAssistant, data: Mapping[str, Any]) -> dict[str, Any]:
    """Return the journal fields present in a call. One given as nothing is cleared."""
    _check_paths(hass, data)
    fields: dict[str, Any] = {}
    for key in JOURNAL_FIELDS:
        if (name := str(key)) in data:
            fields["start" if name.startswith("start_") else name] = data[name]
    if "summary" in data:
        fields["summary"] = data["summary"]
    if fields.get(ATTR_URL) == "":
        fields[ATTR_URL] = None
    return fields


async def _async_create_journal(entity: HaCaldavEntity, call: ServiceCall) -> None:
    _require_writable(entity)
    _require_journals(entity)
    data = _journal_fields(entity.hass, call.data)
    await entity.async_write(
        partial(
            create_journal,
            entity.calendar,
            {key: value for key, value in data.items() if value is not None},
        ),
        SERVICE_CREATE_JOURNAL,
    )


async def _async_update_journal(entity: HaCaldavEntity, call: ServiceCall) -> None:
    _require_writable(entity)
    _require_journals(entity)
    data = _journal_fields(entity.hass, call.data)
    await entity.async_write(
        partial(update_journal, entity.calendar, call.data["uid"], data),
        SERVICE_UPDATE_JOURNAL,
    )


async def _async_delete_journal(entity: HaCaldavEntity, call: ServiceCall) -> None:
    _require_writable(entity)
    _require_journals(entity)
    await entity.async_write(
        partial(delete_journal, entity.calendar, call.data["uid"]),
        SERVICE_DELETE_JOURNAL,
    )


async def _async_search_journals(
    entity: HaCaldavEntity, call: ServiceCall
) -> ServiceResponse:
    _require_journals(entity)
    start, end = call.data.get("start"), call.data.get("end")
    _check_window(start, end)
    journals = await _async_account_job(
        entity.hass,
        partial(read_journals, entity.calendar, start, end),
        SERVICE_SEARCH_JOURNALS,
    )
    if text := call.data.get(ATTR_TEXT):
        field = call.data[ATTR_FIELD]
        journals = [entry for entry in journals if _matches(entry, field, text)]
    return {"journals": journals}


async def _async_require_admin(hass: HomeAssistant, call: ServiceCall) -> None:
    """Refuse a caller who is not an administrator, as an admin service would."""
    if (user_id := call.context.user_id) is None:
        return
    user = await hass.auth.async_get_user(user_id)
    if user is None:
        raise UnknownUser(context=call.context)
    if not user.is_admin:
        raise Unauthorized(context=call.context)


async def _async_share_calendar(entity: HaCaldavEntity, call: ServiceCall) -> None:
    await _async_require_admin(entity.hass, call)
    _require_writable(entity)
    await _async_account_job(
        entity.hass,
        partial(
            share_calendar,
            entity.calendar,
            call.data[ATTR_USER],
            call.data[ATTR_ACCESS] == "read_write",
        ),
        SERVICE_SHARE_CALENDAR,
    )


async def _async_unshare_calendar(entity: HaCaldavEntity, call: ServiceCall) -> None:
    await _async_require_admin(entity.hass, call)
    _require_writable(entity)
    await _async_account_job(
        entity.hass,
        partial(unshare_calendar, entity.calendar, call.data[ATTR_USER]),
        SERVICE_UNSHARE_CALENDAR,
    )


async def _async_get_shares(
    entity: HaCaldavEntity, call: ServiceCall
) -> ServiceResponse:
    shares = await _async_account_job(
        entity.hass, partial(read_shares, entity.calendar), SERVICE_GET_CALENDAR_SHARES
    )
    return {"shares": shares}


async def _async_import_ics(entity: HaCaldavEntity, call: ServiceCall) -> None:
    _require_writable(entity)
    await entity.async_write(
        partial(import_ics, entity.calendar, call.data[ATTR_ICS]),
        SERVICE_IMPORT_ICS,
    )


async def _async_export_ics(
    entity: HaCaldavEntity, call: ServiceCall
) -> ServiceResponse:
    ics = await _async_account_job(
        entity.hass,
        partial(export_ics, entity.calendar, call.data.get("uid")),
        SERVICE_EXPORT_ICS,
    )
    return {"ics": ics}


async def _async_set_color(entity: HaCaldavEntity, call: ServiceCall) -> None:
    _require_writable(entity)
    await entity.async_write(
        partial(set_calendar_color, entity.calendar, call.data[ATTR_COLOR]),
        SERVICE_SET_CALENDAR_COLOR,
    )
    if isinstance(entity, CalendarEntity):
        # Pushing a color releases the hold a local pick puts on the sync.
        entity.async_follow_server_color()
    await entity.runtime_data.colors.async_request_refresh()


async def _async_respond(entity: HaCaldavCalendarEntity, call: ServiceCall) -> None:
    _require_writable(entity)
    addresses = entity.addresses
    if not addresses:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_scheduling"
        )
    await entity.async_write(
        partial(
            respond_to_invitation,
            entity.calendar,
            call.data["uid"],
            PARTSTAT_BY_RESPONSE[call.data[ATTR_RESPONSE]],
            addresses,
            call.data.get("recurrence_id"),
        ),
        SERVICE_RESPOND_TO_INVITATION,
        # The reply is a PUT, so the etag held for this event is stale.
        forget=("etags", (call.data["uid"],)),
    )


async def _async_create_calendar(hass: HomeAssistant, call: ServiceCall) -> None:
    entry = _loaded_entry(hass, call.data[ATTR_CONFIG_ENTRY_ID])
    if account_settings(entry)[CONF_READ_ONLY]:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="read_only",
            translation_placeholders={"name": entry.title},
        )
    created = await _async_account_job(
        hass,
        partial(
            create_calendar,
            entry.runtime_data.client,
            call.data[ATTR_NAME],
            call.data.get(ATTR_COMPONENTS),
        ),
        SERVICE_CREATE_CALENDAR,
    )
    # An empty selection is every calendar, the new one included.
    if selected := entry.options.get(CONF_CALENDARS):
        hass.config_entries.async_update_entry(
            entry,
            options={
                **entry.options,
                CONF_CALENDARS: [*selected, calendar_key(created.url)],
            },
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
    if not managed.writable:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="read_only",
            translation_placeholders={"name": managed.name},
        )
    key = calendar_key(managed.calendar.url)
    selected = entry.options.get(CONF_CALENDARS)
    remaining = [item for item in selected or [] if item != key]
    if selected and not remaining:
        # An empty selection reads as every calendar the account has.
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="last_selected_calendar",
            translation_placeholders={"name": managed.name},
        )
    await _async_account_job(
        hass, partial(delete_calendar, managed.calendar), SERVICE_DELETE_CALENDAR
    )
    _async_forget_entities(hass, entry, managed.calendar.url)
    options = {**entry.options}
    if selected and key in selected:
        options[CONF_CALENDARS] = remaining
    if key in (overrides := options.get(CONF_CALENDAR_OPTIONS, {})):
        options[CONF_CALENDAR_OPTIONS] = {
            item: value for item, value in overrides.items() if item != key
        }
    if options != entry.options:
        hass.config_entries.async_update_entry(entry, options=options)
    await hass.config_entries.async_reload(entry.entry_id)


def _async_forget_entities(
    hass: HomeAssistant, entry: HaCaldavConfigEntry, url: Any
) -> None:
    """Remove the entities of a calendar that no longer exists."""
    registry = er.async_get(hass)
    for domain, unique_id in (
        (CALENDAR_DOMAIN, calendar_unique_id(entry.entry_id, url)),
        (TODO_DOMAIN, todo_unique_id(entry.entry_id, url)),
    ):
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
