"""Offline tests for GoogleAIModeProvider against local mock pages.

These drive a real headless Chromium through the same BrowserManager used in
production, pointed at ``tests/assets/mock_google*.html`` — no network, no
Google. They exist to pin the provider's observable behaviour (what it yields,
what it raises) so its internals can be refactored safely; the only live test
(``test_live_google.py``) is opt-in and skipped by default.

Skipped automatically if Chromium is not installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.browser import BrowserManager
from src.config import Settings
from src.core.errors import ProviderError, ProviderTimeout
from src.core.types import ChatMessage, ChatRequest
from src.providers.google_aimode import GoogleAIModeProvider

ASSETS = Path(__file__).parent / "assets"


def _url(name: str) -> str:
    # The prompt is appended to this, so a trailing `?` keeps it out of the path.
    return f"{(ASSETS / name).resolve().as_uri()}?q="


def _settings(tmp_path, page: str, timeout: float = 20.0) -> Settings:
    return Settings(
        headless=True,
        provider="googleaimode",
        user_data_dir=str(tmp_path / "profile"),
        max_concurrency=1,
        nav_timeout_ms=15_000,
        googleaimode_search_url=_url(page),
        response_timeout_s=timeout,
        poll_interval_s=0.05,
        api_keys="",
    )


async def _run(tmp_path, page: str, timeout: float = 20.0) -> list[str]:
    settings = _settings(tmp_path, page, timeout)
    browser = BrowserManager(settings)
    try:
        await browser.start()
    except Exception as exc:  # chromium missing / cannot launch
        pytest.skip(f"Browser unavailable: {exc}")
    provider = GoogleAIModeProvider(settings, browser)
    request = ChatRequest(messages=[ChatMessage("user", "what is a pi")], model="m")
    try:
        return [delta async for delta in provider.generate(request)]
    finally:
        await browser.stop()


@pytest.mark.asyncio
async def test_yields_settled_answer_as_markdown(tmp_path):
    """One delta, emitted only once the streaming answer has stopped growing."""
    deltas = await _run(tmp_path, "mock_google.html")

    assert len(deltas) == 1, "answer is buffered, not streamed token-by-token"
    answer = deltas[0]
    # Every chunk arrived — settle detection did not fire early.
    assert "Raspberry Pi" in answer
    assert "Runs Linux" in answer
    assert "GPIO" in answer


@pytest.mark.asyncio
async def test_strips_echo_marker_and_footer_chrome(tmp_path):
    answer = (await _run(tmp_path, "mock_google.html"))[0]

    assert "You said:" not in answer
    assert "AI can make mistakes" not in answer
    assert "Check important info" not in answer


@pytest.mark.asyncio
async def test_echoed_prompt_currently_leaks_into_the_answer(tmp_path):
    """Documents a known defect, so the refactor can be proven behaviour-neutral.

    ``_extract_answer`` cuts up to and including the ``You said:`` marker but not
    the prompt that follows it, so every answer is prefixed with the user's own
    question. The separate no-marker path (searching for the prompt itself) does
    strip it — the two branches disagree. Fix pending; see the report.
    """
    answer = (await _run(tmp_path, "mock_google.html"))[0]

    assert answer.startswith("what is a pi")


@pytest.mark.asyncio
async def test_preserves_markdown_formatting(tmp_path):
    """innerHTML -> Markdown, not inner_text: lists and links must survive."""
    answer = (await _run(tmp_path, "mock_google.html"))[0]

    assert "Raspberry Pi" in answer
    assert "- Runs Linux" in answer or "* Runs Linux" in answer
    assert "**small**" in answer
    assert "https://example.com/gpio" in answer


@pytest.mark.asyncio
async def test_bot_wall_raises_provider_error(tmp_path):
    with pytest.raises(ProviderError) as exc:
        await _run(tmp_path, "mock_google_blocked.html")
    assert "blocked" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_no_answer_text_raises_timeout(tmp_path):
    with pytest.raises(ProviderTimeout):
        await _run(tmp_path, "mock_google_empty.html", timeout=1.0)
