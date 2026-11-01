"""Server side of the Hibira stream protocol: send an LLM's output (or any
stream of text) to a channel as it is produced. The useStream() hook in
@hibira-io/react and composable in @hibira-io/vue put it back together.

Same wire format as streamToChannel in those packages: event "ai-stream",
payloads {id, seq, delta, meta (first event only)}, then a final
{id, seq, done: true, text?, error?}. Tokens are batched (one event per
interval rather than one per token), each event stays under max_bytes (Free
plans allow 4 KB per message) and events are sent one after another, in order.
"""

from __future__ import annotations

import asyncio
import codecs
import inspect
import json
import threading
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterable, Callable, Iterable, Optional, Union

STREAM_EVENT = "ai-stream"

_MISSING = object()


@dataclass(frozen=True)
class StreamResult:
    id: str
    text: str
    events: int


def _get(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, str):
                out.append(block)
            elif _get(block, "type") in ("text", "text_delta", "output_text") and isinstance(_get(block, "text"), str):
                out.append(_get(block, "text"))
        return "".join(out)
    return ""


def text_of(chunk: Any, decoder: Optional[codecs.IncrementalDecoder] = None) -> str:
    """Text of one chunk from the common LLM SDK streams, or "" when it has none.

    Handles str and bytes, Anthropic (content_block_delta events), OpenAI chat
    completions (choices[0].delta.content) and responses
    (response.output_text.delta), the OpenAI Agents SDK (raw_response_event),
    LangChain / LangGraph message chunks (stream_mode="messages", also as
    (mode, chunk) tuples), Gemini (chunk.text) and dicts of the same shapes.
    """
    if chunk is None:
        return ""
    if isinstance(chunk, str):
        return chunk
    if isinstance(chunk, (bytes, bytearray, memoryview)):
        if decoder is None:
            return bytes(chunk).decode("utf-8", errors="replace")
        return decoder.decode(bytes(chunk))
    if isinstance(chunk, tuple) and chunk:
        if len(chunk) >= 2 and isinstance(chunk[0], str):
            return text_of(chunk[-1], decoder)
        return text_of(chunk[0], decoder)
    kind = _get(chunk, "type")
    if kind == "content_block_delta":
        delta = _get(chunk, "delta")
        return (_get(delta, "text") or "") if _get(delta, "type") == "text_delta" else ""
    if kind == "response.output_text.delta":
        return _get(chunk, "delta") or ""
    if kind == "raw_response_event":
        return text_of(_get(chunk, "data"), decoder)
    if kind == "text-delta":
        for name in ("text", "textDelta", "delta"):
            v = _get(chunk, name)
            if isinstance(v, str):
                return v
        return ""
    if kind in ("AIMessageChunk", "ai"):
        return _content_text(_get(chunk, "content"))
    choices = _get(chunk, "choices")
    if isinstance(choices, list):
        if not choices:
            return ""
        return _get(_get(choices[0], "delta"), "content") or ""
    if kind is None or isinstance(kind, str):
        try:
            text = _get(chunk, "text")
        except Exception:  # noqa: BLE001
            return ""
        if isinstance(text, str):
            return text
    return ""


def _size(s: str) -> int:
    return len(json.dumps(s, ensure_ascii=False).encode()) - 2


def _bytes(s: str) -> int:
    return len(s.encode())


def split(text: str, budget: int) -> list[str]:
    """Split text so each piece's JSON-escaped UTF-8 size fits budget, never inside a character."""
    out: list[str] = []
    piece: list[str] = []
    size = 0
    for ch in text:
        n = _size(ch)
        if size + n > budget and piece:
            out.append("".join(piece))
            piece, size = [], 0
        piece.append(ch)
        size += n
    if piece:
        out.append("".join(piece))
    return out


def _payload_bytes(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())


class _Encoder:
    def __init__(self, id: str, meta: Any, max_bytes: int) -> None:
        self.id = id
        self.meta = meta
        self.max_bytes = max_bytes
        envelope = {"id": id, "seq": 1_000_000_000, "delta": ""}
        if meta is not _MISSING:
            envelope["meta"] = meta
        self.budget = max_bytes - _payload_bytes(envelope)
        if self.budget < 16:
            raise ValueError("stream_to_channel: max_bytes is too small for the id and meta")
        self.seq = 0

    def deltas(self, buffer: str) -> list[dict[str, Any]]:
        out = []
        for delta in split(buffer, self.budget):
            payload: dict[str, Any] = {"id": self.id, "seq": self.seq, "delta": delta}
            if self.seq == 0 and self.meta is not _MISSING:
                payload["meta"] = self.meta
            self.seq += 1
            out.append(payload)
        return out

    def done(self, text: str, error: Optional[str]) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": self.id, "seq": self.seq, "done": True}
        if self.seq == 0 and self.meta is not _MISSING:
            payload["meta"] = self.meta
        if error is not None:
            payload["error"] = error
        if _payload_bytes({**payload, "text": text}) <= self.max_bytes:
            payload["text"] = text
        self.seq += 1
        return payload


def _error_text(err: BaseException) -> str:
    if isinstance(err, (asyncio.CancelledError, KeyboardInterrupt)):
        return "aborted"
    return str(err) or type(err).__name__


def stream_to_channel(
    client: Any,
    channel: str,
    source: Iterable[Any],
    *,
    id: Optional[str] = None,
    event: str = STREAM_EVENT,
    interval: float = 0.1,
    max_bytes: int = 3500,
    meta: Any = _MISSING,
    pick: Optional[Callable[[Any], Optional[str]]] = None,
    socket_id: Optional[str] = None,
    stop: Optional[threading.Event] = None,
) -> StreamResult:
    """Send a stream of text to a channel as events useStream() puts back together.

    client is a hibira.Client (anything with trigger(channel, event, data,
    socket_id=None) works). source yields text chunks or LLM SDK chunks (see
    text_of; pass pick for other shapes). meta is sent with the first event.
    Setting stop ends the stream with error "aborted". Returns after the last
    event is sent; if source raises, the final event carries the error and it
    is raised again.
    """
    enc = _Encoder(id or str(uuid.uuid4()), meta, max_bytes)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    lock = threading.Lock()
    send_lock = threading.Lock()
    state: dict[str, Any] = {"buffer": "", "timer": None, "send_error": None}

    def send(payloads: list[dict[str, Any]]) -> None:
        with send_lock:
            for p in payloads:
                if state["send_error"] is not None:
                    return
                try:
                    client.trigger(channel, event, p, socket_id=socket_id)
                except Exception as err:  # noqa: BLE001
                    state["send_error"] = err

    def flush() -> None:
        with lock:
            timer = state["timer"]
            if timer is not None:
                timer.cancel()
            state["timer"] = None
            buffer, state["buffer"] = state["buffer"], ""
            if not buffer:
                return
            # Taking send_lock before releasing lock keeps events in seq order.
            send_lock.acquire()
            try:
                payloads = enc.deltas(buffer)
            except BaseException:
                send_lock.release()
                raise
        try:
            for p in payloads:
                if state["send_error"] is not None:
                    break
                try:
                    client.trigger(channel, event, p, socket_id=socket_id)
                except Exception as err:  # noqa: BLE001
                    state["send_error"] = err
        finally:
            send_lock.release()

    text_parts: list[str] = []
    error: Optional[BaseException] = None
    try:
        for chunk in source:
            if stop is not None and stop.is_set():
                raise _Aborted()
            if state["send_error"] is not None:
                raise state["send_error"]
            delta = (pick(chunk) or "") if pick else text_of(chunk, decoder)
            if not delta:
                continue
            text_parts.append(delta)
            with lock:
                state["buffer"] += delta
                full = _bytes(state["buffer"]) >= enc.budget
                if not full and state["timer"] is None:
                    t = threading.Timer(interval, flush)
                    t.daemon = True
                    state["timer"] = t
                    t.start()
            if full:
                flush()
        tail = decoder.decode(b"", final=True)
        if tail:
            text_parts.append(tail)
            with lock:
                state["buffer"] += tail
        if stop is not None and stop.is_set():
            raise _Aborted()
    except BaseException as err:  # noqa: BLE001
        error = err
    flush()

    text = "".join(text_parts)
    message = None
    if error is not None:
        message = "aborted" if isinstance(error, _Aborted) else _error_text(error)
    send([enc.done(text, message)])

    if error is not None and not isinstance(error, _Aborted):
        raise error
    if state["send_error"] is not None:
        raise state["send_error"]
    return StreamResult(enc.id, text, enc.seq)


class _Aborted(Exception):
    pass


async def _aiter(source: Union[AsyncIterable[Any], Iterable[Any]]) -> Any:
    if hasattr(source, "__aiter__"):
        async for item in source:
            yield item
    else:
        for item in source:
            yield item


async def astream_to_channel(
    client: Any,
    channel: str,
    source: Union[AsyncIterable[Any], Iterable[Any]],
    *,
    id: Optional[str] = None,
    event: str = STREAM_EVENT,
    interval: float = 0.1,
    max_bytes: int = 3500,
    meta: Any = _MISSING,
    pick: Optional[Callable[[Any], Optional[str]]] = None,
    socket_id: Optional[str] = None,
) -> StreamResult:
    """asyncio version of stream_to_channel, for a hibira.AsyncClient (or a
    sync Client, whose trigger then runs in a thread) and an async iterable
    such as an OpenAI / Anthropic async stream or graph.astream(...).

    Cancelling the task sends the final event with error "aborted" and
    re-raises CancelledError.
    """
    enc = _Encoder(id or str(uuid.uuid4()), meta, max_bytes)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    send_lock = asyncio.Lock()
    buffer = ""
    timer: Optional[asyncio.TimerHandle] = None
    pending: set[asyncio.Task[None]] = set()
    send_error: Optional[BaseException] = None
    loop = asyncio.get_running_loop()

    async def trigger(payload: dict[str, Any]) -> None:
        result = client.trigger(channel, event, payload, socket_id=socket_id)
        if inspect.isawaitable(result):
            await result

    async def send(payloads: list[dict[str, Any]]) -> None:
        nonlocal send_error
        async with send_lock:
            for p in payloads:
                if send_error is not None:
                    return
                try:
                    await trigger(p)
                except Exception as err:  # noqa: BLE001
                    send_error = err

    def flush() -> None:
        nonlocal buffer, timer
        if timer is not None:
            timer.cancel()
        timer = None
        if not buffer:
            return
        payloads = enc.deltas(buffer)
        buffer = ""
        task = loop.create_task(send(payloads))
        pending.add(task)
        task.add_done_callback(pending.discard)

    if not _is_async_client(client):
        sync_client = client

        class _Threaded:
            def trigger(self, *a: Any, **kw: Any) -> Any:
                return asyncio.to_thread(sync_client.trigger, *a, **kw)

        client = _Threaded()

    text_parts: list[str] = []
    error: Optional[BaseException] = None
    try:
        async for chunk in _aiter(source):
            if send_error is not None:
                raise send_error
            delta = (pick(chunk) or "") if pick else text_of(chunk, decoder)
            if not delta:
                continue
            text_parts.append(delta)
            buffer += delta
            if _bytes(buffer) >= enc.budget:
                flush()
            elif timer is None:
                timer = loop.call_later(interval, flush)
        tail = decoder.decode(b"", final=True)
        if tail:
            text_parts.append(tail)
            buffer += tail
    except BaseException as err:  # noqa: BLE001
        error = err
    flush()

    text = "".join(text_parts)
    message = _error_text(error) if error is not None else None
    final = enc.done(text, message)

    async def finish() -> None:
        # Tasks run in creation order, but each waits on send_lock (FIFO), so
        # the final event goes last.
        await send([final])
        if pending:
            await asyncio.gather(*list(pending), return_exceptions=True)

    finisher = asyncio.ensure_future(finish())
    try:
        await asyncio.shield(finisher)
    except asyncio.CancelledError:
        await finisher
        raise

    if error is not None:
        raise error
    if send_error is not None:
        raise send_error
    return StreamResult(enc.id, text, enc.seq)


def _is_async_client(client: Any) -> bool:
    trigger = getattr(client, "trigger", None)
    return inspect.iscoroutinefunction(trigger)
