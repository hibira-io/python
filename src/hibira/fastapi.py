"""FastAPI (and Starlette) helpers: the channel and user auth endpoints
pusher-js calls, and a dependency that verifies webhooks.

    from hibira import AsyncClient
    from hibira.fastapi import auth_router, webhook

    hibira = AsyncClient.from_env()

    async def authorize(request, channel):
        user = await current_user(request)
        if channel == f"private-user-{user.id}":
            return True
        if channel.startswith("presence-room-"):
            return {"user_id": user.id, "user_info": {"name": user.name}}
        return False

    app.include_router(auth_router(hibira, authorize))

    @app.post("/hibira/webhook")
    async def on_webhook(payload: dict = Depends(webhook(hibira))):
        ...
"""

from __future__ import annotations

import inspect
import json
from typing import Any, Awaitable, Callable, Mapping, Optional, Union
from urllib.parse import parse_qs

try:
    from fastapi import APIRouter, HTTPException, Request
    from fastapi.responses import JSONResponse
except ImportError as err:  # pragma: no cover
    raise ImportError('hibira.fastapi needs FastAPI: pip install "hibira[fastapi]"') from err

from ._client import _Base
from ._errors import InvalidWebhook

Authorize = Callable[[Request, str], Union[Any, Awaitable[Any]]]
Identify = Callable[[Request], Union[Optional[Mapping[str, Any]], Awaitable[Optional[Mapping[str, Any]]]]]


async def _maybe(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _form(request: Request) -> dict[str, str]:
    raw = await request.body()
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {}
        return {k: str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    return {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}


def auth_router(
    client: _Base,
    authorize: Authorize,
    *,
    path: str = "/hibira/auth",
    user: Optional[Identify] = None,
    user_path: str = "/hibira/user-auth",
) -> APIRouter:
    """Router with the endpoint pusher-js calls to authorize private- and presence- channels.

    authorize(request, channel) (sync or async) returns False/None to refuse,
    True to allow a private channel, or the member's user_data
    ({"user_id", "user_info"?}) for a presence channel. Point the client's
    channelAuthorization.endpoint at path.

    With user (request -> {"id", ...} or None), the router also answers user
    sign-in at user_path (userAuthentication.endpoint).
    """
    router = APIRouter()

    @router.post(path, include_in_schema=False)
    async def channel_auth(request: Request) -> JSONResponse:
        form = await _form(request)
        socket_id, channel = form.get("socket_id", ""), form.get("channel_name", "")
        if not socket_id or not channel:
            raise HTTPException(400, "socket_id and channel_name are required")
        verdict = await _maybe(authorize(request, channel))
        if not verdict:
            raise HTTPException(403, "forbidden")
        user_data = verdict if isinstance(verdict, Mapping) else None
        if channel.startswith("presence-") and user_data is None:
            raise HTTPException(500, "authorize must return user_data with a user_id for presence channels")
        try:
            return JSONResponse(client.authenticate_channel(channel, socket_id, user_data))
        except ValueError as err:
            raise HTTPException(400, str(err)) from err

    if user is not None:

        @router.post(user_path, include_in_schema=False)
        async def user_auth(request: Request) -> JSONResponse:
            form = await _form(request)
            socket_id = form.get("socket_id", "")
            if not socket_id:
                raise HTTPException(400, "socket_id is required")
            data = await _maybe(user(request))
            if not data:
                raise HTTPException(403, "forbidden")
            try:
                return JSONResponse(client.authenticate_user(socket_id, data))
            except ValueError as err:
                raise HTTPException(400, str(err)) from err

    return router


def webhook(client: _Base) -> Callable[[Request], Awaitable[Any]]:
    """Dependency that verifies a Hibira webhook and returns its parsed body
    ({"time_ms", "events": [...]}); a bad signature answers 401."""

    async def verified(request: Request) -> Any:
        body = await request.body()
        try:
            return client.verify_webhook(
                request.headers.get("x-pusher-key"), request.headers.get("x-pusher-signature"), body
            )
        except InvalidWebhook as err:
            raise HTTPException(401, str(err)) from err

    return verified
