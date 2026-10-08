from types import SimpleNamespace
from unittest.mock import Mock, patch

from caldav.lib.error import PutError
from caldav.lib.url import URL
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import Unauthorized
from lxml import etree
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from custom_components.ha_caldav.const import DOMAIN
from custom_components.ha_caldav.errors import Refused
from custom_components.ha_caldav.sharing import (
    read_shares,
    share_calendar,
    unshare_calendar,
)

D = "{DAV:}"
OC = "{http://owncloud.org/ns}"


class SharedCalendar:
    """A calendar that keeps its sharees the way Nextcloud ("oc") or sabre/dav
    ("dav") does: Nextcloud drops an account it does not know, sabre/dav keeps it
    as invalid and lists a principal by its full path."""

    def __init__(self, dialect: str | None, known: tuple[str, ...] = ("ann",)) -> None:
        self.url = URL("https://dav.test/dav.php/calendars/me/work/")
        self.client = self
        self.dialect = dialect
        self.known = known
        self.sharees: dict[str, str] = {}
        self.posts: list[tuple[str, bytes]] = []
        self.status = 200

    def get_properties(self, props, parse_response_xml=False):
        root = etree.Element(f"{D}multistatus")
        response = etree.SubElement(root, f"{D}response")
        etree.SubElement(response, f"{D}href").text = "/dav.php/calendars/me/work/"
        for tag, mine in ((f"{OC}invite", "oc"), (f"{D}invite", "dav")):
            propstat = etree.SubElement(response, f"{D}propstat")
            invite = etree.SubElement(etree.SubElement(propstat, f"{D}prop"), tag)
            found = self.dialect == mine
            etree.SubElement(propstat, f"{D}status").text = (
                "HTTP/1.1 200 OK" if found else "HTTP/1.1 404 Not Found"
            )
            if found:
                self._list(invite)
        return SimpleNamespace(tree=root)

    def _list(self, invite) -> None:
        if self.dialect == "dav":
            owner = etree.SubElement(invite, f"{D}sharee")
            etree.SubElement(owner, f"{D}href").text = "/dav.php/principals/me"
            etree.SubElement(
                etree.SubElement(owner, f"{D}share-access"), f"{D}shared-owner"
            )
        for href, access in self.sharees.items():
            if self.dialect == "oc":
                user = etree.SubElement(invite, f"{OC}user")
                etree.SubElement(user, f"{D}href").text = href
                etree.SubElement(user, f"{OC}common-name").text = "Ann"
                granted = etree.SubElement(user, f"{OC}access")
                etree.SubElement(granted, f"{OC}{access}")
            else:
                sharee = etree.SubElement(invite, f"{D}sharee")
                etree.SubElement(sharee, f"{D}href").text = href
                etree.SubElement(
                    etree.SubElement(sharee, f"{D}share-access"), f"{D}{access}"
                )
                if not self._knows(href):
                    etree.SubElement(sharee, f"{D}invite-invalid")

    def _knows(self, href: str) -> bool:
        return href.rstrip("/").rsplit("/", 1)[-1].removeprefix("mailto:") in self.known

    def request(self, url, method, body, headers):
        self.posts.append((headers["Content-Type"], body))
        root = etree.XML(body)
        if self.status == 200 and self.dialect == "oc":
            for change in root:
                href = change.findtext(f"{D}href")
                if etree.QName(change).localname == "remove":
                    self.sharees.pop(href, None)
                elif self._knows(href):
                    writes = change.find(f"{OC}read-write") is not None
                    self.sharees[href] = "read-write" if writes else "read"
        elif self.status == 200:
            for sharee in root:
                href = sharee.findtext(f"{D}href")
                if href.startswith("principals/"):
                    href = f"/dav.php/{href}"
                access = etree.QName(sharee.find(f"{D}share-access")[0]).localname
                if access == "no-access":
                    self.sharees.pop(href, None)
                else:
                    self.sharees[href] = access
        return SimpleNamespace(status=self.status, reason="", raw="")


def test_nextcloud_is_asked_to_share_with_a_principal() -> None:
    calendar = SharedCalendar("oc")

    share_calendar(calendar, "ann", write=True)

    content_type, body = calendar.posts[0]
    assert content_type.startswith("application/xml")
    assert b"<d:href>principal:principals/users/ann</d:href>" in body
    assert b"read-write" in body
    assert read_shares(calendar) == [
        {
            "user": "ann",
            "href": "principal:principals/users/ann",
            "access": "read_write",
            "name": "Ann",
        }
    ]


def test_sabre_is_asked_to_share_with_a_principal_or_an_address() -> None:
    calendar = SharedCalendar("dav", known=("ann", "ann@example.com"))

    share_calendar(calendar, "ann", write=False)
    share_calendar(calendar, "ann@example.com", write=True)

    content_type, body = calendar.posts[0]
    assert content_type.startswith("application/davsharing+xml")
    assert b"<d:href>principals/ann</d:href>" in body
    assert b"<d:read/>" in body
    assert read_shares(calendar) == [
        {"user": "ann", "href": "/dav.php/principals/ann", "access": "read"},
        {
            "user": "ann@example.com",
            "href": "mailto:ann@example.com",
            "access": "read_write",
        },
    ]


def test_an_address_given_whole_is_sent_as_it_is() -> None:
    calendar = SharedCalendar("oc", known=("staff",))

    share_calendar(calendar, "principal:principals/groups/staff", write=False)

    assert b"<d:href>principal:principals/groups/staff</d:href>" in calendar.posts[0][1]


def test_an_account_nextcloud_does_not_know_is_refused() -> None:
    """Nextcloud answers 200 and shares with nobody."""
    calendar = SharedCalendar("oc")

    with pytest.raises(Refused, match="sharee_not_found"):
        share_calendar(calendar, "nobody", write=False)


def test_an_account_sabre_does_not_know_is_taken_back_out() -> None:
    """sabre/dav keeps the sharee, marked invalid."""
    calendar = SharedCalendar("dav")

    with pytest.raises(Refused, match="sharee_not_found"):
        share_calendar(calendar, "nobody", write=False)

    assert calendar.sharees == {}
    assert b"no-access" in calendar.posts[-1][1]


@pytest.mark.parametrize("dialect", ["oc", "dav"])
def test_a_share_is_taken_away_by_the_name_it_is_listed_under(dialect: str) -> None:
    calendar = SharedCalendar(dialect)
    share_calendar(calendar, "ann", write=True)

    unshare_calendar(calendar, read_shares(calendar)[0]["user"])

    assert calendar.sharees == {}


def test_taking_away_a_share_that_is_not_there_is_refused() -> None:
    calendar = SharedCalendar("oc")

    with pytest.raises(Refused, match="not_shared_with"):
        unshare_calendar(calendar, "ann")

    assert calendar.posts == []


def test_a_server_with_neither_dialect_is_refused() -> None:
    calendar = SharedCalendar(None)

    with pytest.raises(Refused, match="sharing_unsupported"):
        read_shares(calendar)
    with pytest.raises(Refused, match="sharing_unsupported"):
        share_calendar(calendar, "ann", write=False)

    assert calendar.posts == []


def test_a_refused_share_is_an_error() -> None:
    calendar = SharedCalendar("oc")
    calendar.status = 403

    with pytest.raises(PutError):
        share_calendar(calendar, "ann", write=False)


ENTRY_DATA = {
    CONF_URL: "https://cloud.example.com/remote.php/dav",
    CONF_USERNAME: "iven",
    CONF_PASSWORD: "secret",
    CONF_VERIFY_SSL: True,
}


async def _setup(hass: HomeAssistant) -> None:
    calendar = Mock()
    calendar.name = "Personal"
    calendar.url = "https://cloud.example.com/remote.php/dav/Personal"
    calendar.search.return_value = []
    entry = MockConfigEntry(domain=DOMAIN, title="iven", data=ENTRY_DATA, unique_id="x")
    entry.add_to_hass(hass)
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = [calendar]
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def test_the_share_action_hands_on_the_account_and_its_access(
    hass: HomeAssistant,
) -> None:
    await _setup(hass)

    with patch("custom_components.ha_caldav.services.share_calendar") as share:
        await hass.services.async_call(
            DOMAIN,
            "share_calendar",
            {"entity_id": "calendar.personal", "user": "ann", "access": "read_write"},
            blocking=True,
        )

    assert share.call_args.args[1:] == ("ann", True)


async def test_the_shares_of_a_calendar_are_returned(hass: HomeAssistant) -> None:
    await _setup(hass)
    shares = [{"user": "ann", "href": "principals/ann", "access": "read"}]

    with patch("custom_components.ha_caldav.services.read_shares", return_value=shares):
        result = await hass.services.async_call(
            DOMAIN,
            "get_calendar_shares",
            {"entity_id": "calendar.personal"},
            blocking=True,
            return_response=True,
        )

    assert result["calendar.personal"] == {"shares": shares}


@pytest.mark.parametrize(
    ("service", "data"),
    [("share_calendar", {"user": "ann"}), ("unshare_calendar", {"user": "ann"})],
)
async def test_only_an_administrator_shares_a_calendar(
    hass: HomeAssistant, service: str, data: dict
) -> None:
    await _setup(hass)
    user = MockUser(is_owner=False)
    user.add_to_hass(hass)
    user.mock_policy({"entities": {"entity_ids": {"calendar.personal": True}}})

    with (
        patch(f"custom_components.ha_caldav.services.{service}") as write,
        pytest.raises(Unauthorized),
    ):
        await hass.services.async_call(
            DOMAIN,
            service,
            {"entity_id": "calendar.personal", **data},
            blocking=True,
            context=Context(user_id=user.id),
        )

    write.assert_not_called()
