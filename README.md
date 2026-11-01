# hibira

Python server library for [Hibira](https://hibira.io), hosted realtime that
speaks the Pusher Channels protocol. Publish events, authorize private and
presence channels, verify webhooks, send push notifications, ask a person for
an approval, and stream LLM output to the browser as it is written.

```sh
pip install hibira
```

Python 3.9+, one dependency ([httpx](https://www.python-httpx.org/)). Both a
regular and an asyncio client. pusher-js, Laravel Echo and the
`@hibira-io/*` front-end packages connect to Hibira unchanged.

## Set up

Create an app at [hibira.io/portal](https://hibira.io/portal) (the free plan
needs no card) and put its keys in the environment:

```sh
HIBIRA_APP_ID=...
HIBIRA_APP_KEY=...
HIBIRA_APP_SECRET=...
```

```python
import hibira

client = hibira.Client.from_env()  # or hibira.Client(app_id, key, secret)
client.trigger("orders", "created", {"id": 1042})
```

```python
async with hibira.AsyncClient.from_env() as client:
    await client.trigger(["orders", "dashboard"], "created", {"id": 1042})
```

| Setting | Environment | Default |
| --- | --- | --- |
| `app_id`, `key`, `secret` | `HIBIRA_APP_ID`, `HIBIRA_APP_KEY`, `HIBIRA_APP_SECRET` | |
| `host` | `HIBIRA_HOST` | `ws.hibira.io` (or the host shown on your app page) |
| `port`, `use_tls` | `HIBIRA_PORT`, `HIBIRA_USE_TLS` | `443`, `True` |
| `push_instance_id`, `push_secret_key` | `HIBIRA_PUSH_INSTANCE_ID`, `HIBIRA_PUSH_SECRET_KEY` | |
| `push_endpoint` | `HIBIRA_PUSH_ENDPOINT` | `https://<host>` |
| `timeout` | | `10.0` seconds |

Keyword arguments to `from_env()` win over the environment. Both clients
accept `http_client=` to reuse your own `httpx.Client` / `httpx.AsyncClient`.

## Stream LLM output to the browser

`stream_to_channel` sends a stream of text to a channel as it is produced, in
the format `useStream()` in [@hibira-io/react](https://github.com/hibira-io/react)
and [@hibira-io/vue](https://github.com/hibira-io/vue) puts back together.
Tokens are batched (about 10 events a second rather than one per token), each
event stays under the plan's message size, and the last event carries the
whole text when it fits, so people who open the page late catch up. With
channel history on (paid plans), `useStream(channel, { rewind: true })`
replays a stream after a reload.

```python
from openai import OpenAI
import hibira

client = hibira.Client.from_env()
openai = OpenAI()

stream = openai.chat.completions.create(model="gpt-5-mini", messages=messages, stream=True)
result = hibira.stream_to_channel(client, f"private-conversation-{conversation_id}", stream, meta={"role": "assistant"})
save_message(conversation_id, result.text)
```

```jsx
// React
const { text, done } = useStream(`private-conversation-${conversationId}`);
```

`text_of` reads the text out of chunks from the common SDKs, so you can pass
their streams as they are:

| Source | Pass |
| --- | --- |
| OpenAI chat completions | `client.chat.completions.create(..., stream=True)` |
| OpenAI Responses | `client.responses.create(..., stream=True)` |
| Anthropic | `client.messages.create(..., stream=True)` (or the async client) |
| OpenAI Agents SDK | `Runner.run_streamed(agent, input).stream_events()` |
| LangGraph / LangChain | `graph.astream(input, stream_mode="messages")`, `llm.astream(...)` |
| Gemini (google-genai) | `client.models.generate_content_stream(...)` |
| Anything else | an iterable of `str` or `bytes`, or pass `pick=lambda chunk: ...` |

With asyncio, use `astream_to_channel` (it takes async or plain iterables,
and an `AsyncClient` or a `Client`):

```python
async def answer(conversation_id: str, question: str) -> str:
    stream = graph.astream({"messages": [("user", question)]}, stream_mode="messages")
    result = await hibira.astream_to_channel(client, f"private-conversation-{conversation_id}", stream)
    return result.text
```

To stream only one LangGraph node, filter with `pick`:
`pick=lambda c: hibira.text_of(c) if c[1].get("langgraph_node") == "agent" else ""`.

| Option | Default | |
| --- | --- | --- |
| `id` | random UUID | stream id, also in the result |
| `event` | `"ai-stream"` | event name |
| `interval` | `0.1` | seconds to gather tokens before sending them |
| `max_bytes` | `3500` | largest event payload (Free plans allow 4 KB per message) |
| `meta` | | JSON sent with the first event (model, message id, author) |
| `pick` | `text_of` | text of one chunk |
| `socket_id` | | leave out this connection (the sender's own tab) |
| `stop` | | a `threading.Event`; set it to end the stream with error `"aborted"` (sync only; cancel the task with asyncio) |

If the source raises, the final event carries the error (useStream shows it)
and the exception is raised again. Both return a `StreamResult(id, text, events)`.

## Authorize private and presence channels

```python
client.authenticate_channel("private-user-42", socket_id)
# {"auth": "key:signature"}

client.authenticate_channel("presence-room-7", socket_id, {"user_id": "42", "user_info": {"name": "Ann"}})
# {"auth": "...", "channel_data": "{\"user_id\":\"42\",...}"}

client.authenticate_user(socket_id, {"id": "42", "name": "Ann"})  # user sign-in
```

Return the dict as JSON from the endpoint pusher-js calls
(`channelAuthorization.endpoint`). It receives `socket_id` and
`channel_name` as form fields. These methods need no network and are plain
functions on `AsyncClient` too.

### FastAPI

```sh
pip install "hibira[fastapi]"
```

```python
from fastapi import Depends, FastAPI
from hibira import AsyncClient
from hibira.fastapi import auth_router, webhook

client = AsyncClient.from_env()
app = FastAPI()


async def authorize(request, channel: str):
    user = await current_user(request)
    if channel == f"private-user-{user.id}":
        return True
    if channel.startswith("presence-room-"):
        return {"user_id": str(user.id), "user_info": {"name": user.name}}
    return False


app.include_router(auth_router(client, authorize))  # POST /hibira/auth


@app.post("/hibira/webhook")
async def on_webhook(payload: dict = Depends(webhook(client))):
    for event in payload["events"]:
        ...
```

`auth_router(client, authorize, user=...)` also answers user sign-in at
`/hibira/user-auth`. Django support is in
[django-hibira](https://github.com/hibira-io/django).

## Webhooks

```python
payload = client.verify_webhook(
    request.headers.get("X-Pusher-Key"),
    request.headers.get("X-Pusher-Signature"),
    request.body,  # the raw body, bytes or str
)
```

Raises `hibira.InvalidWebhook` when the key or signature does not match.

## Channels API

```python
client.trigger_batch([{"channel": "a", "name": "e", "data": {"x": 1}}])
client.channels(prefix="presence-", info="user_count")
client.channel_info("presence-room-7", info=["user_count", "subscription_count"])
client.users("presence-room-7")
client.terminate_user_connections("42")
```

`trigger` takes one channel or a list of up to 100. Data that is not a `str`
is sent as JSON. `socket_id=` leaves out the sender's own connection.

## Push notifications

Turn on Push for the app in the portal and set `HIBIRA_PUSH_INSTANCE_ID` and
`HIBIRA_PUSH_SECRET_KEY`. Bodies use the Beams format.

```python
client.push_to_interests(
    ["news"],
    {
        "web": {"notification": {"title": "New post", "deep_link": "https://example.com/p/1"}},
        "fcm": {"notification": {"title": "New post"}},
        "apns": {"aps": {"alert": {"title": "New post"}}},
    },
)
client.push_to_users(["42"], {...})
client.push_token("42")  # {"token": ...} for setUserId in the web, Android and iOS SDKs
client.delete_push_user("42")
```

## Approvals

Ask a person for a decision from a script, worker or agent. Hibira hosts the
answer page.

```python
approval = client.request_approval(
    "Refund order #1042 for $89?",
    details="Customer says the parcel never arrived.",
    options=["Refund", "Decline"],  # the first one approves
    push_interests=["ops"],  # or push_users=[...], or share approval["url"]
    channel="private-hibira-approvals.ops",  # shows in the @hibira-io/approvals inbox
)
result = client.wait_for_approval(approval["approval_id"], timeout=3600)
if result["status"] == "approved":
    refund(1042)
```

`status` is `pending`, `approved`, `rejected` or `expired`. `get_approval(id, wait=25)`
reads it once, waiting up to 25 seconds for an answer.

## Errors

Every error is a `hibira.HibiraError`. HTTP errors are `hibira.HTTPError`
(`status`, `message`, `body`), with subclasses `AuthenticationError` (401,
403), `NotFound` (404), `PayloadTooLarge` (413, the event is over the plan's
message size) and `RateLimited` (429). Network failures (DNS, connect,
TLS, proxy, timeout) raise `hibira.ConnectionError`. Missing settings raise
`ConfigError`.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e . pytest fastapi ruff mypy
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/mypy
```

`tests/test_e2e.py` runs against a real server when `HIBIRA_E2E=1` and the
`HIBIRA_*` settings point at it (it needs `pip install websockets`).

## 中文摘要

`hibira` 是 Hibira 的 Python 伺服器端套件（Python 3.9 以上，同步與 asyncio 兩種
client）：發送事件、私人 / presence 頻道授權、驗證 webhook、推播通知、人工核准，
以及用 `stream_to_channel` 把 LLM 的輸出即時串流到瀏覽器（React / Vue 的
`useStream()` 直接接收）。支援 OpenAI、Anthropic、OpenAI Agents SDK、LangGraph、
Gemini 的串流格式，並附 FastAPI 授權路由與 webhook 驗證。

## License

MIT
