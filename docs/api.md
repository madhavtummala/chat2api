# API

## Endpoints

- `POST /v1/chat/completions` — streaming (SSE) and non-streaming.
- `POST /v1/responses` — Responses API with a server-side agentic loop (executes
  MCP tools itself); enabled by default, toggle with `CHAT2API_ENABLE_RESPONSES`.
- `GET  /v1/models` — every routable model, advertised as `provider/model`.
- `GET  /health` — liveness + per-provider login state (`?deep=1` re-probes).

Set `CHAT2API_API_KEYS=key1,key2` to require `Authorization: Bearer <key>` (auth
is off by default for local use).

## Model routing

One server can front several chat UIs. Pick a backend by prefixing the model with
the provider name; a bare model uses the default provider (`CHAT2API_PROVIDER`):

```jsonc
{"model": "perplexity/Gemini 3.1 Pro", ...}   // explicit provider
{"model": "GPT OSS 120B", ...}                 // → default provider
```

`GET /v1/models` lists every routable model in `provider/model` form. Restrict
which providers are routable with `CHAT2API_PROVIDERS` (empty = all registered;
the default is always allowed). A model the resolved provider doesn't offer is
rejected with `404 model_not_found`; an unknown provider prefix is also `404`.

## Tool calls (function calling)

Chat UIs don't expose a model's native function-calling channel, so tool calls
are **emulated as text**: when a request includes `tools`, a preamble instructs
the model to emit calls as
`<tool_call>{"name": ..., "arguments": {...}}</tool_call>`, and a streaming
parser converts those back into standard OpenAI `tool_calls`
(`finish_reason: "tool_calls"`) — never leaking a partial tag as content.

For **Chat Completions**, execution is delegated to the client (the standard
loop): you receive the `tool_calls`, run them, and send results back as `tool`
messages. Providers opt in via `supports_tools`; a request with `tools` against a
provider that can't emulate them returns `400`.

### MCP tools

Drop an `mcp.json` in the project root (auto-loaded; see `mcp.example.json`) or
point `CHAT2API_MCP_CONFIG_PATH` elsewhere. It lists MCP servers (stdio or
streamable-HTTP); at startup the wrapper connects, lists their tools, and
advertises them to the model alongside client-declared tools — namespaced
`<server_label>__<tool>`.

- **Chat Completions** — MCP tool calls are **delegated** like any other tool.
- **Responses** (`/v1/responses`) — MCP tool calls are **executed server-side**:
  the wrapper runs the tool, feeds the result back, and loops (up to
  `CHAT2API_MAX_AGENT_TURNS`) to a final answer. Unknown (client-owned) tools halt
  the loop with `requires_action` for the client to handle.

## Provider capabilities

A provider only advertises what its UI supports, so unsupported request fields
degrade gracefully instead of erroring.

**Web search** — enable via any OpenAI-compatible spelling (normalised to one
flag); unsupported providers ignore it:
- `web_search_options: {...}` (native OpenAI field), or
- a `:online` model suffix — `"model": "GPT OSS 120B:online"` (OpenRouter style), or
- `web_search: true` (vendor field via the SDK's `extra_body`).

**Attachments** — use OpenAI's native multimodal `content` parts; `data:` URLs
are decoded and uploaded into the chat UI:
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
thinking toggle (e.g. Perplexity, on reasoning-capable models), `minimal`/`none`
turns it off and any other value turns it on; an absent value leaves the model
default, and providers without the toggle ignore it.

## Models

`/v1/models` reports each provider's catalogue — normally the static
`available_models` list. A provider may instead override `async list_models()` to
discover them live from the site; the app calls `refresh_models()` at startup.
Per request, `select_model(page, model)` switches the UI to the requested model
before submitting, only when it differs from the one already selected (a no-op for
single-model UIs). An omitted model uses the provider's default.
