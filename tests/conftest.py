from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

import caldav
from caldav.elements import dav
from caldav.lib.url import URL
import pytest

from custom_components.ha_caldav.patches import apply as apply_patches


class RecordingClient:
    """A caldav client that keeps what is PUT in memory instead of sending it, so
    caldav's own objects run for real, the preconditions of a write included."""

    def __init__(self) -> None:
        # A resource built against this client resolves its url through here.
        self.url = URL("https://dav.test/")
        self.puts: list[tuple[str, str]] = []
        self.put_headers: list[dict[str, str]] = []
        self.deletes: list[str] = []
        self.fail_from: int | None = None
        self.delete_status: int = 204
        self.stored: dict[str, tuple[str, str]] = {}

    def put(self, url, body, headers=None):
        url, headers = str(url), dict(headers or {})
        self.puts.append((url, body))
        self.put_headers.append(headers)
        if self.fail_from is not None and len(self.puts) > self.fail_from:
            return _answer(507, "Insufficient Storage")
        held = self.stored.get(url)
        if ("If-None-Match" in headers and held is not None) or (
            "If-Match" in headers and (held is None or held[1] != headers["If-Match"])
        ):
            return _answer(412, "Precondition Failed")
        self.stored[url] = (body, f'"{len(self.puts)}"')
        return _answer(204 if held else 201)

    def request(self, url, method="GET", body="", headers=None):
        url, headers = str(url), dict(headers or {})
        held = self.stored.get(url)
        if method == "GET":
            if held is None:
                return _answer(404, "Not Found")
            return _answer(200, raw=held[0], headers={"Etag": held[1]})
        self.deletes.append(url)
        if (
            "If-Match" in headers
            and held is not None
            and held[1] != headers["If-Match"]
        ):
            return _answer(412, "Precondition Failed")
        self.stored.pop(url, None)
        return _answer(self.delete_status, "Locked")

    @property
    def bodies(self) -> list[str]:
        return [body for _, body in self.puts]


def _answer(status: int, reason: str = "", raw: str = "", headers=None):
    return SimpleNamespace(status=status, reason=reason, headers=headers or {}, raw=raw)


def written_through(resource: Any, status: int = 204) -> Any:
    """Give a fake resource the client a write talks to: a PUT goes to its save and a
    DELETE to its delete, so a test reads what was written off the fake itself."""

    def put(url, body, headers=None):
        resource.save(headers=dict(headers or {}))
        return _answer(status)

    def request(url, method="GET", body="", headers=None):
        if method == "DELETE":
            resource.delete()
        return _answer(status)

    resource.client = SimpleNamespace(put=put, request=request)
    if not isinstance(getattr(resource, "props", None), dict):
        resource.props = {}
    if isinstance(getattr(resource, "url", None), (Mock, type(None))):
        resource.url = "https://dav.test/cal/resource.ics"
    return resource


def dav_calendar(url: str = "https://dav.test/cal/") -> Mock:
    calendar = Mock()
    calendar.client = RecordingClient()
    calendar.url = URL(url)
    return calendar


def stored(ics: str, etag: str | None = None) -> caldav.Event:
    """Return a search result the way caldav builds one from a REPORT."""
    item = caldav.Event(data=ics)
    if etag is not None:
        item.props[dav.GetEtag.tag] = etag
    return item


@pytest.fixture(autouse=True, scope="session")
def patched_libraries():
    """Read as the integration does once set up, whichever test runs first."""
    apply_patches()


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    return


@pytest.fixture(autouse=True)
def bootstrap_probe():
    """Resolve the unauthenticated well-known lookup to the url probed."""

    def answer(_method: str, url: str, **_kwargs: object) -> Mock:
        return Mock(url=url, history=[])

    with patch(
        "custom_components.ha_caldav.connection.requests.request",
        side_effect=answer,
    ) as request:
        yield request


def propfind_answer(found: dict[str, dict[str, object]]) -> Mock:
    """Return a depth-1 PROPFIND answer, href -> property tag -> text. A property
    left out is one the server answered 404 for."""
    root = ET.Element(dav.MultiStatus.tag)
    for href, props in found.items():
        response = ET.SubElement(root, dav.Response.tag)
        ET.SubElement(response, dav.Href.tag).text = href
        propstat = ET.SubElement(response, dav.PropStat.tag)
        holder = ET.SubElement(propstat, dav.Prop.tag)
        for tag, value in props.items():
            element = ET.SubElement(holder, tag)
            if isinstance(value, tuple):
                # A property holding elements, such as a resource type.
                for child in value:
                    ET.SubElement(element, child)
            else:
                element.text = getattr(value, "text", value)
        ET.SubElement(propstat, dav.Status.tag).text = "HTTP/1.1 200 OK"
    return Mock(tree=root)
