"""Sharing a calendar with other accounts, which no RFC covers.

Nextcloud speaks the ownCloud dialect and sabre/dav, Baikal with it, the
draft-pot-webdav-resource-sharing one. A server with neither is refused.
"""

from __future__ import annotations

from typing import Any, ClassVar

import caldav
from caldav.elements import dav
from caldav.elements.base import BaseElement
from caldav.lib.error import PutError, errmsg
from lxml import etree

from .capability import objects_and_props
from .errors import Refused

_OC = "http://owncloud.org/ns"
_DAV = "DAV:"


class _OwncloudInvite(BaseElement):
    tag: ClassVar[str] = f"{{{_OC}}}invite"


class _DavInvite(BaseElement):
    tag: ClassVar[str] = f"{{{_DAV}}}invite"


def _invite(calendar: caldav.Calendar) -> tuple[str, Any]:
    """Return the dialect a calendar is shared in and whom it is shared with."""
    response = calendar.get_properties(
        [_OwncloudInvite(), _DavInvite()], parse_response_xml=False
    )
    for props in objects_and_props(response, calendar.url).values():
        for dialect, tag in ((_OC, _OwncloudInvite.tag), (_DAV, _DavInvite.tag)):
            if tag in props:
                return dialect, props[tag]
    raise Refused("sharing_unsupported")


def read_shares(calendar: caldav.Calendar) -> list[dict[str, Any]]:
    """Return who a calendar is shared with, its owner left out."""
    dialect, invite = _invite(calendar)
    shares = []
    for sharee in invite:
        href = sharee.findtext(dav.Href.tag) or ""
        if dialect == _OC:
            writes = sharee.find(f"{{{_OC}}}access/{{{_OC}}}read-write") is not None
            access: str | None = "read_write" if writes else "read"
            name = sharee.findtext(f"{{{_OC}}}common-name")
        else:
            granted = sharee.find(f"{{{_DAV}}}share-access")
            kinds = (
                []
                if granted is None
                else [etree.QName(item).localname for item in granted]
            )
            access = {"read": "read", "read-write": "read_write"}.get("".join(kinds))
            name = sharee.findtext(f"{{{_DAV}}}prop/{dav.DisplayName.tag}")
        if not href or access is None:
            continue
        share = {"user": _short(href), "href": href, "access": access}
        if name:
            share["name"] = name
        if sharee.find(f"{{{_DAV}}}invite-invalid") is not None:
            share["invalid"] = True
        shares.append(share)
    return shares


def share_calendar(calendar: caldav.Calendar, user: str, write: bool) -> None:
    """Share a calendar with an account, or change what that account may do.

    sabre/dav keeps a sharee it could not find, as invalid; Nextcloud drops
    one without a word. Neither is a share.
    """
    dialect, _ = _invite(calendar)
    href = _href(dialect, user)
    _post(calendar, dialect, href, "read-write" if write else "read")
    listed = [share for share in read_shares(calendar) if _same(share["href"], href)]
    if not listed or listed[0].get("invalid"):
        if listed:
            _post(calendar, dialect, href, None)
        raise Refused("sharee_not_found", user=user)


def unshare_calendar(calendar: caldav.Calendar, user: str) -> None:
    """Stop sharing a calendar with an account."""
    dialect, _ = _invite(calendar)
    href = _href(dialect, user)
    listed = [share for share in read_shares(calendar) if _same(share["href"], href)]
    if not listed:
        raise Refused("not_shared_with", user=user)
    # sabre/dav lists a principal by its path and drops it by the address it
    # was shared under.
    known = listed[0]["href"] if dialect == _OC else _href(dialect, listed[0]["user"])
    _post(calendar, dialect, known, None)


def _href(dialect: str, user: str) -> str:
    """Return the address a server knows an account by.

    Nextcloud names one by its principal, where sabre/dav takes that or an
    email address. A value that already is an address is left alone.
    """
    user = user.strip()
    if ":" in user or "/" in user:
        return user
    if dialect == _OC:
        return f"principal:principals/users/{user}"
    return f"mailto:{user}" if "@" in user else f"principals/{user}"


def _short(href: str) -> str:
    """Return the name or email address in a sharee's address."""
    bare = href.removeprefix("principal:").removeprefix("mailto:")
    return bare.rstrip("/").rsplit("/", 1)[-1]


def _same(listed: str, sent: str) -> bool:
    """Return whether a listed sharee is the one asked for.

    sabre/dav lists a principal by its full path.
    """
    listed, sent = listed.rstrip("/").lower(), sent.rstrip("/").lower()
    return listed == sent or listed.endswith("/" + sent.removeprefix("principal:"))


def _post(
    calendar: caldav.Calendar, dialect: str, href: str, access: str | None
) -> None:
    """Send one sharee with its access, None taking the share away."""
    if dialect == _OC:
        root = etree.Element(f"{{{_OC}}}share", nsmap={"d": _DAV, "o": _OC})
        change = etree.SubElement(root, f"{{{_OC}}}{'set' if access else 'remove'}")
        etree.SubElement(change, dav.Href.tag).text = href
        if access == "read-write":
            etree.SubElement(change, f"{{{_OC}}}read-write")
        content_type = "application/xml; charset=utf-8"
    else:
        root = etree.Element(f"{{{_DAV}}}share-resource", nsmap={"d": _DAV})
        sharee = etree.SubElement(root, f"{{{_DAV}}}sharee")
        etree.SubElement(sharee, dav.Href.tag).text = href
        granted = etree.SubElement(sharee, f"{{{_DAV}}}share-access")
        etree.SubElement(granted, f"{{{_DAV}}}{access or 'no-access'}")
        content_type = "application/davsharing+xml; charset=utf-8"
    body = etree.tostring(root, xml_declaration=True, encoding="utf-8")
    response = calendar.client.request(
        str(calendar.url), "POST", body, {"Content-Type": content_type}
    )
    if response.status not in (200, 204):
        raise PutError(errmsg(response))
