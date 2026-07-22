# chat2api

**Turn a free chat UI into an OpenAI-compatible API.**

Many great models are free to *chat* with but have no free API. Official APIs are
pay-per-use and want your billing details up front — and aggregators like
Perplexity, which blend several frontier models, often expose no API at all
because those models aren't theirs to resell. chat2api bridges that gap: it drives
the real website in a headless browser and puts a standard **OpenAI-compatible API** in
front of it, so any OpenAI client, SDK, or tool just works.

Built with [FastAPI](https://fastapi.tiangolo.com/) and
[Playwright](https://playwright.dev/python/) — great for quick prototyping and
personal projects where you want to build against a real model without setting up
API billing.

> Intended for prototyping and personal use. Automating a site may run against its
> Terms of Service — respect each provider's terms and rate limits.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

python -m src.main          # API on http://localhost:9000
```

The default provider (**Google AI Mode**) needs no login, so that's it. Then call
it like the OpenAI API:

```bash
curl http://localhost:9000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"google-ai-mode","stream":true,
       "messages":[{"role":"user","content":"Hello!"}]}'
```

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:9000/v1", api_key="unused")
print(client.chat.completions.create(
    model="google-ai-mode",
    messages=[{"role": "user", "content": "Hello!"}],
).choices[0].message.content)
```

Other backends (ExpressAI, Perplexity) need a one-time login — see
[Deployment](docs/deployment.md).

## Extending

Adding a backend is usually one small file — a block of CSS selectors on top of a
shared base:

1. Create `src/providers/<name>.py` (subclass `BrowserChatProvider` with a
   `Selectors` block, or `BaseChatProvider` for a non-chat backend).
2. Set its capability flags (tools, web search, attachments, …).
3. Register it in `src/providers/registry.py`.

Full walkthrough in [Providers → Adding a provider](docs/providers.md#adding-a-provider).

## Documentation

- **[Architecture](docs/architecture.md)** — how it works, the tab pool, sessions.
- **[Deployment](docs/deployment.md)** — install, Docker, login flows, config reference.
- **[API](docs/api.md)** — endpoints, model routing, tool calls & MCP, capabilities.
- **[Providers](docs/providers.md)** — built-in backends, adding & tuning your own.

## Tests

```bash
pip install -r requirements.txt
pytest          # API + core unit tests (no real browser required)
```

## Notes

Browser automation of a UI is inherently fragile — expect to update selectors when
a site changes. Please respect each target service's Terms of Service.
