"""The exception types a CalDAV call can fail with, and how they are reported.

caldav binds ``requests`` to niquests when that is importable, and the two
exception hierarchies share only ``OSError``. Catching caldav's own alias is
therefore the only way to name the errors it will actually raise.
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
    """A write this integration declines to make, named by translation key.

    A ValueError so the write paths that predate it still catch it; the key is
    what reaches the user, and str() is the key so tests can name it.
    """

    def __init__(self, key: str, **placeholders: str) -> None:
        super().__init__(key)
        self.key = key
        self.placeholders = placeholders


WRITE_ERRORS = (*NETWORK_ERRORS, ValueError)


def as_reported(err: Exception, action: str) -> HomeAssistantError:
    """Return the error to raise at the user for a failed call.

    Server errors carry the collection url, and with it the account name, so
    only the kind of failure is reported.
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
        # From a library, not from us, so there is no key to translate. Network
        # errors are excluded because several niquests ones are ValueError too
        # and name the collection in their message.
        return ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="refused",
            translation_placeholders={"reason": str(err)},
        )
    # At error level, because the message this returns tells the user the log
    # has the details and a write that failed is one they have to know about.
    # The url the library prints belongs here rather than in what they are then
    # asked to paste into an issue.
    _LOGGER.error("CalDAV %s failed: %s", action, err)
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="server_error",
        translation_placeholders={"action": action},
    )
