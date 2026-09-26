"""Calendar colors and names read from the CalDAV server."""

from __future__ import annotations

import re
from typing import NamedTuple

import caldav
from caldav.elements import cdav, dav, ical
from homeassistant.util.color import color_name_to_rgb

from .capability import objects_and_props

_HEX = re.compile(r"[0-9a-fA-F]+")
_EXPANDABLE = (3, 4)
_FULL = (6, 8)


def normalize_color(value: object) -> str | None:
    """Return value as #rrggbb, or None if unusable.

    Clients write CSS shorthand, color names and trailing alpha pairs; core
    accepts none of them. Shorthand needs its "#", or "bad" would be a color.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text.startswith("#"):
        try:
            return "#{:02x}{:02x}{:02x}".format(*color_name_to_rgb(text))
        except ValueError:
            pass
    digits = text.removeprefix("#")
    if len(digits) in _EXPANDABLE and text.startswith("#"):
        digits = "".join(digit * 2 for digit in digits)
    if len(digits) not in _FULL or not _HEX.fullmatch(digits):
        return None
    return f"#{digits[:6].lower()}"


class Collection(NamedTuple):
    """The color, display name and kind the server reports for one collection.

    A kind the server did not say is None.
    """

    color: str | None
    name: str | None
    calendar: bool | None = None


def fetch_collections(client: caldav.DAVClient) -> dict[str, Collection]:
    """Return calendar key -> color, name and kind for everything the home set holds.

    Whatever a calendar lacks holds None. A calendar is what caldav lists as
    one: a collection whose resource type names it.
    """
    home = client.principal().calendar_home_set
    # The parsed form collapses to the home set itself.
    response = home.get_properties(
        [ical.CalendarColor(), dav.DisplayName(), dav.ResourceType()],
        depth=1,
        parse_response_xml=False,
    )
    collections: dict[str, Collection] = {}
    for key, props in objects_and_props(response, home.url).items():
        color = props.get(ical.CalendarColor.tag)
        name = props.get(dav.DisplayName.tag)
        kind = props.get(dav.ResourceType.tag)
        collections[key] = Collection(
            None if color is None else normalize_color(color.text),
            None if name is None else name.text or None,
            None
            if kind is None
            else any(item.tag == cdav.Calendar.tag for item in kind),
        )
    return collections
