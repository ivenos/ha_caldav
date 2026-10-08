"""Constants for the CalDAV integration."""

DOMAIN = "ha_caldav"

CONF_CALENDARS = "calendars"
CONF_DAYS = "days"
CONF_INCLUDE_ALL_DAY = "include_all_day"
CONF_READ_ONLY = "read_only"
CONF_CALENDAR_OPTIONS = "calendar_options"
CONF_CLIENT_CERT = "client_cert"
CONF_CLIENT_KEY = "client_key"
CONF_CA_BUNDLE = "ca_bundle"

DEFAULT_DAYS = 7
DEFAULT_INCLUDE_ALL_DAY = True
DEFAULT_READ_ONLY = False
DEFAULT_SCAN_INTERVAL = 15
DEFAULT_TIMEOUT = 30

RANGE_THIS_AND_FUTURE = "THISANDFUTURE"

COMPONENT_EVENT = "VEVENT"
COMPONENT_TODO = "VTODO"
COMPONENT_JOURNAL = "VJOURNAL"

# RFC 3744 privileges that grant writes; servers disagree on which one they
# report for a shared calendar, so any of them counts.
WRITE_PRIVILEGES = frozenset({"write", "write-content", "all", "bind"})

SERVICE_SEARCH_EVENTS = "search_events"
SERVICE_GET_FREE_BUSY = "get_free_busy"
SERVICE_GET_INVITATIONS = "get_invitations"
SERVICE_CREATE_EVENT = "create_event"
SERVICE_UPDATE_EVENT = "update_event"
SERVICE_DELETE_EVENT = "delete_event"
SERVICE_MOVE_EVENT = "move_event"
SERVICE_IMPORT_ICS = "import_ics"
SERVICE_EXPORT_ICS = "export_ics"
SERVICE_SET_CALENDAR_COLOR = "set_calendar_color"
SERVICE_CREATE_CALENDAR = "create_calendar"
SERVICE_DELETE_CALENDAR = "delete_calendar"
SERVICE_RESPOND_TO_INVITATION = "respond_to_invitation"
SERVICE_CREATE_TODO = "create_todo"
SERVICE_UPDATE_TODO = "update_todo"
SERVICE_SEARCH_TODOS = "search_todos"
SERVICE_MOVE_TODO = "move_todo"
SERVICE_CREATE_JOURNAL = "create_journal"
SERVICE_UPDATE_JOURNAL = "update_journal"
SERVICE_DELETE_JOURNAL = "delete_journal"
SERVICE_SEARCH_JOURNALS = "search_journals"
SERVICE_SHARE_CALENDAR = "share_calendar"
SERVICE_UNSHARE_CALENDAR = "unshare_calendar"
SERVICE_GET_CALENDAR_SHARES = "get_calendar_shares"

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_ALARMS = "alarms"
ATTR_ATTACHMENTS = "attachments"
ATTR_ATTENDEES = "attendees"
ATTR_CATEGORIES = "categories"
ATTR_CLASSIFICATION = "classification"
ATTR_PRIORITY = "priority"
ATTR_EVENT_STATUS = "status"
ATTR_TRANSPARENCY = "transparency"
ATTR_URL = "url"
ATTR_ORGANIZER = "organizer"
ATTR_RESPONSE = "response"
ATTR_TARGET_ENTITY_ID = "target_entity_id"
ATTR_KEEP_ORIGINAL = "keep_original"
ATTR_ICS = "ics"
ATTR_COLOR = "color"
ATTR_NAME = "name"
ATTR_COMPONENTS = "components"
ATTR_TEXT = "text"
ATTR_FIELD = "field"
ATTR_ITEM = "item"
ATTR_RENAME = "rename"
ATTR_PARENT = "parent"
ATTR_PARENT_UID = "parent_uid"
ATTR_PERCENT_COMPLETE = "percent_complete"
ATTR_TIME_ZONE = "time_zone"
ATTR_USER = "user"
ATTR_ACCESS = "access"

# What the upcoming event publishes beyond core's fields; kept out of the recorder.
EVENT_ATTRIBUTES = (
    ATTR_URL,
    ATTR_EVENT_STATUS,
    ATTR_TRANSPARENCY,
    ATTR_CLASSIFICATION,
    ATTR_PRIORITY,
    ATTR_CATEGORIES,
    ATTR_ATTENDEES,
    ATTR_ORGANIZER,
    ATTR_ALARMS,
    ATTR_ATTACHMENTS,
)

# Closed vocabularies from RFC 5545.
EVENT_STATUSES = ("TENTATIVE", "CONFIRMED", "CANCELLED")
EVENT_CLASSIFICATIONS = ("PUBLIC", "PRIVATE", "CONFIDENTIAL")
EVENT_TRANSPARENCIES = ("OPAQUE", "TRANSPARENT")
TODO_STATUSES = ("NEEDS-ACTION", "IN-PROCESS", "COMPLETED", "CANCELLED")
JOURNAL_STATUSES = ("DRAFT", "FINAL", "CANCELLED")
ALARM_ACTIONS = ("DISPLAY", "AUDIO", "EMAIL")
SHARE_ACCESS = ("read", "read_write")

PARTSTAT_BY_RESPONSE = {
    "accept": "ACCEPTED",
    "decline": "DECLINED",
    "tentative": "TENTATIVE",
}

SEARCH_FIELDS = (
    "summary",
    "description",
    "location",
    "category",
    "status",
    "uid",
)

TODO_SEARCH_FIELDS = (
    "summary",
    "description",
    "location",
    "category",
    "uid",
)

JOURNAL_SEARCH_FIELDS = ("summary", "description", "category", "uid")

# No RFC 5545 equivalent; Nextcloud Tasks and Apple Reminders both order by it.
SORT_ORDER_PROPERTY = "X-APPLE-SORT-ORDER"

EVENT_REMINDER = "ha_caldav_reminder"

ISSUE_BUILTIN_CALDAV = "builtin_caldav_conflict"
ISSUE_NO_SYNC_COLLECTION = "no_sync_collection"
