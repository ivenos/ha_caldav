from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, PropertyMock, patch
from urllib.parse import unquote, urlparse

import caldav
from caldav.davclient import DAVResponse
from caldav.elements import dav
from caldav.lib.error import DAVError
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import requests

from custom_components.ha_caldav.capability import (
    UNKNOWN,
    Capability,
    Delegation,
    account_calendars,
    capability_for,
    fetch_address_set,
    fetch_capabilities,
    supports_scheduling,
    supports_sync_collection,
)
from custom_components.ha_caldav.color import fetch_collections
from custom_components.ha_caldav.connection import (
    build_client,
    calendar_key,
    display_name,
)
from custom_components.ha_caldav.const import DOMAIN

ENTRY_DATA = {
    CONF_URL: "https://cloud.example.com/remote.php/dav",
    CONF_USERNAME: "iven",
    CONF_PASSWORD: "secret",
    CONF_VERIFY_SSL: True,
}

HOME = "/remote.php/dav/calendars/iven"


def _multistatus(body: str) -> DAVResponse:
    response = Mock()
    response.status_code = 207
    response.headers = {"Content-Type": "application/xml"}
    response.text = body
    response.content = body.encode()
    return DAVResponse(response)


def _home_set_response(entries: str) -> DAVResponse:
    return _multistatus(
        '<?xml version="1.0"?>'
        '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        f"{entries}</d:multistatus>"
    )


def _entry(href: str, components: list[str], privileges: list[str]) -> str:
    comps = "".join(f'<c:comp name="{name}"/>' for name in components)
    privs = "".join(f"<d:privilege><d:{name}/></d:privilege>" for name in privileges)
    return (
        f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
        f"<c:supported-calendar-component-set>{comps}"
        "</c:supported-calendar-component-set>"
        f"<d:current-user-privilege-set>{privs}</d:current-user-privilege-set>"
        "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    )


def _client(response: DAVResponse) -> Mock:
    client = Mock()
    client.principal.return_value.calendar_home_set.get_properties.return_value = (
        response
    )
    return client


def test_components_and_privileges_are_read_per_calendar() -> None:
    client = _client(
        _home_set_response(
            _entry(f"{HOME}/personal/", ["VEVENT", "VTODO"], ["read", "write"])
            + _entry(f"{HOME}/shared/", ["VEVENT"], ["read"])
        )
    )

    capabilities = fetch_capabilities(client)

    assert capabilities[f"{HOME}/personal"] == Capability(
        frozenset({"VEVENT", "VTODO"}), writable=True
    )
    assert capabilities[f"{HOME}/shared"] == Capability(
        frozenset({"VEVENT"}), writable=False
    )


def test_write_content_alone_counts_as_writable() -> None:
    # Servers disagree on which privilege they report for a shared calendar.
    client = _client(
        _home_set_response(_entry(f"{HOME}/x/", ["VEVENT"], ["read", "write-content"]))
    )

    assert fetch_capabilities(client)[f"{HOME}/x"].writable is True


def test_calendar_the_server_did_not_answer_for_stays_permissive() -> None:
    capabilities = {f"{HOME}/personal": Capability(frozenset({"VEVENT"}), True)}
    calendar = Mock()
    calendar.url = f"https://cloud.example.com{HOME}/other/"

    assert capability_for(capabilities, calendar) is UNKNOWN


def test_missing_properties_do_not_lock_the_calendar_down() -> None:
    # caldav drops a 404 propstat, so the calendar is in the result with no
    # properties at all.
    client = _client(
        _home_set_response(
            f"<d:response><d:href>{HOME}/bare/</d:href><d:propstat><d:prop>"
            "<c:supported-calendar-component-set/>"
            "</d:prop><d:status>HTTP/1.1 404 Not Found</d:status>"
            "</d:propstat></d:response>"
        )
    )

    capability = fetch_capabilities(client)[f"{HOME}/bare"]
    assert capability.supports_events
    assert capability.supports_todos
    assert capability.writable


def test_address_set_is_empty_when_the_server_has_none() -> None:
    client = Mock()
    client.principal.return_value.calendar_user_address_set.side_effect = ValueError(
        "no such property"
    )

    assert fetch_address_set(client) == []


def _outbox_answer(propstat: str) -> Mock:
    client = Mock()
    client.principal.return_value.get_properties.return_value = _multistatus(
        '<?xml version="1.0"?>'
        '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        f"<d:response><d:href>/principals/iven/</d:href>{propstat}</d:response>"
        "</d:multistatus>"
    )
    return client


def test_an_account_with_an_outbox_does_scheduling() -> None:
    client = _outbox_answer(
        "<d:propstat><d:prop><c:schedule-outbox-URL>"
        "<d:href>/calendars/iven/outbox/</d:href></c:schedule-outbox-URL></d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
    )

    assert supports_scheduling(client) is True


def test_an_account_answered_without_an_outbox_does_no_scheduling() -> None:
    """Radicale names the principal as an address and has no outbox."""
    client = _outbox_answer(
        "<d:propstat><d:prop><c:schedule-outbox-URL/></d:prop>"
        "<d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>"
    )

    assert supports_scheduling(client) is False


def test_scheduling_is_left_to_the_addresses_when_the_outbox_goes_unanswered() -> None:
    silent = _outbox_answer(
        "<d:propstat><d:prop/><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
    )
    broken = Mock()
    broken.principal.return_value.get_properties.side_effect = AssertionError("xml")

    assert supports_scheduling(silent) is True
    assert supports_scheduling(broken) is True


def _report_set(reports: str) -> DAVResponse:
    return _multistatus(
        '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
        f"<d:response><d:href>{HOME}/x/</d:href><d:propstat><d:prop>"
        f"<d:supported-report-set>{reports}</d:supported-report-set>"
        "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        "</d:multistatus>"
    )


def test_sync_collection_probe_reports_a_server_without_it() -> None:
    calendar = Mock()
    calendar.get_properties.return_value = _report_set(
        "<d:supported-report><d:report><d:expand-property/></d:report>"
        "</d:supported-report>"
    )

    assert supports_sync_collection(calendar) is False


def test_sync_collection_probe_finds_the_report() -> None:
    calendar = Mock()
    calendar.get_properties.return_value = _report_set(
        "<d:supported-report><d:report><d:sync-collection/></d:report>"
        "</d:supported-report>"
    )

    assert supports_sync_collection(calendar) is True


def test_sync_collection_probe_trusts_a_server_that_did_not_answer() -> None:
    calendar = Mock()
    calendar.get_properties.return_value = _multistatus(
        '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
        f"<d:response><d:href>{HOME}/x/</d:href><d:propstat><d:prop/>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        "</d:multistatus>"
    )

    assert supports_sync_collection(calendar) is True


def test_sync_collection_probe_trusts_a_server_that_did_not_find_the_property() -> None:
    calendar = Mock()
    calendar.get_properties.return_value = _multistatus(
        '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
        f"<d:response><d:href>{HOME}/x/</d:href><d:propstat><d:prop>"
        "<d:supported-report-set/></d:prop>"
        "<d:status>HTTP/1.1 404 Not Found</d:status></d:propstat></d:response>"
        "</d:multistatus>"
    )

    assert supports_sync_collection(calendar) is True


def test_sync_collection_probe_gives_a_broken_lookup_the_benefit_of_the_doubt() -> None:
    calendar = Mock()
    calendar.get_properties.side_effect = AssertionError("weird xml")

    assert supports_sync_collection(calendar) is True


def test_a_calendar_listed_without_a_name_is_named_without_asking_the_server() -> None:
    client = caldav.DAVClient(url=ENTRY_DATA[CONF_URL])
    calendar = caldav.Calendar(client=client, url=f"{ENTRY_DATA[CONF_URL]}/x/")

    with patch.object(
        requests.adapters.HTTPAdapter, "send", side_effect=AssertionError
    ) as send:
        assert display_name(calendar) == "CalDAV"

    send.assert_not_called()


def _setup_calendar(name: str, url: str) -> Mock:
    calendar = Mock()
    calendar.name = name
    calendar.url = url
    calendar.search.return_value = []
    return calendar


async def _setup(hass: HomeAssistant, capabilities: dict) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="iven", data=ENTRY_DATA, unique_id="x")
    entry.add_to_hass(hass)
    calendars = [
        _setup_calendar("Events", f"https://cloud.example.com{HOME}/events/"),
        _setup_calendar("Tasks", f"https://cloud.example.com{HOME}/tasks/"),
    ]
    with (
        patch("custom_components.ha_caldav.caldav.DAVClient") as client,
        patch(
            "custom_components.ha_caldav.fetch_capabilities", return_value=capabilities
        ),
    ):
        client.return_value.principal.return_value.calendars.return_value = calendars
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_component_set_decides_which_entities_exist(
    hass: HomeAssistant,
) -> None:
    await _setup(
        hass,
        {
            f"{HOME}/events": Capability(frozenset({"VEVENT"}), True),
            f"{HOME}/tasks": Capability(frozenset({"VTODO"}), True),
        },
    )

    assert hass.states.get("calendar.events") is not None
    assert hass.states.get("todo.events") is None
    assert hass.states.get("todo.tasks") is not None
    assert hass.states.get("calendar.tasks") is None


async def test_a_calendar_we_may_not_write_to_hides_its_controls(
    hass: HomeAssistant,
) -> None:
    await _setup(
        hass,
        {
            f"{HOME}/events": Capability(frozenset({"VEVENT"}), writable=False),
            f"{HOME}/tasks": Capability(frozenset({"VEVENT"}), writable=True),
        },
    )

    assert hass.states.get("calendar.events").attributes["supported_features"] == 0
    assert hass.states.get("calendar.tasks").attributes["supported_features"] != 0


def test_an_empty_component_set_stays_permissive() -> None:
    # Radicale answers an empty set with a single nameless comp element.
    client = _client(
        _home_set_response(
            f"<d:response><d:href>{HOME}/bare/</d:href><d:propstat><d:prop>"
            "<c:supported-calendar-component-set><c:comp/>"
            "</c:supported-calendar-component-set>"
            "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        )
    )

    capability = fetch_capabilities(client)[f"{HOME}/bare"]
    assert capability.supports_events
    assert capability.supports_todos


def test_an_empty_privilege_set_stays_permissive() -> None:
    client = _client(_home_set_response(_entry(f"{HOME}/bare/", ["VEVENT"], [])))

    assert fetch_capabilities(client)[f"{HOME}/bare"].writable is True


def test_the_address_set_is_read_when_the_server_has_one() -> None:
    client = Mock()
    client.url = "https://cloud.example.com/remote.php/dav/"
    client.principal.return_value.calendar_user_address_set.return_value = [
        "mailto:iven@example.com",
        "",
    ]

    assert fetch_address_set(client) == ["mailto:iven@example.com"]


def test_a_principal_named_by_path_is_read_as_the_uri_it_stands_for() -> None:
    """RFC 5545 has a CAL-ADDRESS be a URI, and sabre/dav and Nextcloud answer a
    bare path for an account carrying no mail address."""
    client = Mock()
    client.url = "http://localhost:8082/dav.php/"
    client.principal.return_value.calendar_user_address_set.return_value = [
        "/dav.php/principals/admin/",
        "mailto:iven@example.com",
        "MailTo:Iven@Example.com",
        "http://elsewhere.test/principals/bob/",
    ]

    assert fetch_address_set(client) == [
        "http://localhost:8082/dav.php/principals/admin/",
        "mailto:iven@example.com",
        "MailTo:Iven@Example.com",
        "http://elsewhere.test/principals/bob/",
    ]


def test_an_address_the_account_url_cannot_be_joined_to_is_kept() -> None:
    client = Mock()
    type(client).url = property(
        lambda self: (_ for _ in ()).throw(ValueError("no url"))
    )
    client.principal.return_value.calendar_user_address_set.return_value = [
        "mailto:iven@example.com"
    ]

    assert fetch_address_set(client) == ["mailto:iven@example.com"]


def test_the_capability_propfind_asks_one_level_down() -> None:
    client = _client(
        _home_set_response(_entry(f"{HOME}/personal/", ["VEVENT"], ["read"]))
    )

    fetch_capabilities(client)

    # At depth 0 the server answers for the home set alone.
    home = client.principal.return_value.calendar_home_set
    assert home.get_properties.call_args.kwargs["depth"] == 1


async def test_a_calendar_with_one_component_still_gates_on_the_sync_token(
    hass: HomeAssistant,
) -> None:
    entry = await _setup(
        hass, {f"{HOME}/events": Capability(frozenset({"VEVENT"}), True)}
    )
    coordinator = entry.runtime_data.calendars[0].coordinator
    calendar = coordinator.calendar
    calendar.objects_by_sync_token.return_value.sync_token = "same"
    await coordinator.async_refresh()
    before = calendar.search.call_count

    await coordinator.async_refresh()

    assert calendar.search.call_count == before


def test_a_lowercase_component_name_still_counts() -> None:
    """RFC 5545 makes component names case-insensitive."""
    import xml.etree.ElementTree as ET

    from custom_components.ha_caldav.capability import Capability, _components

    element = ET.fromstring(
        '<set xmlns="urn:ietf:params:xml:ns:caldav">'
        '<comp name="vevent"/><comp name="vtodo"/></set>'
    )
    capability = Capability(components=_components(element), writable=True)

    assert capability.supports_events
    assert capability.supports_todos


def test_a_calendar_that_hides_its_privileges_costs_no_other_calendar() -> None:
    """RFC 4918 9.1 lets a server answer 403 for a property the client may not
    read, and caldav's parser raises on the whole response when it meets one."""
    response = _home_set_response(
        _entry("/dav/iven/personal/", ["VEVENT", "VTODO"], ["write-content"])
        + "<d:response><d:href>/dav/alice/shared/</d:href>"
        "<d:propstat><d:prop><c:supported-calendar-component-set>"
        '<c:comp name="VEVENT"/></c:supported-calendar-component-set>'
        "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
        "<d:propstat><d:prop><d:current-user-privilege-set/></d:prop>"
        "<d:status>HTTP/1.1 403 Forbidden</d:status></d:propstat></d:response>"
    )
    client = Mock()
    client.principal.return_value.calendar_home_set.get_properties.return_value = (
        response
    )

    capabilities = fetch_capabilities(client)

    personal = capabilities["/dav/iven/personal"]
    assert personal.supports_events and personal.supports_todos
    shared = capabilities["/dav/alice/shared"]
    assert shared.supports_events
    assert not shared.supports_todos


def test_a_property_the_server_did_not_return_does_not_overwrite_one_it_did() -> None:
    """SabreDAV answers a second propstat with an empty shell and a 404 for a
    property it cannot produce."""
    response = _home_set_response(
        "<d:response><d:href>/dav/iven/events/</d:href>"
        "<d:propstat><d:prop><c:supported-calendar-component-set>"
        '<c:comp name="VEVENT"/></c:supported-calendar-component-set>'
        "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
        "<d:propstat><d:prop><c:supported-calendar-component-set/></d:prop>"
        "<d:status>HTTP/1.1 404 Not Found</d:status></d:propstat></d:response>"
    )
    client = Mock()
    client.principal.return_value.calendar_home_set.get_properties.return_value = (
        response
    )

    capability = fetch_capabilities(client)["/dav/iven/events"]

    assert capability.supports_events
    assert not capability.supports_todos


def test_an_absolute_href_still_names_its_calendar() -> None:
    """RFC 4918 8.3 lets an href be an absolute URI, and caldav's own parser
    reduces one to its path."""
    from unittest.mock import Mock
    import xml.etree.ElementTree as ET

    from custom_components.ha_caldav.capability import (
        capability_for,
        fetch_capabilities,
    )

    body = (
        '<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
        "<D:response>"
        "<D:href>https://cal.example.com/dav/bob/tasks/</D:href>"
        "<D:propstat><D:prop><C:supported-calendar-component-set>"
        '<C:comp name="VTODO"/>'
        "</C:supported-calendar-component-set></D:prop>"
        "<D:status>HTTP/1.1 200 OK</D:status></D:propstat>"
        "</D:response></D:multistatus>"
    )
    client = Mock()
    home = client.principal.return_value.calendar_home_set
    home.url = "https://cal.example.com/dav/bob/"
    home.get_properties.return_value = Mock(tree=ET.fromstring(body))
    calendar = Mock(url="https://cal.example.com/dav/bob/tasks/")

    capability = capability_for(fetch_capabilities(client), calendar)

    assert capability.supports_todos
    assert not capability.supports_events


def test_a_propstat_without_a_status_is_read_rather_than_crashed_on() -> None:
    client = _client(
        _home_set_response(
            f"<d:response><d:href>{HOME}/x/</d:href><d:propstat><d:prop>"
            '<c:supported-calendar-component-set><c:comp name="VEVENT"/>'
            "</c:supported-calendar-component-set>"
            "</d:prop></d:propstat></d:response>"
        )
    )

    capability = fetch_capabilities(client)[f"{HOME}/x"]

    assert capability.supports_events
    assert not capability.supports_todos


def test_a_response_for_another_server_does_not_answer_for_ours() -> None:
    import xml.etree.ElementTree as ET

    body = (
        '<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
        "<D:response><D:href>https://elsewhere.test/dav/bob/personal/</D:href>"
        "<D:propstat><D:prop><D:current-user-privilege-set>"
        "<D:privilege><D:read/></D:privilege>"
        "</D:current-user-privilege-set></D:prop>"
        "<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
        "<D:response><D:href>/dav/bob/personal/</D:href>"
        "<D:propstat><D:prop><D:current-user-privilege-set>"
        "<D:privilege><D:read/></D:privilege><D:privilege><D:write/></D:privilege>"
        "</D:current-user-privilege-set></D:prop>"
        "<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
        "</D:multistatus>"
    )
    client = Mock()
    home = client.principal.return_value.calendar_home_set
    home.url = "https://cal.example.com/dav/bob/"
    home.get_properties.return_value = Mock(tree=ET.fromstring(body))
    calendar = Mock(url="https://cal.example.com/dav/bob/personal/")

    assert capability_for(fetch_capabilities(client), calendar).writable


def test_an_absolute_href_is_ignored_when_there_is_nothing_to_check_it_against(
    hass: HomeAssistant,
) -> None:
    import xml.etree.ElementTree as ET

    body = (
        '<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
        "<D:response>"
        "<D:href>https://elsewhere.example.com/dav/bob/tasks/</D:href>"
        "<D:propstat><D:prop><C:supported-calendar-component-set>"
        '<C:comp name="VTODO"/>'
        "</C:supported-calendar-component-set></D:prop>"
        "<D:status>HTTP/1.1 200 OK</D:status></D:propstat>"
        "</D:response></D:multistatus>"
    )
    client = Mock()
    home = client.principal.return_value.calendar_home_set
    home.url = None
    home.get_properties.return_value = Mock(tree=ET.fromstring(body))

    assert fetch_capabilities(client) == {}


@pytest.mark.parametrize(
    "href",
    [
        "https://CAL.example.com/dav/bob/tasks/",
        "https://cal.example.com:443/dav/bob/tasks/",
    ],
    ids=["host_case", "default_port"],
)
def test_an_absolute_href_on_the_same_server_spelled_otherwise_counts(
    href: str,
) -> None:
    import xml.etree.ElementTree as ET

    from custom_components.ha_caldav.capability import fetch_capabilities

    body = (
        '<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
        f"<D:response><D:href>{href}</D:href>"
        "<D:propstat><D:prop><C:supported-calendar-component-set>"
        '<C:comp name="VTODO"/>'
        "</C:supported-calendar-component-set></D:prop>"
        "<D:status>HTTP/1.1 200 OK</D:status></D:propstat>"
        "</D:response></D:multistatus>"
    )
    client = Mock()
    home = client.principal.return_value.calendar_home_set
    home.url = "https://cal.example.com/dav/bob/"
    home.get_properties.return_value = Mock(tree=ET.fromstring(body))

    assert not fetch_capabilities(client)["/dav/bob/tasks"].supports_events


BOSS = "/remote.php/dav/principals/users/boss/"
BOSS_HOME = "/remote.php/dav/calendars/boss"


def _proxy_response(write_for: str = "", read_for: str = "") -> DAVResponse:
    def hrefs(found: str) -> str:
        return "".join(f"<d:href>{href}</d:href>" for href in found.split())

    return _multistatus(
        '<?xml version="1.0"?>'
        '<d:multistatus xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/">'
        "<d:response><d:href>/remote.php/dav/principals/users/iven/</d:href>"
        "<d:propstat><d:prop>"
        f"<cs:calendar-proxy-read-for>{hrefs(read_for)}</cs:calendar-proxy-read-for>"
        f"<cs:calendar-proxy-write-for>{hrefs(write_for)}</cs:calendar-proxy-write-for>"
        "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
        "</d:response></d:multistatus>"
    )


def _delegating_client(response: DAVResponse, own: list[Mock]) -> caldav.DAVClient:
    """A real client, as a calendar reads its name through the client it hangs on."""
    client = caldav.DAVClient("https://cloud.example.com/remote.php/dav/")
    principal = Mock()
    principal.calendars.return_value = own
    principal.get_properties.return_value = response
    client.principal = Mock(return_value=principal)
    return client


def _delegator(calendars: list[Mock]) -> Mock:
    delegator = Mock()
    delegator.calendar_home_set.url = f"https://cloud.example.com{BOSS_HOME}/"
    delegator.calendar_home_set.calendars.return_value = calendars
    delegator.calendar_user_address_set.return_value = ["mailto:boss@example.com"]
    return delegator


def test_a_delegated_calendar_is_listed_with_the_name_of_its_owner() -> None:
    own = _setup_calendar("Personal", f"https://cloud.example.com{HOME}/personal/")
    theirs = _setup_calendar(
        "Personal", f"https://cloud.example.com{BOSS_HOME}/personal/"
    )
    client = _delegating_client(_proxy_response(write_for=BOSS), [own])

    with patch(
        "custom_components.ha_caldav.capability.caldav.Principal",
        return_value=_delegator([theirs]),
    ) as principal:
        calendars, delegations = account_calendars(client)

    assert [display_name(item) for item in calendars] == ["Personal", "Personal (boss)"]
    assert str(principal.call_args.kwargs["url"]) == f"https://cloud.example.com{BOSS}"
    [delegation] = delegations
    assert delegation.owner == "boss"
    assert delegation.addresses == ["mailto:boss@example.com"]
    assert client.principal.call_count == 1


def _holders(home: str, held: dict[str, tuple[str, str, str]]) -> Mock:
    """A depth-1 answer naming owner, share resource and access by calendar."""
    import xml.etree.ElementTree as ET

    responses = "".join(
        f"<d:response><d:href>{home}/{name}/</d:href><d:propstat><d:prop>"
        f"<d:owner><d:href>/remote.php/dav/principals/users/{owner}/</d:href>"
        f"</d:owner><d:share-resource-uri><d:href>{resource}</d:href>"
        f"</d:share-resource-uri><d:share-access><d:{access}/></d:share-access>"
        "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        for name, (owner, resource, access) in held.items()
    )
    return Mock(
        tree=ET.fromstring(f'<d:multistatus xmlns:d="DAV:">{responses}</d:multistatus>')
    )


@pytest.mark.parametrize(
    ("own", "theirs", "listed"),
    [
        (
            {"work": ("iven", "/ns/share/1", "shared-owner")},
            {
                "personal": ("boss", "/ns/share/2", "not-shared"),
                "work_shared_by_iven": ("iven", "/ns/share/1", "read-write"),
            },
            ["work", "personal"],
        ),
        (
            {"work": ("iven", "/ns/share/1", "not-shared")},
            {
                "personal": ("boss", "/ns/share/2", "not-shared"),
                "8f1c": ("boss", "/ns/share/1", "read-write"),
            },
            ["work", "personal"],
        ),
        (
            {
                "work": ("iven", "/ns/share/1", "not-shared"),
                "4ab2": ("iven", "/ns/share/2", "read"),
            },
            {"personal": ("boss", "/ns/share/2", "not-shared")},
            ["work", "4ab2", "personal"],
        ),
    ],
    ids=["named_as_owner", "same_share_resource", "theirs_shared_with_us_stays"],
)
def test_an_own_calendar_shared_with_the_delegator_is_not_listed_again(
    own: dict, theirs: dict, listed: list[str]
) -> None:
    from caldav.lib.url import URL

    client = _delegating_client(
        _proxy_response(write_for=BOSS),
        [
            _setup_calendar(name, f"https://cloud.example.com{HOME}/{name}/")
            for name in own
        ],
    )
    principal = client.principal.return_value
    principal.url = URL(
        "https://cloud.example.com/remote.php/dav/principals/users/iven/"
    )
    principal.calendar_home_set.get_properties.return_value = _holders(HOME, own)
    delegator = _delegator(
        [
            _setup_calendar(name, f"https://cloud.example.com{BOSS_HOME}/{name}/")
            for name in theirs
        ]
    )
    delegator.calendar_home_set.get_properties.return_value = _holders(
        BOSS_HOME, theirs
    )

    with patch(
        "custom_components.ha_caldav.capability.caldav.Principal",
        return_value=delegator,
    ):
        calendars, _ = account_calendars(client)

    assert [calendar_key(item.url).rsplit("/", 1)[-1] for item in calendars] == listed


def test_an_account_named_as_reader_and_writer_is_listed_once() -> None:
    client = _delegating_client(_proxy_response(write_for=BOSS, read_for=BOSS), [])
    theirs = _setup_calendar("Team", f"https://cloud.example.com{BOSS_HOME}/team/")

    with patch(
        "custom_components.ha_caldav.capability.caldav.Principal",
        return_value=_delegator([theirs]),
    ):
        calendars, delegations = account_calendars(client)

    assert len(calendars) == len(delegations) == 1


def test_a_calendar_reached_through_two_delegations_is_listed_once() -> None:
    absolute = f"https://cloud.example.com{BOSS}"
    client = _delegating_client(_proxy_response(write_for=BOSS, read_for=absolute), [])
    theirs = _setup_calendar("Team", f"https://cloud.example.com{BOSS_HOME}/team/")

    with patch(
        "custom_components.ha_caldav.capability.caldav.Principal",
        return_value=_delegator([theirs]),
    ):
        calendars, _ = account_calendars(client)

    assert [display_name(item) for item in calendars] == ["Team (boss)"]


def test_a_server_without_delegation_lists_only_the_accounts_own_calendars() -> None:
    own = _setup_calendar("Personal", f"https://cloud.example.com{HOME}/personal/")
    client = _delegating_client(_proxy_response(), [own])

    assert account_calendars(client) == ([own], [])


def test_a_server_that_cannot_tell_its_delegations_says_so() -> None:
    own = _setup_calendar("Personal", f"https://cloud.example.com{HOME}/personal/")
    client = _delegating_client(_proxy_response(), [own])
    client.principal.return_value.get_properties.side_effect = RuntimeError("no")

    assert account_calendars(client) == ([own], None)


def test_a_delegator_that_cannot_be_read_leaves_the_delegations_untold() -> None:
    own = _setup_calendar("Personal", f"https://cloud.example.com{HOME}/personal/")
    client = _delegating_client(_proxy_response(write_for=BOSS), [own])
    broken = Mock()
    type(broken).calendar_home_set = PropertyMock(side_effect=RuntimeError("gone"))

    with patch(
        "custom_components.ha_caldav.capability.caldav.Principal", return_value=broken
    ):
        assert account_calendars(client) == ([own], None)


def test_a_delegated_account_that_cannot_be_read_leaves_the_others() -> None:
    own = _setup_calendar("Personal", f"https://cloud.example.com{HOME}/personal/")
    client = _delegating_client(_proxy_response(write_for=BOSS), [own])
    broken = _delegator([])
    broken.calendar_home_set.calendars.side_effect = RuntimeError("gone")

    with patch(
        "custom_components.ha_caldav.capability.caldav.Principal", return_value=broken
    ):
        calendars, _ = account_calendars(client)

    assert calendars == [own]


SHARD = "https://p42-caldav.icloud.test:443"
SHARED = (
    "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
    '<c:supported-calendar-component-set><c:comp name="VEVENT"/>'
    "</c:supported-calendar-component-set>"
)
DENTIST = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Apple Inc.//iOS 18.0//EN\r\n"
    "BEGIN:VEVENT\r\nUID:dentist\r\nDTSTAMP:20261001T100000Z\r\n"
    "DTSTART:20261009T100000Z\r\nDTEND:20261009T110000Z\r\nSUMMARY:Dentist\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)


def _sharded(delegator_home: str = ""):
    """Answer like iCloud, which names the calendar home on a host of its own."""

    def props(href: str, found: str) -> str:
        return (
            f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{found}</d:prop>"
            "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        )

    def home(url: str) -> str:
        return f"<c:calendar-home-set><d:href>{url}</d:href></c:calendar-home-set>"

    def answer(request: requests.PreparedRequest) -> str:
        path = "/" + request.url.split("/", 3)[3]
        me = "<d:href>/1/principal/</d:href>"
        me = f"<d:current-user-principal>{me}</d:current-user-principal>"
        if request.method == "REPORT":
            data = f"<c:calendar-data><![CDATA[{DENTIST}]]></c:calendar-data>"
            return props("/1/calendars/home/dentist.ics", data) * (
                "query" in request.body.decode()
            )
        if path == "/1/principal/":
            boss = "<d:href>/2/principal/</d:href>" * bool(delegator_home)
            proxy = f"<cs:calendar-proxy-write-for>{boss}</cs:calendar-proxy-write-for>"
            return props(path, me + home(f"{SHARD}/1/calendars/") + proxy)
        if path == "/2/principal/":
            boss = "<d:href>mailto:boss@icloud.test</d:href>"
            boss = f"<c:calendar-user-address-set>{boss}</c:calendar-user-address-set>"
            return props(path, home(delegator_home) + boss)
        if path == "/1/calendars/":
            return props(
                path, "<d:resourcetype><d:collection/></d:resourcetype>"
            ) + props(
                "/1/calendars/home/", SHARED + "<d:displayname>Home</d:displayname>"
            )
        return props(path, me)

    def send(self, request: requests.PreparedRequest, **kwargs) -> requests.Response:
        reply = requests.Response()
        reply.status_code = 207
        reply.reason = "Multi-Status"
        reply.headers["Content-Type"] = "text/xml; charset=UTF-8"
        reply._content = (
            '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
            'xmlns:c="urn:ietf:params:xml:ns:caldav" '
            f'xmlns:cs="http://calendarserver.org/ns/">{answer(request)}</d:multistatus>'
        ).encode()
        reply.url = request.url
        reply.request = request
        return reply

    return patch.object(requests.adapters.HTTPAdapter, "send", send)


def _events(calendar: caldav.Calendar) -> list[str]:
    start = datetime(2026, 10, 8, tzinfo=UTC)
    found = calendar.search(
        start=start, end=start + timedelta(days=7), event=True, props=[dav.GetEtag()]
    )
    return [str(item.url) for item in found]


def test_events_are_read_when_the_calendar_home_is_on_another_host() -> None:
    with _sharded():
        client = build_client("https://caldav.icloud.test/", "jane", "secret", {})
        [calendar], _ = account_calendars(client)
        fetch_capabilities(client)

        assert _events(calendar) == [f"{SHARD}/1/calendars/home/dentist.ics"]


def test_a_delegated_account_on_another_host_does_not_take_the_client_along() -> None:
    with _sharded(delegator_home="https://p57-caldav.icloud.test:443/2/calendars/"):
        client = build_client("https://caldav.icloud.test/", "jane", "secret", {})
        [calendar], delegations = account_calendars(client)

        assert delegations == []
        assert _events(calendar) == [f"{SHARD}/1/calendars/home/dentist.ics"]


def test_capabilities_are_read_from_every_home_set() -> None:
    client = _client(
        _home_set_response(_entry(f"{HOME}/personal/", ["VEVENT"], ["write"]))
    )
    theirs = Mock()
    theirs.home.url = f"https://cloud.example.com{BOSS_HOME}/"
    theirs.home.get_properties.return_value = _home_set_response(
        _entry(f"{BOSS_HOME}/personal/", ["VEVENT"], ["read"])
    )

    capabilities = fetch_capabilities(client, [theirs])

    assert capabilities[f"{HOME}/personal"].writable is True
    assert capabilities[f"{BOSS_HOME}/personal"].writable is False


async def test_a_delegated_calendar_writes_as_the_account_it_belongs_to(
    hass: HomeAssistant,
) -> None:
    """An event with attendees names its organizer, who is the calendar's owner."""
    own = _setup_calendar("Personal", f"https://cloud.example.com{HOME}/personal/")
    theirs = _setup_calendar(
        "Personal (boss)", f"https://cloud.example.com{BOSS_HOME}/personal/"
    )
    home = Mock(url=f"https://cloud.example.com{BOSS_HOME}/")
    delegation = Delegation(home, ["mailto:boss@example.com"], "boss")
    entry = MockConfigEntry(domain=DOMAIN, title="iven", data=ENTRY_DATA, unique_id="x")
    entry.add_to_hass(hass)
    with (
        patch("custom_components.ha_caldav.caldav.DAVClient") as client,
        patch("custom_components.ha_caldav.fetch_capabilities", return_value={}),
        patch(
            "custom_components.ha_caldav.account_calendars",
            return_value=([own, theirs], [delegation]),
        ),
    ):
        principal = client.return_value.principal.return_value
        principal.calendar_user_address_set.return_value = ["mailto:iven@example.com"]
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.get("calendar.personal") is not None
    assert hass.states.get("calendar.personal_boss") is not None
    by_name = {item.name: item for item in entry.runtime_data.calendars}
    assert by_name["Personal"].addresses is None
    assert by_name["Personal (boss)"].addresses == ["mailto:boss@example.com"]
    with patch("custom_components.ha_caldav.calendar.create_event") as create:
        await hass.services.async_call(
            DOMAIN,
            "create_event",
            {
                "entity_id": "calendar.personal_boss",
                "summary": "Review",
                "start_date_time": "2026-07-06 09:00:00",
                "end_date_time": "2026-07-06 10:00:00",
            },
            blocking=True,
        )
    assert create.call_args.args[2] == "mailto:boss@example.com"


async def test_two_delegators_whose_homes_share_a_prefix_keep_their_own_addresses(
    hass: HomeAssistant,
) -> None:
    owners = ("anna", "annabel")
    calendars = [
        _setup_calendar(
            f"Personal ({owner})",
            f"https://cloud.example.com/remote.php/dav/calendars/{owner}/personal/",
        )
        for owner in owners
    ]
    delegations = [
        Delegation(
            Mock(url=f"https://cloud.example.com/remote.php/dav/calendars/{owner}/"),
            [f"mailto:{owner}@example.com"],
            owner,
        )
        for owner in owners
    ]
    entry = MockConfigEntry(domain=DOMAIN, title="iven", data=ENTRY_DATA, unique_id="x")
    entry.add_to_hass(hass)
    with (
        patch("custom_components.ha_caldav.caldav.DAVClient"),
        patch("custom_components.ha_caldav.fetch_capabilities", return_value={}),
        patch(
            "custom_components.ha_caldav.account_calendars",
            return_value=(calendars, delegations),
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert {item.name: item.addresses for item in entry.runtime_data.calendars} == {
        "Personal (anna)": ["mailto:anna@example.com"],
        "Personal (annabel)": ["mailto:annabel@example.com"],
    }


def test_a_delegated_calendar_already_named_after_its_owner_keeps_its_name() -> None:
    """SOGo lists it as "Personal (boss <boss@example.com>)" by itself."""
    theirs = _setup_calendar(
        "Team (Boss <boss@example.com>)", f"https://cloud.example.com{BOSS_HOME}/team/"
    )
    client = _delegating_client(_proxy_response(write_for=BOSS), [])

    with patch(
        "custom_components.ha_caldav.capability.caldav.Principal",
        return_value=_delegator([theirs]),
    ):
        calendars, _ = account_calendars(client)

    assert display_name(calendars[0]) == "Team (Boss <boss@example.com>)"


def test_a_delegation_that_fails_leaves_the_own_calendars_their_capabilities() -> None:
    client = _client(_home_set_response(_entry(f"{HOME}/tasks/", ["VTODO"], ["read"])))
    theirs = Mock()
    theirs.home.url = "https://cloud.example.com/remote.php/dav/calendars/boss/"
    theirs.home.get_properties.side_effect = DAVError("timed out")

    capabilities = fetch_capabilities(client, [theirs])

    assert capabilities[f"{HOME}/tasks"] == Capability(
        frozenset({"VTODO"}), writable=False
    )


def _sogo(login: str = "admin", own: dict[str, str] | None = None, owners: bool = True):
    """Answer like SOGo, which subscribes a delegate to what it may act on.

    Own is the delegate's own calendars by name and owner; the delegator holds
    "personal". Without owners the question for them gets an empty answer.
    Returns the patch and the requests it was asked.
    """
    held = {"personal": "other", f"{login}_personal": login} if own is None else own
    asked: list[tuple[str, str, str]] = []

    def props(href: str, found: str) -> str:
        return (
            f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{found}</d:prop>"
            "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        )

    def calendar(home: str, name: str, owner: str) -> str:
        return props(
            f"{home}{name}/",
            "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
            f"<d:displayname>{name}</d:displayname>"
            f"<d:owner><d:href>/SOGo/dav/{owner}/</d:href></d:owner>",
        )

    def principal(account: str, extra: str = "") -> str:
        return props(
            f"/SOGo/dav/{account}/",
            "<d:current-user-principal><d:href>/SOGo/dav/other/</d:href>"
            "</d:current-user-principal><c:calendar-home-set>"
            f"<d:href>/SOGo/dav/{account}/Calendar/</d:href></c:calendar-home-set>"
            "<c:calendar-user-address-set>"
            f"<d:href>mailto:{account}@example.com</d:href>"
            f"</c:calendar-user-address-set>{extra}",
        )

    def answer(request: requests.PreparedRequest) -> str:
        path = unquote(urlparse(request.url).path)
        body = request.body.decode() if isinstance(request.body, bytes) else ""
        asked.append((request.method, path, body))
        home = "<d:resourcetype><d:collection/></d:resourcetype>"
        if path == "/SOGo/dav/other/":
            delegator = f"<d:href>/SOGo/dav/{login}/</d:href>"
            return principal(
                "other",
                f"<cs:calendar-proxy-write-for>{delegator}</cs:calendar-proxy-write-for>",
            )
        if path == f"/SOGo/dav/{login}/":
            return principal(login)
        if path == "/SOGo/dav/other/Calendar/":
            return props(path, home) + "".join(
                calendar(path, name, owner) for name, owner in held.items()
            )
        if path == f"/SOGo/dav/{login}/Calendar/":
            return props(path, home) + calendar(path, "personal", login)
        return principal("other")

    def send(self, request: requests.PreparedRequest, **kwargs) -> requests.Response:
        reply = requests.Response()
        reply.status_code = 207
        reply.reason = "Multi-Status"
        reply.headers["Content-Type"] = "text/xml; charset=UTF-8"
        reply._content = (
            '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
            'xmlns:c="urn:ietf:params:xml:ns:caldav" '
            f'xmlns:cs="http://calendarserver.org/ns/">{answer(request)}</d:multistatus>'
        ).encode()
        if not owners and b"owner" in (request.body or b""):
            reply._content = b""
        reply.url = request.url
        reply.request = request
        return reply

    return patch.object(requests.adapters.HTTPAdapter, "send", send), asked


def _sogo_listing(
    login: str = "admin", own: dict[str, str] | None = None, owners: bool = True
):
    transport, asked = _sogo(login, own, owners)
    with transport:
        client = build_client("https://mail.example.com/SOGo/dav/", "other", "x", {})
        calendars, delegations = account_calendars(client)
        collections = fetch_collections(client, delegations)
    return (
        [calendar_key(item.url) for item in calendars],
        delegations,
        collections,
        asked,
    )


@pytest.mark.parametrize(
    ("login", "copy"),
    [
        ("admin", "admin_personal"),
        ("john.doe_x", "john_D_doe_U_x_personal"),
        ("2nd@example.com", "_2nd_A_example_D_com_personal"),
    ],
)
def test_sogo_lists_a_delegated_calendar_once_as_the_owners(login, copy) -> None:
    listed, [delegation], _, _ = _sogo_listing(
        login, {"personal": "other", copy: login}
    )

    assert listed == [
        "/SOGo/dav/other/Calendar/personal",
        f"/SOGo/dav/{login}/Calendar/personal",
    ]
    assert delegation.copies == {
        f"/SOGo/dav/other/Calendar/{copy}": f"/SOGo/dav/{login}/Calendar/personal"
    }


def test_the_color_poll_leaves_out_what_the_listing_left_out() -> None:
    _, _, collections, _ = _sogo_listing()

    assert "/SOGo/dav/other/Calendar/admin_personal" not in collections
    assert collections["/SOGo/dav/admin/Calendar/personal"].calendar is True


def test_an_own_calendar_named_like_a_copy_stays_listed() -> None:
    listed, _, collections, _ = _sogo_listing(
        own={"personal": "other", "admin_personal": "other"}
    )

    assert "/SOGo/dav/other/Calendar/admin_personal" in listed
    assert "/SOGo/dav/other/Calendar/admin_personal" in collections


def test_a_subscription_to_a_calendar_the_delegation_does_not_list_stays() -> None:
    listed, _, _, _ = _sogo_listing(own={"personal": "other", "admin_work": "admin"})

    assert "/SOGo/dav/other/Calendar/admin_work" in listed


def test_a_listing_with_nothing_named_like_a_copy_asks_for_no_owner() -> None:
    listed, _, _, asked = _sogo_listing(
        own={"personal": "other", "work_shared_by_admin": "admin"}
    )

    assert "/SOGo/dav/other/Calendar/work_shared_by_admin" in listed
    assert not any(
        "owner" in body
        for _, path, body in asked
        if path == "/SOGo/dav/other/Calendar/"
    )


def test_owners_the_server_will_not_name_leave_the_listing_as_it_was() -> None:
    listed, _, _, _ = _sogo_listing(owners=False)

    assert "/SOGo/dav/other/Calendar/admin_personal" in listed
