# Changelog

## 0.1.0

First release.

- `Client` and `AsyncClient`: `trigger`, `trigger_batch`, `channels`, `channel_info`, `users`, `terminate_user_connections`, settings from `HIBIRA_*` environment variables.
- Channel and user auth (`authenticate_channel`, `authenticate_user`) and webhook verification (`verify_webhook`).
- Hibira Push: `push_to_interests`, `push_to_users`, `push_token`, `delete_push_user`.
- Approvals: `request_approval`, `get_approval`, `wait_for_approval`.
- `stream_to_channel` / `astream_to_channel`: stream LLM output to `useStream()` in `@hibira-io/react` and `@hibira-io/vue`, with `text_of` for OpenAI, Anthropic, OpenAI Agents SDK, LangChain / LangGraph and Gemini chunks.
- Errors: `HibiraError` base; `HTTPError` with `AuthenticationError`, `NotFound`, `PayloadTooLarge`, `RateLimited`; `ConnectionError` for network failures; `InvalidWebhook`, `ConfigError`.
- `hibira.fastapi`: channel / user auth router and webhook dependency.
