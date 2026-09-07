"""Calendar colors read from the CalDAV server."""

from __future__ import annotations

import re

import caldav
from caldav.elements import ical

_HEX = re.compile(r"[0-9a-fA-F]+")
_EXPANDABLE = (3, 4)
_FULL = (6, 8)


def normalize_color(value: object) -> str | None:
    """Return value as #rrggbb, or None if unusable.

    Clients write CSS shorthand and trailing alpha pairs; core accepts neither.
    """
    if not isinstance(value, str):
        return None
    digits = value.strip().removeprefix("#")
    if len(digits) in _EXPANDABLE:
        digits = "".join(digit * 2 for digit in digits)
    if len(digits) not in _FULL or not _HEX.fullmatch(digits):
        return None
    return f"#{digits[:6].lower()}"


def fetch_colors(client: caldav.DAVClient) -> dict[str, str | None]:
    """Return calendar key -> color for everything the home set reports on.

    A calendar without a color is a key holding None.
    """
    home = client.principal().calendar_home_set
    # The parsed form collapses to the home set itself.
    response = home.get_properties(
        [ical.CalendarColor()], depth=1, parse_response_xml=False
    )
    colors: dict[str, str | None] = {}
    # The element itself: caldav's expansion logs an error for an unknown
    # attribute, and Apple sends one. The href is already unquoted.
    for href, props in response.find_objects_and_props().items():
        element = props.get(ical.CalendarColor.tag)
        colors[href.rstrip("/")] = (
            None if element is None else normalize_color(element.text)
        )
    return colors
