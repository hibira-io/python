"""Hibira for Python: publish realtime events, authorize private and presence
channels, verify webhooks, send push notifications, ask people for approvals
and stream LLM output to browsers.

Hibira speaks the Pusher Channels protocol, so pusher-js, Laravel Echo and
the @hibira-io/* front-end packages connect to it unchanged.
"""

from ._client import DEFAULT_HOST, AsyncClient, Client
from ._errors import (
    AuthenticationError,
    ConfigError,
    HibiraError,
    HTTPError,
    InvalidWebhook,
    NotFound,
    PayloadTooLarge,
    RateLimited,
)
from ._version import __version__
from .stream import STREAM_EVENT, StreamResult, astream_to_channel, stream_to_channel, text_of

__all__ = [
    "DEFAULT_HOST",
    "STREAM_EVENT",
    "AsyncClient",
    "AuthenticationError",
    "Client",
    "ConfigError",
    "HTTPError",
    "HibiraError",
    "InvalidWebhook",
    "NotFound",
    "PayloadTooLarge",
    "RateLimited",
    "StreamResult",
    "__version__",
    "astream_to_channel",
    "stream_to_channel",
    "text_of",
]
