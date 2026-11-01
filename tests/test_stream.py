import asyncio
import json
import threading
import time
from types import SimpleNamespace as NS

import pytest

import hibira
from hibira.stream import split, text_of


class Recorder:
    def __init__(self, fail_at=None, delay=0.0):
        self.events = []
        self.fail_at = fail_at
        self.delay = delay
        self.lock = threading.Lock()

    def trigger(self, channel, event, data, socket_id=None):
        if self.delay:
            time.sleep(self.delay)
        with self.lock:
            if self.fail_at is not None and len(self.events) == self.fail_at:
                raise hibira.PayloadTooLarge(413, "too large")
            self.events.append((channel, event, data, socket_id))


class AsyncRecorder(Recorder):
    async def trigger(self, channel, event, data, socket_id=None):
        await asyncio.sleep(self.delay)
        if self.fail_at is not None and len(self.events) == self.fail_at:
            raise hibira.PayloadTooLarge(413, "too large")
        self.events.append((channel, event, data, socket_id))


def rebuild(events):
    payloads = [e[2] for e in events]
    assert [p["seq"] for p in payloads] == list(range(len(payloads)))
    return "".join(p.get("delta", "") for p in payloads if not p.get("done")), payloads[-1]


def test_text_of_shapes():
    assert text_of("hi") == "hi"
    assert text_of(b"hi") == "hi"
    assert text_of({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "a"}}) == "a"
    assert text_of({"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "{"}}) == ""
    assert text_of(NS(type="content_block_delta", delta=NS(type="text_delta", text="b"))) == "b"
    assert text_of({"type": "response.output_text.delta", "delta": "c"}) == "c"
    assert text_of(NS(type="raw_response_event", data=NS(type="response.output_text.delta", delta="d"))) == "d"
    assert text_of(NS(type="run_item_stream_event", item=None)) == ""
    assert text_of({"choices": [{"delta": {"content": "e"}}]}) == "e"
    assert text_of(NS(choices=[NS(delta=NS(content=None))])) == ""
    assert text_of(NS(choices=[])) == ""
    ai = NS(type="AIMessageChunk", content=[{"type": "text", "text": "f"}, {"type": "tool_use", "id": "x"}])
    assert text_of((ai, {"langgraph_node": "agent"})) == "f"
    assert text_of(("messages", (NS(type="AIMessageChunk", content="g"), {}))) == "g"
    assert text_of(NS(type="tool", content="secret tool output")) == ""
    assert text_of(NS(text="h")) == "h"
    assert text_of({"type": "text-delta", "textDelta": "i"}) == "i"
    assert text_of(None) == "" and text_of(42) == ""


def test_split_respects_budget_and_characters():
    text = 'héllo "wörld"\n😀' * 50
    for budget in (16, 20, 33):
        pieces = split(text, budget)
        assert "".join(pieces) == text
        for p in pieces:
            assert len(json.dumps(p, ensure_ascii=False).encode()) - 2 <= budget


def test_stream_batches_and_finishes():
    rec = Recorder()

    def tokens():
        for word in "the quick brown fox".split():
            yield word + " "

    out = hibira.stream_to_channel(rec, "private-chat", tokens(), id="s1", meta={"role": "assistant"}, socket_id="1.2")
    assert out == hibira.StreamResult("s1", "the quick brown fox ", len(rec.events))
    text, last = rebuild(rec.events)
    assert text == "the quick brown fox "
    assert last == {"id": "s1", "seq": len(rec.events) - 1, "done": True, "text": text}
    first = rec.events[0]
    assert first[0] == "private-chat" and first[1] == "ai-stream" and first[3] == "1.2"
    assert first[2]["meta"] == {"role": "assistant"}
    assert len(rec.events) == 2  # fast tokens are batched into one event
    assert all("meta" not in e[2] for e in rec.events[1:])


def test_stream_interval_flushes_slow_tokens():
    rec = Recorder()

    def tokens():
        yield "a"
        time.sleep(0.15)
        assert [e[2]["delta"] for e in rec.events] == ["a"]
        yield "b"

    hibira.stream_to_channel(rec, "c", tokens(), interval=0.05)
    assert rebuild(rec.events)[0] == "ab"


def test_stream_splits_large_text_under_max_bytes():
    rec = Recorder()
    big = "字" * 5000
    out = hibira.stream_to_channel(rec, "c", [big], max_bytes=1000, meta={"m": 1})
    text, last = rebuild(rec.events)
    assert text == big == out.text
    for e in rec.events:
        assert len(json.dumps(e[2], separators=(",", ":"), ensure_ascii=False).encode()) <= 1000
    assert "text" not in last


def test_empty_stream_sends_done_with_meta():
    rec = Recorder()
    hibira.stream_to_channel(rec, "c", [], id="x", meta=None)
    assert [e[2] for e in rec.events] == [{"id": "x", "seq": 0, "done": True, "meta": None, "text": ""}]


def test_source_error_is_sent_and_raised():
    rec = Recorder()

    def tokens():
        yield "partial"
        raise RuntimeError("model overloaded")

    with pytest.raises(RuntimeError):
        hibira.stream_to_channel(rec, "c", tokens())
    text, last = rebuild(rec.events)
    assert text == "partial" and last["error"] == "model overloaded" and last["done"]


def test_stop_event_aborts():
    rec = Recorder()
    stop = threading.Event()

    def tokens():
        yield "a"
        stop.set()
        yield "b"

    out = hibira.stream_to_channel(rec, "c", tokens(), stop=stop)
    assert out.text == "a"
    assert rec.events[-1][2]["error"] == "aborted"


def test_send_error_stops_and_raises():
    rec = Recorder(fail_at=0)
    with pytest.raises(hibira.PayloadTooLarge):
        hibira.stream_to_channel(rec, "c", iter(["a"] * 3), interval=10)
    assert rec.events == []


def test_utf8_bytes_split_across_chunks():
    rec = Recorder()
    data = "日本語".encode()
    out = hibira.stream_to_channel(rec, "c", [data[:1], data[1:4], data[4:]])
    assert out.text == "日本語"


def test_max_bytes_too_small():
    with pytest.raises(ValueError):
        hibira.stream_to_channel(Recorder(), "c", ["a"], max_bytes=40, meta={"x": "y" * 20})


def test_async_stream_in_order():
    rec = AsyncRecorder(delay=0.01)

    async def tokens():
        for i in range(30):
            yield f"{i},"
            await asyncio.sleep(0.004)

    out = asyncio.run(hibira.astream_to_channel(rec, "c", tokens(), interval=0.02, meta={"a": 1}))
    text, last = rebuild(rec.events)
    assert text == out.text == "".join(f"{i}," for i in range(30))
    assert last["done"] and last["text"] == text
    assert 2 < len(rec.events) < 30


def test_async_stream_with_sync_client_and_plain_iterable():
    rec = Recorder()
    out = asyncio.run(hibira.astream_to_channel(rec, "c", ["x", "y"]))
    assert out.text == "xy" and rebuild(rec.events)[0] == "xy"


def test_async_cancel_sends_aborted():
    rec = AsyncRecorder()

    async def tokens():
        yield "a"
        await asyncio.sleep(10)
        yield "b"

    async def main():
        task = asyncio.create_task(hibira.astream_to_channel(rec, "c", tokens(), interval=0.01))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    text, last = rebuild(rec.events)
    assert text == "a" and last["error"] == "aborted"


def test_async_source_error():
    rec = AsyncRecorder()

    async def tokens():
        yield "a"
        raise ValueError("boom")

    with pytest.raises(ValueError):
        asyncio.run(hibira.astream_to_channel(rec, "c", tokens()))
    assert rec.events[-1][2]["error"] == "boom"
