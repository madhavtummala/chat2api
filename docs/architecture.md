# Architecture

chat2api puts an OpenAI-compatible HTTP surface in front of chat **web UIs** that
have no public API, driving them with a real browser.

```
OpenAI client ──HTTP──▶ FastAPI (/v1/chat/completions, /v1/responses)
                             │  normalise request
                             ▼
                      Provider (text deltas)  ◀── drives ──▶ Chromium (Playwright)
                             │                                   the chat web UI
                             ▼  wrap as OpenAI chunks
                      SSE stream / JSON response
```

## Layers

| Layer | Location | Responsibility |
|-------|----------|----------------|
| API | `src/api/` | OpenAI wire schema, auth, SSE formatting, routing |
| Providers | `src/providers/` | Adapt one chat UI; **yield plain text deltas** |
| Browser | `src/browser/` | Playwright lifecycle + per-provider pools of tabs |
| Core | `src/core/` | Provider-facing types, message flattening, markdown, errors |
| Config | `src/config.py` | Env-driven settings (`CHAT2API_*`) |

**The key seam:** providers only produce text; the API layer owns *all* OpenAI
formatting. That keeps providers tiny and the wire format in one place. Most new
providers are a single file of CSS selectors (see
[Providers → Adding a provider](providers.md#adding-a-provider)).

## Faithful markdown

Providers read the reply as **Markdown**, reconstructed from the answer's
`innerHTML` (`src/core/markdown.py`), not the flattened `inner_text`. This
preserves lists, headings, emphasis, code fences, and inline reference links, so
the client renders output just as a direct LLM API response would.

## Tab pool

Tabs are drawn from a **bounded pool per provider** (keyed by provider name),
warmed lazily on first use, so a request always borrows a tab already sitting on
the right site. Pool size is `CHAT2API_MAX_CONCURRENCY`. A tab is recycled —
closed and replaced — if a request raised, if the page died, or after
`CHAT2API_MAX_TAB_USES` uses, so a wedged tab never re-enters rotation and there
are never runaway tabs.

## Multi-provider routing

One server can front several chat UIs at once. Clients pick a backend by
prefixing the model with the provider name (`perplexity/Gemini 3.1 Pro`); a bare
model goes to the configured default provider. Provider instances are created
lazily and cached. See [API → Model routing](api.md#model-routing).

## Statelessness & sessions

The API is stateless in the OpenAI sense: for Chat Completions the client resends
the full `messages` array each call, and we flatten it into one prompt in a
**fresh conversation** (the provider starts a new thread before submitting) while
the **browser login/session persists** across requests via the pooled tabs.

The **Responses API** (`/v1/responses`) keeps conversation history server-side
(keyed by `previous_response_id`), so it works for every provider regardless of
whether the underlying UI persists threads. Within a single agentic request, on
chat-box providers the loop holds one tab and sends the system prompt once + only
the new (delta) messages each turn — the thread's own context supplies the rest.
Gated by the provider capability `supports_thread_continuation` (true for chat
boxes, false for stateless backends like Google AI Mode).
