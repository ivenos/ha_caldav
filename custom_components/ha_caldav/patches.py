"""Fixes to caldav 2.1.0 that no call of ours can route around."""

from __future__ import annotations

import logging
import re
from typing import Any

from caldav.lib import vcal
from caldav.lib.python_utilities import to_normal_str

# caldav's own rule for this eats the line break behind the date, as SOGo writes it.
_DATE_COMPLETED = re.compile(r"^COMPLETED(?:;VALUE=DATE)?:(\d{8})$", re.MULTILINE)
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
    logger = logging.getLogger("caldav")
    if not any(isinstance(item, _NoSessionCookies) for item in logger.filters):
        logger.addFilter(_NoSessionCookies())


def _dated_completion_fixed(original: Any) -> Any:
    def fix(event: Any) -> Any:
        text = to_normal_str(event)
        if text is not None:
            text = _DATE_COMPLETED.sub(r"COMPLETED:\1T120000Z", text)
        return original(text)

    fix.ha_caldav = True  # type: ignore[attr-defined]
    return fix
