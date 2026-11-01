import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

import hibira
from hibira import _signing


def recorder(status=200, body=b"{}"):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, content=body, headers={"content-type": "application/json"})

    return seen, handler


def make(handler, **kw):
    return hibira.Client(
        "app1", "key1", "secret1", http_client=httpx.Client(transport=httpx.MockTransport(handler)), **kw
    )


def check_signature(req: httpx.Request):
    q = {k: v[0] for k, v in parse_qs(urlsplit(str(req.url)).query).items()}
    sig = q.pop("auth_signature")
    signed = _signing.sign_query(
        "key1",
        "secret1",
        req.method,
        req.url.path,
        {k: v for k, v in q.items() if not k.startswith("auth_") and k != "body_md5"},
        req.content or None,
        now=int(q["auth_timestamp"]),
    )
    assert signed["auth_signature"] == sig
    return q


def test_trigger():
    seen, handler = recorder()
    make(handler).trigger(["a", "b"], "created", {"id": 1}, socket_id="1.2")
    req = seen[0]
    assert req.method == "POST"
    assert str(req.url).startswith("https://ws.hibira.io/apps/app1/events?")
    assert json.loads(req.content) == {
        "name": "created",
        "data": '{"id":1}',
        "channels": ["a", "b"],
        "socket_id": "1.2",
    }
    check_signature(req)


def test_trigger_string_data_and_validation():
    seen, handler = recorder()
    c = make(handler)
    c.trigger("a", "e", "raw")
    assert json.loads(seen[0].content)["data"] == "raw"
    with pytest.raises(ValueError):
        c.trigger([], "e", 1)
    with pytest.raises(ValueError):
        c.trigger([f"c{i}" for i in range(101)], "e", 1)
    with pytest.raises(ValueError):
        c.trigger("a", "e", 1, socket_id="bad")


def test_batch_and_queries():
    seen, handler = recorder(body=b'{"channels":{}}')
    c = make(handler, host="localhost", port=8080, use_tls=False)
    c.trigger_batch([{"channel": "a", "name": "e", "data": {"x": 1}}])
    assert json.loads(seen[0].content) == {"batch": [{"channel": "a", "name": "e", "data": '{"x":1}'}]}
    assert c.channels(prefix="presence-", info=["user_count"]) == {"channels": {}}
    q = check_signature(seen[1])
    assert q["filter_by_prefix"] == "presence-" and q["info"] == "user_count"
    assert str(seen[1].url).startswith("http://localhost:8080/apps/app1/channels?")
    c.channel_info("presence-x", info="user_count,subscription_count")
    assert seen[2].url.path == "/apps/app1/channels/presence-x"
    c.users("presence-x")
    assert seen[3].url.path == "/apps/app1/channels/presence-x/users"
    with pytest.raises(ValueError):
        c.users("private-x")
    c.terminate_user_connections("u 1")
    assert seen[4].url.raw_path.startswith(b"/apps/app1/users/u%201/terminate_connections")
    for r in seen:
        check_signature(r)


@pytest.mark.parametrize(
    ("status", "cls"),
    [
        (413, hibira.PayloadTooLarge),
        (401, hibira.AuthenticationError),
        (403, hibira.AuthenticationError),
        (404, hibira.NotFound),
        (429, hibira.RateLimited),
        (500, hibira.HTTPError),
    ],
)
def test_errors(status, cls):
    _, handler = recorder(status, b'{"error":"nope"}')
    with pytest.raises(cls) as info:
        make(handler).trigger("a", "e", 1)
    assert info.value.status == status
    assert info.value.message == "nope"
    assert isinstance(info.value, hibira.HibiraError)


def test_push():
    seen, handler = recorder(body=b'{"publishId":"p1"}')
    c = make(handler, push_instance_id="inst", push_secret_key="sk")
    assert c.push_to_interests("news", {"web": {"notification": {"title": "Hi"}}}) == {"publishId": "p1"}
    assert seen[0].url == "https://ws.hibira.io/publish_api/v1/instances/inst/publishes/interests"
    assert seen[0].headers["authorization"] == "Bearer sk"
    assert json.loads(seen[0].content)["interests"] == ["news"]
    c.push_to_users(["u1", 2], {"fcm": {}})
    assert json.loads(seen[1].content)["users"] == ["u1", "2"]
    c.delete_push_user("u1")
    assert seen[2].method == "DELETE" and seen[2].url.path == "/customer_api/v1/instances/inst/users/u1"
    with pytest.raises(hibira.ConfigError):
        make(handler).push_to_users("u1", {})


def test_approvals():
    answers = iter(
        [
            {"approval_id": "apr_1", "status": "pending", "url": "https://hibira.io/approve/apr_1/t"},
            {"approval_id": "apr_1", "status": "pending"},
            {"approval_id": "apr_1", "status": "approved", "choice": "Refund"},
        ]
    )
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(201 if req.method == "POST" else 200, json=next(answers))

    c = make(handler)
    out = c.request_approval("Refund #1042?", options=["Refund", "Decline"], push_interests=["ops"])
    assert out["url"].startswith("https://hibira.io/approve/")
    assert json.loads(seen[0].content) == {
        "title": "Refund #1042?",
        "options": ["Refund", "Decline"],
        "push_interests": ["ops"],
    }
    assert c.wait_for_approval("apr_1", timeout=60)["choice"] == "Refund"
    assert seen[1].url.path == "/apps/app1/approvals/apr_1"
    assert check_signature(seen[1])["wait"] == "25"
    with pytest.raises(ValueError):
        c.get_approval("apr_1", wait=26)


def test_from_env(monkeypatch):
    monkeypatch.setenv("HIBIRA_APP_ID", "1")
    monkeypatch.setenv("HIBIRA_APP_KEY", "k")
    monkeypatch.setenv("HIBIRA_APP_SECRET", "s")
    monkeypatch.setenv("HIBIRA_HOST", "ws-tyo.hibira.io")
    monkeypatch.setenv("HIBIRA_USE_TLS", "false")
    monkeypatch.setenv("HIBIRA_PORT", "6001")
    c = hibira.Client.from_env(app_id="2")
    assert c.app_id == "2" and c.base_url == "http://ws-tyo.hibira.io:6001"
    assert c.push_endpoint == "http://ws-tyo.hibira.io:6001"
    monkeypatch.delenv("HIBIRA_APP_SECRET")
    with pytest.raises(hibira.ConfigError):
        hibira.Client.from_env()


def test_async_client():
    seen, handler = recorder()

    async def main():
        async with hibira.AsyncClient(
            "app1", "key1", "secret1", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        ) as c:
            await c.trigger("a", "e", {"x": 1})
            assert c.authenticate_channel("private-a", "1.1")["auth"].startswith("key1:")

    asyncio.run(main())
    assert json.loads(seen[0].content)["data"] == '{"x":1}'
    check_signature(seen[0])


def test_network_errors_are_hibira_errors():
    def handler(req):
        raise httpx.ConnectError("connection refused", request=req)

    with pytest.raises(hibira.ConnectionError) as info:
        make(handler).trigger("a", "e", 1)
    assert isinstance(info.value, hibira.HibiraError) and isinstance(info.value, ConnectionError)
    assert isinstance(info.value.__cause__, httpx.ConnectError)

    async def main():
        c = hibira.AsyncClient("1", "k", "s", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with pytest.raises(hibira.ConnectionError):
            await c.trigger("a", "e", 1)

    asyncio.run(main())
