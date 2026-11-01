import hashlib
import hmac

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import hibira  # noqa: E402
from hibira.fastapi import auth_router, webhook  # noqa: E402

client = hibira.Client("1", "key", "secret")


async def authorize(request, channel):
    if request.headers.get("x-user") != "ann":
        return False
    if channel.startswith("presence-"):
        return {"user_id": "ann", "user_info": {"name": "Ann"}}
    return channel == "private-ann"


app = FastAPI()
app.include_router(auth_router(client, authorize, user=lambda r: {"id": "ann"} if r.headers.get("x-user") else None))
received = []


@app.post("/hook")
def hook(payload: dict = Depends(webhook(client))):
    received.append(payload)
    return {"ok": True}


http = TestClient(app)


def test_private_and_presence_auth():
    r = http.post("/hibira/auth", data={"socket_id": "1.2", "channel_name": "private-ann"}, headers={"x-user": "ann"})
    assert r.status_code == 200 and r.json() == client.authenticate_channel("private-ann", "1.2")
    r = http.post("/hibira/auth", json={"socket_id": "1.2", "channel_name": "presence-room"}, headers={"x-user": "ann"})
    assert r.json()["channel_data"] == '{"user_id":"ann","user_info":{"name":"Ann"}}'
    assert (
        http.post(
            "/hibira/auth", data={"socket_id": "1.2", "channel_name": "private-bob"}, headers={"x-user": "ann"}
        ).status_code
        == 403
    )
    assert http.post("/hibira/auth", data={"socket_id": "1.2", "channel_name": "private-ann"}).status_code == 403
    assert (
        http.post(
            "/hibira/auth", data={"socket_id": "x", "channel_name": "private-ann"}, headers={"x-user": "ann"}
        ).status_code
        == 400
    )
    assert http.post("/hibira/auth", data={}).status_code == 400


def test_user_auth():
    r = http.post("/hibira/user-auth", data={"socket_id": "1.2"}, headers={"x-user": "ann"})
    assert r.json() == client.authenticate_user("1.2", {"id": "ann"})
    assert http.post("/hibira/user-auth", data={"socket_id": "1.2"}).status_code == 403


def test_webhook():
    body = b'{"time_ms":1,"events":[]}'
    sig = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    r = http.post("/hook", content=body, headers={"x-pusher-key": "key", "x-pusher-signature": sig})
    assert r.status_code == 200 and received[-1] == {"time_ms": 1, "events": []}
    r = http.post("/hook", content=body, headers={"x-pusher-key": "key", "x-pusher-signature": "0" * 64})
    assert r.status_code == 401
