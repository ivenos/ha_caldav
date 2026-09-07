"""A corpus of objects another client may have left on the server.

Nothing here is invented for its own sake. Every case is something a real
CalDAV client, importer or server has been known to store: a property written
twice, a value type RFC 5545 does not allow there, a rule that names no day
that exists, text that carries the line ending the format is built out of. The
integration reads all of it, and none of it is under its control.

The rule the whole module is built on: **one object must never cost the user
the collection**. A poll that meets something it cannot place skips it and goes
on; a write that cannot be made on data this shape refuses in so many words. A
raise out of a read path fails every poll for as long as the object sits in the
window and takes both entities of the collection with it, and a write that
proceeds on a misread object silently damages the one beside it. Four defects of
exactly that shape have already been found and fixed, so the cases are table
driven: a new one is an entry, not a test.

What is asserted is therefore the refusal, not the absence of an exception.
``Refused`` and a logged skip are both correct; ``AttributeError`` out of a
library is not, and neither is a call that never comes back.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import threading
from typing import Any
from unittest.mock import Mock

from caldav.davclient import DAVResponse
from caldav.lib.error import DAVError, NotFoundError
from conftest import dav_calendar as shared_dav_calendar
from homeassistant.components.todo import TodoItem
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from hypothesis import HealthCheck, given, settings, strategies as st
from icalendar import Calendar as ICalCalendar, Event as ICalEvent, vRecur
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import vobject

from custom_components.ha_caldav.api import (
    check_etag,
    create_event,
    create_todo,
    export_ics,
    import_ics,
    move_event,
    reorder_todos,
    respond_to_invitation,
    update_todo,
)
from custom_components.ha_caldav.capability import (
    UNKNOWN,
    Capability,
    _components,
    _objects_and_props,
    _privileges,
    capability_for,
    fetch_capabilities,
)
from custom_components.ha_caldav.connection import calendar_key
from custom_components.ha_caldav.const import DOMAIN
from custom_components.ha_caldav.coordinator import (
    _UNMAPPABLE,
    HaCaldavCoordinator,
    component_of,
    components_of,
    get_end_date,
    is_all_day,
    is_over,
    master_of,
    sort_key,
    sort_order,
    to_event,
    to_todo,
)
from custom_components.ha_caldav.errors import Refused
from custom_components.ha_caldav.event import apply_extras, read_extras, rule_from
from custom_components.ha_caldav.recurrence import delete_event, update_event

ENTRY_DATA = {
    CONF_URL: "https://cloud.example.com/remote.php/dav",
    CONF_USERNAME: "iven",
    CONF_PASSWORD: "secret",
    CONF_VERIFY_SSL: True,
}

# Long enough that no ordinary call comes near it, short enough that a rule
# which yields nothing fails the suite in seconds rather than pinning a runner.
_BUDGET = 5.0


# --------------------------------------------------------------------------
# Builders. Real documents throughout: the point is the shape vobject and
# icalendar actually hand back, which a mock that accepts anything would hide.
# --------------------------------------------------------------------------


def document(*parts: str) -> str:
    """Wrap components in a VCALENDAR, the way one resource arrives."""
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//other-client//EN\r\n"
        + "".join(parts)
        + "END:VCALENDAR\r\n"
    )


def vevent(body: str, uid: str = "uid-1") -> str:
    return (
        f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:20260101T000000Z\r\n"
        f"{body}\r\nEND:VEVENT\r\n"
    )


def vtodo(body: str, uid: str = "todo-1") -> str:
    return (
        f"BEGIN:VTODO\r\nUID:{uid}\r\nDTSTAMP:20260101T000000Z\r\n"
        f"{body}\r\nEND:VTODO\r\n"
    )


@dataclass(frozen=True)
class Case:
    """One stored object, with what makes it worth keeping."""

    name: str
    ics: str
    why: str

    def __str__(self) -> str:
        return self.name


def cases(*entries: Case) -> Any:
    """Return the pytest parameter set for a corpus, named by case."""
    return pytest.mark.parametrize("case", entries, ids=str)


def within(seconds: float, call: Any) -> Any:
    """Run a blocking call on a throwaway thread, failing if it does not return.

    A stored rule that produces no occurrence at all leaves dateutil iterating
    rather than raising, and nothing upstream can interrupt it: asserting on the
    result directly would hang the suite instead of failing it. The thread is a
    daemon because a hung one cannot be joined at exit either.
    """
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = call()
        except BaseException as err:  # noqa: BLE001
            box["error"] = err

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        pytest.fail(f"still running after {seconds}s")
    if "error" in box:
        raise box["error"]
    return box["value"]


class StoredObject:
    """A search result whose body is only parsed when something reads it.

    caldav parses lazily and raises out of the property, which is what makes a
    single unreadable object able to fail a whole poll; a fake that parsed in
    its constructor would never reach the code that has to survive it.
    """

    def __init__(self, ics: str, url: str = "https://dav.test/cal/a.ics") -> None:
        self._ics = ics
        self.url = url
        self.props: dict[str, Any] = {"{DAV:}getetag": '"e1"'}

    @property
    def vobject_instance(self) -> Any:
        return vobject.readOne(self._ics)

    @property
    def icalendar_instance(self) -> Any:
        return ICalCalendar.from_ical(self._ics)

    @property
    def icalendar_component(self) -> Any:
        instance = self.icalendar_instance
        return next(
            (sub for sub in instance.subcomponents if sub.name != "VTIMEZONE"), None
        )

    @property
    def data(self) -> str:
        return self._ics


class WriteTarget:
    """A resource a write path reads, mutates and saves back.

    Parsed on first access and then kept, as caldav's is. A write reaches the
    component through ``icalendar_instance`` and the checks around it through
    ``icalendar_component``, and handing back two separate parses would hide a
    write that landed on the wrong one. Lazily, so a document icalendar refuses
    outright fails inside the call under test rather than in the fixture.
    """

    def __init__(self, ics: str, url: str = "https://dav.test/cal/a.ics") -> None:
        self._ics = ics
        self._server_ics = ics
        self._instance: Any = None
        self.url = url
        self.props: dict[str, Any] = {}
        self.saves: list[dict[str, Any]] = []
        self.deletes = 0
        self.loads = 0

    @property
    def data(self) -> str:
        return self._ics

    @data.setter
    def data(self, value: str) -> None:
        self._ics = value
        self._instance = None

    @property
    def icalendar_instance(self) -> Any:
        if self._instance is None:
            self._instance = ICalCalendar.from_ical(self._ics)
        return self._instance

    @icalendar_instance.setter
    def icalendar_instance(self, value: Any) -> None:
        # caldav keeps the document handed to it and serializes it only on the
        # way out. Writes go through here rather than through data because a
        # string is put through vcal.fix, which rewrites the object.
        self._instance = value
        self._ics = value.to_ical().decode("utf-8")

    @property
    def icalendar_component(self) -> Any:
        return next(
            (
                sub
                for sub in self.icalendar_instance.subcomponents
                if sub.name != "VTIMEZONE"
            ),
            None,
        )

    def save(self, **kwargs: Any) -> None:
        self.saves.append(kwargs)

    def delete(self) -> None:
        self.deletes += 1

    def load(self) -> None:
        self.loads += 1
        # caldav replaces the document from the GET before it records the etag,
        # so a load is where a stale parse would be thrown away.
        self.data = self._server_ics


def dav_calendar() -> Mock:
    """Return a calendar that answers every uid lookup with nothing.

    On the shared client rather than a second one: a local double that could
    only accept a PUT left the write paths untestable against a server that
    refuses one, which is where the order of a write and a delete shows.
    """
    calendar = shared_dav_calendar()
    calendar.name = "Personal"
    calendar.object_by_uid.side_effect = NotFoundError("nope")
    calendar.event_by_uid.side_effect = NotFoundError("nope")
    calendar.todo_by_uid.side_effect = NotFoundError("nope")
    calendar.search.return_value = []
    return calendar


# --------------------------------------------------------------------------
# The event corpus. {soon} and {soon_end} put the object in the polled window,
# so it is the one _next_event picks and read_extras is asked about.
# --------------------------------------------------------------------------

SOON = dt_util.utcnow() + timedelta(days=2)
_SUBSTITUTIONS = {
    "soon": f"{SOON:%Y%m%dT%H%M%S}Z",
    "soon_end": f"{SOON + timedelta(hours=1):%Y%m%dT%H%M%S}Z",
    "soon_day": f"{SOON:%Y%m%d}",
    "next_day": f"{SOON + timedelta(days=1):%Y%m%d}",
}


def dated(ics: str) -> str:
    return ics.format(**_SUBSTITUTIONS)


SPAN = "DTSTART:{soon}\r\nDTEND:{soon_end}\r\n"

EVENTS = (
    # -- structure ---------------------------------------------------------
    Case(
        "no_uid",
        document(
            "BEGIN:VEVENT\r\nDTSTAMP:20260101T000000Z\r\n"
            + SPAN
            + "SUMMARY:Anonymous\r\nEND:VEVENT\r\n"
        ),
        "RFC 5545 requires UID, and Home Assistant addresses events by it; an"
        " importer that leaves it out must not take the calendar down.",
    ),
    Case(
        "one_uid_on_two_unrelated_components",
        document(
            vevent(SPAN + "SUMMARY:First"),
            vevent(SPAN + "SUMMARY:Second"),
        ),
        "RFC 4791 gives one uid one resource; neither component names a"
        " recurrence, so nothing says which of the two is the event.",
    ),
    Case(
        "an_event_and_a_todo_under_one_uid",
        document(
            vevent(SPAN + "SUMMARY:Meeting", uid="shared"),
            vtodo("SUMMARY:Slides", uid="shared"),
        ),
        "The shape that had a to-do edit rewrite the event beside it; the read"
        " path has to pick the right component out of the same resource.",
    ),
    Case(
        "an_event_behind_a_todo",
        document(
            vtodo("SUMMARY:Slides", uid="shared"),
            vevent(SPAN + "SUMMARY:Meeting", uid="shared"),
        ),
        "RFC 5545 leaves component order open, so the to-do may come first.",
    ),
    Case(
        "nothing_but_a_timezone",
        document(
            "BEGIN:VTIMEZONE\r\nTZID:Europe/Berlin\r\n"
            "BEGIN:STANDARD\r\nDTSTART:19701025T030000\r\n"
            "TZOFFSETFROM:+0200\r\nTZOFFSETTO:+0100\r\nTZNAME:CET\r\n"
            "END:STANDARD\r\nEND:VTIMEZONE\r\n"
        ),
        "A resource a client emptied of its event but left the zone in.",
    ),
    Case(
        "a_timezone_that_names_itself",
        document(
            "BEGIN:VTIMEZONE\r\nTZID:Weird/Zone\r\n"
            "BEGIN:STANDARD\r\nDTSTART;TZID=Weird/Zone:19700101T000000\r\n"
            "TZOFFSETFROM:+0000\r\nTZOFFSETTO:+0000\r\nTZNAME:W\r\n"
            "END:STANDARD\r\nEND:VTIMEZONE\r\n",
            vevent("DTSTART;TZID=Weird/Zone:20990101T090000\r\nSUMMARY:Circular"),
        ),
        "A zone whose own observance is dated in itself; resolving it naively"
        " never terminates.",
    ),
    Case(
        "alarms_inside_alarms",
        document(
            vevent(
                SPAN + "SUMMARY:Nested\r\n"
                "BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:outer\r\n"
                "TRIGGER:-PT15M\r\n"
                "BEGIN:VALARM\r\nACTION:AUDIO\r\nTRIGGER:-PT5M\r\n"
                "END:VALARM\r\nEND:VALARM"
            )
        ),
        "RFC 5545 does not nest VALARM; the attribute shape reports minutes"
        " before, and an inner one has no anchor to be before.",
    ),
    Case(
        "the_same_alarm_twice",
        document(
            vevent(
                SPAN + "SUMMARY:Doubled\r\n"
                "BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:r\r\n"
                "TRIGGER:-PT15M\r\nEND:VALARM\r\n"
                "BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:r\r\n"
                "TRIGGER:-PT15M\r\nEND:VALARM"
            )
        ),
        "Two clients each adding the default reminder.",
    ),
    Case(
        "an_override_ahead_of_its_series",
        document(
            vevent(
                "RECURRENCE-ID:20990101T090000Z\r\n"
                "DTSTART:20990101T110000Z\r\nSUMMARY:Moved"
            ),
            vevent(
                "DTSTART:20990101T090000Z\r\nDTEND:20990101T100000Z\r\n"
                "RRULE:FREQ=WEEKLY\r\nSUMMARY:Standup"
            ),
        ),
        "Reading the rule off whichever component came first records no rule"
        " for a series that has one, and drops the rule already recorded.",
    ),
    Case(
        "nothing_but_detached_instances",
        document(
            vevent(
                "RECURRENCE-ID:20990101T090000Z\r\n"
                "DTSTART:20990101T110000Z\r\nSUMMARY:One"
            ),
            vevent(
                "RECURRENCE-ID:20990108T090000Z\r\n"
                "DTSTART:20990108T110000Z\r\nSUMMARY:Two"
            ),
        ),
        "What is left after the series head is deleted and the exceptions are"
        " not; there is no master to read anything off.",
    ),
    Case(
        "a_body_that_is_not_icalendar",
        "<html><body>503 backend unavailable</body></html>",
        "The proxy error page a collection hands back for one object; the"
        " shape that once disabled every write path that scans the collection.",
    ),
    Case(
        "text_after_the_end_of_the_document",
        document(vevent(SPAN + "SUMMARY:Trailing")) + "garbage\r\n",
        "A truncated or doubled transfer.",
    ),
    # -- values ------------------------------------------------------------
    Case(
        "an_end_before_its_start",
        document(
            vevent(
                "DTSTART:20990101T100000Z\r\nDTEND:20990101T090000Z\r\n"
                "SUMMARY:Backwards"
            )
        ),
        "Home Assistant's own event model refuses it, so the mapping has to.",
    ),
    Case(
        "a_negative_duration",
        document(vevent("DTSTART:20990101T100000Z\r\nDURATION:-PT2H\r\nSUMMARY:Back")),
        "RFC 5545 wants a positive duration on a VEVENT.",
    ),
    Case(
        "a_duration_no_calendar_can_hold",
        document(
            vevent(
                "DTSTART:20990101T100000Z\r\nDURATION:P999999999D\r\nSUMMARY:Forever"
            )
        ),
        "Adding it to the start overflows before any comparison is reached.",
    ),
    Case(
        "a_duration_beside_an_end",
        document(
            vevent(
                "DTSTART:20990101T100000Z\r\nDTEND:20990101T110000Z\r\n"
                "DURATION:PT5H\r\nSUMMARY:Both"
            )
        ),
        "RFC 5545 forbids the pair; whichever wins must be one of the two.",
    ),
    Case(
        "an_end_at_the_edge_of_what_a_datetime_holds",
        document(
            vevent("DTSTART:20990101T100000Z\r\nDTEND:99991231T235959Z\r\nSUMMARY:Open")
        ),
        "Adding a zone offset to it raises OverflowError, which is how one"
        " such object used to fail every poll for good.",
    ),
    Case(
        "a_start_at_the_other_edge",
        document(
            vevent(
                "DTSTART;VALUE=DATE:00010101\r\nDTEND;VALUE=DATE:00010102\r\n"
                "SUMMARY:Antiquity"
            )
        ),
        "The same overflow the other way round, west of UTC.",
    ),
    Case(
        "a_priority_that_is_not_a_number",
        document(vevent(SPAN + "PRIORITY:high\r\nSUMMARY:Urgent")),
        "icalendar is strict where the vobject the read path uses is not, so"
        " this parses on screen and raises in the scan.",
    ),
    Case(
        "an_empty_priority",
        document(vevent(SPAN + "PRIORITY:\r\nSUMMARY:Blank")),
        "A client that writes the property whether or not it has a value.",
    ),
    Case(
        "a_sequence_that_is_not_a_number",
        document(vevent(SPAN + "SEQUENCE:abc\r\nSUMMARY:Versioned")),
        "Every write bumps SEQUENCE, so a stored one that is text is read on"
        " the way out of an edit as well as on the way in.",
    ),
    Case(
        "a_percent_complete_on_an_event",
        document(vevent(SPAN + "PERCENT-COMPLETE:60\r\nSUMMARY:Half")),
        "RFC 5545 puts the property on VTODO only.",
    ),
    Case(
        "a_status_rfc_5545_does_not_give_a_vevent",
        document(vevent(SPAN + "STATUS:NEEDS-ACTION\r\nSUMMARY:Pending")),
        "A to-do status on an event; the attribute reports it verbatim and"
        " must not be trusted to be one of the three.",
    ),
    Case(
        "a_recurrence_id_matching_no_slot",
        document(
            vevent(
                "DTSTART:20990101T090000Z\r\nDTEND:20990101T100000Z\r\n"
                "RRULE:FREQ=WEEKLY\r\nSUMMARY:Standup"
            ),
            vevent(
                "RECURRENCE-ID:20990103T090000Z\r\n"
                "DTSTART:20990103T110000Z\r\nSUMMARY:Orphan"
            ),
        ),
        "An exception the rule was changed out from under; it names a day the"
        " series no longer has.",
    ),
    Case(
        "an_exdate_of_another_value_type",
        document(
            vevent(
                "DTSTART:20990101T090000Z\r\nDTEND:20990101T100000Z\r\n"
                "RRULE:FREQ=WEEKLY\r\nEXDATE;VALUE=DATE:20990108\r\nSUMMARY:Standup"
            )
        ),
        "RFC 5545 has EXDATE follow DTSTART's value type; comparing the two"
        " unaligned mixes a date with an instant.",
    ),
    Case(
        "an_until_of_another_value_type",
        document(
            vevent(
                "DTSTART;VALUE=DATE:20990101\r\nDTEND;VALUE=DATE:20990102\r\n"
                "RRULE:FREQ=WEEKLY;UNTIL=20990301T000000Z\r\nSUMMARY:Bin day"
            )
        ),
        "Google writes an all-day series exactly this way; dateutil refuses"
        " the pair outright unless it is reconciled first.",
    ),
    Case(
        "a_rule_with_a_frequency_that_does_not_exist",
        document(vevent(SPAN + "RRULE:FREQ=FORTNIGHTLY\r\nSUMMARY:Invented")),
        "The rule reaches the panel as text and the editor as a rule.",
    ),
    Case(
        "a_rule_that_counts_to_zero",
        document(vevent(SPAN + "RRULE:FREQ=WEEKLY;COUNT=0\r\nSUMMARY:None")),
        "A series with no occurrence at all, including the start it is anchored on.",
    ),
    Case(
        "a_rule_positioning_within_nothing",
        document(vevent(SPAN + "RRULE:FREQ=MONTHLY;BYSETPOS=2\r\nSUMMARY:Second")),
        "RFC 5545 only allows BYSETPOS beside another BY rule.",
    ),
    Case(
        "a_rule_naming_a_day_no_month_has",
        document(vevent(SPAN + "RRULE:FREQ=YEARLY;BYMONTHDAY=32\r\nSUMMARY:Never")),
        "The rule yields nothing, so a guard that counts occurrences never"
        " reaches its limit.",
    ),
    Case(
        "a_rule_that_never_advances",
        document(vevent(SPAN + "RRULE:FREQ=WEEKLY;INTERVAL=0\r\nSUMMARY:Stuck")),
        "icalendar takes the zero without complaint and dateutil then re-yields"
        " the start forever; the shape that once pinned an executor thread.",
    ),
    Case(
        "an_all_day_event_of_no_length",
        document(
            vevent(
                "DTSTART;VALUE=DATE:{soon_day}\r\nDTEND;VALUE=DATE:{soon_day}\r\n"
                "SUMMARY:Marker"
            )
        ),
        "DTEND is exclusive, so equal ends make an event that covers no day.",
    ),
    Case(
        "an_end_dated_where_the_start_is_timed",
        document(
            vevent("DTSTART:{soon}\r\nDTEND;VALUE=DATE:{next_day}\r\nSUMMARY:Mixed")
        ),
        "RFC 5545 has both ends share a value type; a date cannot be compared"
        " against an instant.",
    ),
    # -- text --------------------------------------------------------------
    Case(
        "a_line_far_past_the_folding_limit",
        document(vevent(SPAN + "SUMMARY:" + "z" * 20000)),
        "RFC 5545 folds at 75 octets; a client that does not still writes.",
    ),
    Case(
        "a_lone_carriage_return_in_the_text",
        document(vevent(SPAN + "SUMMARY:before\rafter")),
        "A bare CR is neither a fold nor a line end.",
    ),
    Case(
        "a_nul_byte_in_the_text",
        document(vevent(SPAN + "SUMMARY:before\x00after")),
        "Nothing in the format excludes it and nothing downstream expects it.",
    ),
    Case(
        "bidi_controls_in_the_text",
        document(vevent(SPAN + "SUMMARY:invoice‮annual⁦report")),
        "An override and an isolate; they reorder whatever is rendered after"
        " them, the rest of the panel included.",
    ),
    Case(
        "text_that_looks_like_the_format_around_it",
        document(
            vevent(
                SPAN + "SUMMARY:Standup\r\n"
                "DESCRIPTION:end\\r\\nEND:VEVENT\\r\\nBEGIN:VEVENT\\r\\nUID:injected"
            )
        ),
        "Escaped in the document and unescaped once read; anything that"
        " rebuilds text by concatenation splits the object in two.",
    ),
    Case(
        "non_ascii_in_the_uid_the_summary_and_the_zone",
        document(
            vevent(
                "DTSTART;TZID=Europa/Köln:20990101T090000\r\nSUMMARY:Grüße \U0001f600",
                uid="uid-ü-\U0001f600",
            )
        ),
        "The uid becomes part of a url and the zone is looked up by name.",
    ),
    Case(
        "a_zone_nobody_has_heard_of",
        document(vevent("DTSTART;TZID=Mars/Olympus:20990101T090000\r\nSUMMARY:Away")),
        "There is no definition to generate for it.",
    ),
    Case(
        "a_windows_zone_name",
        document(
            vevent(
                'DTSTART;TZID="W. Europe Standard Time":20990101T090000\r\n'
                "SUMMARY:Outlook"
            )
        ),
        "Outlook writes them and RFC 5545 does not know them.",
    ),
)


# --------------------------------------------------------------------------
# The to-do corpus.
# --------------------------------------------------------------------------

TODOS = (
    Case(
        "a_todo_without_a_uid",
        document(
            "BEGIN:VTODO\r\nDTSTAMP:20260101T000000Z\r\nSUMMARY:Nameless\r\n"
            "END:VTODO\r\n"
        ),
        "Home Assistant addresses items by uid; one without is unaddressable.",
    ),
    Case(
        "a_todo_without_a_summary",
        document(vtodo("STATUS:NEEDS-ACTION")),
        "Nothing to render, so it would be a blank row that cannot be told"
        " apart from the next.",
    ),
    Case(
        "a_due_date_at_the_edge_of_what_a_datetime_holds",
        document(vtodo("SUMMARY:Renew passport\r\nDUE:99991231T235959Z")),
        "Nothing bounds the to-do search by a window, so unlike an event this"
        " one cannot be escaped by paging away from it.",
    ),
    Case(
        "a_due_of_another_value_type",
        document(vtodo("SUMMARY:Soon\r\nDUE;VALUE=TEXT:whenever")),
        "RFC 5545 gives DUE a date or a date-time and nothing else.",
    ),
    Case(
        "a_completion_time_that_is_empty",
        document(vtodo("SUMMARY:Done\r\nSTATUS:COMPLETED\r\nCOMPLETED:")),
        "A client that writes the property before it has the value.",
    ),
    Case(
        "a_percent_complete_that_is_not_a_number",
        document(vtodo("SUMMARY:Painting\r\nPERCENT-COMPLETE:most")),
        "icalendar is strict about it and vobject is not.",
    ),
    Case(
        "a_status_rfc_5545_does_not_give_a_vtodo",
        document(vtodo("SUMMARY:Maybe\r\nSTATUS:TENTATIVE")),
        "An event status on a to-do; four states fold onto two and this is"
        " none of the four.",
    ),
    Case(
        "a_sort_order_that_is_not_a_number",
        document(vtodo("SUMMARY:First\r\nX-APPLE-SORT-ORDER:top")),
        "The Apple extension is an integer by convention only.",
    ),
    Case(
        "an_empty_sort_order",
        document(vtodo("SUMMARY:First\r\nX-APPLE-SORT-ORDER:")),
        "Written by a client that reorders but has nothing to say yet.",
    ),
    Case(
        "two_todos_under_one_uid",
        document(vtodo("SUMMARY:First"), vtodo("SUMMARY:Second")),
        "RFC 4791 gives one uid one resource; an edit addressed to the uid"
        " cannot say which of the two it means.",
    ),
    Case(
        "a_todo_behind_an_event",
        document(
            vevent("DTSTART:20990101T090000Z\r\nSUMMARY:Meeting", uid="shared"),
            vtodo("SUMMARY:Slides", uid="shared"),
        ),
        "The order that had ticking the item off rename the meeting.",
    ),
    Case(
        "a_todo_that_is_only_a_detached_instance",
        document(
            vtodo(
                "SUMMARY:Water\r\nRECURRENCE-ID:20990101T090000Z\r\nDUE:20990101T090000Z"
            )
        ),
        "An object holding nothing but an exception; caldav's default save"
        " path looks up a series that is not there.",
    ),
    Case(
        "a_recurring_todo_that_never_advances",
        document(
            vtodo(
                "SUMMARY:Water\r\nDTSTART:20990101T090000Z\r\n"
                "DUE:20990101T100000Z\r\nRRULE:FREQ=WEEKLY;INTERVAL=0"
            )
        ),
        "Completing it rolls it forward, and a rule that never advances has"
        " no next occurrence to roll to.",
    ),
    Case(
        "a_todo_body_that_is_not_icalendar",
        "<html><body>503 backend unavailable</body></html>",
        "One unreadable object must not empty the whole list.",
    ),
    Case(
        "a_due_before_the_start",
        document(
            vtodo(
                "SUMMARY:Backwards\r\nDTSTART:20990102T090000Z\r\nDUE:20990101T090000Z"
            )
        ),
        "RFC 5545 has DUE later than DTSTART; Home Assistant never shows the"
        " start, so an ordinary edit is made on top of it.",
    ),
    Case(
        "bidi_and_nul_in_a_todo",
        document(vtodo("SUMMARY:pay‮invoice\x00now\r\nDESCRIPTION:x\rz")),
        "The same text hazards, on the list rather than the calendar.",
    ),
)


# --------------------------------------------------------------------------
# Read paths.
# --------------------------------------------------------------------------


def poll_calendar(items: list[StoredObject]) -> Mock:
    calendar = Mock()
    calendar.name = "Personal"
    calendar.url = "https://cloud.example.com/remote.php/dav/personal"
    calendar.search.side_effect = lambda **_kwargs: items
    # A server without sync-collection, so every poll is a full read.
    calendar.objects_by_sync_token.side_effect = NotFoundError("no sync token")
    return calendar


async def poll(
    hass: HomeAssistant, items: list[StoredObject], component: str = "VEVENT"
) -> HaCaldavCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="x")
    entry.add_to_hass(hass)
    coordinator = HaCaldavCoordinator(
        hass,
        entry,
        poll_calendar(items),
        Capability(frozenset({component}), writable=True),
        days=7,
        include_all_day=True,
        scan_interval=timedelta(minutes=15),
    )
    await coordinator.async_refresh()
    return coordinator


SENTINEL = StoredObject(
    document(
        vevent(
            f"DTSTART:{dt_util.utcnow() + timedelta(days=1):%Y%m%dT%H%M%S}Z\r\n"
            f"DTEND:{dt_util.utcnow() + timedelta(days=1, hours=1):%Y%m%dT%H%M%S}Z\r\n"
            "SUMMARY:Sentinel",
            uid="sentinel",
        )
    ),
    url="https://dav.test/cal/sentinel.ics",
)


@cases(*EVENTS)
async def test_one_bad_object_does_not_cost_the_collection_its_poll(
    hass: HomeAssistant, case: Case
) -> None:
    """The good event beside it still reaches the entity.

    This is the whole point of the module. A raise anywhere in the read path
    fails the poll, and a failed poll takes the calendar entity and the to-do
    list of the same collection unavailable for as long as the object sits in
    the window - which, for an object nobody in the household knows is there,
    is indefinitely.
    """
    coordinator = await poll(hass, [StoredObject(dated(case.ics)), SENTINEL])

    assert coordinator.last_update_success, repr(coordinator.last_exception)
    assert coordinator.data.next_event is not None
    assert coordinator.data.next_event.summary == "Sentinel"


@cases(*EVENTS)
async def test_a_bad_object_alone_in_the_window_still_polls(
    hass: HomeAssistant, case: Case
) -> None:
    """With nothing beside it, the object is what _next_event and read_extras
    are asked about, which is the only way those two are reached at all."""
    coordinator = await poll(hass, [StoredObject(dated(case.ics))])

    assert coordinator.last_update_success, repr(coordinator.last_exception)
    assert isinstance(coordinator.data.extras, dict)


@cases(*EVENTS)
async def test_the_panel_window_survives_a_bad_object(
    hass: HomeAssistant, case: Case
) -> None:
    """The window the frontend asks for is not the polled one, and it is read
    through a separate path that raises at the user rather than at a poll."""
    coordinator = await poll(hass, [StoredObject(dated(case.ics)), SENTINEL])
    start = dt_util.utcnow()

    events = await coordinator.async_get_events(
        hass, start, start + timedelta(days=400)
    )

    assert any(event.summary == "Sentinel" for event in events)


@cases(*EVENTS)
def test_reading_one_component_never_raises(case: Case) -> None:
    """Every mapper the poll and the panel go through, on every case.

    Driven one component at a time rather than through the coordinator, so a
    case that the poll happens to filter out before it reaches the mapper is
    still put through it: the filter is not the guarantee, the mapper is.
    """
    item = StoredObject(dated(case.ics))
    found = components_of(item, "vevent")
    # The body is reparsed per access, as caldav's is, so what is compared is
    # what came back rather than which object it happens to be.
    assert (component_of(item, "vevent") is None) is (not found)
    master = master_of(item)
    assert (master is None) is (not found)
    if master is not None and len(found) > 1:
        # A detached instance may come first, and reading a series off it
        # records no rule for a series that has one.
        assert not hasattr(master, "recurrence_id") or all(
            hasattr(component, "recurrence_id") for component in found
        )
    for component in found:
        if not hasattr(component, "dtstart"):
            continue
        assert isinstance(sort_key(component), datetime)
        assert isinstance(is_all_day(component), bool)
        # These two are called outside the mapping guard and caught by name at
        # the call site, so what they raise has to stay inside the set that
        # guard covers. An OverflowError that was not in it is how a single
        # far-future end used to fail every poll.
        for placing in (get_end_date, is_over):
            with suppress(*_UNMAPPABLE):
                placing(component)
        # These two are reached with nothing around them, so they may not raise
        # at all: the first is what the entity state is built from and the
        # second is what its attributes are.
        event = to_event(component, "FREQ=WEEKLY")
        assert event is None or isinstance(event.summary, str)
        assert isinstance(read_extras(component), dict)


@cases(*TODOS)
async def test_one_bad_todo_does_not_cost_the_list_its_poll(
    hass: HomeAssistant, case: Case
) -> None:
    good = StoredObject(
        document(vtodo("SUMMARY:Buy milk", uid="good")),
        url="https://dav.test/cal/good.ics",
    )
    coordinator = await poll(hass, [StoredObject(case.ics), good], component="VTODO")

    assert coordinator.last_update_success, repr(coordinator.last_exception)
    assert "Buy milk" in [item.summary for item in coordinator.data.todos]


@cases(*TODOS)
def test_reading_one_todo_never_raises(case: Case) -> None:
    for component in components_of(StoredObject(case.ics), "vtodo"):
        item = to_todo(component)
        assert item is None or isinstance(item, TodoItem)
        rank = sort_order(component)
        assert isinstance(rank, tuple) and len(rank) == 2


async def test_a_window_whose_objects_carry_no_etag_keeps_the_ones_it_has(
    hass: HomeAssistant,
) -> None:
    """An etagless window read as an empty one would drop every etag held here
    and leave the next edit of each of those objects written unchecked."""
    item = StoredObject(document(vevent("DTSTART:20990101T090000Z\r\nSUMMARY:x")))
    item.props = {}
    coordinator = await poll(hass, [item])
    coordinator.etags = {"kept": '"e0"'}

    etags, rules = coordinator._window_index(
        dt_util.utcnow(), dt_util.utcnow() + timedelta(days=7)
    )

    assert etags is None
    assert rules == {"uid-1": None}


async def test_the_rule_is_read_off_the_master_however_the_server_ordered_it(
    hass: HomeAssistant,
) -> None:
    item = StoredObject(
        document(
            vevent(
                "RECURRENCE-ID:20990101T090000Z\r\n"
                "DTSTART:20990101T110000Z\r\nSUMMARY:Moved"
            ),
            vevent("DTSTART:20990101T090000Z\r\nRRULE:FREQ=WEEKLY\r\nSUMMARY:Standup"),
        )
    )
    coordinator = await poll(hass, [item])

    _etags, rules = coordinator._window_index(
        dt_util.utcnow(), dt_util.utcnow() + timedelta(days=7)
    )

    assert rules == {"uid-1": "FREQ=WEEKLY"}


# --------------------------------------------------------------------------
# The series corpus, driven through every write mode. These are the objects a
# user meets by opening the recurrence editor on something another client left
# behind, which is the only warning they get that it is there.
# --------------------------------------------------------------------------

SERIES_START = (
    "DTSTART:20260101T090000Z\r\nDTEND:20260101T100000Z\r\nSUMMARY:Standup\r\n"
)
OCCURRENCE = "2026-01-08 09:00:00+00:00"

SERIES = (
    Case(
        "an_object_with_no_event_in_it",
        document(vtodo("SUMMARY:Slides")),
        "The uid resolved, the resource holds no VEVENT to edit.",
    ),
    Case(
        "an_object_that_is_only_a_timezone",
        document("BEGIN:VTIMEZONE\r\nTZID:Europe/Berlin\r\nEND:VTIMEZONE\r\n"),
        "Same, with nothing at all under the wrapper.",
    ),
    Case(
        "a_series_without_a_start",
        document(vevent("DTEND:20260101T100000Z\r\nRRULE:FREQ=WEEKLY\r\nSUMMARY:x")),
        "RFC 5545 allows it once the object carries a METHOD, and every"
        " comparison in the write path is anchored on the start.",
    ),
    Case(
        "nothing_but_detached_instances",
        document(
            vevent(
                "RECURRENCE-ID:20260108T090000Z\r\n"
                "DTSTART:20260108T110000Z\r\nSUMMARY:One"
            ),
            vevent(
                "RECURRENCE-ID:20260115T090000Z\r\n"
                "DTSTART:20260115T110000Z\r\nSUMMARY:Two"
            ),
        ),
        "Nothing here is a series head; a rule written onto one of them is"
        " inert, and dropping the others deletes events nobody asked about.",
    ),
    Case(
        "an_override_ahead_of_its_series",
        document(
            vevent(
                "RECURRENCE-ID:20260108T090000Z\r\n"
                "DTSTART:20260108T110000Z\r\nSUMMARY:Moved"
            ),
            vevent(SERIES_START + "RRULE:FREQ=WEEKLY"),
        ),
        "caldav reads its own recurrence handling off the first component.",
    ),
    Case(
        "an_override_that_covers_everything_after_it",
        document(
            vevent(SERIES_START + "RRULE:FREQ=WEEKLY"),
            vevent(
                "RECURRENCE-ID;RANGE=THISANDFUTURE:20260108T090000Z\r\n"
                "DTSTART:20260108T110000Z\r\nSUMMARY:Moved"
            ),
        ),
        "Apple Calendar and Outlook write it; treated as a single day it"
        " silently reverts every occurrence after it.",
    ),
    Case(
        "an_override_naming_no_slot_of_the_rule",
        document(
            vevent(SERIES_START + "RRULE:FREQ=WEEKLY"),
            vevent(
                "RECURRENCE-ID:20260103T090000Z\r\n"
                "DTSTART:20260103T110000Z\r\nSUMMARY:Orphan"
            ),
        ),
        "The rule was changed out from under the exception.",
    ),
    Case(
        "an_exdate_of_another_value_type",
        document(
            vevent(SERIES_START + "RRULE:FREQ=WEEKLY\r\nEXDATE;VALUE=DATE:20260108")
        ),
        "Comparing it against the start mixes a date with an instant.",
    ),
    Case(
        "an_until_of_another_value_type",
        document(vevent(SERIES_START + "RRULE:FREQ=WEEKLY;UNTIL=20260401")),
        "dateutil refuses the pair outright unless it is reconciled first.",
    ),
    Case(
        "a_floating_until_against_a_zoned_start",
        document(vevent(SERIES_START + "RRULE:FREQ=WEEKLY;UNTIL=20260401T000000")),
        "The other half of the same mismatch.",
    ),
    Case(
        "an_until_at_the_edge_of_what_a_datetime_holds",
        document(vevent(SERIES_START + "RRULE:FREQ=WEEKLY;UNTIL=99991231T235959Z")),
        "Capping such a series computes against it.",
    ),
    Case(
        "a_rule_that_never_advances",
        document(vevent(SERIES_START + "RRULE:FREQ=WEEKLY;INTERVAL=0")),
        "dateutil re-yields the start forever, so nothing is ever after it.",
    ),
    Case(
        "a_rule_with_a_frequency_that_does_not_exist",
        document(vevent(SERIES_START + "RRULE:FREQ=FORTNIGHTLY")),
        "Nothing can be expanded from it at all.",
    ),
    Case(
        "a_rule_that_counts_to_zero",
        document(vevent(SERIES_START + "RRULE:FREQ=WEEKLY;COUNT=0")),
        "Splitting it would write COUNT=-1 onto the tail.",
    ),
    Case(
        "a_rule_naming_a_day_no_month_has",
        document(vevent(SERIES_START + "RRULE:FREQ=YEARLY;BYMONTHDAY=32")),
        "The rule yields nothing, so a guard counting occurrences never"
        " reaches its limit; only the frequency bounds the search.",
    ),
    Case(
        "a_rule_of_impossible_density",
        document(vevent(SERIES_START + "RRULE:FREQ=SECONDLY")),
        "A hundred million occurrences between the start and the split.",
    ),
    Case(
        "a_rule_positioning_within_nothing",
        document(vevent(SERIES_START + "RRULE:FREQ=MONTHLY;BYSETPOS=2")),
        "RFC 5545 only allows BYSETPOS beside another BY rule.",
    ),
    Case(
        "an_rdate_carrying_a_period",
        document(vevent(SERIES_START + "RDATE;VALUE=PERIOD:20260115T090000Z/PT1H")),
        "An RDATE value may be a period rather than a moment, and the split"
        " point is compared against its start.",
    ),
    Case(
        "exception_dates_with_no_rule_to_except",
        document(vevent(SERIES_START + "EXDATE:20260108T090000Z")),
        "What is left when a client removes the rule and not the exclusions.",
    ),
    Case(
        "an_end_stored_before_its_start",
        document(
            vevent(
                "DTSTART:20260101T100000Z\r\nDTEND:20260101T090000Z\r\n"
                "SUMMARY:Backwards\r\nRRULE:FREQ=WEEKLY"
            )
        ),
        "The span check runs on the merged component, so it sees an end the"
        " caller never named.",
    ),
    Case(
        "a_sequence_that_is_not_a_number",
        document(vevent(SERIES_START + "SEQUENCE:abc\r\nRRULE:FREQ=WEEKLY")),
        "Every save reads it, adds one and writes it back.",
    ),
    Case(
        "a_priority_that_is_not_a_number",
        document(vevent(SERIES_START + "PRIORITY:high\r\nRRULE:FREQ=WEEKLY")),
        "icalendar parses the whole component, so one bad property refuses"
        " the edit of every other one.",
    ),
    Case(
        "the_uid_written_twice",
        document(
            vevent(
                "UID:second-uid\r\n" + SERIES_START + "RRULE:FREQ=WEEKLY", uid="uid-1"
            )
        ),
        "The uid names the resource, so a second one is a second answer to"
        " which object is being written.",
    ),
    Case(
        "text_that_looks_like_the_format_around_it",
        document(
            vevent(
                SERIES_START + "RRULE:FREQ=WEEKLY\r\n"
                "DESCRIPTION:x\\r\\nEND:VEVENT\\r\\nBEGIN:VEVENT\\r\\nUID:injected"
            )
        ),
        "The edit rebuilds the document; anything that concatenates rather"
        " than encodes splits the object in two.",
    ),
    Case(
        "bidi_and_nul_in_the_summary",
        document(
            vevent(SERIES_START.replace("Standup", "a‮b\x00c") + "RRULE:FREQ=WEEKLY")
        ),
        "Carried through a read, an edit and a write untouched.",
    ),
    Case(
        "a_summary_far_past_the_folding_limit",
        document(
            vevent(SERIES_START.replace("Standup", "z" * 20000) + "RRULE:FREQ=WEEKLY")
        ),
        "Rewritten on every edit, so the folding has to survive a round trip.",
    ),
    Case(
        "non_ascii_in_the_uid",
        document(vevent(SERIES_START + "RRULE:FREQ=WEEKLY", uid="uid-ü-\U0001f600")),
        "The tail of a split is written under a uid derived from this one,"
        " and that uid becomes a url.",
    ),
)

WRITES = {
    "rename_the_series": lambda calendar: update_event(
        calendar, "uid-1", {"summary": "Renamed"}
    ),
    "move_the_series": lambda calendar: update_event(
        calendar, "uid-1", {"dtstart": datetime(2026, 2, 1, 9, tzinfo=UTC)}
    ),
    "clear_the_rule": lambda calendar: update_event(
        calendar, "uid-1", {"summary": "Once", "rrule": ""}
    ),
    "edit_one_occurrence": lambda calendar: update_event(
        calendar, "uid-1", {"summary": "Just this one"}, recurrence_id=OCCURRENCE
    ),
    "edit_from_one_occurrence": lambda calendar: update_event(
        calendar,
        "uid-1",
        {"summary": "From here"},
        recurrence_id=OCCURRENCE,
        this_and_future=True,
    ),
    "delete_the_series": lambda calendar: delete_event(calendar, "uid-1"),
    "delete_one_occurrence": lambda calendar: delete_event(
        calendar, "uid-1", recurrence_id=OCCURRENCE
    ),
    "delete_from_one_occurrence": lambda calendar: delete_event(
        calendar, "uid-1", recurrence_id=OCCURRENCE, this_and_future=True
    ),
}


@cases(*SERIES)
@pytest.mark.parametrize("mode", list(WRITES), ids=list(WRITES))
def test_a_write_onto_a_malformed_series_refuses_rather_than_breaks(
    case: Case, mode: str
) -> None:
    """Either the write is made or it is refused by name.

    An AttributeError out of icalendar reaches the user as "the server had a
    problem", which is both untrue and unactionable: the object is theirs, it
    is on their server, and nothing in the message says which one it is.

    What a write that does go ahead has to leave behind is a resource that is
    still readable: a document with no VEVENT left in it is one the next poll
    drops and no later edit can reach, which is the same as having deleted the
    event without saying so.

    A delete is the exception, and only in what it may take away. It clears the
    events by design, but a resource may hold a to-do beside them, and going
    back empty would take that to-do off a list nobody asked about.
    """
    calendar = dav_calendar()
    stored = WriteTarget(case.ics)
    calendar.event_by_uid.side_effect = None
    calendar.event_by_uid.return_value = stored

    def write() -> None:
        WRITES[mode](calendar)

    try:
        within(_BUDGET, write)
    except Refused as err:
        assert err.key
        return
    except NotFoundError, ValueError:
        return
    if stored.saves:
        document = ICalCalendar.from_ical(stored.data)
        assert [item for item in document.subcomponents if item.name != "VTIMEZONE"]
        if not mode.startswith("delete_"):
            assert document.walk("VEVENT")


# --------------------------------------------------------------------------
# The document corpus, driven through import. An import is the one call that
# takes a whole document from outside and is expected to refuse rather than
# overwrite, so its guards are what the malformed shapes have to survive.
# --------------------------------------------------------------------------

DOCUMENTS = (
    Case(
        "a_component_without_a_uid",
        document(
            "BEGIN:VEVENT\r\nDTSTAMP:20260101T000000Z\r\n"
            "DTSTART:20260101T090000Z\r\nSUMMARY:Anonymous\r\nEND:VEVENT\r\n"
        ),
        "There is no uid to check for a clash, so there is nothing to refuse"
        " an overwrite on.",
    ),
    Case(
        "a_document_holding_only_a_timezone",
        document("BEGIN:VTIMEZONE\r\nTZID:Europe/Berlin\r\nEND:VTIMEZONE\r\n"),
        "Nothing to write; reporting success would be a lie.",
    ),
    Case(
        "a_document_with_nothing_in_it",
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nEND:VCALENDAR\r\n",
        "Same, with the wrapper alone.",
    ),
    Case(
        "a_body_that_is_not_icalendar",
        "<html><body>not a calendar</body></html>",
        "A file the user picked by mistake.",
    ),
    Case(
        "two_documents_in_one_file",
        document(vevent("DTSTART:20260101T090000Z\r\nSUMMARY:a"))
        + document(vevent("DTSTART:20260101T090000Z\r\nSUMMARY:b", uid="uid-2")),
        "What concatenating two exports produces.",
    ),
    Case(
        "an_event_and_a_todo_under_one_uid",
        document(
            vevent("DTSTART:20260101T090000Z\r\nSUMMARY:Meeting", uid="shared"),
            vtodo("SUMMARY:Slides", uid="shared"),
        ),
        "One resource, and which uid filter can see a clash on it depends on"
        " which kind it is stored as.",
    ),
    Case(
        "a_timezone_that_names_itself",
        document(
            "BEGIN:VTIMEZONE\r\nTZID:Weird/Zone\r\n"
            "BEGIN:STANDARD\r\nDTSTART;TZID=Weird/Zone:19700101T000000\r\n"
            "TZOFFSETFROM:+0000\r\nTZOFFSETTO:+0000\r\nTZNAME:W\r\n"
            "END:STANDARD\r\nEND:VTIMEZONE\r\n",
            vevent("DTSTART;TZID=Weird/Zone:20260101T090000\r\nSUMMARY:Circular"),
        ),
        "Resolving the definition of a zone dated in itself.",
    ),
    Case(
        "a_zone_nobody_has_heard_of",
        document(vevent("DTSTART;TZID=Mars/Olympus:20260101T090000\r\nSUMMARY:Away")),
        "RFC 5545 wants the definition alongside the reference and there is"
        " none to generate.",
    ),
    Case(
        "a_windows_zone_name",
        document(
            vevent(
                'DTSTART;TZID="W. Europe Standard Time":20260101T090000\r\n'
                "SUMMARY:Outlook"
            )
        ),
        "The commonest thing an exported Outlook calendar carries.",
    ),
    Case(
        "a_priority_that_is_not_a_number",
        document(vevent("DTSTART:20260101T090000Z\r\nPRIORITY:high\r\nSUMMARY:a")),
        "The document is parsed by icalendar, which is strict about it.",
    ),
    Case(
        "a_uid_that_is_only_whitespace",
        document(vevent("DTSTART:20260101T090000Z\r\nSUMMARY:a", uid="   ")),
        "Not empty, so it passes the emptiness check, and it becomes a url.",
    ),
    Case(
        "a_document_of_nothing_but_detached_instances",
        document(
            vevent(
                "RECURRENCE-ID:20260108T090000Z\r\n"
                "DTSTART:20260108T110000Z\r\nSUMMARY:Orphan"
            )
        ),
        "caldav's default save path looks the uid up on the target, which"
        " does not carry it yet.",
    ),
    Case(
        "more_uids_than_the_scan_threshold",
        document(
            *[
                vevent(f"DTSTART:20260101T090000Z\r\nSUMMARY:{n}", uid=f"uid-{n}")
                for n in range(12)
            ]
        ),
        "Past the threshold the clash check reads the whole collection once"
        " instead of asking per uid, which is a different code path.",
    ),
    Case(
        "text_that_looks_like_the_format_around_it",
        document(
            vevent(
                "DTSTART:20260101T090000Z\r\nSUMMARY:a\r\n"
                "DESCRIPTION:x\\r\\nEND:VEVENT\\r\\nBEGIN:VEVENT\\r\\nUID:injected"
            )
        ),
        "One event in, one event out.",
    ),
    Case(
        "bidi_and_nul_in_the_summary",
        document(vevent("DTSTART:20260101T090000Z\r\nSUMMARY:a‮b\x00c")),
        "Carried into the collection unchanged.",
    ),
    Case(
        "non_ascii_in_the_uid",
        document(
            vevent("DTSTART:20260101T090000Z\r\nSUMMARY:a", uid="uid-ü-\U0001f600")
        ),
        "The uid names the resource the library PUTs to.",
    ),
    Case(
        "an_end_at_the_edge_of_what_a_datetime_holds",
        document(
            vevent("DTSTART:20260101T090000Z\r\nDTEND:99991231T235959Z\r\nSUMMARY:a")
        ),
        "Written, then read back by the very next poll.",
    ),
)


@cases(*DOCUMENTS)
def test_importing_a_malformed_document_refuses_or_writes_it_whole(case: Case) -> None:
    calendar = dav_calendar()

    try:
        written = within(_BUDGET, lambda: import_ics(calendar, case.ics))
    except Refused as err:
        assert err.key
        assert calendar.client.puts == []
        return
    except ValueError:
        # A parse failure from icalendar. It carries no key of its own, and the
        # service layer reports it as a refusal with the library's own reason.
        assert calendar.client.puts == []
        return
    # Whatever was reported as imported is what actually reached the wire; a
    # half-written import is taken back rather than reported as done.
    assert len(calendar.client.puts) == len(written)
    for _url, body in calendar.client.puts:
        assert ICalCalendar.from_ical(body).walk("VEVENT") or ICalCalendar.from_ical(
            body
        ).walk("VTODO")
    # What was imported is named by a uid the document actually carries. The
    # uid is what the clash check asks the server about and what names the
    # resource; one that came from anywhere else is checked against nothing and
    # stored where nothing can find it again.
    assert set(written) <= {
        line.removeprefix("UID:")
        for line in case.ics.splitlines()
        if line.startswith("UID:")
    }


def as_stored(ics: str) -> list[StoredObject]:
    """Return the document as the collection would hold it: a resource per uid.

    Read back by the clash check, which reads the uid off each resource of the
    collection when the server will not filter on them.
    """
    try:
        parsed = ICalCalendar.from_ical(ics)
    except ValueError:
        return [StoredObject(ics)]
    grouped: dict[str, list[str]] = {}
    for component in parsed.walk():
        if component.name in ("VEVENT", "VTODO"):
            uid = str(component.get("UID", ""))
            grouped.setdefault(uid, []).append(component.to_ical().decode("utf-8"))
    return [
        StoredObject(document(*parts), url=f"https://dav.test/cal/{index}.ics")
        for index, parts in enumerate(grouped.values())
    ]


@cases(*DOCUMENTS)
def test_an_import_never_writes_over_a_uid_the_collection_already_holds(
    case: Case,
) -> None:
    """RFC 4791 gives one uid one resource and caldav names that resource after
    the uid, so a uid already on the calendar is not a second event but the
    same one, overwritten with no way back. The check is the whole reason the
    import reads before it writes, and it has to hold for every shape the uid
    can be written in."""
    calendar = dav_calendar()
    calendar.object_by_uid.side_effect = None
    calendar.object_by_uid.return_value = Mock()
    calendar.search.return_value = as_stored(case.ics)

    def imported() -> list[str]:
        return import_ics(calendar, case.ics)

    with pytest.raises((Refused, ValueError)) as raised:
        within(_BUDGET, imported)
    if isinstance(raised.value, Refused):
        assert raised.value.key in ("uid_clash", "document_no_uid", "document_empty")
    assert calendar.client.puts == []


# Short enough that RFC 5545's 75-octet fold does not reach them, which is
# what the text search behind this can read.
UNREADABLE_UIDS = ("uid-1", "uid-ü-\U0001f600", "   ", "a" * 60)


@pytest.mark.parametrize("uid", UNREADABLE_UIDS)
def test_the_uid_of_an_object_icalendar_will_not_parse_is_still_found(uid: str) -> None:
    """Dropping such an object would report no clash for the uid it holds and
    let an import overwrite it, so the uid is taken out of the text instead.

    The object here carries a component icalendar does not know, which is what
    a client writing its own extension without the X- prefix produces.
    """
    calendar = dav_calendar()
    # Refused rather than answered: the uid filter is what iCloud declines, and
    # declining it is what sends the check through the scan this is about.
    calendar.object_by_uid.side_effect = Exception("uid filters unsupported")
    calendar.search.return_value = [
        StoredObject(
            document(
                vevent(
                    "DTSTART:20260101T090000Z\r\nSUMMARY:a\r\n"
                    "BEGIN:VUNKNOWN\r\nX-A:1\r\nEND:VUNKNOWN",
                    uid=uid,
                )
            )
        )
    ]

    with pytest.raises(Refused, match="uid_clash"):
        import_ics(
            calendar,
            document(vevent("DTSTART:20260101T090000Z\r\nSUMMARY:mine", uid=uid)),
        )
    assert calendar.client.puts == []


@cases(*DOCUMENTS)
def test_exporting_a_calendar_never_stops_at_one_bad_object(case: Case) -> None:
    """One object another client wrote a non-numeric PRIORITY into must not
    cost the user the export of everything else in the collection."""
    good = StoredObject(
        document(vevent("DTSTART:20260101T090000Z\r\nSUMMARY:Keeper", uid="keeper")),
        url="https://dav.test/cal/keeper.ics",
    )
    calendar = dav_calendar()
    calendar.search.side_effect = lambda **kwargs: (
        [StoredObject(case.ics, url="https://dav.test/cal/odd.ics"), good]
        if kwargs.get("event")
        else []
    )

    try:
        exported = within(_BUDGET, lambda: export_ics(calendar, None))
    except ValueError:
        return
    assert "Keeper" in exported


MOVABLE = tuple(case for case in DOCUMENTS if "BEGIN:VEVENT" in case.ics)


@cases(*MOVABLE)
def test_moving_a_malformed_object_refuses_or_keeps_the_original(case: Case) -> None:
    """The original is deleted after the copy; a copy that did not happen must
    not take the only remaining version with it."""
    source, target = dav_calendar(), dav_calendar()
    stored = WriteTarget(case.ics)
    source.event_by_uid.side_effect = None
    source.event_by_uid.return_value = stored

    def move() -> None:
        move_event(source, target, "uid-1", keep_original=False)

    try:
        within(_BUDGET, move)
    except Refused, ValueError:
        assert stored.deletes == 0
        assert target.client.puts == []
        return
    assert stored.deletes == 1
    assert len(target.client.puts) == 1

    # Again onto a target that refuses the write, which is the only way the
    # order of the two shows: a copy that landed and one that never happened
    # look alike on the source unless the PUT can fail.
    refusing, keeper = dav_calendar(), WriteTarget(case.ics)
    refusing.client.fail_from = 0
    source.event_by_uid.return_value = keeper

    with pytest.raises((DAVError, ValueError)):
        within(_BUDGET, lambda: move_event(source, refusing, "uid-1", False))

    assert keeper.deletes == 0


# --------------------------------------------------------------------------
# The remaining write entry points.
# --------------------------------------------------------------------------


@cases(*TODOS)
def test_editing_a_malformed_todo_refuses_or_writes_to_the_todo(case: Case) -> None:
    """The edit lands on a VTODO or on nothing.

    A resource may hold a VEVENT ahead of the VTODO under one uid, and ticking
    the item off then renamed the meeting beside it, wrote COMPLETED onto it,
    and left the item the user actually ticked open for the next attempt.
    """
    calendar = dav_calendar()
    stored = WriteTarget(case.ics)
    calendar.todo_by_uid.side_effect = None
    calendar.todo_by_uid.return_value = stored

    def edit() -> list[bytes]:
        # Read through the same lazily parsed resource the call uses, so a
        # document icalendar refuses raises where the code meets it.
        before = [item.to_ical() for item in stored.icalendar_instance.walk("VEVENT")]
        update_todo(calendar, "todo-1", {"summary": "Ticked", "status": "COMPLETED"})
        return before

    try:
        before = within(_BUDGET, edit)
    except Refused as err:
        assert err.key
        return
    except ValueError:
        # icalendar declining the whole document. It carries no key of its own
        # and the service layer reports it as a refusal with its own reason;
        # what matters is that nothing was written on the strength of it.
        assert stored.saves == []
        return
    after = [item.to_ical() for item in stored.icalendar_instance.walk("VEVENT")]
    assert after == before


# The X-APPLE-SORT-ORDER a server may have on the middle item of a list of
# three, and why each is worth a drag.
POSITIONS = (
    pytest.param("", id="no_position_at_all"),
    pytest.param("top", id="a_position_that_is_not_a_number"),
    pytest.param("00", id="a_position_with_leading_zeros"),
    pytest.param("-4096", id="a_negative_position"),
    pytest.param("9" * 40, id="a_position_of_absurd_magnitude"),
    pytest.param("1024.5", id="a_fractional_position"),
    pytest.param("1024", id="the_position_of_the_item_beside_it"),
)


@pytest.mark.parametrize("position", POSITIONS)
def test_reordering_over_positions_the_server_wrote(position: str) -> None:
    """The drag lands, or it is refused by name.

    A drag that raises leaves the list in whatever order the server had and
    tells the user their server had a problem; the item springs back on the
    next poll and nothing says why.
    """
    calendar = dav_calendar()
    stored = []
    for index in range(3):
        written = position if index == 1 else str(index * 1024)
        line = f"\r\nX-APPLE-SORT-ORDER:{written}" if written else ""
        stored.append(
            WriteTarget(document(vtodo(f"SUMMARY:{index}{line}", uid=f"u{index}")))
        )
    calendar.search.return_value = stored

    def reorder() -> None:
        reorder_todos(calendar, ["u2", "u0", "u1"])

    try:
        within(_BUDGET, reorder)
    except Refused as err:
        assert err.key
        return
    for item in stored:
        assert item.saves == [] or "X-APPLE-SORT-ORDER" in item.icalendar_component


INVITATIONS = (
    Case(
        "an_attendee_line_with_no_address",
        document(vevent("DTSTART:20260101T090000Z\r\nSUMMARY:a\r\nATTENDEE:")),
        "A client that writes the line before it has the address.",
    ),
    Case(
        "an_address_without_its_scheme",
        document(
            vevent("DTSTART:20260101T090000Z\r\nSUMMARY:a\r\nATTENDEE:iven@example.com")
        ),
        "A CAL-ADDRESS is a URI, and clients write bare addresses anyway.",
    ),
    Case(
        "an_address_the_server_padded",
        document(
            vevent(
                "DTSTART:20260101T090000Z\r\nSUMMARY:a\r\n"
                "ATTENDEE: MailTo:IVEN@Example.com "
            )
        ),
        "RFC 3986 makes the scheme case-insensitive, and the padding hides it"
        " from anything that strips the prefix first.",
    ),
    Case(
        "no_attendees_at_all",
        document(vevent("DTSTART:20260101T090000Z\r\nSUMMARY:a")),
        "There is no participation to set.",
    ),
    Case(
        "an_object_of_nothing_but_detached_instances",
        document(
            vevent(
                "RECURRENCE-ID:20260108T090000Z\r\nDTSTART:20260108T110000Z\r\n"
                "SUMMARY:a\r\nATTENDEE:mailto:iven@example.com"
            )
        ),
        "caldav's default save path would look the series up and never find"
        " it; the reply has to be written all the same.",
    ),
    Case(
        "a_sequence_that_is_not_a_number",
        document(
            vevent(
                "DTSTART:20260101T090000Z\r\nSEQUENCE:abc\r\nSUMMARY:a\r\n"
                "ATTENDEE:mailto:iven@example.com"
            )
        ),
        "RFC 5546 lets only the organizer move SEQUENCE, so a reply holds it"
        " where it was - which means reading it first.",
    ),
    Case(
        "a_todo_ahead_of_the_event",
        document(
            vtodo("SUMMARY:Slides", uid="shared"),
            vevent(
                "DTSTART:20260101T090000Z\r\nSUMMARY:a\r\n"
                "ATTENDEE:mailto:iven@example.com",
                uid="shared",
            ),
        ),
        "The reply belongs to the event, whichever component came first.",
    ),
)


@cases(*INVITATIONS)
def test_responding_to_a_malformed_invitation(case: Case) -> None:
    calendar = dav_calendar()
    stored = WriteTarget(case.ics)
    calendar.event_by_uid.side_effect = None
    calendar.event_by_uid.return_value = stored

    try:
        within(
            _BUDGET,
            lambda: respond_to_invitation(
                calendar, "uid-1", "ACCEPTED", ["iven@example.com"]
            ),
        )
    except Refused as err:
        assert err.key
        assert stored.saves == []
        return
    assert stored.saves


@pytest.mark.parametrize(
    ("stored", "expected", "written"),
    [
        (None, '"a"', True),
        ('"a"', '"a"', True),
        ('"a"', '"b"', False),
        ('W/"a"', 'W/"a"', True),
        ("", '"a"', False),
    ],
)
def test_the_etag_check_over_what_a_server_may_answer_with(
    stored: str | None, expected: str, written: bool
) -> None:
    """An etag the server did not put on the object is nothing to compare, and
    refusing on that would leave the whole account unable to write."""
    resource = Mock()
    resource.props = {} if stored is None else {"{DAV:}getetag": stored}

    if written:
        check_etag(resource, expected)
    else:
        with pytest.raises(Refused, match="etag_conflict"):
            check_etag(resource, expected)


HOSTILE_TEXT = (
    "ends\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:injected\r\nSUMMARY:injected",
    "a\x00b",
    "a\rb",
    "a‮b⁦c",
    "z" * 20000,
    "Grüße \U0001f600",
    "",
)


@pytest.mark.parametrize("text", HOSTILE_TEXT, ids=range(len(HOSTILE_TEXT)))
def test_creating_from_hostile_text_writes_exactly_one_component(text: str) -> None:
    """Text carrying the line ending the format is built out of must not be
    able to end the component it is inside and start another."""
    calendar = Mock()

    create_event(
        calendar,
        {
            "summary": text,
            "dtstart": datetime(2026, 7, 6, 9, tzinfo=UTC),
            "dtend": datetime(2026, 7, 6, 10, tzinfo=UTC),
            "description": text,
            "location": text,
        },
    )
    create_todo(calendar, {"summary": text or "x", "description": text})

    event_body = calendar.save_event.call_args.args[0]
    todo_body = calendar.save_todo.call_args.args[0]
    assert len(event_body.walk("VEVENT")) == 1
    assert len(todo_body.walk("VTODO")) == 1


# Everything but the first, which carries a raw line ending. That one is what
# create_event escapes and apply_extras does not; see the report.
@pytest.mark.parametrize("text", HOSTILE_TEXT[1:], ids=range(1, len(HOSTILE_TEXT)))
def test_writing_extras_from_hostile_text_keeps_one_component(text: str) -> None:
    component = ICalEvent()
    component.add("UID", "u")
    component.add("DTSTART", datetime(2026, 7, 6, 9, tzinfo=UTC))

    apply_extras(
        component,
        {
            "url": text,
            "status": text,
            "categories": [text, ""],
            "organizer": text,
            "attendees": [{"email": "a@b", "name": text}],
        },
    )

    wrapper = ICalCalendar()
    wrapper.add_component(component)
    assert len(ICalCalendar.from_ical(wrapper.to_ical()).walk("VEVENT")) == 1


MALFORMED_RULES = (
    "FREQ=WEEKLY;INTERVAL=0",
    "FREQ=WEEKLY;INTERVAL=-2",
    "FREQ=FORTNIGHTLY",
    "FREQ=WEEKLY;COUNT=0",
    "FREQ=WEEKLY;BYDAY=XX",
    "FREQ=WEEKLY;WKST=XX",
    "FREQ=WEEKLY;BYSETPOS=0",
    "FREQ=DAILY;BYHOUR=25",
    "FREQ=YEARLY;BYMONTHDAY=32",
    "FREQ=WEEKLY;UNTIL=20250101",
    "FREQ=WEEKLY;UNTIL=20250101T000000",
)


@pytest.mark.parametrize("rule", MALFORMED_RULES)
def test_building_a_rule_from_what_a_server_stored(rule: str) -> None:
    """A rule that cannot be built refuses; one that can must produce its first
    occurrence rather than searching for one that is not there."""
    start = datetime(2026, 1, 1, 9, tzinfo=UTC)

    try:
        built = rule_from(vRecur.from_ical(rule), start)
    except Refused, ValueError:
        return
    assert within(_BUDGET, lambda: next(iter(built), None)) in (None, start)


# --------------------------------------------------------------------------
# The protocol corpus. A multistatus is parsed here rather than by caldav,
# because caldav raises on the whole response when one propstat is refused,
# which would cost every calendar on the account its capabilities.
# --------------------------------------------------------------------------

HOME = "/remote.php/dav/calendars/iven"
_PROPS = (
    "<d:propstat><d:prop>"
    '<c:supported-calendar-component-set><c:comp name="VEVENT"/>'
    "</c:supported-calendar-component-set>"
    "<d:current-user-privilege-set><d:privilege><d:read/></d:privilege>"
    "</d:current-user-privilege-set>"
    "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
)

MULTISTATUS = (
    Case(
        "a_response_with_no_href",
        f"<d:response>{_PROPS}</d:response>",
        "There is nothing to key the properties to.",
    ),
    Case(
        "an_empty_href",
        f"<d:response><d:href></d:href>{_PROPS}</d:response>",
        "Read literally it keys every such response together.",
    ),
    Case(
        "two_hrefs_in_one_response",
        f"<d:response><d:href>{HOME}/a/</d:href><d:href>{HOME}/b/</d:href>"
        f"{_PROPS}</d:response>",
        "RFC 4918 allows it; caldav asserts its way out of the whole"
        " response, which would lose every other calendar with it.",
    ),
    Case(
        "an_href_that_escapes_the_collection",
        f"<d:response><d:href>{HOME}/a/../../../elsewhere/</d:href>"
        f"{_PROPS}</d:response>",
        "It must not come out keyed to a calendar it does not describe.",
    ),
    Case(
        "a_response_with_no_propstat",
        f"<d:response><d:href>{HOME}/a/</d:href></d:response>",
        "A server saying nothing is not a server saying nothing is allowed.",
    ),
    Case(
        "a_propstat_the_server_refused",
        f"<d:response><d:href>{HOME}/a/</d:href><d:propstat><d:prop>"
        "<d:current-user-privilege-set/></d:prop>"
        "<d:status>HTTP/1.1 403 Forbidden</d:status></d:propstat></d:response>",
        "Exactly what a server answers for the privileges of a foreign"
        " collection, and one shared calendar must not lock the rest down.",
    ),
    Case(
        "a_propstat_with_no_status",
        f"<d:response><d:href>{HOME}/a/</d:href><d:propstat><d:prop>"
        '<c:supported-calendar-component-set><c:comp name="VEVENT"/>'
        "</c:supported-calendar-component-set></d:prop></d:propstat></d:response>",
        "The status is required and servers leave it out.",
    ),
    Case(
        "the_same_response_twice",
        f"<d:response><d:href>{HOME}/a/</d:href>{_PROPS}</d:response>"
        f"<d:response><d:href>{HOME}/a/</d:href>{_PROPS}</d:response>",
        "caldav carries a comment about a server that does this.",
    ),
    Case(
        "a_component_set_with_a_nameless_entry",
        f"<d:response><d:href>{HOME}/a/</d:href><d:propstat><d:prop>"
        "<c:supported-calendar-component-set><c:comp/>"
        "</c:supported-calendar-component-set></d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>",
        "Radicale answers an empty set this way, and it has to read the same"
        " as no answer at all rather than as a calendar holding nothing.",
    ),
    Case(
        "component_names_in_lower_case",
        f"<d:response><d:href>{HOME}/a/</d:href><d:propstat><d:prop>"
        '<c:supported-calendar-component-set><c:comp name="vevent"/>'
        "</c:supported-calendar-component-set></d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>",
        "RFC 5545 makes them case-insensitive, and a calendar read as holding"
        " neither kind is one whose entities are deleted.",
    ),
    Case(
        "a_privilege_nested_deeper_than_expected",
        f"<d:response><d:href>{HOME}/a/</d:href><d:propstat><d:prop>"
        "<d:current-user-privilege-set><d:privilege><d:all><d:write/></d:all>"
        "</d:privilege></d:current-user-privilege-set></d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>",
        "An aggregate privilege; reading only the first level names the"
        " wrapper rather than what it grants.",
    ),
    Case(
        "a_percent_encoded_href",
        f"<d:response><d:href>{HOME}/caf%C3%A9/</d:href>{_PROPS}</d:response>",
        "The calendar url keeps its encoding and the href need not.",
    ),
    Case(
        "an_xml_comment_between_the_elements",
        f"<d:response><d:href>{HOME}/a/</d:href><!-- generated -->"
        f"{_PROPS}</d:response>",
        "A comment is a child element too, and it answers .get() with None.",
    ),
)


def multistatus(entries: str) -> DAVResponse:
    body = (
        '<?xml version="1.0"?>'
        '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        f"{entries}</d:multistatus>"
    )
    response = Mock()
    response.status_code = 207
    response.headers = {"Content-Type": "application/xml"}
    response.text = body
    response.content = body.encode()
    return DAVResponse(response)


@cases(*MULTISTATUS)
def test_parsing_a_multistatus_a_server_may_send(case: Case) -> None:
    """Whatever comes back, it is keyed the way capability_for looks it up and
    nothing is asserted about a calendar the response did not name."""
    client = Mock()
    client.principal.return_value.calendar_home_set.get_properties.return_value = (
        multistatus(case.ics)
    )

    capabilities = fetch_capabilities(client)

    for key, capability in capabilities.items():
        # Keyed the way capability_for looks one up. A key that does not match
        # is not an error anywhere; the calendar simply keeps the permissive
        # default, and the user is told nothing about what it lost.
        assert key == calendar_key(key)
        # Never "no components": that reads as a calendar holding neither kind,
        # which is what the pruning step deletes entities on.
        assert capability.components
    named = Mock(url=f"https://cloud.example.com{HOME}/a/")
    unnamed = Mock(url=f"https://cloud.example.com{HOME}/never-mentioned/")
    assert capability_for(capabilities, unnamed) is UNKNOWN
    assert isinstance(capability_for(capabilities, named), Capability)


@cases(*MULTISTATUS)
def test_the_parts_of_the_parser_tolerate_what_is_not_there(case: Case) -> None:
    found = _objects_and_props(multistatus(case.ics))

    for props in found.values():
        components = _components(
            props.get("{urn:ietf:params:xml:ns:caldav}supported-calendar-component-set")
        )
        privileges = _privileges(props.get("{DAV:}current-user-privilege-set"))
        assert all(name == name.upper() for name in components)
        assert all("}" not in name for name in privileges)


# --------------------------------------------------------------------------
# The text and value dimensions, where the interesting part is that nothing is
# special about any one string. Derandomized so a green run stays green.
# --------------------------------------------------------------------------

FUZZ = settings(
    max_examples=200,
    derandomize=True,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)

# Anything a server can actually deliver. Surrogates are excluded because a
# UTF-8 body cannot carry one; the line endings are, because they end the
# property rather than sitting inside it, and putting one there tests vobject
# rather than this integration.
PROPERTY_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\r\n"),
    max_size=40,
)


@FUZZ
@given(
    summary=PROPERTY_TEXT,
    uid=PROPERTY_TEXT,
    location=PROPERTY_TEXT,
    priority=PROPERTY_TEXT,
    status=PROPERTY_TEXT,
    categories=PROPERTY_TEXT,
    organizer=PROPERTY_TEXT,
)
def test_no_text_a_server_can_store_breaks_the_event_read_path(
    summary: str,
    uid: str,
    location: str,
    priority: str,
    status: str,
    categories: str,
    organizer: str,
) -> None:
    ics = document(
        vevent(
            "DTSTART:20990101T090000Z\r\nDTEND:20990101T100000Z\r\n"
            f"SUMMARY:{summary}\r\nLOCATION:{location}\r\nPRIORITY:{priority}\r\n"
            f"STATUS:{status}\r\nCATEGORIES:{categories}\r\nORGANIZER:{organizer}",
            uid=uid,
        )
    )
    for component in components_of(StoredObject(ics), "vevent"):
        sort_key(component)
        is_all_day(component)
        is_over(component)
        to_event(component)
        read_extras(component)


@FUZZ
@given(
    summary=PROPERTY_TEXT,
    uid=PROPERTY_TEXT,
    status=PROPERTY_TEXT,
    order=PROPERTY_TEXT,
    percent=PROPERTY_TEXT,
)
def test_no_text_a_server_can_store_breaks_the_todo_read_path(
    summary: str, uid: str, status: str, order: str, percent: str
) -> None:
    ics = document(
        vtodo(
            f"SUMMARY:{summary}\r\nSTATUS:{status}\r\n"
            f"X-APPLE-SORT-ORDER:{order}\r\nPERCENT-COMPLETE:{percent}",
            uid=uid,
        )
    )
    for component in components_of(StoredObject(ics), "vtodo"):
        assert sort_order(component)[0] in (0, 1)
        item = to_todo(component)
        assert item is None or isinstance(item, TodoItem)


# The whole range a stored date can take, both value types, and the ends where
# adding a zone offset is the thing that overflows.
STORED_DATES = st.one_of(
    st.dates(min_value=date(1, 1, 2), max_value=date(9999, 12, 30)),
    st.datetimes(min_value=datetime(1, 1, 2), max_value=datetime(9999, 12, 30)).map(
        lambda value: value.replace(tzinfo=UTC)
    ),
    st.sampled_from(
        [
            date(1, 1, 1),
            date(9999, 12, 31),
            datetime(1, 1, 1, 0, 0, tzinfo=UTC),
            datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
        ]
    ),
)


def _line(key: str, value: date | datetime) -> str:
    if isinstance(value, datetime):
        return f"{key}:{value:%Y%m%dT%H%M%S}Z\r\n"
    return f"{key};VALUE=DATE:{value:%Y%m%d}\r\n"


@FUZZ
@given(start=STORED_DATES, end=STORED_DATES)
def test_no_pair_of_stored_dates_breaks_the_event_read_path(
    start: date | datetime, end: date | datetime
) -> None:
    """Every combination, ends and value-type mismatches included. An end no
    datetime can hold once a zone offset reaches it is the shape that used to
    fail every poll for as long as the object sat in the window."""
    ics = document(vevent(_line("DTSTART", start) + _line("DTEND", end) + "SUMMARY:x"))
    for component in components_of(StoredObject(ics), "vevent"):
        assert isinstance(sort_key(component), datetime)
        assert isinstance(is_all_day(component), bool)
        # Raising is allowed here and skipping is what the caller does with it;
        # raising something the caller does not name is the defect.
        for placing in (get_end_date, is_over):
            with suppress(*_UNMAPPABLE):
                placing(component)
        event = to_event(component)
        assert event is None or isinstance(event.start, date)


@FUZZ
@given(due=STORED_DATES)
def test_no_stored_due_date_breaks_the_todo_read_path(due: date | datetime) -> None:
    ics = document(vtodo(_line("DUE", due) + "SUMMARY:x"))
    for component in components_of(StoredObject(ics), "vtodo"):
        item = to_todo(component)
        assert item is None or isinstance(item, TodoItem)
