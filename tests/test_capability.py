"""Tests for what the server reports a calendar can do."""

from unittest.mock import Mock, patch

from caldav.davclient import DAVResponse
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_caldav.capability import (
    UNKNOWN,
    Capability,
    capability_for,
    fetch_address_set,
    fetch_capabilities,
    supports_sync_collection,
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
    """Build a real DAVResponse so the parsing sees the actual element shapes."""
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
    # properties at all, which must not read as "no components, no writing".
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
    # The property came back empty rather than without sync-collection, which
    # must not raise a repair issue on a server that supports it after all.
    calendar = Mock()
    calendar.get_properties.return_value = _multistatus(
        '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
        f"<d:response><d:href>{HOME}/x/</d:href><d:propstat><d:prop/>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        "</d:multistatus>"
    )

    assert supports_sync_collection(calendar) is True


def test_sync_collection_probe_gives_a_broken_lookup_the_benefit_of_the_doubt() -> None:
    calendar = Mock()
    calendar.get_properties.side_effect = AssertionError("weird xml")

    assert supports_sync_collection(calendar) is True


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

    # An event-only calendar has no to-do list, and a task list is no calendar.
    assert hass.states.get("calendar.iven_events") is not None
    assert hass.states.get("todo.iven_events") is None
    assert hass.states.get("todo.iven_tasks") is not None
    assert hass.states.get("calendar.iven_tasks") is None


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

    assert hass.states.get("calendar.iven_events").attributes["supported_features"] == 0
    assert hass.states.get("calendar.iven_tasks").attributes["supported_features"] != 0


def test_an_empty_component_set_stays_permissive() -> None:
    # Radicale answers an empty set with a single nameless comp element, which
    # must not read as "this calendar holds nothing".
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
    # A server that names no privileges is not denying them, and locking the
    # calendar would remove every control.
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
    """RFC 5545 has a CAL-ADDRESS be a URI, and RFC 6638 lets the set name the
    principal by its url; sabre/dav and Nextcloud both answer with a bare path
    for an account carrying no mail address.

    Written onto an ATTENDEE as it stands, sabre reads it as a local principal,
    fails to resolve it, and answers 500 to every DELETE of that object from
    then on. Measured against Baikal: the event became impossible to remove at
    all, and the whole live suite stalled on the leftover.
    """
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
        # Anything carrying a scheme of its own is left exactly as it came.
        "mailto:iven@example.com",
        "MailTo:Iven@Example.com",
        "http://elsewhere.test/principals/bob/",
    ]


def test_an_address_the_account_url_cannot_be_joined_to_is_kept() -> None:
    """A client with no usable url must not cost the account its own address:
    matched against nothing, no ATTENDEE line is ever recognised as ours and
    the reply service reports the user is not on their own event."""
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

    # At depth 0 the server answers for the home set alone, every calendar
    # falls back to the permissive default, and a read-only shared calendar
    # would show write controls.
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

    # Nothing changed on the server, so the expensive search must not run
    # again; a half that is not supported must not read as "still stale".
    assert calendar.search.call_count == before


def test_a_lowercase_component_name_still_counts() -> None:
    """RFC 5545 makes them case-insensitive, and a calendar reported as holding
    neither kind is one whose entities the prune deletes from the registry."""
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
    read, and caldav's parser raises on the whole response when it meets one.
    Every calendar would then fall back to the permissive guess, offering edit
    controls on collections that are genuinely read-only."""
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
    property it cannot produce. Read as an answer, it blanks the component set
    the first propstat gave and the calendar falls back to the permissive
    guess, offering a to-do list on a collection that holds none."""
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
    reduces one to its path. Standing in for that parser means standing in for
    its normalization too: read literally, not one calendar on such a server
    matches, every one falls back to the permissive default, and a read-only
    shared calendar is offered write controls whose every use fails."""
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
    """The status element is what the propstat filter reads. Absent — and a
    server is free to leave it out of a malformed response — reaching for its
    text raises, and one such response costs the whole account its
    capabilities."""
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
    """calendar_key drops the scheme and the host, so a foreign href keys onto
    the same path as a real calendar and the first one seen wins: the stranger
    then decides whether ours is writable and what it holds."""
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
    """calendar_key drops the scheme and the host, so a response for another
    server keys onto the same path as a real calendar and the first one seen
    wins. Without a home-set url there is nothing to tell the two apart, and
    guessing would hand one calendar the capabilities of another."""
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
