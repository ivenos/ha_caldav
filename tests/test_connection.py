import asyncio
from base64 import b64encode
from datetime import timedelta
import threading
from unittest.mock import Mock, patch

import caldav
from caldav.davclient import requests
from caldav.lib.error import AuthorizationError
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_caldav.capability import Capability
from custom_components.ha_caldav.connection import (
    HostLockedSession,
    account_key,
    build_client,
    calendar_key,
    connection_kwargs,
    size_pool,
    url_candidates,
    without_userinfo,
)
from custom_components.ha_caldav.const import (
    CONF_CA_BUNDLE,
    CONF_CLIENT_CERT,
    CONF_CLIENT_KEY,
    DEFAULT_TIMEOUT,
    DOMAIN,
)
from custom_components.ha_caldav.coordinator import (
    HaCaldavColorCoordinator,
    HaCaldavCoordinator,
)
from custom_components.ha_caldav.errors import rejected


def test_plain_account_verifies_against_the_system_store() -> None:
    assert connection_kwargs({CONF_VERIFY_SSL: True}) == {
        "ssl_verify_cert": True,
        "ssl_cert": None,
        "timeout": DEFAULT_TIMEOUT,
    }


def test_verification_can_be_turned_off() -> None:
    assert connection_kwargs({CONF_VERIFY_SSL: False})["ssl_verify_cert"] is False


def test_a_ca_bundle_takes_the_place_of_the_verify_flag() -> None:
    kwargs = connection_kwargs({CONF_VERIFY_SSL: True, CONF_CA_BUNDLE: "/etc/ca.pem"})

    assert kwargs["ssl_verify_cert"] == "/etc/ca.pem"


def test_a_client_certificate_without_a_key_is_passed_alone() -> None:
    kwargs = connection_kwargs({CONF_CLIENT_CERT: "/etc/client.pem"})

    assert kwargs["ssl_cert"] == "/etc/client.pem"


def test_a_certificate_and_key_are_passed_as_a_pair() -> None:
    kwargs = connection_kwargs(
        {CONF_CLIENT_CERT: "/etc/client.pem", CONF_CLIENT_KEY: "/etc/client.key"}
    )

    assert kwargs["ssl_cert"] == ("/etc/client.pem", "/etc/client.key")


def _probe(resolved: str | None = None, hops: list[str] | None = None):
    response = Mock()
    response.url = resolved
    response.history = [Mock(url=hop) for hop in hops or []]
    return patch(
        "custom_components.ha_caldav.connection.requests.request",
        return_value=response,
    )


SAME_ACCOUNT = [
    "https://cloud.example.com/remote.php/dav",
    "https://cloud.example.com/remote.php/dav/",
    "https://CLOUD.Example.COM/remote.php/dav",
    "https://cloud.example.com:443/remote.php/dav",
    "HTTPS://cloud.example.com/remote.php/dav",
]


@pytest.mark.parametrize("url", SAME_ACCOUNT)
def test_one_account_keys_the_same_however_it_was_typed(url: str) -> None:
    assert account_key(url, "iven") == account_key(SAME_ACCOUNT[0], "iven")


@pytest.mark.parametrize(
    "url",
    [
        "https://cloud.example.com:8443/remote.php/dav",
        "http://cloud.example.com/remote.php/dav",
        "https://other.example.com/remote.php/dav",
        "https://cloud.example.com/remote.php/caldav",
    ],
)
def test_a_different_account_keys_differently(url: str) -> None:
    assert account_key(url, "iven") != account_key(SAME_ACCOUNT[0], "iven")


def test_two_accounts_on_one_server_key_apart() -> None:
    assert account_key(SAME_ACCOUNT[0], "iven") != account_key(SAME_ACCOUNT[0], "ada")


def test_a_path_keeps_its_case() -> None:
    # RFC 3986 leaves the path case-sensitive even where the host is not.
    assert account_key("https://dav.example.com/DAV", "iven") != account_key(
        "https://dav.example.com/dav", "iven"
    )


def test_an_ipv6_host_keys_without_losing_its_brackets() -> None:
    key = account_key("https://[2001:db8::1]:8443/dav", "iven")

    assert "[2001:db8::1]:8443" in key


def test_a_url_urlparse_refuses_still_keys() -> None:
    assert account_key("https://dav.example.com:notaport/dav", "iven").endswith("#iven")


def test_a_bootstrap_that_steps_out_of_https_is_ignored() -> None:
    """A proxy terminating TLS without X-Forwarded-Proto redirects to http."""
    with _probe(
        resolved="https://dav.example.com/dav/",
        hops=["https://dav.example.com/.well-known/caldav", "http://dav.example.com/"],
    ):
        candidates = list(url_candidates("https://dav.example.com", {}))

    assert candidates == ["https://dav.example.com"]


def test_a_bootstrap_landing_on_plain_http_is_ignored() -> None:
    with _probe(resolved="http://dav.example.com/dav/"):
        candidates = list(url_candidates("https://dav.example.com", {}))

    assert candidates == ["https://dav.example.com"]


def test_a_bootstrap_entered_as_http_may_stay_on_http() -> None:
    with _probe(resolved="http://dav.example.com/dav/"):
        candidates = list(url_candidates("http://dav.example.com", {}))

    assert candidates == ["http://dav.example.com", "http://dav.example.com/dav/"]


def test_the_bootstrap_result_is_stripped_of_userinfo() -> None:
    """caldav prefers userinfo in the url over the account handed to it."""
    with _probe(resolved="https://intruder:secret@dav.example.com/dav/"):
        candidates = list(url_candidates("https://dav.example.com", {}))

    assert candidates == ["https://dav.example.com", "https://dav.example.com/dav/"]
    assert not any("intruder" in candidate for candidate in candidates)


def test_a_bootstrap_pointing_back_at_the_entered_url_is_not_tried_twice() -> None:
    with _probe(resolved="https://dav.example.com/dav/"):
        candidates = list(url_candidates("https://dav.example.com/dav", {}))

    assert candidates == ["https://dav.example.com/dav"]


def test_the_pool_holds_a_connection_for_every_calendar_and_the_colors() -> None:
    client = Mock(session=HostLockedSession())

    size_pool(client, 14)

    assert client.session.get_adapter("https://dav.example.com")._pool_maxsize == 15


def _candidates(url: str, resolved: str | None = None) -> list[str]:
    with _probe(resolved or _bootstrap_of(url)):
        return list(url_candidates(url, {}))


def _bootstrap_of(url: str) -> str:
    entered = url if "://" in url else f"https://{url}"
    scheme, _, rest = entered.partition("://")
    return f"{scheme}://{rest.split('/')[0]}/.well-known/caldav"


def test_the_entered_url_is_tried_first() -> None:
    assert _candidates("https://cloud.example.com/remote.php/dav")[0] == (
        "https://cloud.example.com/remote.php/dav"
    )


def test_a_bare_host_falls_back_to_the_bootstrap_url() -> None:
    assert _candidates("https://cloud.example.com") == [
        "https://cloud.example.com",
        "https://cloud.example.com/.well-known/caldav",
    ]


def test_a_url_without_a_scheme_still_produces_a_bootstrap_candidate() -> None:
    assert _candidates("cloud.example.com")[1] == (
        "https://cloud.example.com/.well-known/caldav"
    )


def test_the_bootstrap_url_is_not_appended_to_itself() -> None:
    entered = "https://cloud.example.com/.well-known/caldav"

    assert _candidates(entered) == [entered]


def test_the_scheme_of_the_entered_url_is_kept() -> None:
    assert _candidates("http://dav.local")[1] == "http://dav.local/.well-known/caldav"


def test_the_entered_url_is_yielded_without_asking_the_server() -> None:
    with patch("custom_components.ha_caldav.connection.requests.request") as request:
        candidates = url_candidates("https://cloud.example.com", {})
        assert next(candidates) == "https://cloud.example.com"
        assert not request.called


def test_the_bootstrap_is_followed_to_where_it_lands() -> None:
    # RFC 6764 lets the host you type differ from the one holding the account.
    assert _candidates("https://example.com", "https://dav.example.net/dav/") == [
        "https://example.com",
        "https://dav.example.net/dav/",
    ]


def test_a_lookup_that_fails_does_not_license_the_bootstrap() -> None:
    with patch(
        "custom_components.ha_caldav.connection.requests.request",
        side_effect=OSError("no route"),
    ):
        assert list(url_candidates("https://cloud.example.com", {})) == [
            "https://cloud.example.com"
        ]


def test_the_bootstrap_lookup_carries_no_credentials() -> None:
    with _probe("https://cloud.example.com/.well-known/caldav") as request:
        list(url_candidates("https://cloud.example.com", {}))

    assert "auth" not in request.call_args.kwargs


def test_the_bootstrap_lookup_keeps_the_accounts_tls_settings() -> None:
    kwargs = {
        "ssl_verify_cert": "/etc/ca.pem",
        "ssl_cert": ("/etc/client.pem", "/etc/client.key"),
        "timeout": 42,
    }
    with _probe("https://cloud.example.com/.well-known/caldav") as request:
        list(url_candidates("https://cloud.example.com", kwargs))

    assert request.call_args.kwargs["verify"] == "/etc/ca.pem"
    assert request.call_args.kwargs["cert"] == ("/etc/client.pem", "/etc/client.key")
    assert request.call_args.kwargs["timeout"] == 42


def test_the_bootstrap_lookup_always_has_a_timeout() -> None:
    with _probe("https://cloud.example.com/.well-known/caldav") as request:
        list(url_candidates("https://cloud.example.com", {}))

    assert request.call_args.kwargs["timeout"] == DEFAULT_TIMEOUT


def test_defaults_apply_when_nothing_was_configured() -> None:
    assert connection_kwargs({CONF_URL: "https://x"})["ssl_verify_cert"] is True


def _redirected(source: str, target: str):
    response = Mock()
    response.request.url = source
    prepared = Mock()
    prepared.url = target
    prepared.headers = {"Authorization": "Digest secret"}
    prepared.hooks = {"response": [Mock()]}
    return prepared, response


@pytest.mark.parametrize(
    "target",
    [
        "https://attacker.example/dav/",
        "http://cloud.example.com/dav/",
        "https://cloud.example.com:8443/dav/",
    ],
)
def test_a_redirect_off_the_host_takes_the_digest_hook_with_it(target: str) -> None:
    # requests' digest auth answers its challenge from a hook on the request, which
    # survives the hop.
    prepared, response = _redirected("https://cloud.example.com/dav/", target)

    HostLockedSession().rebuild_auth(prepared, response)

    assert prepared.hooks["response"] == []
    assert "Authorization" not in prepared.headers


def test_a_redirect_that_stays_on_the_host_keeps_authenticating() -> None:
    prepared, response = _redirected(
        "https://cloud.example.com/dav/", "https://cloud.example.com/remote.php/dav/"
    )

    HostLockedSession().rebuild_auth(prepared, response)

    assert prepared.hooks["response"] != []
    assert prepared.headers["Authorization"] == "Digest secret"


def test_every_client_is_locked_to_the_host_of_its_url() -> None:
    client = build_client("https://cloud.example.com/dav/", "u", "p", {})
    try:
        assert isinstance(client.session, HostLockedSession)
    finally:
        client.close()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://cloud.example.com/dav/personal/", "/dav/personal"),
        ("/dav/personal/", "/dav/personal"),
        ("/dav/personal", "/dav/personal"),
        # The calendar url keeps its percent-encoding, the PROPFIND href does not.
        ("https://x.example/dav/cal%C3%A4nder/", "/dav/caländer"),
        ("/dav/caländer", "/dav/caländer"),
    ],
)
def test_calendar_key_matches_both_spellings(url, expected) -> None:
    assert calendar_key(url) == expected


def test_credentials_in_the_url_are_dropped() -> None:
    # caldav logs the url it was handed twice before stripping those itself.
    assert (
        without_userinfo("https://alice:hunter2@cloud.example.com/remote.php/dav")
        == "https://cloud.example.com/remote.php/dav"
    )


def test_a_port_survives_the_stripping() -> None:
    assert (
        without_userinfo("https://alice:pw@cloud.example.com:8443/dav")
        == "https://cloud.example.com:8443/dav"
    )


def test_a_url_without_credentials_is_left_exactly_as_it_is() -> None:
    for url in ("https://cloud.example.com/remote.php/dav", "cloud.example.com"):
        assert without_userinfo(url) == url


def test_stripping_userinfo_keeps_an_ipv6_literal_bracketed() -> None:
    from custom_components.ha_caldav.connection import without_userinfo

    assert without_userinfo("https://u:p@[::1]:8443/dav") == "https://[::1]:8443/dav"


def test_a_url_too_malformed_to_parse_does_not_escape_the_flow() -> None:
    from custom_components.ha_caldav.connection import url_candidates, without_userinfo

    broken = "https://[cloud.example.com/dav"

    assert without_userinfo(broken) == broken
    assert list(url_candidates(broken, {})) == [broken]


def test_a_password_comes_off_a_url_too_malformed_to_parse() -> None:
    """caldav logs the url it was handed, whether or not it is usable."""
    from custom_components.ha_caldav.connection import without_userinfo

    stripped = without_userinfo("https://alice:pw@cloud.example.com:80o/dav")

    assert stripped == "https://cloud.example.com:80o/dav"
    assert "pw" not in stripped


def test_stored_connection_details_carry_no_userinfo() -> None:
    """caldav prefers userinfo in the url over the account handed to it."""
    from custom_components.ha_caldav.config_flow import _cleaned

    cleaned = _cleaned(
        {
            "url": "https://intruder:secret@dav.example.com/dav/",
            "username": "iven",
            "password": "own",
        }
    )

    assert cleaned["url"] == "https://dav.example.com/dav/"
    assert "secret" not in cleaned["url"]


def test_the_configured_timeout_is_what_the_client_gets() -> None:
    assert connection_kwargs({CONF_VERIFY_SSL: True}, 90)["timeout"] == 90


def test_a_password_holding_an_at_sign_comes_off_a_malformed_url() -> None:
    stripped = without_userinfo("https://alice:p@ss@cloud.example.com:80o/dav")

    assert stripped == "https://cloud.example.com:80o/dav"


def _answer(status: int, url: str, *, location: str | None = None) -> Mock:
    return Mock(
        status_code=status,
        url=url,
        reason="",
        headers={} if location is None else {"Location": location},
    )


@pytest.mark.parametrize("status", [301, 302, 307, 308])
@pytest.mark.parametrize("method", ["PROPFIND", "DELETE"])
def test_a_write_redirected_on_the_same_host_goes_on_as_it_was_sent(
    method: str, status: int
) -> None:
    session = HostLockedSession()
    moved = _answer(status, "https://cloud.example.com/dav", location="/dav/")
    answer = _answer(207, "https://cloud.example.com/dav/")

    with patch.object(requests.Session, "request", side_effect=[moved, answer]) as sent:
        response = session.request(method, "https://cloud.example.com/dav", data="<x/>")

    assert response is answer
    assert [call.args for call in sent.call_args_list] == [
        (method, "https://cloud.example.com/dav"),
        (method, "https://cloud.example.com/dav/"),
    ]
    for call in sent.call_args_list:
        assert call.kwargs["data"] == "<x/>"
        assert call.kwargs["allow_redirects"] is False
    assert session.moved_to == (
        "https://cloud.example.com/dav/" if status in (301, 308) else None
    )


def test_a_write_follows_every_hop_of_a_redirect_chain() -> None:
    session = HostLockedSession()
    hops = [
        _answer(307, "https://cloud.example.com/dav", location="/dav/"),
        _answer(307, "https://cloud.example.com/dav/", location="/remote.php/dav/"),
        _answer(207, "https://cloud.example.com/remote.php/dav/"),
    ]

    with patch.object(requests.Session, "request", side_effect=hops):
        response = session.request("PROPFIND", "https://cloud.example.com/dav")

    assert response is hops[-1]


def test_the_account_moves_where_its_first_moved_request_went() -> None:
    session = HostLockedSession()
    answers = [
        _answer(301, "https://cloud.example.com/dav", location="/dav/"),
        _answer(207, "https://cloud.example.com/dav/"),
        _answer(301, "https://cloud.example.com/dav/home", location="/home/"),
        _answer(207, "https://cloud.example.com/home/"),
    ]

    with patch.object(requests.Session, "request", side_effect=answers):
        session.request("PROPFIND", "https://cloud.example.com/dav")
        session.request("PROPFIND", "https://cloud.example.com/dav/home")

    assert session.moved_to == "https://cloud.example.com/dav/"


@pytest.mark.parametrize(
    ("location", "moved"),
    [
        ("https://elsewhere.example/dav/", 301),
        ("http://cloud.example.com/dav/", 307),
        ("https://cloud.example.com/dav/", 303),
    ],
    ids=["another_host", "out_of_https", "see_other"],
)
def test_a_redirect_the_write_does_not_follow_is_answered_as_it_came(
    location: str, moved: int
) -> None:
    session = HostLockedSession()
    answer = _answer(moved, "https://cloud.example.com/dav", location=location)

    with patch.object(requests.Session, "request", return_value=answer) as sent:
        response = session.request(
            "PROPFIND", "https://cloud.example.com/dav", data="<x/>"
        )

    assert response is answer
    assert sent.call_count == 1
    assert session.moved_to is None


@pytest.mark.parametrize("method", ["PROPFIND", "GET"])
def test_an_account_entered_over_https_is_never_sent_over_http(method: str) -> None:
    """caldav moves the client onto a calendar home set on another host, scheme
    included, as a SOGo behind a proxy names its backend."""
    session = build_client("https://cloud.example.com/dav/", "u", "p", {}).session

    with (
        patch.object(requests.Session, "request") as sent,
        pytest.raises(requests.exceptions.InvalidURL),
    ):
        session.request(method, "http://backend.internal:20000/SOGo/dav/")

    sent.assert_not_called()


def test_an_account_entered_over_http_stays_on_http() -> None:
    session = build_client("http://nas.local/dav/", "u", "p", {}).session
    answer = _answer(207, "http://nas.local/dav/")

    with patch.object(requests.Session, "request", return_value=answer) as sent:
        session.request("PROPFIND", "http://nas.local/dav/", data="<x/>")

    sent.assert_called_once()


def test_a_read_leaves_its_redirects_to_requests() -> None:
    session = HostLockedSession()
    answer = _answer(200, "https://cloud.example.com/dav/")

    with patch.object(requests.Session, "request", return_value=answer) as sent:
        session.request("GET", "https://cloud.example.com/dav")

    assert "allow_redirects" not in sent.call_args.kwargs


def test_a_read_asks_for_the_answer_unencoded_so_a_proxy_leaves_its_etag_alone() -> (
    None
):
    session = HostLockedSession()
    answer = _answer(200, "https://cloud.example.com/dav/a.ics")

    with patch.object(requests.Session, "request", return_value=answer) as sent:
        session.request(
            "GET", "https://cloud.example.com/dav/a.ics", headers={"Accept": "*/*"}
        )
        session.request("PROPFIND", "https://cloud.example.com/dav/", data="<x/>")

    read, listing = sent.call_args_list
    assert read.kwargs["headers"] == {"Accept": "*/*", "Accept-Encoding": "identity"}
    assert "headers" not in listing.kwargs


@pytest.mark.parametrize(
    ("status", "phrase"), [(401, "Unauthorized"), (403, "Forbidden")]
)
def test_a_refusal_carries_its_standard_phrase(status: int, phrase: str) -> None:
    # caldav tells a bad password from a refusal by the phrase alone.
    answer = _answer(status, "https://cloud.example.com/dav/")
    answer.reason = "Authorization Required"

    with patch.object(requests.Session, "request", return_value=answer):
        response = HostLockedSession().request("PROPFIND", "https://x/", data="<x/>")

    assert response.reason == phrase


@pytest.mark.parametrize(
    "password", ["pässwört", "€uro"], ids=["in_latin_1", "outside_latin_1"]
)
def test_a_basic_login_goes_out_as_utf_8(password: str) -> None:
    client = build_client("https://cloud.example.com/dav/", "jürgen", password, {})
    challenge = requests.Response()
    challenge.status_code = 401
    challenge.headers["WWW-Authenticate"] = 'Basic realm="x", charset="UTF-8"'
    challenge._content = b""

    with (
        patch.object(requests.Session, "request", return_value=challenge) as sent,
        pytest.raises(AuthorizationError),
    ):
        client.principal()

    expected = "Basic " + b64encode(f"jürgen:{password}".encode()).decode()
    logins = [auth for call in sent.call_args_list if (auth := call.kwargs["auth"])]
    assert logins
    for auth in logins:
        prepared = requests.Request("GET", "https://x/", auth=auth).prepare()
        assert prepared.headers["Authorization"] == expected


def _radicale():
    """Answer like Radicale: well-known points at the root, a GET there at the UI."""

    def send(self, request, **kwargs):
        reply = requests.Response()
        reply.url = request.url
        reply.request = request
        path = "/" + request.url.split("/", 3)[3]
        if path == "/.well-known/caldav":
            reply.status_code, reply.headers["Location"] = 301, "/"
        elif path == "/" and request.method == "GET":
            reply.status_code, reply.headers["Location"] = 302, ".web"
        elif path == "/.web":
            reply.status_code, reply.headers["Location"] = 301, "/.web/"
        else:
            reply.status_code = 200
            reply.headers["Content-Type"] = "text/html"
            reply._content = b"<html></html>"
        return reply

    return patch.object(requests.adapters.HTTPAdapter, "send", send)


def test_the_bootstrap_stops_where_the_well_known_url_points() -> None:
    with (
        patch(
            "custom_components.ha_caldav.connection.requests.request",
            new=requests.api.request,
        ),
        _radicale(),
    ):
        candidates = list(url_candidates("http://radicale.example.com:5232/.web/", {}))

    assert candidates == [
        "http://radicale.example.com:5232/.web/",
        "http://radicale.example.com:5232/",
    ]


def _login() -> requests.auth.HTTPBasicAuth:
    return requests.auth.HTTPBasicAuth("iven", "old")


def test_after_a_refused_login_no_request_of_the_account_goes_out() -> None:
    """Servers block an address after a few refused logins, the right one included."""
    session = HostLockedSession()
    refusal = _answer(401, "https://cloud.example.com/dav/")

    with patch.object(requests.Session, "request", return_value=refusal) as sent:
        session.request("PROPFIND", "https://x/", auth=_login(), data="<x/>")
        with pytest.raises(AuthorizationError) as raised:
            session.request("REPORT", "https://x/cal/", auth=_login(), data="<x/>")

    assert sent.call_count == 1
    assert rejected(raised.value)


def test_logins_sent_together_wait_for_the_first_answer() -> None:
    session = HostLockedSession()
    asked, release = threading.Event(), threading.Event()
    calls: list[str] = []
    outcomes: list[object] = []

    def send(method, url, *args, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            asked.set()
            release.wait(5)
        return _answer(401, url)

    def poll(url: str) -> None:
        try:
            outcomes.append(session.request("REPORT", url, auth=_login(), data="<x/>"))
        except AuthorizationError as err:
            outcomes.append(err)

    with patch.object(requests.Session, "request", side_effect=send):
        first = threading.Thread(target=poll, args=("https://x/a/",))
        first.start()
        asked.wait(5)
        second = threading.Thread(target=poll, args=("https://x/b/",))
        second.start()
        second.join(0.2)
        waited = second.is_alive()
        release.set()
        first.join(5)
        second.join(5)

    assert waited
    assert calls == ["https://x/a/"]
    assert any(isinstance(outcome, AuthorizationError) for outcome in outcomes)


def test_a_digest_password_handed_over_as_bytes_is_signed_as_text() -> None:
    """caldav 2 keeps the password as bytes, which requests hashes as "b'...'"."""
    auth = requests.auth.HTTPDigestAuth("iven", "pässwort".encode())
    answer = _answer(207, "https://cloud.example.com/dav/")

    with patch.object(requests.Session, "request", return_value=answer):
        HostLockedSession().request("PROPFIND", "https://x/", auth=auth, data="<x/>")

    assert auth.password == "pässwort"


async def test_one_round_of_polls_after_a_password_change_logs_in_once(
    hass: HomeAssistant,
) -> None:
    url = "https://cloud.example.com/remote.php/dav/"
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_URL: url, CONF_USERNAME: "iven", CONF_PASSWORD: "old"},
        unique_id="x",
    )
    entry.add_to_hass(hass)
    logins: list[str] = []

    def send(self, request, **kwargs):
        if "Authorization" in request.headers:
            logins.append(request.url)
        reply = requests.Response()
        reply.status_code = 401
        reply.headers["WWW-Authenticate"] = 'Basic realm="Nextcloud"'
        reply._content = b""
        reply.url = request.url
        reply.request = request
        return reply

    client = build_client(url, "iven", "old", {})
    client.auth = requests.auth.HTTPBasicAuth("iven", "old")
    polls = [HaCaldavColorCoordinator(hass, entry, client, timedelta(minutes=15))]
    for index in range(4):
        calendar = caldav.Calendar(client=client, url=f"{url}calendars/iven/c{index}/")
        coordinator = HaCaldavCoordinator(
            hass,
            entry,
            calendar,
            Capability(frozenset({"VEVENT", "VTODO"}), True),
            7,
            True,
            timedelta(minutes=15),
        )
        for half in coordinator.halves.values():
            half.cached = []
        polls.append(coordinator)
    with patch.object(requests.adapters.HTTPAdapter, "send", send):
        await asyncio.gather(*(poll.async_refresh() for poll in polls))
    client.close()

    assert len(logins) == 1
    assert not any(poll.last_update_success for poll in polls)
