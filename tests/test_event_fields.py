"""Tests for the VEVENT properties Home Assistant has no field for."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from homeassistant.util import dt as dt_util
from icalendar import Calendar as ICalCalendar, Event as ICalEvent
import pytest
import vobject

from custom_components.ha_caldav.event import apply_extras, read_extras


def _vevent(body: str):
    ics = (
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//test//EN\n"
        f"BEGIN:VEVENT\nUID:test-1\nDTSTAMP:20260101T000000Z\n"
        f"DTSTART:20260706T090000Z\nDTEND:20260706T100000Z\nSUMMARY:x\n{body}\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )
    return vobject.readOne(ics).vevent


def _component() -> ICalEvent:
    event = ICalEvent()
    event.add("UID", "test-1")
    event.add("SUMMARY", "x")
    return event


def _written(component: ICalEvent) -> str:
    """Return the serialized event, unfolded so assertions can match on values.

    RFC 5545 folds lines at 75 octets, which lands mid-address on an ATTENDEE.
    """
    calendar = ICalCalendar()
    calendar.add("prodid", "-//test//EN")
    calendar.add("version", "2.0")
    calendar.add_component(component)
    return calendar.to_ical().decode("utf-8").replace("\r\n ", "")


def test_absent_properties_produce_no_attributes() -> None:
    assert read_extras(_vevent("LOCATION:Office")) == {}


def test_meeting_link_and_flags_are_read() -> None:
    extras = read_extras(
        _vevent(
            "URL:https://meet.example.com/standup\n"
            "STATUS:confirmed\nTRANSP:TRANSPARENT\nCLASS:private\nPRIORITY:2"
        )
    )

    assert extras == {
        "url": "https://meet.example.com/standup",
        "status": "CONFIRMED",
        "transparency": "TRANSPARENT",
        "classification": "PRIVATE",
        "priority": 2,
    }


def test_a_priority_that_is_not_a_number_is_skipped() -> None:
    assert "priority" not in read_extras(_vevent("PRIORITY:high"))


def test_categories_are_flattened_over_repeated_lines() -> None:
    extras = read_extras(_vevent("CATEGORIES:work,travel\nCATEGORIES:billable"))

    assert extras["categories"] == ["work", "travel", "billable"]


def test_attendees_keep_their_parameters() -> None:
    extras = read_extras(
        _vevent(
            "ORGANIZER:mailto:boss@example.com\n"
            "ATTENDEE;CN=Ann;PARTSTAT=ACCEPTED;ROLE=REQ-PARTICIPANT:"
            "mailto:ann@example.com\n"
            "ATTENDEE:mailto:bob@example.com"
        )
    )

    assert extras["organizer"] == "boss@example.com"
    assert extras["attendees"] == [
        {
            "email": "ann@example.com",
            "name": "Ann",
            "status": "ACCEPTED",
            "role": "REQ-PARTICIPANT",
        },
        {"email": "bob@example.com"},
    ]


def test_relative_alarms_are_reported_as_minutes_before() -> None:
    vevent = vobject.readOne(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//EN\nBEGIN:VEVENT\n"
        "UID:a\nDTSTAMP:20260101T000000Z\nDTSTART:20260706T090000Z\n"
        "DTEND:20260706T100000Z\nSUMMARY:x\n"
        "BEGIN:VALARM\nACTION:DISPLAY\nTRIGGER:-PT15M\nDESCRIPTION:Soon\n"
        "END:VALARM\nEND:VEVENT\nEND:VCALENDAR\n"
    ).vevent

    assert read_extras(vevent)["alarms"] == [
        {"minutes_before": 15, "action": "DISPLAY", "description": "Soon"}
    ]


def test_an_absolute_alarm_has_no_offset_to_report() -> None:
    vevent = vobject.readOne(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//EN\nBEGIN:VEVENT\n"
        "UID:a\nDTSTAMP:20260101T000000Z\nDTSTART:20260706T090000Z\n"
        "DTEND:20260706T100000Z\nSUMMARY:x\n"
        "BEGIN:VALARM\nACTION:DISPLAY\nTRIGGER;VALUE=DATE-TIME:20260706T084500Z\n"
        "DESCRIPTION:Soon\nEND:VALARM\nEND:VEVENT\nEND:VCALENDAR\n"
    ).vevent

    assert "alarms" not in read_extras(vevent)


def test_a_field_nobody_named_is_left_alone() -> None:
    component = _component()
    component.add("URL", "https://kept.example.com")

    apply_extras(component, {"status": "CONFIRMED"})

    assert str(component["URL"]) == "https://kept.example.com"
    assert str(component["STATUS"]) == "CONFIRMED"


def test_an_explicit_none_clears_the_property() -> None:
    component = _component()
    component.add("URL", "https://gone.example.com")

    apply_extras(component, {"url": None})

    assert "URL" not in component


def test_attendees_are_written_with_the_parameters_a_server_expects() -> None:
    component = _component()

    apply_extras(
        component,
        {"attendees": ["bob@example.com", {"email": "ann@example.com", "name": "Ann"}]},
    )

    written = _written(component)
    assert "mailto:bob@example.com" in written
    assert "CN=Ann" in written
    assert "PARTSTAT=NEEDS-ACTION" in written
    assert "RSVP=TRUE" in written


def test_writing_attendees_replaces_the_previous_list() -> None:
    component = _component()
    apply_extras(component, {"attendees": ["old@example.com"]})

    apply_extras(component, {"attendees": ["new@example.com"]})

    written = _written(component)
    assert "old@example.com" not in written
    assert "new@example.com" in written


def test_alarms_become_valarms_with_a_negative_trigger() -> None:
    component = _component()

    apply_extras(component, {"alarms": [15, {"minutes_before": 60, "action": "EMAIL"}]})

    written = _written(component)
    assert written.count("BEGIN:VALARM") == 2
    assert "TRIGGER:-PT15M" in written
    assert "TRIGGER:-PT1H" in written
    assert "ACTION:EMAIL" in written


def test_clearing_alarms_removes_every_valarm() -> None:
    component = _component()
    apply_extras(component, {"alarms": [15]})

    apply_extras(component, {"alarms": []})

    assert "BEGIN:VALARM" not in _written(component)


def test_a_round_trip_survives_both_libraries() -> None:
    component = _component()
    component.add("DTSTART", datetime(2026, 7, 6, 9, 0, tzinfo=UTC))
    apply_extras(
        component,
        {
            "url": "https://meet.example.com/x",
            "categories": ["work"],
            "status": "TENTATIVE",
            "priority": 5,
            "alarms": [10],
            "attendees": [{"email": "ann@example.com", "name": "Ann"}],
        },
    )

    back = read_extras(vobject.readOne(_written(component)).vevent)

    assert back["url"] == "https://meet.example.com/x"
    assert back["categories"] == ["work"]
    assert back["status"] == "TENTATIVE"
    assert back["priority"] == 5
    assert back["alarms"] == [
        {"minutes_before": 10, "action": "DISPLAY", "description": "Reminder"}
    ]
    # Writing fills in the parameters RFC 5545 wants, so they come back too.
    assert back["attendees"] == [
        {
            "email": "ann@example.com",
            "name": "Ann",
            "status": "NEEDS-ACTION",
            "role": "REQ-PARTICIPANT",
        }
    ]


def test_an_attendee_who_stays_keeps_what_the_server_recorded() -> None:
    component = _component()
    component.add(
        "ATTENDEE",
        _held_attendee("mailto:ann@example.com", PARTSTAT="ACCEPTED", CUTYPE="GROUP"),
        encode=False,
    )

    # An unrelated edit rewrites the whole list, and a reply already given must
    # survive it rather than being reset to NEEDS-ACTION.
    apply_extras(
        component, {"attendees": [{"email": "ann@example.com", "name": "Ann Meier"}]}
    )

    written = _written(component)
    assert "PARTSTAT=ACCEPTED" in written
    assert "CUTYPE=GROUP" in written
    assert 'CN="Ann Meier"' in written


def test_a_named_status_still_overrides_the_one_on_the_server() -> None:
    component = _component()
    component.add(
        "ATTENDEE", _held_attendee("mailto:ann@example.com", PARTSTAT="ACCEPTED")
    )

    apply_extras(
        component, {"attendees": [{"email": "ann@example.com", "status": "DECLINED"}]}
    )

    assert "PARTSTAT=DECLINED" in _written(component)


def _held_attendee(address: str, **params: str):
    from icalendar import vCalAddress, vText

    held = vCalAddress(address)
    for name, value in params.items():
        held.params[name] = vText(value)
    return held


def _valarms(written: str) -> list[str]:
    """Return each VALARM on its own, so an assertion cannot match a sibling."""
    return [part.split("END:VALARM")[0] for part in written.split("BEGIN:VALARM")[1:]]


def test_a_display_alarm_gets_the_description_rfc_5545_requires() -> None:
    component = _component()

    apply_extras(component, {"alarms": [{"minutes_before": 15, "action": "DISPLAY"}]})

    assert "DESCRIPTION:Reminder" in _valarms(_written(component))[0]


def test_an_audio_alarm_gets_no_description() -> None:
    component = _component()

    apply_extras(
        component,
        {
            "alarms": [
                {"minutes_before": 15, "action": "DISPLAY"},
                {"minutes_before": 30, "action": "AUDIO", "description": "ignored"},
            ]
        },
    )

    # RFC 5545 does not permit a DESCRIPTION on an AUDIO alarm, and a sibling
    # DISPLAY alarm in the same object would satisfy a whole-document match.
    display, audio = _valarms(_written(component))
    assert "DESCRIPTION" in display
    assert "DESCRIPTION" not in audio


def test_an_alarm_can_hang_off_the_end_of_the_event() -> None:
    component = _component()

    apply_extras(component, {"alarms": [{"minutes_before": 5, "related": "END"}]})

    assert "RELATED=END" in _written(component)


def test_an_alarm_related_to_the_end_reads_back_as_such() -> None:
    extras = read_extras(
        _vevent(
            "BEGIN:VALARM\nACTION:DISPLAY\nDESCRIPTION:Reminder\n"
            "TRIGGER;RELATED=END:-PT5M\nEND:VALARM"
        )
    )

    assert extras["alarms"] == [
        {
            "minutes_before": 5,
            "related": "END",
            "action": "DISPLAY",
            "description": "Reminder",
        }
    ]


def test_an_address_that_already_names_a_scheme_is_left_alone() -> None:
    component = _component()

    apply_extras(
        component,
        {
            "attendees": ["mailto:iven@example.com"],
            "organizer": "mailto:boss@example.com",
        },
    )

    written = _written(component)
    assert "mailto:mailto:" not in written
    assert written.count("mailto:iven@example.com") == 1
    assert "ORGANIZER:mailto:boss@example.com" in written


def test_a_sub_minute_alarm_still_reads_as_one_minute_before() -> None:
    """Rounded away from zero, or it would report a reminder after the event."""
    event = _vevent(
        "BEGIN:VALARM\nACTION:DISPLAY\nDESCRIPTION:x\nTRIGGER:-PT90S\nEND:VALARM"
    )

    assert read_extras(event)["alarms"] == [
        {"minutes_before": 2, "action": "DISPLAY", "description": "x"}
    ]


def test_an_address_is_compared_without_its_scheme_or_case() -> None:
    from custom_components.ha_caldav.event import comparable_address

    assert comparable_address(" MailTo:User@Example.com ") == "user@example.com"


def test_clearing_the_categories_removes_the_property() -> None:
    component = _component()
    apply_extras(component, {"categories": ["work"]})

    apply_extras(component, {"categories": []})

    assert "CATEGORIES" not in _written(component)


def test_categories_are_reported_without_blanks_or_padding() -> None:
    event = _vevent("CATEGORIES: work ,,home")

    assert read_extras(event)["categories"] == ["work", "home"]


def test_an_all_day_until_keeps_its_date_wherever_this_runs() -> None:
    """Google writes a DATE start with a UTC UNTIL. Reconciling that through
    the local zone moves the date a day west of UTC and drops the last
    occurrence, which would make the series shape depend on the installation.
    """
    from custom_components.ha_caldav.event import rule_from

    document = ICalCalendar.from_ical(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VEVENT\r\nUID:s\r\nDTSTAMP:20260101T000000Z\r\n"
        "DTSTART;VALUE=DATE:20260706\r\n"
        "RRULE:FREQ=DAILY;UNTIL=20260731T000000Z\r\nSUMMARY:S\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    master = next(iter(document.walk("VEVENT")))

    # The stored value, not a datetime made from it: converting first is what
    # makes an all-day series look like a floating one, and the two read the
    # UNTIL beside them in different frames.
    moments = list(rule_from(master["RRULE"], master["DTSTART"].dt))

    assert moments[-1].date() == date(2026, 7, 31)
    assert len(moments) == 26


def test_a_floating_until_is_read_in_local_terms_not_in_utc() -> None:
    """A floating series is dated in local terms and to_utc reads it that way,
    so its end has to be a local wall time too. Read in UTC the rule ends a
    whole offset away from its own occurrences, and the series keeps or loses
    its last one depending on where Home Assistant runs."""
    from custom_components.ha_caldav.event import rule_from

    document = ICalCalendar.from_ical(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VEVENT\r\nUID:f\r\nDTSTAMP:20260101T000000Z\r\n"
        "DTSTART:20260105T200000\r\n"
        "RRULE:FREQ=WEEKLY;UNTIL=20260202T190000Z\r\nSUMMARY:F\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    master = next(iter(document.walk("VEVENT")))
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(ZoneInfo("Europe/Berlin"))
    try:
        moments = list(rule_from(master["RRULE"], master["DTSTART"].dt))
    finally:
        dt_util.set_default_time_zone(previous)

    # 19:00Z is 20:00 in Berlin, so the occurrence on 2 February is the last one
    # the rule still reaches.
    assert moments[-1] == datetime(2026, 2, 2, 20, 0)


def test_naming_the_same_organizer_again_keeps_their_parameters() -> None:
    """read_extras reports the bare address, so an unrelated edit names it
    again; rebuilt from that alone the line loses CN and the SENT-BY that
    authorises an assistant to act for them."""
    event = ICalEvent.from_ical(
        "BEGIN:VEVENT\r\nUID:u\r\nDTSTAMP:20260101T000000Z\r\n"
        "DTSTART:20260706T090000Z\r\n"
        'ORGANIZER;CN=The Boss;SENT-BY="mailto:pa@example.com"'
        ":mailto:boss@example.com\r\nEND:VEVENT\r\n"
    )

    apply_extras(event, {"organizer": "boss@example.com"})

    assert event["ORGANIZER"].params["CN"] == "The Boss"
    assert event["ORGANIZER"].params["SENT-BY"] == "mailto:pa@example.com"


def test_a_different_organizer_does_not_inherit_the_old_parameters() -> None:
    event = ICalEvent.from_ical(
        "BEGIN:VEVENT\r\nUID:u\r\nDTSTAMP:20260101T000000Z\r\n"
        "DTSTART:20260706T090000Z\r\n"
        "ORGANIZER;CN=The Boss:mailto:boss@example.com\r\nEND:VEVENT\r\n"
    )

    apply_extras(event, {"organizer": "someone@example.com"})

    assert str(event["ORGANIZER"]) == "mailto:someone@example.com"
    assert "CN" not in event["ORGANIZER"].params


def test_an_attendee_who_already_answered_is_not_asked_again() -> None:
    """RSVP=TRUE re-requests a reply, and the server relays that."""
    event = ICalEvent.from_ical(
        "BEGIN:VEVENT\r\nUID:u\r\nDTSTAMP:20260101T000000Z\r\n"
        "DTSTART:20260706T090000Z\r\n"
        "ATTENDEE;PARTSTAT=ACCEPTED;CN=Iven:mailto:iven@example.com\r\n"
        "END:VEVENT\r\n"
    )

    apply_extras(
        event,
        {"attendees": [{"email": "iven@example.com", "status": "ACCEPTED"}]},
    )

    attendee = event["ATTENDEE"]
    attendee = attendee[0] if isinstance(attendee, list) else attendee
    assert "RSVP" not in attendee.params
    assert str(attendee.params["PARTSTAT"]) == "ACCEPTED"


def test_an_attendee_added_for_the_first_time_is_asked_to_reply() -> None:
    event = ICalEvent.from_ical(
        "BEGIN:VEVENT\r\nUID:u\r\nDTSTAMP:20260101T000000Z\r\n"
        "DTSTART:20260706T090000Z\r\nEND:VEVENT\r\n"
    )

    apply_extras(event, {"attendees": ["new@example.com"]})

    attendee = event["ATTENDEE"]
    attendee = attendee[0] if isinstance(attendee, list) else attendee
    assert str(attendee.params["RSVP"]) == "TRUE"


def test_attendees_written_without_an_organizer_get_one() -> None:
    """RFC 5546 3 requires ORGANIZER wherever ATTENDEE appears. sabre/dav —
    Baikal and much else — hands the missing one to its scheduling plugin when
    the object is deleted and answers 500, and the event cannot be removed at
    all after that. Nextcloud guards its own copy of that plugin, and the two
    servers that do no scheduling never look, so only a sabre server shows it.
    """
    from custom_components.ha_caldav.event import apply_extras

    component = ICalEvent()
    apply_extras(
        component,
        {"attendees": ["ada@example.com"]},
        own_address="mailto:iven@example.com",
    )

    assert str(component["ORGANIZER"]) == "mailto:iven@example.com"


def test_an_organizer_the_server_already_holds_is_not_claimed() -> None:
    # Someone else's event, on a calendar shared with us: naming ourselves the
    # organizer would take it over.
    from custom_components.ha_caldav.event import apply_extras

    component = ICalEvent()
    component.add("organizer", "mailto:ada@example.com")
    apply_extras(
        component,
        {"attendees": ["bob@example.com"]},
        own_address="mailto:iven@example.com",
    )

    assert str(component["ORGANIZER"]) == "mailto:ada@example.com"


def test_an_event_without_attendees_gets_no_organizer() -> None:
    from custom_components.ha_caldav.event import apply_extras

    component = ICalEvent()
    apply_extras(component, {"summary": "Alone"}, own_address="mailto:iven@example.com")

    assert component.get("ORGANIZER") is None


@pytest.mark.parametrize("interval", ["1", "2", "12"])
def test_an_ordinary_interval_is_accepted(interval: str) -> None:
    """The guard is against a non-positive INTERVAL. One off by a step refuses
    INTERVAL=1, which many clients write out explicitly, and every such series
    becomes uneditable."""
    from custom_components.ha_caldav.event import rule_from

    document = ICalCalendar.from_ical(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VEVENT\r\nUID:i\r\nDTSTAMP:20260101T000000Z\r\n"
        "DTSTART:20260706T090000Z\r\n"
        f"RRULE:FREQ=DAILY;INTERVAL={interval};COUNT=3\r\nSUMMARY:S\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    master = next(iter(document.walk("VEVENT")))

    moments = list(rule_from(master["RRULE"], master["DTSTART"].dt))

    assert len(moments) == 3
    assert moments[1] - moments[0] == timedelta(days=int(interval))


def test_a_line_break_in_the_organizer_does_not_reach_the_content_line() -> None:
    """icalendar asserts on an unescaped one and the failure reaches the user as
    a server error, though nothing about it came from the server. A template in
    a service call is enough to produce one; create_event escapes the same text
    correctly, so only this path was exposed."""
    from custom_components.ha_caldav.event import apply_extras

    component = ICalEvent()

    apply_extras(component, {"organizer": "ada@example.com\r\nEND:VEVENT"})

    lines = component.to_ical().decode("utf-8").splitlines()
    # One content line, so the text cannot pass for structure around it.
    assert lines == [
        "BEGIN:VEVENT",
        "ORGANIZER:ada@example.com END:VEVENT",
        "END:VEVENT",
    ]


def test_a_line_break_in_the_url_does_not_reach_the_content_line() -> None:
    # Same hazard as the organizer, same source: a template in a service call.
    from custom_components.ha_caldav.event import apply_extras

    component = ICalEvent()

    apply_extras(component, {"url": "https://meet.example.com/x\r\nEND:VEVENT"})

    lines = component.to_ical().decode("utf-8").splitlines()
    assert lines == [
        "BEGIN:VEVENT",
        "URL:https://meet.example.com/x END:VEVENT",
        "END:VEVENT",
    ]
