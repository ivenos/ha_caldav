"""Calendar colors read from the CalDAV server."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

import caldav
from caldav.elements import ical

_HEX = re.compile(r"[0-9a-fA-F]+")
_EXPANDABLE = (3, 4)
_FULL = (6, 8)


def normalize_color(value: object) -> str | None:
    """Return value as #rrggbb, or None if it is not a color we can use.

    A server hands back whatever a client wrote into calendar-color; the CSS
    shorthand and a trailing alpha pair both occur, and Home Assistant, which
    validates against #rrggbb, accepts neither.
    """
    if not isinstance(value, str):
        return None
    digits = value.strip().removeprefix("#")
    if len(digits) in _EXPANDABLE:
        digits = "".join(digit * 2 for digit in digits)
    if len(digits) not in _FULL or not _HEX.fullmatch(digits):
        return None
    return f"#{digits[:6].lower()}"


def calendar_key(url: object) -> str:
    """Return the comparable form of a calendar url.

    A calendar url keeps its percent-encoding, the hrefs it is matched against
    do not, and the two need not agree on a trailing slash.
    """
    path = str(url)
    if "://" in path:
        path = urlparse(path).path
    return unquote(path).rstrip("/")


def fetch_colors(client: caldav.DAVClient) -> dict[str, str | None]:
    """Return calendar key -> color for everything the home set reports on.

    A single depth-1 PROPFIND on the calendar home set covers every calendar.
    Anything answered for is a key, colorless ones included, so that a calendar
    missing from the result stays distinguishable from one without a color.
    """
    home = client.principal().calendar_home_set
    # The parsed form collapses to the home set itself; the raw response keeps
    # the per-href props that depth 1 returns.
    response = home.get_properties(
        [ical.CalendarColor()], depth=1, parse_response_xml=False
    )
    colors: dict[str, str | None] = {}
    # Read the element rather than let caldav expand it: its expansion logs an
    # error for every attribute it does not know, and Apple sends one.
    for href, props in response.find_objects_and_props().items():
        element = props.get(ical.CalendarColor.tag)
        # Already unquoted by caldav; unquoting twice would fold a literal
        # percent sequence into something no calendar url reduces to.
        colors[href.rstrip("/")] = (
            None if element is None else normalize_color(element.text)
        )
    return colors
