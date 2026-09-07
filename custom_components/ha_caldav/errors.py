"""The exception types a CalDAV call can fail with, and how they are reported.

caldav binds ``requests`` to niquests when that is importable, so only its own
alias names the exceptions it raises.
"""

from __future__ import annotations

import logging

from caldav.davclient import requests
from caldav.lib.error import DAVError, NotFoundError
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CONNECTION_ERRORS = (requests.ConnectionError, requests.Timeout, DAVError)
NETWORK_ERRORS = (requests.RequestException, DAVError)


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
        # Several niquests errors are ValueErrors naming the collection.
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
