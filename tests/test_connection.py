"""Tests for urls and for turning connection details into client arguments."""

from unittest.mock import Mock, patch

from homeassistant.const import CONF_URL, CONF_VERIFY_SSL
import pytest

from custom_components.ha_caldav.connection import (
    HostLockedSession,
    account_key,
    build_client,
    calendar_key,
    connection_kwargs,
    url_candidates,
    without_userinfo,
)
from custom_components.ha_caldav.const import (
    CONF_CA_BUNDLE,
    CONF_CLIENT_CERT,
    CONF_CLIENT_KEY,
    REQUEST_TIMEOUT,
)


def test_plain_account_verifies_against_the_system_store() -> None:
    assert connection_kwargs({CONF_VERIFY_SSL: True}) == {
        "ssl_verify_cert": True,
        "ssl_cert": None,
        "timeout": REQUEST_TIMEOUT,
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
    """Stub the unauthenticated bootstrap lookup with where it ended up."""
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
    """The key is the config entry unique_id. Keyed on the text as entered, the
    same account is set up a second time and every calendar and to-do list of
    it appears twice."""
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
    # A non-default port, another scheme, another host and another path each
    # name a server this one has no claim on.
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
    # A key is what the entry is stored under; raising here would leave the
    # account impossible to set up rather than merely oddly keyed.
    assert account_key("https://dav.example.com:notaport/dav", "iven").endswith("#iven")


def test_a_bootstrap_that_steps_out_of_https_is_ignored() -> None:
    """A proxy terminating TLS without X-Forwarded-Proto writes its redirects
    as http. Following one puts the account password on the wire in clear, and
    stores that url for every poll after it."""
    with _probe(
        resolved="https://dav.example.com/dav/",
        hops=["https://dav.example.com/.well-known/caldav", "http://dav.example.com/"],
    ):
        candidates = list(url_candidates("https://dav.example.com", {}))

    assert candidates == ["https://dav.example.com"]


def test_a_bootstrap_landing_on_plain_http_is_ignored() -> None:
    # The last hop counts too, not only the ones in between.
    with _probe(resolved="http://dav.example.com/dav/"):
        candidates = list(url_candidates("https://dav.example.com", {}))

    assert candidates == ["https://dav.example.com"]


def test_a_bootstrap_entered_as_http_may_stay_on_http() -> None:
    # Nothing was promised, so nothing is broken; refusing here would leave a
    # plain-http server unreachable rather than more secure.
    with _probe(resolved="http://dav.example.com/dav/"):
        candidates = list(url_candidates("http://dav.example.com", {}))

    assert candidates == ["http://dav.example.com", "http://dav.example.com/dav/"]


def test_the_bootstrap_result_is_stripped_of_userinfo() -> None:
    """caldav prefers userinfo in the url over the account handed to it, so a
    redirect naming its own would authenticate as whoever it likes."""
    with _probe(resolved="https://intruder:secret@dav.example.com/dav/"):
        candidates = list(url_candidates("https://dav.example.com", {}))

    assert candidates == ["https://dav.example.com", "https://dav.example.com/dav/"]
    assert not any("intruder" in candidate for candidate in candidates)


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
    # The lookup costs a request; a server that answers the entered url
    # directly must not pay for it.
    with patch("custom_components.ha_caldav.connection.requests.request") as request:
        candidates = url_candidates("https://cloud.example.com", {})
        assert next(candidates) == "https://cloud.example.com"
        assert not request.called


def test_the_bootstrap_is_followed_to_where_it_lands() -> None:
    # RFC 6764 exists so the host you type need not be the one holding the
    # account. Authenticating against the settled url rather than following the
    # redirect later is what keeps the handshake off the intermediate hops.
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
    # A self-signed or client-certificate account would otherwise lose its
    # bootstrap candidate to a verification error it already answered for.
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

    assert request.call_args.kwargs["timeout"] == REQUEST_TIMEOUT


def test_defaults_apply_when_nothing_was_configured() -> None:
    assert connection_kwargs({CONF_URL: "https://x"})["ssl_verify_cert"] is True


def _redirected(source: str, target: str):
    """Return the pair a session sees when a request is redirected."""
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
    # The header alone is not enough: digest answers its challenge from a hook
    # on the request, which survives the hop and re-signs for whoever answers.
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
    # caldav logs the url it was handed twice before stripping those itself,
    # and the form has its own fields for both.
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
    """It reaches the connection attempt, which fails into an ordinary error."""
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
    """caldav prefers userinfo in the url over the account handed to it, so a
    url pasted with credentials in it would authenticate as whoever it names —
    and the entry would keep that password in a second place forever."""
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
