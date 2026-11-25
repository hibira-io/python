import hashlib
import hmac

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from hibira.waiting_room import (
    ASGIMiddleware,
    Pass,
    WaitingRoom,
    WSGIMiddleware,
    sign_user,
    verify_pass,
    waiting_room_url,
)

# The shared test vector of hibira-io/waiting-room test/server.test.mjs:
# signed by the Hibira server (internal/waitroom NewPass) at 1800000000, ttl 30 min.
SECRET = "wr-secret-0123456789"
PASS = (
    "eyJ2IjoxLCJhcHAiOiJ3ci1hcHAiLCJyb29tIjoicHJlc2FsZSIsIm4iOjQyLCJ0aWQiOiJkNzlmZTc4OWEzOGI4NjMxYjUiLCJpYXQiOjE4MDAw"
    "MDAwMDAsImV4cCI6MTgwMDAwMTgwMH0.q7Uc_O6XKwNA_LVLQITlgv_YtDi9dIwiRzUUUSsGa08"
)
TICKET = (
    "eyJ2IjoxLCJhcHAiOiJ3ci1hcHAiLCJyb29tIjoicHJlc2FsZSIsIm4iOjQyLCJ0aWQiOiJkNzlmZTc4OWEzOGI4NjMxYjUiLCJpYXQiOjE4MDAw"
    "MDAwMDAsImV4cCI6MTgwMDA4NjQwMH0.l-KNMOVwV_xJJCQaQ-JXQKsrj-XlEdsTxG7X4Kp_2ck"
)
NOW = 1800000100


def test_verifies_a_server_signed_pass():
    p = verify_pass(PASS, SECRET, room="presale", app_id="wr-app", now=NOW)
    assert p == Pass("wr-app", "presale", 42, "d79fe789a38b8631b5", 1800000000, 1800001800)
    assert verify_pass(PASS, ["old-secret", SECRET], now=NOW), "rotation: any secret"


@pytest.mark.parametrize(
    "token,kw",
    [
        (PASS, {"secret": "wrong"}),
        (PASS, {"room": "other"}),
        (PASS, {"app_id": "other"}),
        (PASS, {"now": 1800001800}),
        (TICKET, {}),
        (PASS[:-3] + "AAA", {}),
        ("", {}),
        (None, {}),
        ("a.b.c", {}),
        ("x" * 1025, {}),
    ],
)
def test_rejects(token, kw):
    args = {"secret": SECRET, "room": "presale", "app_id": "wr-app", "now": NOW, **kw}
    assert verify_pass(token, **args) is None


def test_sign_user_matches_the_server_format():
    tok = sign_user(SECRET, "presale", "cust_42", ttl_seconds=600, now=1800000000)
    want = hmac.new(SECRET.encode(), b"hibira-waitroom-user.presale.cust_42.1800000600", hashlib.sha256).hexdigest()
    assert tok == "cust_42.1800000600." + want
    assert sign_user(SECRET, "presale", "u", ttl_seconds=30 * 86400, now=1800000000).startswith("u.1800604800.")
    with pytest.raises(ValueError):
        sign_user(SECRET, "presale", "a.b")


def test_waiting_room_url():
    url = waiting_room_url("key", "presale", return_url="https://shop.test/drop?a=1", lang="ja")
    assert url == "https://ws.hibira.io/wait/key/presale?return=https%3A%2F%2Fshop.test%2Fdrop%3Fa%3D1&lang=ja"


ROOM = WaitingRoom(app_key="key", secret=SECRET, room="presale")


def test_decide_without_a_pass():
    d = ROOM.decide("https://shop.test/drop", now=NOW)
    assert d.action == "redirect"
    assert d.location == "https://ws.hibira.io/wait/key/presale?return=https%3A%2F%2Fshop.test%2Fdrop"
    assert ROOM.decide("https://shop.test/drop", "POST", now=NOW).action == "deny"


def test_decide_from_the_queue_and_with_the_cookie():
    d = ROOM.decide(f"https://shop.test/drop?item=7&hibira_pass={PASS}", now=NOW)
    assert d.action == "redirect" and d.location == "https://shop.test/drop?item=7"
    assert d.set_cookie == f"hibira_wr_presale={PASS}; Path=/; Max-Age=1700; HttpOnly; SameSite=Lax; Secure"
    d = ROOM.decide("http://shop.test/drop", "POST", cookie=f"a=1; hibira_wr_presale={PASS}", now=NOW)
    assert d.action == "allow" and d.pass_ is not None and d.pass_.number == 42
    forged = ROOM.decide("https://shop.test/drop?hibira_pass=forged&x=1", now=NOW)
    assert forged.set_cookie is None
    assert forged.location is not None and "return=https%3A%2F%2Fshop.test%2Fdrop%3Fx%3D1" in forged.location


def fresh(room="presale"):
    import base64
    import json
    import time

    now = int(time.time())
    payload = base64.urlsafe_b64encode(
        json.dumps({"v": 1, "app": "wr-app", "room": room, "n": 7, "tid": "t7", "iat": now, "exp": now + 600}).encode()
    ).rstrip(b"=")
    sig = hmac.new(SECRET.encode(), b"hibira-waitroom-pass." + payload, hashlib.sha256).digest()
    return payload.decode() + "." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()


def asgi_app():
    room = WaitingRoom(app_key="key", secret=SECRET, room="presale", user=lambda scope: "u.1.sig")
    app = FastAPI()
    app.add_middleware(ASGIMiddleware, room=room, paths=["/drop"])

    @app.api_route("/drop", methods=["GET", "POST"])
    async def drop(request: Request):
        return {"number": request.state.hibira_pass.number}

    @app.get("/open")
    async def open_page():
        return {"ok": True}

    return TestClient(app, base_url="https://shop.test", follow_redirects=False)


def test_asgi_middleware():
    c = asgi_app()
    assert c.get("/open").json() == {"ok": True}
    r = c.get("/drop")
    assert r.status_code == 302 and r.headers["cache-control"] == "no-store"
    assert r.headers["location"].startswith(
        "https://ws.hibira.io/wait/key/presale?return=https%3A%2F%2Fshop.test%2Fdrop"
    )
    assert "user=u.1.sig" in r.headers["location"]
    assert c.post("/drop").status_code == 403
    token = fresh()
    r = c.get(f"/drop?hibira_pass={token}&x=1")
    assert r.status_code == 302 and r.headers["location"] == "https://shop.test/drop?x=1"
    assert r.headers["set-cookie"].startswith(f"hibira_wr_presale={token}; Path=/;")
    c.cookies.set("hibira_wr_presale", token)
    assert c.post("/drop").json() == {"number": 7}


def wsgi_call(mw, path="/drop", method="GET", qs="", cookie=None):
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": qs,
        "HTTP_HOST": "shop.test",
        "wsgi.url_scheme": "https",
        "SCRIPT_NAME": "",
    }
    if cookie:
        environ["HTTP_COOKIE"] = cookie
    out = {}

    def start_response(status, headers):
        out["status"], out["headers"] = status, dict(headers)

    body = b"".join(mw(environ, start_response))
    return out["status"], out["headers"], body, environ


def test_wsgi_middleware():
    def app(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [str(environ["hibira.pass"].number).encode()]

    mw = WSGIMiddleware(app, ROOM, paths=["/drop/"])
    status, headers, _, _ = wsgi_call(mw)
    assert status == "302 Found" and headers["Location"].startswith("https://ws.hibira.io/wait/key/presale?return=")
    assert wsgi_call(mw, method="DELETE")[0] == "403 Forbidden"
    token = fresh()
    status, headers, _, _ = wsgi_call(mw, qs=f"hibira_pass={token}")
    assert headers["Location"] == "https://shop.test/drop" and "Secure" in headers["Set-Cookie"]
    status, _, body, _ = wsgi_call(mw, method="POST", cookie=f"hibira_wr_presale={token}")
    assert status == "200 OK" and body == b"7"
    assert (
        wsgi_call(WSGIMiddleware(lambda e, s: (s("200 OK", []), [b"open"])[1], ROOM, ["/drop"]), path="/other")[2]
        == b"open"
    )
