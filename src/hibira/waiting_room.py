"""Waiting rooms: send visitors to the Hibira queue, let them in with a pass.

FastAPI, Starlette or any ASGI app::

    from hibira.waiting_room import WaitingRoom, ASGIMiddleware

    room = WaitingRoom(app_key=KEY, secret=SECRET, room="presale")
    app.add_middleware(ASGIMiddleware, room=room, paths=["/drop"])

    @app.get("/drop")
    async def drop(request: Request):
        request.state.hibira_pass  # Pass(number=42, ticket_id=..., ...)

Flask or any WSGI app::

    from hibira.waiting_room import WaitingRoom, WSGIMiddleware

    app.wsgi_app = WSGIMiddleware(app.wsgi_app, room=room, paths=["/drop"])
    # in the view: request.environ["hibira.pass"]

A visitor without a valid pass is redirected to the hosted waiting page
(requests other than GET and HEAD get a 403). When it's their turn the page
sends them back with ``?hibira_pass=...``, which is checked, stored in an
HttpOnly cookie (``hibira_wr_<room>``) and removed from the URL. This is the
same behaviour as ``@hibira-io/waiting-room/server`` and the framework
packages, with no dependencies.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Literal, MutableMapping, Optional, Sequence, Union
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

DEFAULT_HOST = "https://ws.hibira.io"
PARAM = "hibira_pass"
_PURPOSE = b"hibira-waitroom-pass."
_DENY_BODY = b'{"error":"waiting_room","message":"join the waiting room first"}'

__all__ = [
    "DEFAULT_HOST",
    "ASGIMiddleware",
    "Decision",
    "Pass",
    "WSGIMiddleware",
    "WaitingRoom",
    "sign_user",
    "verify_pass",
    "waiting_room_url",
]


@dataclass(frozen=True)
class Pass:
    app_id: str
    room: str
    number: int
    """The visitor's queue number."""
    ticket_id: str
    """The queue ticket's ID: record it to allow one checkout per number."""
    issued_at: int
    expires_at: int
    user_id: Optional[str] = None
    """The signed user ID, in rooms with one place per signed-in customer."""


def _b64decode(value: str) -> Optional[bytes]:
    if not value or not all(c.isalnum() or c in "-_" for c in value):
        return None
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        return None


def _secrets(secret: Union[str, Sequence[str]]) -> list[str]:
    return [secret] if isinstance(secret, str) else [s for s in secret if s]


def verify_pass(
    token: Optional[str],
    secret: Union[str, Sequence[str]],
    room: Optional[str] = None,
    app_id: Optional[str] = None,
    now: Optional[float] = None,
) -> Optional[Pass]:
    """Check a pass. Returns it, or None when it is missing, forged, expired,
    or for another room or app. Give ``[new, old]`` as secret during a rotation."""
    secrets = _secrets(secret)
    if not secrets:
        raise ValueError("verify_pass: secret is required")
    if not isinstance(token, str) or not token or len(token) > 1024:
        return None
    payload, dot, sig_part = token.partition(".")
    if not dot or not payload:
        return None
    sig = _b64decode(sig_part)
    if sig is None or len(sig) != 32:
        return None
    msg = _PURPOSE + payload.encode()
    if not any(hmac.compare_digest(hmac.new(s.encode(), msg, hashlib.sha256).digest(), sig) for s in secrets):
        return None
    raw = _b64decode(payload)
    try:
        claims = json.loads(raw) if raw is not None else None
    except ValueError:
        return None
    if not isinstance(claims, dict) or claims.get("v") != 1 or not isinstance(claims.get("exp"), (int, float)):
        return None
    if (time.time() if now is None else now) >= claims["exp"]:
        return None
    if room is not None and claims.get("room") != room:
        return None
    if app_id is not None and claims.get("app") != app_id:
        return None
    sub = claims.get("sub")
    return Pass(
        app_id=str(claims.get("app", "")),
        room=str(claims.get("room", "")),
        number=int(claims.get("n", 0)),
        ticket_id=str(claims.get("tid", "")),
        issued_at=int(claims.get("iat", 0)),
        expires_at=int(claims["exp"]),
        user_id=sub if isinstance(sub, str) else None,
    )


def sign_user(secret: str, room: str, user_id: str, ttl_seconds: int = 3600, now: Optional[float] = None) -> str:
    """Sign a user token for rooms with one place per signed-in customer: the
    same user_id gets the same place in every browser. user_id is 1 to 128
    characters without "."; ttl_seconds is capped at 7 days."""
    uid = str(user_id or "")
    if not uid or len(uid) > 128 or "." in uid:
        raise ValueError('sign_user: user_id must be 1 to 128 characters without "."')
    exp = int(time.time() if now is None else now) + min(max(1, int(ttl_seconds)), 7 * 24 * 3600)
    msg = f"hibira-waitroom-user.{room}.{uid}.{exp}".encode()
    return f"{uid}.{exp}.{hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()}"


def waiting_room_url(
    app_key: str,
    room: str,
    return_url: Optional[str] = None,
    host: str = DEFAULT_HOST,
    lang: Optional[str] = None,
    user: Optional[str] = None,
) -> str:
    """The hosted waiting page address for a room."""
    query = {k: v for k, v in (("return", return_url), ("lang", lang), ("user", user)) if v}
    url = f"{host.rstrip('/')}/wait/{quote(app_key, safe='')}/{quote(room, safe='')}"
    return url + ("?" + urlencode(query) if query else "")


@dataclass(frozen=True)
class Decision:
    action: Literal["allow", "redirect", "deny"]
    location: Optional[str] = None
    set_cookie: Optional[str] = None
    """A Set-Cookie header value to send with the redirect."""
    pass_: Optional[Pass] = None


def _read_cookie(header: Optional[str], name: str) -> Optional[str]:
    if not header:
        return None
    for part in header.split(";"):
        key, eq, value = part.partition("=")
        if eq and key.strip() == name:
            return value.strip()
    return None


UserToken = Callable[[Any], Optional[str]]


@dataclass
class WaitingRoom:
    """One waiting room's settings, and the framework-neutral decision."""

    app_key: str
    secret: Union[str, Sequence[str]]
    room: str
    app_id: Optional[str] = None
    host: str = DEFAULT_HOST
    lang: Optional[str] = None
    cookie_name: str = ""
    user: Optional[UserToken] = field(default=None, repr=False)
    """Optional: returns a sign_user() token for the request's signed-in customer."""

    def __post_init__(self) -> None:
        if not self.cookie_name:
            self.cookie_name = f"hibira_wr_{self.room}"

    def verify(self, token: Optional[str], now: Optional[float] = None) -> Optional[Pass]:
        return verify_pass(token, self.secret, room=self.room, app_id=self.app_id, now=now)

    def decide(
        self,
        url: str,
        method: str = "GET",
        cookie: Optional[str] = None,
        request: Any = None,
        now: Optional[float] = None,
    ) -> Decision:
        """What to do with a request: allow it, redirect it, or deny it."""
        parts = urlsplit(url)
        query = parse_qsl(parts.query, keep_blank_values=True)
        from_queue = next((v for k, v in query if k == PARAM), None)
        if from_queue:
            rest = [(k, v) for k, v in query if k != PARAM]
            clean = urlunsplit(parts._replace(query=urlencode(rest)))
            p = self.verify(from_queue, now)
            if p is not None:
                max_age = max(0, int(p.expires_at - (time.time() if now is None else now)))
                secure = "; Secure" if parts.scheme == "https" else ""
                cookie_value = f"{self.cookie_name}={from_queue}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax"
                return Decision(
                    "redirect",
                    location=clean,
                    set_cookie=cookie_value + secure,
                    pass_=p,
                )
            url = clean
        p = self.verify(_read_cookie(cookie, self.cookie_name), now)
        if p is not None:
            return Decision("allow", pass_=p)
        if method.upper() not in ("GET", "HEAD"):
            return Decision("deny")
        user = self.user(request) if self.user is not None and request is not None else None
        location = waiting_room_url(self.app_key, self.room, return_url=url, host=self.host, lang=self.lang, user=user)
        return Decision("redirect", location=location)


def _matches(path: str, paths: Optional[Iterable[str]]) -> bool:
    if paths is None:
        return True
    for prefix in paths:
        p = prefix.rstrip("/")
        if path == p or path.startswith(p + "/") or (prefix.endswith("/") and path.startswith(prefix)):
            return True
    return False


Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class ASGIMiddleware:
    """ASGI middleware for FastAPI, Starlette, Quart, Litestar and others.

    ``paths`` are path prefixes the room protects (all paths when None). The
    pass is put in ``scope["state"]["hibira_pass"]``, which Starlette and
    FastAPI show as ``request.state.hibira_pass``. ``room.user`` receives the
    ASGI scope. Behind a proxy that ends TLS, run the server with
    ``--proxy-headers`` so the scheme is https.
    """

    def __init__(self, app: ASGIApp, room: WaitingRoom, paths: Optional[Iterable[str]] = None) -> None:
        self.app = app
        self.room = room
        self.paths = list(paths) if paths is not None else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _matches(scope.get("path", ""), self.paths):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        host = headers.get("host") or "{}:{}".format(*(scope.get("server") or ("localhost", 80)))
        path = scope.get("root_path", "") + scope.get("path", "")
        qs = scope.get("query_string", b"").decode("latin-1")
        url = f"{scope.get('scheme', 'http')}://{host}{quote(path)}" + (f"?{qs}" if qs else "")
        d = self.room.decide(url, scope.get("method", "GET"), headers.get("cookie"), request=scope)
        if d.action == "allow":
            scope.setdefault("state", {})["hibira_pass"] = d.pass_
            await self.app(scope, receive, send)
            return
        if d.action == "deny":
            out = [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]
            await send({"type": "http.response.start", "status": 403, "headers": out})
            await send({"type": "http.response.body", "body": _DENY_BODY})
            return
        out = [(b"location", (d.location or "/").encode("latin-1")), (b"cache-control", b"no-store")]
        if d.set_cookie:
            out.append((b"set-cookie", d.set_cookie.encode("latin-1")))
        await send({"type": "http.response.start", "status": 302, "headers": out})
        await send({"type": "http.response.body", "body": b""})


Environ = dict[str, Any]
WSGIApp = Callable[[Environ, Callable[..., Any]], Iterable[bytes]]


class WSGIMiddleware:
    """WSGI middleware for Flask, Bottle, Pyramid, Falcon and others.

    ``paths`` are path prefixes the room protects (all paths when None). The
    pass is put in ``environ["hibira.pass"]``. ``room.user`` receives the
    WSGI environ. Behind a proxy that ends TLS, use werkzeug's ProxyFix (or
    your server's equivalent) so ``wsgi.url_scheme`` is https.
    """

    def __init__(self, app: WSGIApp, room: WaitingRoom, paths: Optional[Iterable[str]] = None) -> None:
        self.app = app
        self.room = room
        self.paths = list(paths) if paths is not None else None

    def __call__(self, environ: Environ, start_response: Callable[..., Any]) -> Iterable[bytes]:
        path = environ.get("PATH_INFO", "")
        if not _matches(path, self.paths):
            return self.app(environ, start_response)
        server = f"{environ.get('SERVER_NAME', 'localhost')}:{environ.get('SERVER_PORT', '80')}"
        host = environ.get("HTTP_HOST") or server
        full = quote(environ.get("SCRIPT_NAME", "") + path)
        qs = environ.get("QUERY_STRING", "")
        url = f"{environ.get('wsgi.url_scheme', 'http')}://{host}{full}" + (f"?{qs}" if qs else "")
        d = self.room.decide(url, environ.get("REQUEST_METHOD", "GET"), environ.get("HTTP_COOKIE"), request=environ)
        if d.action == "allow":
            environ["hibira.pass"] = d.pass_
            return self.app(environ, start_response)
        if d.action == "deny":
            length = str(len(_DENY_BODY))
            start_response(
                "403 Forbidden",
                [("Content-Type", "application/json"), ("Cache-Control", "no-store"), ("Content-Length", length)],
            )
            return [_DENY_BODY]
        headers = [("Location", d.location or "/"), ("Cache-Control", "no-store"), ("Content-Length", "0")]
        if d.set_cookie:
            headers.append(("Set-Cookie", d.set_cookie))
        start_response("302 Found", headers)
        return [b""]
