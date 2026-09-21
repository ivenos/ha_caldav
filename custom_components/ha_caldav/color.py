"""Calendar colors and names read from the CalDAV server."""

from __future__ import annotations

import re
from typing import NamedTuple

import caldav
from caldav.elements import dav, ical

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


class Collection(NamedTuple):
    """The color and display name the server reports for one collection."""

    color: str | None
    name: str | None


def fetch_collections(client: caldav.DAVClient) -> dict[str, Collection]:
    """Return calendar key -> color and name for everything the home set holds.

    Whatever a calendar lacks holds None.
    """
    home = client.principal().calendar_home_set
    # The parsed form collapses to the home set itself.
    response = home.get_properties(
        [ical.CalendarColor(), dav.DisplayName()], depth=1, parse_response_xml=False
    )
    collections: dict[str, Collection] = {}
    # The element itself: caldav's expansion logs an error for an unknown
    # attribute, and Apple sends one. The href is already unquoted.
    for href, props in response.find_objects_and_props().items():
        color = props.get(ical.CalendarColor.tag)
        name = props.get(dav.DisplayName.tag)
        collections[href.rstrip("/")] = Collection(
            None if color is None else normalize_color(color.text),
            None if name is None else name.text or None,
        )
    return collections
