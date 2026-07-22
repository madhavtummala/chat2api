"""End-to-end browser tests against a local mock chat page (no auth, no network).

These launch a real headless Chromium via the same BrowserManager + ExpressAI
provider used in production, pointed at ``tests/assets/mock_chat.html``. They
verify three things:

  1. E2E:      an HTTP chat request actually drives the browser and returns the
               streamed reply.
  2. Sessions: a reused browser tab keeps its state across requests
               ("Turn 1" -> "Turn 2").
  3. Pooling:  the tab pool is bounded by ``max_concurrency`` and never leaks
               tabs, no matter how many requests are served.

Skipped automatically if Chromium is not installed (`playwright install
chromium`).
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from pathlib import Path
from typing import NamedTuple

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.routes import router
from src.browser import BrowserManager
from src.config import Settings
from src.core.errors import AuthenticationRequired
from src.core.types import ChatMessage, ChatRequest
from src.providers.expressai import ExpressAIProvider

from .conftest import FakeRouter

MOCK_PAGE = Path(__file__).parent / "assets" / "mock_chat.html"
MOCK_URL = MOCK_PAGE.resolve().as_uri()
LOGIN_URL = (Path(__file__).parent / "assets" / "mock_login.html").resolve().as_uri()
REFLOW_URL = (Path(__file__).parent / "assets" / "mock_reflow.html").resolve().as_uri()


def _settings(tmp_path, max_concurrency: int) -> Settings:
    return Settings(
        headless=True,
        provider="expressai",
        user_data_dir=str(tmp_path / "profile"),
        max_concurrency=max_concurrency,
        nav_timeout_ms=15_000,
        expressai_base_url=MOCK_URL,
        response_timeout_s=20.0,
        poll_interval_s=0.05,
        api_keys="",
    )


@pytest.fixture
async def stack(tmp_path, request):
    """A started BrowserManager + provider, torn down after the test.

    Parametrise `max_concurrency` via `indirect`; defaults to 1.
    """
    max_concurrency = getattr(request, "param", 1)
    settings = _settings(tmp_path, max_concurrency)
    browser = BrowserManager(settings)
    try:
        await browser.start()
    except Exception as exc:  # chromium missing / cannot launch
        pytest.skip(f"Browser unavailable: {exc}")
    provider = ExpressAIProvider(settings, browser)
    try:
        yield browser, provider, settings
    finally:
        await browser.stop()


def _make_client(provider) -> AsyncClient:
    app = FastAPI()
    app.include_router(router)
    app.state.router = FakeRouter(provider)
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def _content(client: AsyncClient, message: str) -> str:
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "GPT OSS 120B", "messages": [{"role": "user", "content": message}]},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["choices"][0]["message"]["content"]


# "Turn 1 [tab=ab12 model=GPT OSS 120B web=off files=0]: hello world"
_REPLY = re.compile(
    r"Turn (\d+) \[tab=(\w+) model=(.+?) web=(on|off) files=(\d+)\]: (.*)", re.S
)


class Reply(NamedTuple):
    turn: int
    tab: str
    model: str
    web: str
    files: int
    text: str


def _parse(content: str) -> Reply:
    m = _REPLY.match(content)
    assert m, f"unexpected reply shape: {content!r}"
    return Reply(int(m[1]), m[2], m[3], m[4], int(m[5]), m[6])


async def _post(client: AsyncClient, content, **body) -> str:
    payload = {"model": "GPT OSS 120B", "messages": [{"role": "user", "content": content}]}
    payload.update(body)
    resp = await client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["choices"][0]["message"]["content"]


async def test_e2e_non_streaming(stack):
    _, provider, _ = stack
    async with _make_client(provider) as client:
        r = _parse(await _content(client, "hello world"))
    assert (r.turn, r.text) == (1, "hello world")


async def test_e2e_streaming_deltas(stack):
    _, provider, _ = stack
    async with _make_client(provider) as client:
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "GPT OSS 120B",
                "stream": True,
                "messages": [{"role": "user", "content": "stream me"}],
            },
        ) as resp:
            assert resp.status_code == 200
            payloads = [
                line[len("data: "):]
                async for line in resp.aiter_lines()
                if line.startswith("data: ")
            ]

    assert payloads[-1] == "[DONE]"
    chunks = [json.loads(p) for p in payloads if p != "[DONE]"]
    # Responses are buffered (reflow-safe) then chunked; assert the reassembled
    # content is correct and it's a well-formed SSE stream.
    content_deltas = [
        c["choices"][0]["delta"]["content"]
        for c in chunks
        if c["choices"][0]["delta"].get("content")
    ]
    r = _parse("".join(content_deltas))
    assert (r.turn, r.text) == (1, "stream me")
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


async def test_conversation_resets_but_tab_is_reused(stack):
    """Each request starts a fresh thread (Turn 1) on the *same* reused tab."""
    _, provider, _ = stack
    async with _make_client(provider) as client:
        turns, tabs = [], []
        for msg in ("one", "two", "three"):
            r = _parse(await _content(client, msg))
            turns.append(r.turn)
            tabs.append(r.tab)
    # Conversation is reset every time -> always Turn 1 (no double-counting).
    assert turns == [1, 1, 1]
    # ...but the browser session (tab) is reused, not recreated.
    assert len(set(tabs)) == 1


@pytest.mark.parametrize("stack", [2], indirect=True)
async def test_pool_is_bounded_no_runaway_tabs(stack):
    """Serving many requests never opens more tabs than max_concurrency."""
    browser, provider, settings = stack
    # Pools are warmed lazily per provider on first use, so nothing exists yet.
    assert provider.name not in browser._pool_pages

    async with _make_client(provider) as client:
        tabs = set()
        steady_pages = None
        for i in range(6):
            r = _parse(await _content(client, f"msg {i}"))
            assert (r.turn, r.text) == (1, f"msg {i}")
            tabs.add(r.tab)
            if steady_pages is None:
                # First request warmed the pool; capture the steady-state count.
                steady_pages = len(browser.context.pages)
            else:
                # No tab leak: subsequent requests reuse, never open new tabs.
                assert len(browser.context.pages) == steady_pages

    # At most `max_concurrency` distinct tabs ever served requests.
    assert len(tabs) <= settings.max_concurrency
    assert len(browser._pool_pages[provider.name]) == settings.max_concurrency


async def test_model_switch_via_request(stack):
    """The requested model is selected in the UI dropdown before submitting."""
    _, provider, _ = stack
    async with _make_client(provider) as client:
        r = _parse(await _post(client, "hi", model="Nemotron 12B"))
    assert r.model == "Nemotron 12B"


async def test_web_search_toggle(stack):
    """`web_search` drives the toggle; default leaves it off."""
    _, provider, _ = stack
    async with _make_client(provider) as client:
        off = _parse(await _content(client, "a"))
        on = _parse(await _post(client, "b", web_search=True))
    assert off.web == "off"
    assert on.web == "on"


async def test_attachment_upload(stack):
    """A file content-part is decoded and uploaded via the hidden file input."""
    _, provider, _ = stack
    data_url = "data:text/plain;base64," + base64.b64encode(b"hello").decode()
    async with _make_client(provider) as client:
        content = await _post(
            client,
            [
                {"type": "text", "text": "read this"},
                {"type": "file", "file": {"filename": "note.txt", "file_data": data_url}},
            ],
        )
    assert _parse(content).files == 1


async def test_tab_recycled_on_failure(stack):
    """A tab whose lease raised is closed and replaced, not returned as-is."""
    browser, _, settings = stack
    async with browser.acquire() as lease:
        bad_page = lease.page  # capture the tab we'll poison

    # Simulate a wedged request: raise inside the lease.
    with pytest.raises(RuntimeError):
        async with browser.acquire() as lease:
            assert lease.page is bad_page  # single-tab pool hands it back
            raise RuntimeError("boom")

    # Replacement happens in a detached task; the next acquire waits for it,
    # so a later request is never left hanging on an empty pool.
    async with browser.acquire() as lease:
        assert lease.page is not bad_page
        assert not lease.page.is_closed()
    assert bad_page.is_closed()
    assert len(browser._pool_pages["default"]) == settings.max_concurrency


async def test_markdown_reflow_does_not_duplicate_tool_calls(tmp_path):
    """A mid-stream reflow must not cause the tool call to be captured twice."""
    settings = _settings(tmp_path, 1).model_copy(update={"expressai_base_url": REFLOW_URL})
    browser = BrowserManager(settings)
    try:
        await browser.start()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Browser unavailable: {exc}")
    provider = ExpressAIProvider(settings, browser)
    app = FastAPI()
    app.include_router(router)
    app.state.router = FakeRouter(provider)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            resp = await client.post("/v1/chat/completions", json={
                "model": "GPT OSS 120B",
                "messages": [{"role": "user", "content": "weather in Paris?"}],
                "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}],
            })
        calls = resp.json()["choices"][0]["message"]["tool_calls"]
        assert len(calls) == 1, [c["function"] for c in calls]
        assert calls[0]["function"]["name"] == "get_weather"
    finally:
        await browser.stop()


async def test_logged_out_is_detected(tmp_path):
    """A logged-out screen raises AuthenticationRequired and flips /health."""
    settings = _settings(tmp_path, 1)
    # Logout is concluded only after the composer fails to appear, so keep the
    # nav timeout short here for a fast test.
    settings = settings.model_copy(update={"expressai_base_url": LOGIN_URL, "nav_timeout_ms": 3000})
    browser = BrowserManager(settings)
    try:
        await browser.start()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Browser unavailable: {exc}")
    provider = ExpressAIProvider(settings, browser)
    try:
        request = ChatRequest(messages=[ChatMessage("user", "hi")], model="GPT OSS 120B")
        with pytest.raises(AuthenticationRequired):
            async for _ in provider.generate(request):
                pass
        assert provider.authenticated is False
        assert await provider.check_authentication() is False
    finally:
        await browser.stop()


async def test_pool_survives_request_cancellation(stack):
    """An aborted (cancelled) request must not shrink the pool."""
    browser, _, settings = stack

    async def aborted():
        async with browser.acquire():
            await asyncio.sleep(30)  # will be cancelled mid-lease

    task = asyncio.create_task(aborted())
    await asyncio.sleep(0.2)  # let it check out the tab
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The pool is replenished, so a fresh request acquires without hanging.
    async with browser.acquire() as lease:
        assert not lease.page.is_closed()
    assert len(browser._pool_pages["default"]) == settings.max_concurrency
