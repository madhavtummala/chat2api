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
curl http://localhost:9000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"GPT OSS 120B","stream":true,
       "messages":[{"role":"user","content":"Hello!"}]}'
```

Or with the OpenAI SDK (`model` must be one the active provider offers — see
`GET /v1/models` — or omit it to use the provider's default):

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:9000/v1", api_key="unused")
print(client.chat.completions.create(
    model="GPT OSS 120B",
    messages=[{"role": "user", "content": "Hello!"}],
).choices[0].message.content)
```

## Docker

The image runs the server headless, but Playwright needs a *logged-in* browser
profile — which normally requires a visible window. On a headless server you
can't run `headless=false`, and copying a profile from your laptop is
unreliable: Chrome encrypts its cookie store with an OS-bound key, so a macOS
profile's cookies won't decrypt inside the Linux container.

The default provider is **Google AI Mode** (`CHAT2API_PROVIDER=googleaimode`),
which needs no login — so `docker compose up chat2api` works out of the box. The
login flow below is only needed for the auth-gated providers (`expressai`,
`perplexity`).

The image solves that with two modes sharing one profile volume. The `login`
mode runs a headful Chromium behind [noVNC](https://novnc.com/), so you do the
one-time manual login *inside* the container (right OS, right Chromium) from any
web browser — no VNC client to install.

Pushing to `main` builds and publishes the image to GHCR via GitHub Actions
(`.github/workflows/docker.yml`). To run the published image instead of building
locally, replace `build: .` with
`image: ghcr.io/madhavtummala/chat2api:latest` in `docker-compose.yml`.

```bash
# 1. One-time login (do this before starting the server — Chromium locks the
#    profile, so only one mode can use the volume at a time).
docker compose --profile login up login
#    → open http://<server-host>:6080/vnc.html, click Connect, log in,
#      then Ctrl-C. The session is saved to the `browser_profile` volume.

# 2. Run the API (persistent profile → refreshed cookies are saved back, so the
#    session lasts as long as the site allows).
docker compose up -d chat2api        # serves on :9000
```

To log in again later (e.g. the session finally expired), stop the server first
so it releases the profile lock:

```bash
docker compose stop chat2api
docker compose --profile login up login      # log in, Ctrl-C
docker compose start chat2api
```

Set the provider and any API keys in `.env` (copied from `.env.example`) — the
`login` container reads the same file, so it opens the right site. For a
different login backend, change `CHAT2API_PROVIDER` and repeat step 1.

**Alternative — `storage_state` JSON:** if you'd rather log in on your laptop,
export a Playwright `storage_state` JSON (decrypted cookies + localStorage,
which *is* portable across OSes) and point `CHAT2API_STORAGE_STATE` at a mounted
copy. Simpler, but the context is ephemeral: cookie refreshes aren't saved back,
so you'll re-export more often than the noVNC/persistent-profile route needs.

## Endpoints

- `POST /v1/chat/completions` — streaming (SSE) and non-streaming.
- `POST /v1/responses` — Responses API with a server-side agentic loop (executes
  MCP tools itself); enabled by default, toggle with `CHAT2API_ENABLE_RESPONSES`.
- `GET  /v1/models` — models advertised by the active provider.
- `GET  /health` — liveness check (`?deep=1` re-probes login state).

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
| `expressai` | `app.expressai.com` | login (persisted) | tools, web-search toggle, attachments, model picker |
| `perplexity` | `perplexity.ai` | **optional** | works logged-out (login unlocks more models); native web search (always on), self-enabled incognito, model picker, `reasoning_effort` → Thinking mode |
| `googleaimode` | Google Search AI Mode (`udm=50`) | **none** | auth-free; great for demos, but Google throttles heavy automated use |

Select with `CHAT2API_PROVIDER=<name>`.

A provider only advertises the capabilities its UI has, so unsupported request
fields degrade gracefully: an omitted/unsupported `web_search` is a no-op (e.g.
Perplexity always searches), and `reasoning_effort` only acts where a "thinking"
toggle exists. Perplexity keeps proxied chats out of your history by enabling
**incognito** itself.

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

**Reasoning ("thinking") mode** — pass OpenAI's `reasoning_effort` (chat
completions) or `reasoning: {"effort": ...}` (responses). Where the UI has a
thinking toggle (Perplexity, on reasoning-capable models), `minimal`/`none`
turns it off and any other value turns it on; an absent value leaves the model
default, and providers without the toggle ignore it.

### Models

`/v1/models` reports the active provider's catalogue — normally the static
`available_models` list (e.g. ExpressAI's models, Perplexity's picker options).
A provider may instead override `async list_models()` to discover them live from
the site; the app calls `refresh_models()` at startup and serves whatever it
returns. Per request, `select_model(page, model)` switches the UI to the
requested model before submitting, and only when it differs from the one already
selected (a no-op for single-model UIs). A model the provider doesn't offer is
rejected with `404 model_not_found` — we never switch to an unknown model; an
omitted model uses the provider's default.

### Google AI Mode (auth-free demo backend)

```bash
CHAT2API_PROVIDER=googleaimode python -m src.main
curl http://localhost:9000/v1/chat/completions -H 'Content-Type: application/json' \
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
