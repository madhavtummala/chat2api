"""Opt-in live test against real Google AI Mode (auth-free, but flaky).

Runs only when ``RUN_LIVE_GOOGLE=1`` — it hits google.com with a real headless
browser, so it's kept out of the default suite (Google's DOM shifts and heavy
automated use triggers throttling/CAPTCHA). If Google blocks the request, the
test *skips* rather than fails, since that's an environmental condition.

    RUN_LIVE_GOOGLE=1 pytest tests/test_live_google.py -v -s
"""

from __future__ import annotations

import os

import pytest

from src.browser import BrowserManager
from src.config import Settings
from src.core.errors import ProviderError
from src.core.types import ChatMessage, ChatRequest
from src.providers.google_aimode import GoogleAIModeProvider

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_GOOGLE") != "1",
    reason="set RUN_LIVE_GOOGLE=1 to run the live Google AI Mode test",
)


@pytest.fixture
async def google(tmp_path):
    settings = Settings(
        headless=True,
        user_data_dir=str(tmp_path / "profile"),
        max_concurrency=1,
        response_timeout_s=60,
        poll_interval_s=0.25,
    )
    browser = BrowserManager(settings)
    await browser.start()
    try:
        yield GoogleAIModeProvider(settings, browser)
    finally:
        await browser.stop()


async def test_capital_of_japan(google):
    request = ChatRequest(
        messages=[ChatMessage("user", "In one word, what is the capital of Japan?")],
        model="google-ai-mode",
    )
    parts: list[str] = []
    try:
        async for delta in google.generate(request):
            parts.append(delta)
    except ProviderError as exc:
        if "blocked" in str(exc).lower():
            pytest.skip(f"Google throttled the request: {exc}")
        raise

    answer = "".join(parts)
    assert answer.strip(), "expected a non-empty answer"
    assert "tokyo" in answer.lower(), f"unexpected answer: {answer!r}"
