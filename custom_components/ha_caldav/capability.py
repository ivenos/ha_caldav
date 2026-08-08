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


# A server that answers neither property is not saying "nothing allowed"; it is
# saying nothing, and the pre-capability behaviour has to stand.
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
    # Already keyed the way capability_for asks for them.
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

    RFC 4918 9.1 lets a server answer 403 for a property the client may not
    read, which is exactly what one is likely to do for the privileges of a
    foreign collection. caldav's own parser raises on the whole response when
    it meets one, so a single shared calendar would cost every other calendar
    on the account its capabilities and leave them all permissively guessed.

    Keyed through calendar_key, because standing in for caldav's parser means
    standing in for the normalization it does: RFC 4918 8.3 lets an href be an
    absolute URI, and read literally not one of them would match the calendar
    it describes. First propstat wins, as caldav's does.
    """
    found: dict[str, dict[str, Any]] = {}
    for entry in response.tree.findall(".//" + dav.Response.tag):
        element = entry.find("./" + dav.Href.tag)
        if element is None or not element.text:
            continue
        if not _same_server(element.text, base):
            # calendar_key drops the scheme and the host, so a response for
            # another server keys onto the same path as a real calendar here,
            # and the first one seen wins. caldav's own parser resolves an href
            # against the request url and would never match it.
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
    """Return whether an href belongs to the server the request went to.

    A relative one always does. An absolute one only when it names the same
    host and scheme; RFC 4918 8.3 permits it, but nothing on another server has
    anything to say about a calendar on this one.
    """
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

    Radicale answers an empty component set with a single nameless comp
    element, which has to read the same as no answer at all.

    RFC 5545 makes component names case-insensitive, and a lowercase answer read
    literally would report a calendar as holding neither kind, which is what
    :func:`._async_prune_entities` deletes entities on.
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

    Only a server doing RFC 6638 scheduling answers this, and it is what an
    ATTENDEE line has to be matched against to find our own participation.
    """
    try:
        addresses = client.principal().calendar_user_address_set()
        return [_absolute(client, str(address)) for address in addresses if address]
    except Exception as err:  # noqa: BLE001
        # Anything from NotFoundError to a parse failure on servers that answer
        # the property with a shape caldav does not expect.
        _LOGGER.debug("No calendar user address set: %s", err)
        return []


def _absolute(client: caldav.DAVClient, address: str) -> str:
    """Return a calendar user address as the URI a CAL-ADDRESS has to be.

    RFC 6638 lets the set name the principal by its url, and sabre/dav and
    Nextcloud both answer with a bare path for an account carrying no mail
    address. Written onto an ATTENDEE as it stands, sabre reads it as a local
    principal, cannot resolve it, and answers 500 to every DELETE of the object
    from then on: the event cannot be removed at all, from here or from any
    other client. Resolved against the account it is a principal both ends can
    follow. An address that carries a scheme of its own keeps it.
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
        # Parsing fails on the same servers that answer the property oddly, so
        # the whole probe is guarded, not only the request.
        _LOGGER.debug("Could not read the supported report set: %s", err)
        return True
    return not answered
