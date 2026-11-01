from __future__ import annotations

import builtins
import json
from typing import Any


class HibiraError(Exception):
    """Base class of every error this package raises."""


class ConfigError(HibiraError):
    """A setting is missing or invalid (app id, key, secret, push instance...)."""


class ConnectionError(HibiraError, builtins.ConnectionError):  # noqa: A001
    """The request did not get an answer: DNS, connect, TLS, proxy or timeout failure."""


class InvalidWebhook(HibiraError):
    """A webhook's key or signature does not match this app."""


class HTTPError(HibiraError):
    """The Hibira HTTP API answered with an error status."""

    def __init__(self, status: int, message: str, body: Any = None) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message
        self.body = body


class AuthenticationError(HTTPError):
    """401 or 403: bad key, secret or signature, or a suspended account."""


class NotFound(HTTPError):
    """404: unknown app, channel, approval or push instance."""


class PayloadTooLarge(HTTPError):
    """413: the event is over the plan's message size (4 KB on Free)."""


class RateLimited(HTTPError):
    """429: too many requests or messages for the plan."""


def error_for(status: int, text: str) -> HTTPError:
    body: Any = text
    message = text.strip() or "request failed"
    try:
        body = json.loads(text)
    except ValueError:
        pass
    else:
        if isinstance(body, dict):
            for k in ("error", "message", "description"):
                if isinstance(body.get(k), str):
                    message = body[k]
                    break
    cls: type[HTTPError] = HTTPError
    if status in (401, 403):
        cls = AuthenticationError
    elif status == 404:
        cls = NotFound
    elif status == 413:
        cls = PayloadTooLarge
    elif status == 429:
        cls = RateLimited
    return cls(status, message, body)
