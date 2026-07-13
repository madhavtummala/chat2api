# chat2api

An **OpenAI-compatible API** in front of chat web UIs that have no public API,
built with [FastAPI](https://fastapi.tiangolo.com/) and
[Playwright](https://playwright.dev/python/) browser automation.

The first target is [`app.expressai.com`](https://app.expressai.com), but the
provider layer is a small abstraction so new backends (Google AI Studio,
ChatGPT, …) are added by writing one file.

```
OpenAI client ──HTTP──▶ FastAPI (/v1/chat/completions)
                             │  normalise request
                             ▼
                      Provider (text deltas)  ◀── drives ──▶ Chromium (Playwright)
                             │                                   app.expressai.com
                             ▼  wrap as OpenAI chunks
                      SSE stream / JSON response
```

## Architecture

| Layer | Location | Responsibility |
|-------|----------|----------------|
| API | `src/api/` | OpenAI wire schema, auth, SSE formatting, routes |
| Providers | `src/providers/` | Adapt one chat UI; **yield plain text deltas** |
| Browser | `src/browser/` | Playwright lifecycle + a pool of serialised tabs |
| Core | `src/core/` | Provider-facing types, message flattening, errors |
| Config | `src/config.py` | Env-driven settings (`CHAT2API_*`) |

The key seam: **providers only produce text**; the API layer owns *all* OpenAI
formatting. That keeps providers tiny and the wire format in one place.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env            # edit as needed

# First run: log in to the target site manually (window is visible).
CHAT2API_HEADLESS=false python -m src.main
# ...log in in the browser window; the session is saved to .browser_profile/

# Subsequent runs can be headless:
python -m src.main              # or: uvicorn src.api.app:app
```

Then call it like the OpenAI API:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"expressai-default","stream":true,
       "messages":[{"role":"user","content":"Hello!"}]}'
```

Or with the OpenAI SDK:

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="unused")
print(client.chat.completions.create(
    model="expressai-default",
    messages=[{"role": "user", "content": "Hello!"}],
).choices[0].message.content)
```

## Endpoints

- `POST /v1/chat/completions` — streaming (SSE) and non-streaming.
- `GET  /v1/models` — models advertised by the active provider.
- `GET  /health` — liveness check.

Set `CHAT2API_API_KEYS=key1,key2` to require `Authorization: Bearer <key>`
(auth is off by default for local use).

## Detecting logout

Before each request the provider verifies it's still logged in (ExpressAI keys
off the composer placeholder / "Sign in" button and any redirect to an auth
host). If the session has expired it raises `AuthenticationRequired` — the API
returns a clear upstream-auth error instead of hanging, and the provider's login
state is cached and exposed on **`GET /health`**:

```jsonc
{"status": "ok", "provider": "expressai", "authenticated": true}
```

`GET /health?deep=1` actively re-probes the live UI. To recover, re-authenticate
once with `CHAT2API_HEADLESS=false` (the login persists in the browser profile)
or supply a fresh `CHAT2API_STORAGE_STATE`.

## Statelessness & sessions

The API is stateless in the OpenAI sense: clients resend the full `messages`
array each call, and we flatten it into one prompt. So each request runs in a
**fresh conversation** (the provider clicks "New chat" before submitting) while
the **browser login/session persists** across requests via the pooled tabs.

Tabs are drawn from a bounded pool (`CHAT2API_MAX_CONCURRENCY`), so there are
never runaway browser tabs. A tab is recycled — closed and replaced — if a
request raised, if the page died, or after `CHAT2API_MAX_TAB_USES` uses, so a
wedged tab never re-enters rotation.

## Tool calls (function calling)

Chat UIs don't expose a model's native function-calling channel, so tool calls
are **emulated as text**: when a request includes `tools`, a preamble is
injected instructing the model to emit calls as
`<tool_call>{"name": ..., "arguments": {...}}</tool_call>`, and a streaming
parser converts those back into standard OpenAI `tool_calls`
(`finish_reason: "tool_calls"`) — never leaking a partial tag as content.

Execution is **delegated to the client** (the standard Chat Completions loop):
you receive the `tool_calls`, run them, and send the results back as `tool`
messages on the next request. Providers opt in via `supports_tools`; a request
with `tools` against a provider that can't emulate them returns `400`.

### MCP tools

Drop an `mcp.json` in the project root (auto-loaded; see `mcp.example.json`) or
point `CHAT2API_MCP_CONFIG_PATH` elsewhere. It lists MCP servers (stdio or
streamable-HTTP); at startup the wrapper connects, lists their tools, and
advertises them to the model alongside client-declared tools — namespaced
`<server_label>__<tool>`.

- **Chat Completions** — MCP tool calls are **delegated** like any other tool
  (parsed and returned to the client).
- **Responses** (`/v1/responses`) — MCP tool calls are **executed server-side**:
  the wrapper runs the tool, feeds the result back, and loops to a final answer
  (see the agentic loop below).

## Providers

| Name | Target | Auth | Notes |
|------|--------|------|-------|
| `expressai` | `app.expressai.com` | login (persisted) | selectors need tuning against the live site |
| `googleaimode` | Google Search AI Mode (`udm=50`) | **none** | auth-free; great for demos, but Google throttles heavy automated use |

Select with `CHAT2API_PROVIDER=<name>`.

### Web search & attachments (provider capabilities)

Provider-specific UI features are exposed **without breaking OpenAI
compatibility**, gated by capability flags (`supports_web_search`,
`supports_attachments`; ExpressAI has both).

**Web search** — enable it via any OpenAI-compatible spelling (normalised to one
flag), so unsupported providers just ignore it:
- `web_search_options: {...}` (native OpenAI field), or
- a `:online` model suffix — `"model": "GPT OSS 120B:online"` (OpenRouter style), or
- `web_search: true` (vendor field via the SDK's `extra_body`).

**Attachments** — use OpenAI's native multimodal `content` parts (no custom
API); `data:` URLs are decoded and uploaded into the chat UI:
```jsonc
"content": [
  {"type": "text", "text": "Summarize this"},
  {"type": "file", "file": {"filename": "r.pdf", "file_data": "data:application/pdf;base64,..."}},
  {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
]
```
Sending attachments to a provider that can't accept them returns `400`; remote
(non-`data:`) URLs are skipped.

### Models

`/v1/models` reports the active provider's **live** catalogue. A provider seeds
it from the static `available_models`, but can override `async list_models()` to
**discover models from the site** (e.g. scrape the model picker); the app calls
`refresh_models()` at startup and serves whatever it returns. Per request,
`select_model(page, model)` switches the UI to the requested model before
submitting, and only when it differs from the one already selected (default
no-op for single-model UIs). A model the provider doesn't offer is rejected with
`404 model_not_found` — we never try to switch to an unknown model; an omitted
model uses the provider's default.

### Google AI Mode (auth-free demo backend)

```bash
CHAT2API_PROVIDER=googleaimode python -m src.main
curl http://localhost:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"google-ai-mode","messages":[{"role":"user","content":"capital of Japan?"}]}'
```

This navigates to `google.com/search?udm=50&q=<prompt>` and streams the AI Mode
answer (stripping the query echo + UI boilerplate). It genuinely needs no login.
**Caveat:** Google's markup is obfuscated and varies per request, and repeated
automated hits trigger "unusual traffic" throttling/CAPTCHA — the provider
detects that and returns a clean `502`. Treat it as a demo/dev backend, not a
production dependency. An opt-in live test exists:

```bash
RUN_LIVE_GOOGLE=1 pytest tests/test_live_google.py -v -s   # skips if throttled
```

## Adding a provider

1. Create `src/providers/<name>.py` subclassing `BaseChatProvider`.
2. Implement `async def generate(self, request) -> AsyncIterator[str]` yielding
   incremental text; optionally `startup`/`shutdown`.
3. Register it in `src/providers/registry.py`.
4. Select it with `CHAT2API_PROVIDER=<name>`.

## Tuning ExpressAI selectors

The live DOM of `app.expressai.com` is not public, so the CSS selectors in
`src/providers/expressai.py` (the `Selectors` dataclass) are best-effort
defaults. Open the site with devtools and adjust them to match the real
composer, send button, assistant bubble, and "generating" indicator. All
site-specific assumptions live in that one block.

## Tests

```bash
pip install -r requirements.txt
pytest          # API + core unit tests (no real browser required)
```

## Notes & limitations

- Browser automation of a UI is inherently fragile; expect to update selectors
  when the site changes.
- Each tab serves one request at a time; scale with `CHAT2API_MAX_CONCURRENCY`.
- Respect the target service's Terms of Service.
