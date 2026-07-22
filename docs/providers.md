# Providers

A provider adapts one chat web UI to the uniform streaming interface. Most are a
single file of CSS selectors on top of the shared `BrowserChatProvider` base.

## Built-in providers

| Name | Target | Auth | Notes |
|------|--------|------|-------|
| `expressai` | `app.expressai.com` | login (persisted) | tools, web-search toggle, attachments, model picker |
| `perplexity` | `perplexity.ai` | **optional** | works logged-out (login unlocks more models); native web search (always on), self-enabled incognito, model picker, `reasoning_effort` → Thinking mode |
| `googleaimode` | Google Search AI Mode (`udm=50`) | **none** | auth-free; great for demos, but Google throttles heavy automated use |

Set the default with `CHAT2API_PROVIDER=<name>`, and restrict routable providers
with `CHAT2API_PROVIDERS` (see [API → Model routing](api.md#model-routing)).
Perplexity keeps proxied chats out of your history by enabling **incognito**
itself.

## Adding a provider

1. Create `src/providers/<name>.py`. For a chat UI, subclass
   `BrowserChatProvider` and supply a `Selectors` block (composer, send button,
   assistant bubble, "generating" indicator, login markers, optional model
   picker / web-search toggle / file input). For a non-chat backend, subclass
   `BaseChatProvider` directly and implement
   `async def generate(self, request) -> AsyncIterator[str]` yielding text deltas.
2. Set capability flags (`supports_tools`, `supports_web_search`,
   `supports_attachments`, `supports_thread_continuation`) to match the UI.
3. Register it in `src/providers/registry.py`.
4. Select it with `CHAT2API_PROVIDER=<name>` (or route to it via the
   `provider/model` model syntax).

Because all site-specific assumptions live in the `Selectors` dataclass, the rest
of the codebase is unaffected when a site's DOM changes.

## Tuning selectors

The live DOM of a target site is not public, so the CSS selectors in each
provider's `Selectors` dataclass are best-effort. Open the site with devtools and
adjust them to match the real composer, send button, assistant bubble, and
"generating" indicator. `scripts/inspect_provider.py <url>` helps re-derive them
when a layout changes.

## Google AI Mode (auth-free demo backend)

```bash
CHAT2API_PROVIDER=googleaimode python -m src.main
curl http://localhost:9000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"google-ai-mode","messages":[{"role":"user","content":"capital of Japan?"}]}'
```

This navigates to `google.com/search?udm=50&q=<prompt>` and returns the AI Mode
answer as Markdown (stripping the query echo + UI boilerplate). It genuinely needs
no login.

**Caveat:** Google's markup is obfuscated and varies per request, and repeated
automated hits trigger "unusual traffic" throttling/CAPTCHA — the provider detects
that and returns a clean `502`. Treat it as a demo/dev backend, not a production
dependency. An opt-in live test exists:

```bash
RUN_LIVE_GOOGLE=1 pytest tests/test_live_google.py -v -s   # skips if throttled
```

## Notes & limitations

- Browser automation of a UI is inherently fragile; expect to update selectors
  when the site changes.
- Each tab serves one request at a time; scale with `CHAT2API_MAX_CONCURRENCY`.
- Respect the target service's Terms of Service.
