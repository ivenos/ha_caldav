"""Fixes to caldav 2.1.0 and vobject 0.9.9 that no call of ours can route around."""

from __future__ import annotations

from contextlib import suppress
import logging
import re
from typing import Any
import zoneinfo

import caldav
from caldav.lib import vcal
from caldav.lib.python_utilities import to_normal_str
from caldav.lib.url import URL
from vobject import icalendar as vobject_icalendar

# caldav's own rule for this eats the line break behind the date, as SOGo writes it.
_DATE_COMPLETED = re.compile(r"^COMPLETED(?:;VALUE=DATE)?:(\d{8})$", re.MULTILINE)
_LOOSE_COMPLETED = re.compile(r"(?<=[^\n])COMPL(?=ETED(?:;VALUE=DATE)?:\d+\s)")
_SET_COOKIE = re.compile(
    r"""('set-cookie':\s*)('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")""", re.IGNORECASE
)


class _NoSessionCookies(logging.Filter):
    """Keep the session cookies a server sets out of caldav's debug log."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact the Set-Cookie values in a logged header dict."""
        if isinstance(record.msg, str) and "cookie" in record.msg.lower():
            record.msg = _SET_COOKIE.sub(r"\1'<redacted>'", record.msg)
        return True


def apply() -> None:
    """Install the fixes, once per process."""
    if not getattr(vcal.fix, "ha_caldav", False):
        vcal.fix = _dated_completion_fixed(vcal.fix)
    if not getattr(vobject_icalendar.getTzid, "ha_caldav", False):
        vobject_icalendar.getTzid = _named_zones_first(vobject_icalendar.getTzid)
    report = caldav.Calendar._request_report_build_resultlist
    if not getattr(report, "ha_caldav", False):
        caldav.Calendar._request_report_build_resultlist = _members_as_named(report)
    logger = logging.getLogger("caldav")
    if not any(isinstance(item, _NoSessionCookies) for item in logger.filters):
        logger.addFilter(_NoSessionCookies())


def _dated_completion_fixed(original: Any) -> Any:
    """Return caldav's fix with its COMPLETED rule kept to the property.

    caldav's rule is not anchored and rewrites the text of a DESCRIPTION too;
    a fold reads the same once unfolded (RFC 5545 3.1) and breaks the match.
    """

    def fix(event: Any) -> Any:
        text = to_normal_str(event)
        if text is not None:
            text = _DATE_COMPLETED.sub(r"COMPLETED:\1T120000Z", text)
            text = _LOOSE_COMPLETED.sub("COMPL\n ", text)
        return original(text)

    fix.ha_caldav = True  # type: ignore[attr-defined]
    return fix


def _named_zones_first(original: Any) -> Any:
    """Return vobject's zone lookup with IANA names resolved by zoneinfo.

    vobject keeps the first VTIMEZONE it parses under a TZID for the whole
    process, so one broken definition shifts every later object naming it.
    """

    def get_tzid(tzid: Any, smart: bool = True) -> Any:
        if tzid:
            with suppress(ValueError, zoneinfo.ZoneInfoNotFoundError):
                return zoneinfo.ZoneInfo(str(tzid))
        return original(tzid, smart)

    get_tzid.ha_caldav = True  # type: ignore[attr-defined]
    return get_tzid


def _members_as_named(original: Any) -> Any:
    """Return caldav's REPORT reader with a slash in a resource name encoded again.

    A depth-1 REPORT answers with members of the collection alone, so a path
    reaching deeper is a name holding a slash: caldav decodes a %2F in an href,
    and Xandikos sends it decoded to begin with.
    """

    def build(self: Any, *args: Any, **kwargs: Any) -> Any:
        response, matches = original(self, *args, **kwargs)
        base = str(self.url.path).rstrip("/") + "/"
        for match in matches:
            path = str(match.url.path)
            if path.startswith(base) and "/" in (name := path[len(base) :]):
                named = base + name.replace("/", "%2F")
                match.url = match.url.join(URL.objectify(named))
        return response, matches

    build.ha_caldav = True  # type: ignore[attr-defined]
    return build
