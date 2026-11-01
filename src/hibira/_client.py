from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote, unquote

import httpx

from . import _signing
from ._errors import ConfigError, error_for
from ._version import __version__

DEFAULT_HOST = "ws.hibira.io"
MAX_TRIGGER_CHANNELS = 100
MAX_APPROVAL_WAIT = 25

_USER_AGENT = f"hibira-python/{__version__}"


@dataclass
class _Request:
    method: str
    url: str
    params: dict[str, str] = field(default_factory=dict)
    content: bytes | None = None
    headers: dict[str, str] = field(default_factory=dict)


def _env_bool(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    return value.strip().lower() not in ("0", "false", "no", "off")


def _channels(channels: str | Iterable[str]) -> list[str]:
    out = [channels] if isinstance(channels, str) else list(channels)
    if not out:
        raise ValueError("give at least one channel")
    if len(out) > MAX_TRIGGER_CHANNELS:
        raise ValueError(f"trigger takes at most {MAX_TRIGGER_CHANNELS} channels")
    for ch in out:
        _signing.check_channel(ch)
    return out


def _data(data: Any) -> str:
    return data if isinstance(data, str) else _signing.compact(data)


def _parse(response: httpx.Response) -> Any:
    if response.status_code >= 400:
        raise error_for(response.status_code, response.text)
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        return {}


class _Base:
    def __init__(
        self,
        app_id: str,
        key: str,
        secret: str,
        host: str = DEFAULT_HOST,
        port: int | None = None,
        use_tls: bool = True,
        timeout: float = 10.0,
        push_instance_id: str | None = None,
        push_secret_key: str | None = None,
        push_endpoint: str | None = None,
    ) -> None:
        missing = [n for n, v in (("app_id", app_id), ("key", key), ("secret", secret)) if not v]
        if missing:
            raise ConfigError(f"missing {', '.join(missing)} (set HIBIRA_APP_ID, HIBIRA_APP_KEY, HIBIRA_APP_SECRET)")
        self.app_id = str(app_id)
        self.key = key
        self.secret = secret
        self.host = host or DEFAULT_HOST
        self.use_tls = use_tls
        self.port = port
        self.timeout = timeout
        scheme = "https" if use_tls else "http"
        default_port = 443 if use_tls else 80
        self.base_url = f"{scheme}://{self.host}" + (f":{port}" if port and port != default_port else "")
        self.push_instance_id = push_instance_id
        self.push_secret_key = push_secret_key
        self.push_endpoint = (push_endpoint or self.base_url).rstrip("/")

    @classmethod
    def from_env(cls, **overrides: Any) -> Any:
        """Settings from HIBIRA_* environment variables; keyword arguments win."""
        env = os.environ
        port = env.get("HIBIRA_PORT")
        values: dict[str, Any] = {
            "app_id": env.get("HIBIRA_APP_ID", ""),
            "key": env.get("HIBIRA_APP_KEY", ""),
            "secret": env.get("HIBIRA_APP_SECRET", ""),
            "host": env.get("HIBIRA_HOST") or DEFAULT_HOST,
            "port": int(port) if port else None,
            "use_tls": _env_bool(env.get("HIBIRA_USE_TLS")),
            "push_instance_id": env.get("HIBIRA_PUSH_INSTANCE_ID") or None,
            "push_secret_key": env.get("HIBIRA_PUSH_SECRET_KEY") or None,
            "push_endpoint": env.get("HIBIRA_PUSH_ENDPOINT") or None,
        }
        if values["use_tls"] is None:
            values["use_tls"] = True
        values.update(overrides)
        return cls(**values)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(app_id={self.app_id!r}, host={self.host!r})"

    # Signing (no network)

    def authenticate_channel(
        self, channel: str, socket_id: str, user_data: Mapping[str, Any] | None = None
    ) -> dict[str, str]:
        """Authorization for a private- or presence- channel subscription.

        Return it as JSON from your auth endpoint. Presence channels need
        user_data with a user_id (and optional user_info).
        """
        return _signing.authenticate_channel(self.key, self.secret, channel, socket_id, user_data)

    def authenticate_user(self, socket_id: str, user_data: Mapping[str, Any]) -> dict[str, str]:
        """Response for the client's user sign-in (pusher:signin); user_data needs an id."""
        return _signing.authenticate_user(self.key, self.secret, socket_id, user_data)

    def verify_webhook(self, key: str | None, signature: str | None, body: bytes | str) -> Any:
        """Check a webhook's X-Pusher-Key and X-Pusher-Signature headers and return its parsed body.

        Pass the raw request body. Raises InvalidWebhook when they do not match.
        """
        return _signing.verify_webhook(self.key, self.secret, key, signature, body)

    def push_token(self, user_id: str, ttl: int = 86400) -> dict[str, str]:
        """A Hibira Push user token, for setUserId in the web, Android and iOS SDKs."""
        instance, secret = self._push_settings()
        if not user_id or len(str(user_id)) > 164:
            raise ValueError("user_id must be 1 to 164 characters")
        return {"token": _signing.push_token(instance, secret, str(user_id), ttl)}

    # Request builders

    def _signed(self, method: str, path: str, params: Mapping[str, Any] | None = None, body: Any = None) -> _Request:
        content = _signing.compact(body).encode() if body is not None else None
        full = f"/apps/{quote(self.app_id, safe='')}{path}"
        q = _signing.sign_query(self.key, self.secret, method, unquote(full), params, content)
        headers = {"User-Agent": _USER_AGENT}
        if content is not None:
            headers["Content-Type"] = "application/json"
        return _Request(method, self.base_url + full, q, content, headers)

    def _trigger_req(self, channels: str | Iterable[str], event: str, data: Any, socket_id: str | None) -> _Request:
        if not event or len(event) > 200:
            raise ValueError("event name must be 1 to 200 characters")
        body: dict[str, Any] = {"name": event, "data": _data(data), "channels": _channels(channels)}
        if socket_id:
            _signing.check_socket_id(socket_id)
            body["socket_id"] = socket_id
        return self._signed("POST", "/events", body=body)

    def _batch_req(self, events: Sequence[Mapping[str, Any]]) -> _Request:
        if not events:
            raise ValueError("give at least one event")
        batch = []
        for e in events:
            _signing.check_channel(e["channel"])
            item = {"channel": e["channel"], "name": e["name"], "data": _data(e.get("data", ""))}
            if e.get("socket_id"):
                _signing.check_socket_id(e["socket_id"])
                item["socket_id"] = e["socket_id"]
            if e.get("info"):
                item["info"] = e["info"]
            batch.append(item)
        return self._signed("POST", "/batch_events", body={"batch": batch})

    def _channels_req(self, prefix: str | None, info: str | Iterable[str] | None) -> _Request:
        return self._signed("GET", "/channels", {"filter_by_prefix": prefix, "info": _info(info)})

    def _channel_req(self, channel: str, info: str | Iterable[str] | None) -> _Request:
        _signing.check_channel(channel)
        return self._signed("GET", f"/channels/{channel}", {"info": _info(info)})

    def _users_req(self, channel: str) -> _Request:
        _signing.check_channel(channel)
        if not channel.startswith("presence-"):
            raise ValueError("users() works on presence- channels")
        return self._signed("GET", f"/channels/{channel}/users")

    def _terminate_req(self, user_id: str) -> _Request:
        if not user_id:
            raise ValueError("user_id is required")
        return self._signed("POST", f"/users/{quote(str(user_id), safe='')}/terminate_connections", body={})

    def _approval_req(self, **fields: Any) -> _Request:
        body = {k: v for k, v in fields.items() if v is not None}
        if not body.get("title"):
            raise ValueError("title is required")
        return self._signed("POST", "/approvals", body=body)

    def _get_approval_req(self, approval_id: str, wait: int) -> _Request:
        if not approval_id:
            raise ValueError("approval_id is required")
        if wait < 0 or wait > MAX_APPROVAL_WAIT:
            raise ValueError(f"wait must be 0 to {MAX_APPROVAL_WAIT} seconds")
        params = {"wait": wait} if wait else None
        return self._signed("GET", f"/approvals/{quote(approval_id, safe='')}", params)

    def _push_settings(self) -> tuple[str, str]:
        if not self.push_instance_id or not self.push_secret_key:
            raise ConfigError(
                "push needs push_instance_id and push_secret_key (HIBIRA_PUSH_INSTANCE_ID, HIBIRA_PUSH_SECRET_KEY)"
            )
        return self.push_instance_id, self.push_secret_key

    def _push_req(self, method: str, path: str, body: Any = None) -> _Request:
        instance, secret = self._push_settings()
        headers = {"Authorization": f"Bearer {secret}", "User-Agent": _USER_AGENT}
        content = None
        if body is not None:
            content = _signing.compact(body).encode()
            headers["Content-Type"] = "application/json"
        url = f"{self.push_endpoint}{path.format(instance=quote(instance, safe=''))}"
        return _Request(method, url, {}, content, headers)

    def _push_interests_req(self, interests: Iterable[str], body: Mapping[str, Any]) -> _Request:
        names = [interests] if isinstance(interests, str) else list(interests)
        if not names:
            raise ValueError("give at least one interest")
        return self._push_req(
            "POST", "/publish_api/v1/instances/{instance}/publishes/interests", {**body, "interests": names}
        )

    def _push_users_req(self, user_ids: Iterable[str], body: Mapping[str, Any]) -> _Request:
        users = [user_ids] if isinstance(user_ids, str) else [str(u) for u in user_ids]
        if not users:
            raise ValueError("give at least one user id")
        return self._push_req("POST", "/publish_api/v1/instances/{instance}/publishes/users", {**body, "users": users})

    def _delete_push_user_req(self, user_id: str) -> _Request:
        return self._push_req("DELETE", f"/customer_api/v1/instances/{{instance}}/users/{quote(str(user_id), safe='')}")


def _info(info: str | Iterable[str] | None) -> str | None:
    if info is None or isinstance(info, str):
        return info or None
    return ",".join(info) or None


def _approval_fields(
    title: str,
    details: str | None,
    options: Sequence[str] | None,
    channel: str | None,
    expires_in_minutes: int | None,
    push_interests: Sequence[str] | None,
    push_users: Sequence[str] | None,
    agent: str | None,
) -> dict[str, Any]:
    return {
        "title": title,
        "details": details,
        "options": list(options) if options else None,
        "channel": channel,
        "expires_in_minutes": expires_in_minutes,
        "push_interests": list(push_interests) if push_interests else None,
        "push_users": list(push_users) if push_users else None,
        "agent": agent,
    }


class Client(_Base):
    """Server client for a Hibira app: publish events, authorize channels,
    verify webhooks, send push notifications and ask for approvals.

    >>> hibira = Client.from_env()
    >>> hibira.trigger("orders", "created", {"id": 1042})
    """

    def __init__(
        self, app_id: str, key: str, secret: str, *, http_client: httpx.Client | None = None, **kwargs: Any
    ) -> None:
        super().__init__(app_id, key, secret, **kwargs)
        self._own_http = http_client is None
        self._http = http_client or httpx.Client(timeout=self.timeout)

    def _send(self, req: _Request, timeout: float | None = None) -> Any:
        r = self._http.request(
            req.method,
            req.url,
            params=req.params or None,
            content=req.content,
            headers=req.headers,
            timeout=timeout if timeout is not None else self.timeout,
        )
        return _parse(r)

    def close(self) -> None:
        if self._own_http:
            self._http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def trigger(self, channels: str | Iterable[str], event: str, data: Any, socket_id: str | None = None) -> Any:
        """Publish an event to one channel or up to 100. Data that is not a str is sent as JSON.

        socket_id leaves out that connection (the sender's own tab).
        """
        return self._send(self._trigger_req(channels, event, data, socket_id))

    def trigger_batch(self, events: Sequence[Mapping[str, Any]]) -> Any:
        """Publish several events in one request (10 at most by default).

        events: [{"channel", "name", "data", "socket_id"?}]
        """
        return self._send(self._batch_req(events))

    def channels(self, prefix: str | None = None, info: str | Iterable[str] | None = None) -> Any:
        return self._send(self._channels_req(prefix, info))

    def channel_info(self, channel: str, info: str | Iterable[str] | None = None) -> Any:
        return self._send(self._channel_req(channel, info))

    def users(self, channel: str) -> Any:
        return self._send(self._users_req(channel))

    def terminate_user_connections(self, user_id: str) -> Any:
        return self._send(self._terminate_req(user_id))

    def push_to_interests(self, interests: str | Iterable[str], body: Mapping[str, Any]) -> Any:
        """Send a push notification to devices subscribed to any of interests.

        body is a Beams publish body: {"web": {...}, "fcm": {...}, "apns": {...}}.
        """
        return self._send(self._push_interests_req(interests, body))

    def push_to_users(self, user_ids: str | Iterable[str], body: Mapping[str, Any]) -> Any:
        """Send a push notification to the devices of up to 1000 users."""
        return self._send(self._push_users_req(user_ids, body))

    def delete_push_user(self, user_id: str) -> Any:
        return self._send(self._delete_push_user_req(user_id))

    def request_approval(
        self,
        title: str,
        details: str | None = None,
        options: Sequence[str] | None = None,
        channel: str | None = None,
        expires_in_minutes: int | None = None,
        push_interests: Sequence[str] | None = None,
        push_users: Sequence[str] | None = None,
        agent: str | None = None,
    ) -> Any:
        """Ask a person for a decision; share the returned "url" with them."""
        fields = _approval_fields(
            title, details, options, channel, expires_in_minutes, push_interests, push_users, agent
        )
        return self._send(self._approval_req(**fields))

    def get_approval(self, approval_id: str, wait: int = 0) -> Any:
        """An approval's status; wait (up to 25 s) holds the request until it is answered."""
        return self._send(self._get_approval_req(approval_id, wait), timeout=self.timeout + wait)

    def wait_for_approval(self, approval_id: str, timeout: float = 3600) -> Any:
        """Poll until the approval is no longer pending (or timeout seconds pass) and return it."""
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            out = self.get_approval(approval_id, wait=max(0, min(MAX_APPROVAL_WAIT, int(left))))
            if out.get("status") != "pending" or time.monotonic() >= deadline:
                return out


class AsyncClient(_Base):
    """asyncio version of Client: the same methods, HTTP ones are awaitable.

    authenticate_channel, authenticate_user, verify_webhook and push_token
    need no network and stay synchronous.
    """

    def __init__(
        self, app_id: str, key: str, secret: str, *, http_client: httpx.AsyncClient | None = None, **kwargs: Any
    ) -> None:
        super().__init__(app_id, key, secret, **kwargs)
        self._own_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=self.timeout)

    async def _send(self, req: _Request, timeout: float | None = None) -> Any:
        r = await self._http.request(
            req.method,
            req.url,
            params=req.params or None,
            content=req.content,
            headers=req.headers,
            timeout=timeout if timeout is not None else self.timeout,
        )
        return _parse(r)

    async def aclose(self) -> None:
        if self._own_http:
            await self._http.aclose()

    async def __aenter__(self) -> AsyncClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def trigger(self, channels: str | Iterable[str], event: str, data: Any, socket_id: str | None = None) -> Any:
        return await self._send(self._trigger_req(channels, event, data, socket_id))

    async def trigger_batch(self, events: Sequence[Mapping[str, Any]]) -> Any:
        return await self._send(self._batch_req(events))

    async def channels(self, prefix: str | None = None, info: str | Iterable[str] | None = None) -> Any:
        return await self._send(self._channels_req(prefix, info))

    async def channel_info(self, channel: str, info: str | Iterable[str] | None = None) -> Any:
        return await self._send(self._channel_req(channel, info))

    async def users(self, channel: str) -> Any:
        return await self._send(self._users_req(channel))

    async def terminate_user_connections(self, user_id: str) -> Any:
        return await self._send(self._terminate_req(user_id))

    async def push_to_interests(self, interests: str | Iterable[str], body: Mapping[str, Any]) -> Any:
        return await self._send(self._push_interests_req(interests, body))

    async def push_to_users(self, user_ids: str | Iterable[str], body: Mapping[str, Any]) -> Any:
        return await self._send(self._push_users_req(user_ids, body))

    async def delete_push_user(self, user_id: str) -> Any:
        return await self._send(self._delete_push_user_req(user_id))

    async def request_approval(
        self,
        title: str,
        details: str | None = None,
        options: Sequence[str] | None = None,
        channel: str | None = None,
        expires_in_minutes: int | None = None,
        push_interests: Sequence[str] | None = None,
        push_users: Sequence[str] | None = None,
        agent: str | None = None,
    ) -> Any:
        fields = _approval_fields(
            title, details, options, channel, expires_in_minutes, push_interests, push_users, agent
        )
        return await self._send(self._approval_req(**fields))

    async def get_approval(self, approval_id: str, wait: int = 0) -> Any:
        return await self._send(self._get_approval_req(approval_id, wait), timeout=self.timeout + wait)

    async def wait_for_approval(self, approval_id: str, timeout: float = 3600) -> Any:
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            out = await self.get_approval(approval_id, wait=max(0, min(MAX_APPROVAL_WAIT, int(left))))
            if out.get("status") != "pending" or time.monotonic() >= deadline:
                return out
