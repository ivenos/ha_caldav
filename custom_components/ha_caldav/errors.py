"""The exception types a CalDAV call can fail with, and how they are reported.

caldav binds ``requests`` to niquests when that is importable, so only its own
alias names the exceptions it raises.
"""

from __future__ import annotations

import logging
import ssl

from caldav.davclient import requests
from caldav.lib.error import AuthorizationError, DAVError, NotFoundError
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

NETWORK_ERRORS = (requests.RequestException, DAVError)

# What OpenSSL reports when a handshake is answered in plain text.
_PLAIN_TEXT = frozenset({"WRONG_VERSION_NUMBER", "RECORD_LAYER_FAILURE"})


def rejected(err: BaseException | None) -> bool:
    """Return whether an error is a 401; caldav raises the same type for 403."""
    return isinstance(err, AuthorizationError) and err.reason == "Unauthorized"


def without_tls(err: BaseException) -> bool:
    """Return whether a TLS error comes from a server that does not speak TLS.

    The HTTP libraries wrap the ssl error a few levels down.
    """
    seen: set[int] = set()
    pending: list[object] = [err]
    while pending:
        item = pending.pop()
        if not isinstance(item, BaseException) or id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, ssl.SSLError) and item.reason in _PLAIN_TEXT:
            return True
        pending.extend(
            (item.__cause__, item.__context__, getattr(item, "reason", None))
        )
        pending.extend(item.args)
    return False


class Refused(ValueError):
    """A write this integration declines to make, named by translation key."""

    def __init__(self, key: str, **placeholders: str) -> None:
        super().__init__(key)
        self.key = key
        self.placeholders = placeholders


WRITE_ERRORS = (*NETWORK_ERRORS, ValueError)


def as_reported(err: Exception, action: str) -> HomeAssistantError:
    """Return the error to raise at the user for a failed call.

    A server error carries the collection url, so the user gets the kind of
    failure and the log gets the rest.
    """
    if isinstance(err, Refused):
        return ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=err.key,
            translation_placeholders=err.placeholders,
        )
    if isinstance(err, NotFoundError):
        _LOGGER.debug("CalDAV %s: not on the server: %s", action, err)
        return ServiceValidationError(
            translation_domain=DOMAIN, translation_key="not_found"
        )
    if isinstance(err, ValueError) and not isinstance(err, NETWORK_ERRORS):
        # The HTTP libraries raise ValueErrors too, which name the collection.
        return ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="refused",
            translation_placeholders={"reason": str(err)},
        )
    _LOGGER.error("CalDAV %s failed: %s", action, err)
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="server_error",
        translation_placeholders={"action": action},
    )
