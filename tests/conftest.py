from types import SimpleNamespace
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from caldav.elements import dav
from caldav.lib.url import URL
import pytest


class RecordingClient:
    """A caldav client that records what would be PUT instead of sending it,
    so caldav's own save path runs for real."""

    def __init__(self) -> None:
        # A resource built against this client resolves its url through here.
        self.url = URL("https://dav.test/")
        self.puts: list[tuple[str, str]] = []
        self.deletes: list[str] = []
        self.fail_from: int | None = None
        self.delete_status: int = 204

    def put(self, url, body, headers=None):
        self.puts.append((str(url), body))
        if self.fail_from is not None and len(self.puts) > self.fail_from:
            # A status, not an exception: caldav answers one outside (201, 204, 302) by
            # reserializing through vobject and putting a second time.
            return SimpleNamespace(
                status=507, reason="Insufficient Storage", headers=[], raw=""
            )
        return SimpleNamespace(status=201, headers=[], raw="")

    def delete(self, url):
        self.deletes.append(str(url))
        return SimpleNamespace(
            status=self.delete_status, reason="Locked", headers=[], raw=""
        )

    @property
    def bodies(self) -> list[str]:
        return [body for _, body in self.puts]


def dav_calendar(url: str = "https://dav.test/cal/") -> Mock:
    calendar = Mock()
    calendar.client = RecordingClient()
    calendar.url = URL(url)
    return calendar


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
            ET.SubElement(holder, tag).text = getattr(value, "text", value)
        ET.SubElement(propstat, dav.Status.tag).text = "HTTP/1.1 200 OK"
    return Mock(tree=root)
