"""Fixtures for the CalDAV integration tests."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from caldav.lib.url import URL
import pytest


class RecordingClient:
    """A caldav client that records what would be PUT instead of sending it.

    Enough of one that caldav's own save path runs for real: the tests then see
    the document that would reach the server, rather than which library method
    happened to be called on the way to it. A mocked save_event hid that caldav
    reads its recurrence handling off the first component of whatever it is
    handed, and dies on a document whose exceptions come first.
    """

    def __init__(self) -> None:
        # A resource built against this client resolves its url through here.
        self.url = URL("https://dav.test/")
        self.puts: list[tuple[str, str]] = []
        self.deletes: list[str] = []
        # Index of the first PUT to refuse, for the rollback paths.
        self.fail_from: int | None = None
        # Status the refused DELETE answers with, for the rollback that fails.
        self.delete_status: int = 204

    def put(self, url, body, headers=None):
        """Record the write and answer the way a server would."""
        self.puts.append((str(url), body))
        if self.fail_from is not None and len(self.puts) > self.fail_from:
            # A status rather than an exception. caldav answers anything outside
            # (201, 204, 302) by reserializing through vobject and putting a
            # second time before it gives up, so a double that raises here hides
            # both that retry and the body it sends, which is vobject's and not
            # the one the write path built.
            return SimpleNamespace(
                status=507, reason="Insufficient Storage", headers=[], raw=""
            )
        return SimpleNamespace(status=201, headers=[], raw="")

    def delete(self, url):
        """Record the removal a rollback makes."""
        self.deletes.append(str(url))
        return SimpleNamespace(
            status=self.delete_status, reason="Locked", headers=[], raw=""
        )

    @property
    def bodies(self) -> list[str]:
        """Return just the documents written, in order."""
        return [body for _, body in self.puts]


def dav_calendar(url: str = "https://dav.test/cal/") -> Mock:
    """Return a calendar caldav can actually construct and save objects onto."""
    calendar = Mock()
    calendar.client = RecordingClient()
    calendar.url = URL(url)
    return calendar


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Make Home Assistant load the integration from custom_components."""
    return


@pytest.fixture(autouse=True)
def bootstrap_probe():
    """Resolve the unauthenticated well-known lookup to itself.

    It is a real request, so without this every test that reaches the bootstrap
    url would depend on the network. What the lookup decides is covered on its
    own in test_connection.py.
    """

    def answer(_method: str, url: str, **_kwargs: object) -> Mock:
        # Echoing the url probed rather than a fixed one: a test using another
        # host would otherwise be handed this one back as a candidate, and pass
        # or fail on the order the flow happens to try candidates in.
        return Mock(url=url, history=[])

    with patch(
        "custom_components.ha_caldav.connection.requests.request",
        side_effect=answer,
    ) as request:
        yield request
