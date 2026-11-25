import asyncio
import hashlib
import hmac
import json

import httpx
import pytest
from test_client import check_signature, make, recorder

import hibira
from hibira.livechat import user_hash


def test_put_conversation_and_send_message():
    seen, handler = recorder(201, b'{"conversation": {"id": "support-42"}}')
    c = make(handler)
    c.put_conversation("support-42", name="Order #42", members=["u1", "agent"], roles={"agent": "staff"})
    c.send_message("support-42", "u1", "Hi", push="always", visible_to=["staff"])
    put, post = seen
    assert put.method == "PUT" and put.url.path == "/apps/app1/chat/conversations/support-42"
    assert json.loads(put.content) == {"name": "Order #42", "members": ["u1", "agent"], "roles": {"agent": "staff"}}
    assert post.url.path == "/apps/app1/chat/conversations/support-42/messages"
    assert json.loads(post.content) == {"user_id": "u1", "text": "Hi", "push": "always", "visible_to": ["staff"]}
    check_signature(put)
    check_signature(post)


def test_reads_and_members():
    seen, handler = recorder()
    c = make(handler)
    c.list_messages("a b", after=10, limit=50)
    c.user_conversations("u/1", limit=20)
    c.add_members("c1", "u2")
    c.remove_member("c1", "u2")
    c.mark_read("c1", "u1", 12)
    c.system_message("c1", "Alice joined", {"event": "joined"})
    c.delete_conversation("c1")
    c.chat_request("POST", "/conversations/c1/typing", body={"user_id": "u1"})
    paths = [(r.method, r.url.raw_path.decode().split("?")[0]) for r in seen]
    assert paths == [
        ("GET", "/apps/app1/chat/conversations/a%20b/messages"),
        ("GET", "/apps/app1/chat/users/u%2F1/conversations"),
        ("POST", "/apps/app1/chat/conversations/c1/members"),
        ("DELETE", "/apps/app1/chat/conversations/c1/members/u2"),
        ("POST", "/apps/app1/chat/conversations/c1/read"),
        ("POST", "/apps/app1/chat/conversations/c1/system"),
        ("DELETE", "/apps/app1/chat/conversations/c1"),
        ("POST", "/apps/app1/chat/conversations/c1/typing"),
    ]
    q = check_signature(seen[0])
    assert q["after"] == "10" and q["limit"] == "50" and "last" not in q
    for r in seen:
        check_signature(r)
    assert json.loads(seen[2].content) == {"user_ids": ["u2"]}
    with pytest.raises(ValueError):
        c.send_message("c1", "", "x")
    with pytest.raises(ValueError):
        c.get_conversation("")


def test_async_chat():
    seen, handler = recorder()

    async def run():
        c = hibira.AsyncClient(
            "app1", "key1", "secret1", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        await c.send_message("c1", "u1", "hi")
        await c.get_conversation("c1")

    asyncio.run(run())
    assert [r.method for r in seen] == ["POST", "GET"]
    check_signature(seen[0])


def test_user_hash():
    want = hmac.new(b"lcid_secret", b"42", hashlib.sha256).hexdigest()
    assert user_hash("lcid_secret", 42) == want
    with pytest.raises(ValueError):
        user_hash("", "1")
