from datetime import UTC, datetime
import logging
from unittest.mock import Mock, patch

import caldav
from caldav.davclient import DAVResponse, requests
import pytest
import vobject

from custom_components.ha_caldav.patches import apply

SOGO_DONE = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nBEGIN:VTODO\r\n"
    "UID:t-1\r\nDTSTAMP:20260101T000000Z\r\nSUMMARY:Done in SOGo\r\n"
    "STATUS:COMPLETED\r\nCOMPLETED;VALUE=DATE:20260105\r\nEND:VTODO\r\n"
    "END:VCALENDAR\r\n"
)


def test_a_todo_completed_on_a_date_can_still_be_read() -> None:
    apply()

    todo = caldav.Todo(data=SOGO_DONE).vobject_instance.vtodo

    assert todo.summary.value == "Done in SOGo"
    assert todo.completed.value.isoformat() == "2026-01-05T12:00:00+00:00"


def test_text_that_reads_like_a_completion_date_is_left_as_written() -> None:
    apply()
    body = "Deal COMPLETED:20260101 - archive the tickets COMPLETED:3 of 5"

    todo = caldav.Todo(
        data=SOGO_DONE.replace("SUMMARY:Done in SOGo", f"DESCRIPTION:{body}")
    )

    assert todo.vobject_instance.vtodo.description.value == body
    assert str(todo.icalendar_component["DESCRIPTION"]) == body


BROKEN_BERLIN = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
    "BEGIN:VTIMEZONE\r\nTZID:Europe/Berlin\r\nBEGIN:STANDARD\r\n"
    "DTSTART:19701025T030000\r\nTZOFFSETFROM:+0200\r\nTZOFFSETTO:+0100\r\n"
    "END:STANDARD\r\nEND:VTIMEZONE\r\n"
    "BEGIN:VEVENT\r\nUID:winter\r\nDTSTAMP:20260101T000000Z\r\n"
    "DTSTART;TZID=Europe/Berlin:20260106T090000\r\nSUMMARY:x\r\nEND:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)
SUMMER = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
    "BEGIN:VEVENT\r\nUID:summer\r\nDTSTAMP:20260101T000000Z\r\n"
    "DTSTART;TZID=Europe/Berlin:20260706T090000\r\nSUMMARY:x\r\nEND:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


def test_one_object_with_a_broken_zone_does_not_move_the_next_one() -> None:
    """vobject keeps the first VTIMEZONE it reads under a TZID for the process."""
    apply()
    vobject.readOne(BROKEN_BERLIN)

    start = vobject.readOne(SUMMER).vevent.dtstart.value

    assert start.astimezone(UTC) == datetime(2026, 7, 6, 7, 0, tzinfo=UTC)


def test_the_session_cookies_a_server_sets_stay_out_of_the_debug_log(
    caplog,
) -> None:
    apply()
    headers = requests.structures.CaseInsensitiveDict(
        {"Date": "today", "Set-Cookie": "nc_session_id=secret; path=/"}
    )

    with caplog.at_level(logging.DEBUG, logger="caldav"):
        logging.getLogger("caldav").debug("response headers: " + str(headers))

    assert "secret" not in caplog.text
    assert "today" in caplog.text


def test_the_fixes_go_in_once_however_often_the_integration_is_set_up() -> None:
    apply()
    fix = caldav.lib.vcal.fix
    lookup = vobject.icalendar.getTzid

    apply()

    assert caldav.lib.vcal.fix is fix
    assert vobject.icalendar.getTzid is lookup
    assert len(logging.getLogger("caldav").filters) == 1


def _report(*hrefs: str) -> DAVResponse:
    entries = "".join(
        f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
        f"<c:calendar-data>{SUMMER.replace('UID:summer', f'UID:{href}')}"
        "</c:calendar-data>"
        "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        for href in hrefs
    )
    body = (
        '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
        f'xmlns:c="urn:ietf:params:xml:ns:caldav">{entries}</d:multistatus>'
    )
    response = Mock(status_code=207, headers={"Content-Type": "application/xml"})
    response.text, response.content = body, body.encode()
    return DAVResponse(response)


@pytest.mark.parametrize("href", ["/cal/a%2Fb.ics", "/cal/a/b.ics"])
def test_a_slash_in_a_resource_name_survives_the_report(href: str) -> None:
    """caldav decodes a %2F in an href, and Xandikos sends it decoded."""
    apply()
    client = caldav.DAVClient("https://dav.test/")
    calendar = caldav.Calendar(client=client, url="https://dav.test/cal/")

    with patch.object(
        caldav.Calendar,
        "_query",
        side_effect=lambda *_: _report(href, "/cal/plain%20name.ics"),
    ):
        found = calendar.search(event=True)

    assert sorted(str(item.url) for item in found) == [
        "https://dav.test/cal/a%2Fb.ics",
        "https://dav.test/cal/plain%20name.ics",
    ]
