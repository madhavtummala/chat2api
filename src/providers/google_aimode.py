"""Google AI Mode provider — drives Google Search's AI Mode (``udm=50``).

This backend is **auth-free**: navigating to
``https://www.google.com/search?udm=50&q=<prompt>`` returns an AI-generated
answer that streams into the page, with no login or API key. We poll the answer
region, strip the query echo and UI boilerplate, and emit the growing text as
deltas until it stabilises.

Caveats (see README): Google's markup is obfuscated and varies between requests,
and heavy automated use can trigger throttling/CAPTCHA. Great for demos and
opt-in live tests; not a stable CI dependency. All the brittle, site-specific
bits are isolated in ``Selectors``/``_extract_answer`` below.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncIterator
from urllib.parse import quote_plus

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from ..core.errors import ProviderError, ProviderTimeout
from ..core.messages import flatten_messages
from ..core.types import ChatRequest
from .base import BaseChatProvider

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.google.com/search?udm=50&q="

# Text that marks the end of the answer / start of UI chrome, cut off if seen.
_FOOTER_MARKERS = (
    "AI can make mistakes",
    "AI responses may include mistakes",
    "AI Mode can make mistakes",
    "Check important info",
    "Sources and related content",
)
# Leading UI/nav text that precedes the answer.
_ECHO_MARKERS = ("you said:", "You said:")


@dataclass(frozen=True)
class Selectors:
    # Candidate containers holding the streaming answer, tried in order.
    answer_containers: tuple[str, ...] = (
        "div[data-subtree='aimc']",
        "#main",
        "#rso",
        "div[role='main']",
    )
    # Cookie/consent "accept" buttons (best-effort dismissal).
    consent_accept: str = "button:has-text('Accept all'), button:has-text('I agree'), button[aria-label*='Accept all' i]"
    # A sign that Google blocked us with a bot check.
    blocked_marker: str = "form#captcha-form, div:has-text('unusual traffic')"


class GoogleAIModeProvider(BaseChatProvider):
    name = "googleaimode"
    default_model = "google-ai-mode"
    available_models = ("google-ai-mode",)
    supports_tools = False  # AI Mode won't reliably honour our tool preamble

    def __init__(self, settings, browser):
        super().__init__(settings, browser)
        self.selectors = Selectors()

    async def generate(self, request: ChatRequest) -> AsyncIterator[str]:
        prompt = flatten_messages(request.messages)
        url = SEARCH_URL + quote_plus(prompt)
        async with self.browser.acquire() as lease:
            page = lease.page
            try:
                await page.goto(url, wait_until="domcontentloaded")
                await self._dismiss_consent(page)
                if await page.locator(self.selectors.blocked_marker).count():
                    raise ProviderError(
                        "Google blocked the automated request (CAPTCHA / unusual "
                        "traffic). Retry later or run non-headless."
                    )
                async for delta in self._stream_answer(page, prompt):
                    yield delta
            except PlaywrightTimeout as exc:
                raise ProviderTimeout("Timed out waiting for Google AI Mode.") from exc
            except PlaywrightError as exc:
                raise ProviderError(f"Browser automation failed: {exc}") from exc

    async def _dismiss_consent(self, page: Page) -> None:
        try:
            btn = page.locator(self.selectors.consent_accept).first
            if await btn.count():
                await btn.click(timeout=3000)
                await page.wait_for_load_state("domcontentloaded")
        except PlaywrightError:
            pass  # no consent gate, or it vanished — fine

    async def _container(self, page: Page):
        """Return the first answer container that currently exists on the page."""
        for selector in self.selectors.answer_containers:
            loc = page.locator(selector).first
            if await loc.count():
                return loc
        return page.locator("body").first

    async def _stream_answer(self, page: Page, prompt: str) -> AsyncIterator[str]:
        deadline = time.monotonic() + self.settings.response_timeout_s
        poll = self.settings.poll_interval_s

        emitted = ""
        stable_ticks = 0
        saw_text = False
        while time.monotonic() < deadline:
            container = await self._container(page)
            try:
                raw = await container.inner_text()
            except PlaywrightError:
                await asyncio.sleep(poll)
                continue

            answer = _extract_answer(raw, prompt)
            if answer and answer != emitted:
                saw_text = True
                if answer.startswith(emitted):
                    yield answer[len(emitted):]
                else:
                    yield "\n" + answer
                emitted = answer
                stable_ticks = 0
            elif saw_text:
                # Text stopped growing — treat sustained stability as "done".
                stable_ticks += 1
                if stable_ticks >= 4:  # ~4 * poll seconds of no change
                    return
            await asyncio.sleep(poll)

        if not saw_text:
            raise ProviderTimeout("Google AI Mode produced no answer text.")


def _extract_answer(raw: str, prompt: str) -> str:
    """Pull just the answer prose out of the noisy container text."""
    text = raw

    # Drop everything up to and including the echoed prompt / "you said:".
    lowered = text.lower()
    cut = -1
    for marker in _ECHO_MARKERS:
        idx = lowered.find(marker.lower())
        if idx != -1:
            cut = max(cut, idx + len(marker))
    if cut == -1:
        idx = lowered.find(prompt.lower())
        if idx != -1:
            cut = idx + len(prompt)
    if cut != -1:
        text = text[cut:]

    # Drop trailing UI boilerplate.
    lowered = text.lower()
    for marker in _FOOTER_MARKERS:
        idx = lowered.find(marker.lower())
        if idx != -1:
            text = text[:idx]
            lowered = text.lower()

    return text.strip()
