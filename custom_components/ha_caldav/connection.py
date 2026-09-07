"""How the connection details in a config entry become client arguments."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
import logging
from typing import Any
from urllib.parse import unquote, urlparse, urlunparse

import caldav
from caldav.davclient import requests
from homeassistant.const import CONF_VERIFY_SSL

from .const import CONF_CA_BUNDLE, CONF_CLIENT_CERT, CONF_CLIENT_KEY, DEFAULT_TIMEOUT

_LOGGER = logging.getLogger(__name__)

WELL_KNOWN = "/.well-known/caldav"


def calendar_key(url: object) -> str:
    """Return the comparable form of a calendar url.

    Hrefs come back unquoted and need not agree with the url on a trailing slash.
    """
    path = str(url)
    if "://" in path:
        path = urlparse(path).path
    return unquote(path).rstrip("/")


_DEFAULT_PORTS = {"http": 80, "https": 443}


def account_key(url: str, username: str) -> str:
    """Return the identity of an account, shared by every spelling of its url.

    Config entries are keyed on this, so changing its shape needs a migration.
    """
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return f"{url}#{username}"
    scheme = parsed.scheme.lower()
    if ":" in host:
        host = f"[{host}]"
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        host = f"{host}:{port}"
    normalized = urlunparse((scheme, host, parsed.path.rstrip("/"), "", "", ""))
    return f"{normalized}#{username}"


def connection_kwargs(
    data: Mapping[str, Any], timeout: float = DEFAULT_TIMEOUT
) -> dict[str, Any]:
    """Return the DAVClient keyword arguments for these connection details.

    A CA bundle path replaces the verify flag, which is how requests reads it.
    """
    verify: bool | str = data.get(CONF_VERIFY_SSL, True)
    if bundle := data.get(CONF_CA_BUNDLE):
        verify = bundle
    cert: str | tuple[str, str] | None = None
    if certificate := data.get(CONF_CLIENT_CERT):
        key = data.get(CONF_CLIENT_KEY)
        cert = (certificate, key) if key else certificate
    return {
        "ssl_verify_cert": verify,
        "ssl_cert": cert,
        "timeout": timeout,
    }


def display_name(calendar: object) -> str:
    """Return the name a calendar is selected and shown by."""
    return getattr(calendar, "name", None) or "CalDAV"


def without_userinfo(url: str) -> str:
    """Return the url with any user:password@ part removed.

    caldav logs the url it was handed before stripping those itself.
    """
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        if not parsed.username and not parsed.password:
            return url
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        # Too malformed for urlparse; an @ after the first slash is path.
        scheme, sep, rest = url.rpartition("://")
        head, at, tail = rest.partition("@")
        if not at or "/" in head:
            return url
        return f"{scheme}{sep}{tail}"
    # hostname drops the brackets of an IPv6 literal.
    if ":" in host:
        host = f"[{host}]"
    return urlunparse(parsed._replace(netloc=f"{host}:{port}" if port else host))


def url_candidates(url: str, kwargs: Mapping[str, Any]) -> Iterator[str]:
    """Yield the urls to try, the entered one first. Blocking past the first.

    RFC 6764 puts a bootstrap redirect at /.well-known/caldav, which resolves a
    bare host name.
    """
    # A scheme-less entry would reach requests as a relative url.
    entered = without_userinfo(url if "://" in url else f"https://{url}")
    yield entered
    try:
        parsed = urlparse(entered)
    except ValueError:
        return
    if parsed.netloc and parsed.path.rstrip("/") != WELL_KNOWN.rstrip("/"):
        probe = urlunparse((parsed.scheme, parsed.netloc, WELL_KNOWN, "", "", ""))
        if (resolved := _resolve_bootstrap(probe, kwargs)) is not None:
            yield resolved


def _resolve_bootstrap(probe: str, kwargs: Mapping[str, Any]) -> str | None:
    """Return where the bootstrap points, followed without credentials.

    Followed with credentials, a redirect answers a digest challenge from
    whoever replied, and that answer cracks offline.
    """
    try:
        response = requests.request(
            "GET",
            probe,
            allow_redirects=True,
            verify=kwargs.get("ssl_verify_cert", True),
            cert=kwargs.get("ssl_cert"),
            timeout=kwargs.get("timeout", DEFAULT_TIMEOUT),
        )
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Could not reach %s", WELL_KNOWN)
        return None
    if urlparse(probe).scheme == "https" and any(
        urlparse(str(hop.url)).scheme != "https"
        for hop in (*response.history, response)
    ):
        # A proxy terminating TLS without X-Forwarded-Proto redirects to http.
        _LOGGER.debug("Ignoring a bootstrap that steps out of https")
        return None
    # caldav prefers userinfo in the url over the account handed to it.
    return without_userinfo(str(response.url) or probe)


class HostLockedSession(requests.Session):
    """A session that carries no credentials across a change of host.

    requests drops the Authorization header itself, but the digest response
    hook outlives the redirect and re-signs for the new host.
    """

    def rebuild_auth(self, prepared_request: Any, response: Any) -> None:
        """Strip what authenticates a request that has changed host."""
        super().rebuild_auth(prepared_request, response)
        if self.should_strip_auth(response.request.url, prepared_request.url):
            prepared_request.hooks["response"] = []


def build_client(
    url: str, username: str, password: str, kwargs: Mapping[str, Any]
) -> caldav.DAVClient:
    """Return a client for these details, locked to the host of its url."""
    client = caldav.DAVClient(url, username=username, password=password, **kwargs)
    # caldav never configures its session; multiplexing is optional to it too.
    try:
        client.session = HostLockedSession(multiplexed=True)
    except TypeError:
        client.session = HostLockedSession()
    return client
