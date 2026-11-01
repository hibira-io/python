from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from typing import Any, Mapping

from ._errors import InvalidWebhook

_SOCKET_ID = re.compile(r"\A\d+\.\d+\Z")
_CHANNEL = re.compile(r"\A[-a-zA-Z0-9_=@,.;]+\Z")


def compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def hmac_hex(secret: str, message: str | bytes) -> str:
    if isinstance(message, str):
        message = message.encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def check_socket_id(socket_id: str) -> None:
    if not isinstance(socket_id, str) or not _SOCKET_ID.match(socket_id):
        raise ValueError(f"invalid socket_id: {socket_id!r}")


def check_channel(channel: str) -> None:
    if not isinstance(channel, str) or not _CHANNEL.match(channel) or len(channel) > 200:
        raise ValueError(f"invalid channel name: {channel!r}")


def sign_query(
    key: str,
    secret: str,
    method: str,
    path: str,
    params: Mapping[str, Any] | None = None,
    body: bytes | None = None,
    now: float | None = None,
) -> dict[str, str]:
    """Query parameters of a signed HTTP API request (Pusher auth_version 1.0)."""
    q = {k.lower(): str(v) for k, v in (params or {}).items() if v is not None}
    q["auth_key"] = key
    q["auth_timestamp"] = str(int(now if now is not None else time.time()))
    q["auth_version"] = "1.0"
    if body:
        q["body_md5"] = hashlib.md5(body).hexdigest()
    string = "&".join(f"{k}={q[k]}" for k in sorted(q))
    q["auth_signature"] = hmac_hex(secret, f"{method.upper()}\n{path}\n{string}")
    return q


def authenticate_channel(
    key: str, secret: str, channel: str, socket_id: str, user_data: Mapping[str, Any] | None = None
) -> dict[str, str]:
    check_channel(channel)
    check_socket_id(socket_id)
    if channel.startswith("private-encrypted-"):
        raise ValueError("encrypted channels are not supported")
    if channel.startswith("presence-"):
        if not user_data or user_data.get("user_id") in (None, ""):
            raise ValueError("presence channels need user_data with a user_id")
        channel_data = compact(dict(user_data))
        sig = hmac_hex(secret, f"{socket_id}:{channel}:{channel_data}")
        return {"auth": f"{key}:{sig}", "channel_data": channel_data}
    if not channel.startswith("private-"):
        raise ValueError("only private- and presence- channels need authorization")
    return {"auth": f"{key}:{hmac_hex(secret, f'{socket_id}:{channel}')}"}


def authenticate_user(key: str, secret: str, socket_id: str, user_data: Mapping[str, Any]) -> dict[str, str]:
    check_socket_id(socket_id)
    if not user_data or user_data.get("id") in (None, ""):
        raise ValueError("user_data needs an id")
    encoded = compact(dict(user_data))
    return {"auth": f"{key}:{hmac_hex(secret, f'{socket_id}::user::{encoded}')}", "user_data": encoded}


def verify_webhook(key: str, secret: str, got_key: str | None, signature: str | None, body: bytes | str) -> Any:
    raw = body.encode() if isinstance(body, str) else bytes(body)
    if not got_key or not hmac.compare_digest(got_key, key):
        raise InvalidWebhook("webhook key does not match this app")
    if not signature or not hmac.compare_digest(signature.lower(), hmac_hex(secret, raw)):
        raise InvalidWebhook("webhook signature does not match")
    try:
        return json.loads(raw)
    except ValueError as err:
        raise InvalidWebhook("webhook body is not JSON") from err


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def push_token(instance_id: str, secret_key: str, user_id: str, ttl: int, now: float | None = None) -> str:
    t = int(now if now is not None else time.time())
    header = _b64(compact({"alg": "HS256", "typ": "JWT"}).encode())
    claims = {"sub": user_id, "iss": f"https://{instance_id}.pushnotifications.pusher.com", "iat": t, "exp": t + ttl}
    payload = _b64(compact(claims).encode())
    sig = hmac.new(secret_key.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64(sig)}"
