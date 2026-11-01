"""End-to-end tests against a running Hibira (pusher-server) node.

    HIBIRA_E2E=1 HIBIRA_HOST=127.0.0.1 HIBIRA_PORT=8080 HIBIRA_USE_TLS=false \
    HIBIRA_APP_ID=... HIBIRA_APP_KEY=... HIBIRA_APP_SECRET=... pytest tests/test_e2e.py

The app needs channel history on (history: true) for the rewind test.
"""

import asyncio
import json
import os
import uuid

import pytest

import hibira

pytestmark = pytest.mark.skipif(os.environ.get("HIBIRA_E2E") != "1", reason="set HIBIRA_E2E=1 to run against a server")

websockets = pytest.importorskip("websockets")


class Socket:
    def __init__(self, client):
        self.client = client
        self.events = []

    async def __aenter__(self):
        c = self.client
        scheme = "wss" if c.use_tls else "ws"
        port = f":{c.port}" if c.port else ""
        self.ws = await websockets.connect(f"{scheme}://{c.host}{port}/app/{c.key}?protocol=7&client=python-e2e")
        msg = json.loads(await self.ws.recv())
        assert msg["event"] == "pusher:connection_established"
        self.socket_id = json.loads(msg["data"])["socket_id"]
        self.reader = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *exc):
        self.reader.cancel()
        await self.ws.close()

    async def _read(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            data = msg.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except ValueError:
                    pass
            self.events.append((msg.get("channel"), msg["event"], data))

    async def send(self, event, data):
        await self.ws.send(json.dumps({"event": event, "data": data}))

    async def subscribe(self, channel, user_data=None):
        data = {"channel": channel}
        if channel.startswith(("private-", "presence-")):
            data.update(self.client.authenticate_channel(channel, self.socket_id, user_data))
        await self.send("pusher:subscribe", data)
        await self.wait(lambda e: e[0] == channel and e[1].endswith("subscription_succeeded"))

    async def wait(self, pred, timeout=5.0):
        for _ in range(int(timeout / 0.02)):
            for e in self.events:
                if pred(e):
                    return e
            await asyncio.sleep(0.02)
        raise AssertionError(f"timed out; got {self.events}")


def rebuild(events, stream_id):
    parts = sorted(
        (e[2] for e in events if e[1] == "ai-stream" and e[2].get("id") == stream_id), key=lambda p: p["seq"]
    )
    assert [p["seq"] for p in parts] == list(range(len(parts)))
    assert parts[-1]["done"]
    return "".join(p.get("delta", "") for p in parts), parts


@pytest.fixture
def channel():
    return f"private-py-{uuid.uuid4().hex[:8]}"


def test_trigger_and_query(channel):
    async def main():
        async with hibira.AsyncClient.from_env() as c, Socket(c) as ws:
            await ws.subscribe(channel)
            await c.trigger([channel, "public-other"], "hello", {"n": 1})
            assert (await ws.wait(lambda e: e[1] == "hello"))[2] == {"n": 1}
            await c.trigger(channel, "skip-me", "x", socket_id=ws.socket_id)
            await c.trigger_batch([{"channel": channel, "name": "batched", "data": {"n": 2}}])
            await ws.wait(lambda e: e[1] == "batched")
            assert not any(e[1] == "skip-me" for e in ws.events)
            info = await c.channel_info(channel, info="subscription_count")
            assert info["occupied"] is True
            assert channel in (await c.channels(prefix="private-py-"))["channels"]

    asyncio.run(main())


def test_presence_users():
    room = f"presence-py-{uuid.uuid4().hex[:8]}"

    async def main():
        async with hibira.AsyncClient.from_env() as c, Socket(c) as ws:
            await ws.subscribe(room, {"user_id": "ann", "user_info": {"name": "Ann"}})
            assert (await c.users(room))["users"] == [{"id": "ann"}]

    asyncio.run(main())


def test_errors():
    c = hibira.Client.from_env()
    with pytest.raises(hibira.HTTPError) as info:
        c.trigger("public-x", "big", "x" * 200_000)
    assert info.value.status in (400, 413)
    bad = hibira.Client.from_env(secret="wrong-secret-0123456789")
    with pytest.raises(hibira.AuthenticationError):
        bad.trigger("public-x", "e", 1)


def test_stream_sync_and_rewind(channel):
    async def main():
        c = hibira.Client.from_env()
        async with Socket(c) as ws:
            await ws.subscribe(channel)
            words = [f"word{i} " for i in range(40)] + ["日本語 😀"]
            out = await asyncio.to_thread(
                hibira.stream_to_channel, c, channel, iter(words), meta={"role": "assistant"}, interval=0.01
            )
            await ws.wait(lambda e: e[1] == "ai-stream" and e[2].get("done"))
            text, parts = rebuild(ws.events, out.id)
            assert text == "".join(words) == out.text
            assert parts[0]["meta"] == {"role": "assistant"} and parts[-1]["text"] == text

        async with Socket(c) as late:
            await late.subscribe(channel)
            await late.send("pusher:rewind", {"channel": channel, "seconds": 60})
            ok = await late.wait(lambda e: e[1] in ("pusher:rewind_succeeded", "pusher:rewind_error"))
            if ok[1] == "pusher:rewind_error":
                pytest.skip(f"channel history is off for this app: {ok[2]}")
            text, _ = rebuild(late.events, out.id)
            assert text == out.text

    asyncio.run(main())


def test_stream_async(channel):
    async def tokens():
        for i in range(20):
            yield {"choices": [{"delta": {"content": f"{i} "}}]}
            await asyncio.sleep(0.005)

    async def main():
        async with hibira.AsyncClient.from_env() as c, Socket(c) as ws:
            await ws.subscribe(channel)
            out = await hibira.astream_to_channel(c, channel, tokens(), interval=0.02, max_bytes=200)
            await ws.wait(lambda e: e[1] == "ai-stream" and e[2].get("done"))
            text, parts = rebuild(ws.events, out.id)
            assert text == out.text == "".join(f"{i} " for i in range(20))
            assert all(len(json.dumps(p, separators=(",", ":")).encode()) <= 200 for p in parts)

    asyncio.run(main())
