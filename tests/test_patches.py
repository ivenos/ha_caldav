import logging

import caldav
from caldav.davclient import requests

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

    apply()

    assert caldav.lib.vcal.fix is fix
    assert len(logging.getLogger("caldav").filters) == 1
