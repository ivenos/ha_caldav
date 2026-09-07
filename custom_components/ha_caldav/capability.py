"""What the account and its calendars let us do, from one depth-1 PROPFIND."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, ClassVar
from urllib.parse import urljoin, urlparse

import caldav
from caldav.elements import cdav, dav
from caldav.elements.base import BaseElement
from caldav.lib.namespace import ns

from .connection import calendar_key
from .const import COMPONENT_EVENT, COMPONENT_TODO, WRITE_PRIVILEGES

_LOGGER = logging.getLogger(__name__)


class CurrentUserPrivilegeSet(BaseElement):
    """RFC 3744 current-user-privilege-set, which caldav does not model."""

    tag: ClassVar[str] = ns("D", "current-user-privilege-set")


@dataclass(frozen=True)
class Capability:
    """The component types and write access a calendar reports."""

    components: frozenset[str]
    writable: bool

    @property
    def supports_events(self) -> bool:
        """Return whether the calendar accepts VEVENT."""
        return COMPONENT_EVENT in self.components

    @property
    def supports_todos(self) -> bool:
        """Return whether the calendar accepts VTODO."""
        return COMPONENT_TODO in self.components


# A server answering neither property is saying nothing, not "nothing allowed".
UNKNOWN = Capability(frozenset({COMPONENT_EVENT, COMPONENT_TODO}), writable=True)


def fetch_capabilities(client: caldav.DAVClient) -> dict[str, Capability]:
    """Return calendar key -> capability for everything the home set reports on."""
    home = client.principal().calendar_home_set
    response = home.get_properties(
        [cdav.SupportedCalendarComponentSet(), CurrentUserPrivilegeSet()],
        depth=1,
        parse_response_xml=False,
    )
    capabilities: dict[str, Capability] = {}
    for href, props in _objects_and_props(response, home.url).items():
        components = _components(props.get(cdav.SupportedCalendarComponentSet.tag))
        privileges = _privileges(props.get(CurrentUserPrivilegeSet.tag))
        capabilities[href] = Capability(
            components=components or UNKNOWN.components,
            writable=bool(privileges & WRITE_PRIVILEGES) if privileges else True,
        )
    return capabilities


def _objects_and_props(response: Any, base: Any = None) -> dict[str, dict[str, Any]]:
    """Return href -> property tag -> element, tolerating a refused propstat.

    RFC 4918 9.1 lets a server answer 403 for one property, on which caldav's
    own parser raises for the whole response. RFC 4918 8.3 lets an href be
    absolute, hence calendar_key. First propstat wins, as in caldav.
    """
    found: dict[str, dict[str, Any]] = {}
    for entry in response.tree.findall(".//" + dav.Response.tag):
        element = entry.find("./" + dav.Href.tag)
        if element is None or not element.text:
            continue
        if not _same_server(element.text, base):
            # calendar_key drops the host, so this would key onto a real path.
            _LOGGER.debug("Ignoring a response for another server: %s", element.text)
            continue
        href = calendar_key(element.text)
        props = found.setdefault(href, {})
        for propstat in entry.findall("./" + dav.PropStat.tag):
            status = propstat.find("./" + dav.Status.tag)
            if status is not None and " 200 " not in (status.text or ""):
                _LOGGER.debug("Skipping %s on %s", status.text, href)
                continue
            for holder in propstat.findall("./" + dav.Prop.tag):
                for prop in holder:
                    props.setdefault(prop.tag, prop)
    return found


def _same_server(href: str, base: Any) -> bool:
    """Return whether an href is relative or names the server asked."""
    parsed = urlparse(href)
    if not parsed.netloc:
        return True
    if base is None:
        return False
    here = urlparse(str(base))
    return (parsed.scheme, parsed.netloc) == (here.scheme, here.netloc)


def capability_for(
    capabilities: dict[str, Capability], calendar: caldav.Calendar
) -> Capability:
    """Return the capability recorded for a calendar, or the permissive default."""
    return capabilities.get(calendar_key(calendar.url), UNKNOWN)


def _components(element: Any) -> frozenset[str]:
    """Return the component names, empty when the server named none.

    Radicale answers an empty set as one nameless comp element, and RFC 5545
    makes the names case-insensitive.
    """
    if element is None:
        return frozenset()
    return frozenset(
        name.upper() for child in element if (name := child.get("name")) is not None
    )


def _privileges(element: Any) -> frozenset[str]:
    """Return the granted privilege names, stripped of their namespace."""
    if element is None:
        return frozenset()
    names = set()
    for privilege in element:
        for granted in privilege:
            tag = str(granted.tag)
            names.add(tag.rsplit("}", 1)[-1])
    return frozenset(names)


def fetch_address_set(client: caldav.DAVClient) -> list[str]:
    """Return the calendar user addresses of the account, empty if unsupported.

    Only a server doing RFC 6638 scheduling answers this.
    """
    try:
        addresses = client.principal().calendar_user_address_set()
        return [_absolute(client, str(address)) for address in addresses if address]
    except Exception as err:  # noqa: BLE001
        # NotFoundError, or a parse failure on a shape caldav does not expect.
        _LOGGER.debug("No calendar user address set: %s", err)
        return []


def _absolute(client: caldav.DAVClient, address: str) -> str:
    """Return a calendar user address as an absolute URI.

    RFC 6638 lets the set name the principal by a bare path; written onto an
    ATTENDEE, sabre/dav cannot resolve it and answers 500 to every DELETE.
    """
    try:
        return urljoin(str(client.url), address)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Leaving a calendar user address as it stands: %s", err)
        return address


def supports_sync_collection(calendar: caldav.Calendar) -> bool:
    """Return whether the calendar advertises the RFC 6578 sync REPORT.

    Only an explicit report set without sync-collection counts as a no.
    """
    try:
        response = calendar.get_properties(
            [dav.SupportedReportSet()], parse_response_xml=False
        )
        answered = False
        for props in response.find_objects_and_props().values():
            element = props.get(dav.SupportedReportSet.tag)
            if element is None:
                continue
            answered = True
            if any("sync-collection" in str(node.tag) for node in element.iter()):
                return True
    except Exception as err:  # noqa: BLE001
        # Parsing fails on the same servers that answer the property oddly.
        _LOGGER.debug("Could not read the supported report set: %s", err)
        return True
    return not answered
