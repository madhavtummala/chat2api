"""ExpressAI provider — drives https://app.expressai.com via Playwright.

Streaming strategy: DOM polling. After submitting a prompt we watch the last
assistant message bubble grow and emit the newly-appended text as deltas until
the response stabilises (the "stop/generating" affordance disappears and the
text stops changing).

┌─────────────────────────────────────────────────────────────────────────┐
│ IMPORTANT: The CSS selectors below are best-effort guesses. Open the live │
│ site with devtools and update `Selectors` to match the real DOM. Every    │
│ site-specific assumption is isolated here so the rest of the codebase is   │
│ unaffected when they change.                                               │
└─────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from ..browser import BrowserManager
from ..config import Settings
from ..core.errors import AuthenticationRequired, ProviderError, ProviderTimeout
from ..core.messages import flatten_messages
from ..core.types import ChatRequest
from .base import BaseChatProvider

logger = logging.getLogger(__name__)

_LOGIN_HELP = (
    "ExpressAI is logged out (session expired or missing). Re-authenticate by "
    "running once with CHAT2API_HEADLESS=false and logging in, or provide a "
    "fresh CHAT2API_STORAGE_STATE export. Check GET /health for live auth state."
)


@dataclass(frozen=True)
class Selectors:
    """All ExpressAI DOM assumptions in one place. Tune against the live site."""

    # The composer where the user types a prompt.
    prompt_input: str = "textarea[placeholder='Ask anything']"
    # Explicit send button (disabled until the composer has text; Enter fallback).
    send_button: str = "button[title='Send message']"
    # Control that starts a brand-new empty conversation/thread.
    new_chat_button: str = "button[title='New Chat']"
    # Assistant bubbles are left-aligned (`mr-auto` .prose); the user turn is
    # neither, so this uniquely matches AI replies.
    assistant_message: str = ".mr-auto .prose"
    # While a reply is generating, the send button is replaced by a stop button,
    # so `send_button` matches nothing; it returns only when the reply is fully
    # done. That send->stop->send transition is our completion signal.
    # Something that only appears once the app is authenticated & ready.
    ready_marker: str = "textarea[placeholder='Ask anything']"
    # Signs of a logged-out screen. When signed out the composer is disabled with
    # a "Please sign in…" placeholder and a "Sign in with ExpressVPN" button
    # shows. (We deliberately avoid input[type=password]: the *logged-in* page
    # uses a password to unlock the encrypted-history vault.)
    login_marker: str = (
        "textarea[placeholder*='sign in' i], "
        "button:has-text('Sign in with ExpressVPN')"
    )
    # Model picker: the chevron button opens the list; options are role=button
    # rows. `{model}` is substituted with the requested model id.
    model_selector: str = "button:has(svg.lucide-chevron-down)"
    model_option: str = "[role='button']:has-text('{model}')"
    # Dialogs (model picker, upsell) render as a full-screen modal over a
    # `.fixed.inset-0` backdrop. This is the modal's close (×) button, scoped to
    # the backdrop so it never hits an unrelated icon elsewhere on the page.
    modal_close: str = ".fixed.inset-0 button:has(svg.lucide-x)"
    blocking_overlay: str = ".fixed.inset-0"
    # Web-search toggle button, and how to tell if it's currently active. The
    # real UI has no aria-pressed — the "on" state is the teal text colour
    # (text-[#0F866C]); the aria-pressed clause keeps the test mock working.
    web_search_toggle: str = "[data-testid='web-search-btn']"
    web_search_active: str = (
        "[data-testid='web-search-btn'][class*='#0F866C'], "
        "[data-testid='web-search-btn'][aria-pressed='true']"
    )
    # Hidden file input for attachments (accepts set_input_files directly).
    file_input: str = "input[type='file'][data-testid='file-input']"


class ExpressAIProvider(BaseChatProvider):
    name = "expressai"
    # Seed catalogue (used until list_models() discovers the live set). The
    # display names double as ids so select_model can match dropdown options.
    default_model = "GPT OSS 120B"
    available_models = (
        "GPT OSS 120B",
        "Nemotron 12B",
        "DeepSeek R1 Distill 32B",
        "Qwen2.5-VL 32B",
        "Qwen3.5 35B-A3B",
        "Gemma 4 31B",
    )
    supports_tools = True  # via text-based tool-call emulation
    supports_web_search = True
    supports_attachments = True
    # ExpressAI is incognito by default, so enable_incognito() stays the base no-op.

    def __init__(self, settings: Settings, browser: BrowserManager):
        super().__init__(settings, browser)
        self.base_url = settings.expressai_base_url
        self.selectors = Selectors()

    # -- lifecycle ---------------------------------------------------------
    async def startup(self) -> None:
        # Warm one tab so login state is verified before the first request.
        async with self.browser.acquire() as lease:
            await self._ensure_ready(lease.page)

    async def _ensure_ready(self, page: Page) -> None:
        """Navigate to the app (if needed) and confirm it is logged in & ready.

        The app server-renders a logged-out shell and resolves auth *client-side*
        during hydration, so the initial DOM always looks logged-out. We must
        therefore wait for the logged-in composer to appear rather than judging
        auth eagerly; only a timeout with the logout markers still present is a
        real logout.
        """
        if not page.url.startswith(self.base_url):
            await page.goto(self.base_url, wait_until="domcontentloaded")

        try:
            await page.locator(self.selectors.ready_marker).first.wait_for(
                state="visible", timeout=self.settings.nav_timeout_ms
            )
        except PlaywrightTimeout as exc:
            # Composer never appeared post-hydration -> genuinely logged out, or
            # (rarely) the selectors changed.
            if await self._is_logged_out(page):
                self._authenticated = False
                raise AuthenticationRequired(_LOGIN_HELP) from exc
            raise ProviderError(
                "ExpressAI chat UI did not become ready; the page layout may "
                "have changed (update Selectors in providers/expressai.py)."
            ) from exc

        self._authenticated = True
        # No-op for ExpressAI (already incognito); providers that persist chats
        # override enable_incognito() to flip the temporary-chat toggle.
        await self.enable_incognito(page)

    async def _is_logged_out(self, page: Page) -> bool:
        """True if the page is showing a login/sign-in screen or was redirected
        to an auth host."""
        url = page.url.lower()
        if any(hint in url for hint in ("/login", "/signin", "/sign-in", "/auth", "accounts.")):
            return True
        try:
            return bool(await page.locator(self.selectors.login_marker).count())
        except PlaywrightError:
            return False

    async def check_authentication(self) -> bool:
        """Actively probe login state (used by /health?deep=1)."""
        try:
            async with self.browser.acquire() as lease:
                await self._ensure_ready(lease.page)
            return True
        except AuthenticationRequired:
            return False
        except ProviderError:
            return bool(self._authenticated)  # inconclusive; keep last known

    async def _new_conversation(self, page: Page) -> None:
        """Reset the tab to an empty thread, preserving the logged-in session.

        Prefers an in-app "New chat" control (fast, no reload); falls back to a
        full navigation to the base URL if no such control is present.
        """
        new_chat = page.locator(self.selectors.new_chat_button)
        if await new_chat.count():
            await new_chat.first.click()
        else:
            await page.goto(self.base_url, wait_until="domcontentloaded")
        await page.locator(self.selectors.ready_marker).first.wait_for(
            state="visible", timeout=self.settings.nav_timeout_ms
        )

    # -- generation --------------------------------------------------------
    async def generate(self, request: ChatRequest) -> AsyncIterator[str]:
        prompt = flatten_messages(request.messages)
        async with self.browser.acquire() as lease:
            page = lease.page
            try:
                await self._ensure_ready(page)
                # The API is stateless — the full transcript is in `prompt` — so
                # each request runs in a fresh thread to avoid double-counting
                # history against whatever the tab last held.
                await self._new_conversation(page)
                await self.select_model(page, request.model)
                await self._set_web_search(page, request.web_search)
                await self._upload_attachments(page, request.attachments)
                await self._close_modals(page)  # nothing may block the composer
                await self._submit_prompt(page, prompt)
                text = await self._await_response(page)
                if text:
                    yield text
            except PlaywrightTimeout as exc:
                raise ProviderTimeout("Timed out waiting for ExpressAI.") from exc
            except PlaywrightError as exc:
                raise ProviderError(f"Browser automation failed: {exc}") from exc

    async def select_model(self, page: Page, model: str) -> None:
        """Switch models *only* when needed.

        No-op when the client didn't request a known model, or when it's already
        the selected one — so we never open the picker just to re-pick the model
        the UI already has. Always closes the dropdown if we opened it.
        """
        if not model or not self.supports_model(model):
            return  # unknown/unspecified -> use whatever the UI already has
        picker = page.locator(self.selectors.model_selector).first
        if not await picker.count():
            return
        current = (await picker.inner_text()).strip()
        if model.strip() in current:
            return  # already the selected model -> never open the picker
        try:
            await picker.click()  # opens the "Choose a model" modal
            option = page.locator(self.selectors.model_option.format(model=model)).first
            if await option.count():
                await option.click()
        except PlaywrightError:
            logger.debug("Model switch to %r failed; continuing", model, exc_info=True)
        finally:
            await self._close_modals(page)  # never leave the picker open

    async def _close_modals(self, page: Page) -> None:
        """Close any full-screen modal (model picker, upsell) blocking input."""
        for _ in range(3):
            if not await page.locator(self.selectors.blocking_overlay).count():
                return
            close = page.locator(self.selectors.modal_close).first
            try:
                if await close.count():
                    await close.click(timeout=2000)
                else:
                    await page.keyboard.press("Escape")
            except PlaywrightError:
                break
            await page.wait_for_timeout(300)

    # ExpressAI uses the static `available_models` catalogue — we deliberately do
    # NOT override list_models() to scrape the picker, since that would open the
    # model modal at startup. (Kept static: the catalogue rarely changes.)

    async def _set_web_search(self, page: Page, enabled: bool) -> None:
        """Toggle the web-search button to match the requested state."""
        toggle = page.locator(self.selectors.web_search_toggle).first
        if not await toggle.count():
            return
        active = bool(await page.locator(self.selectors.web_search_active).count())
        if active != enabled:
            await toggle.click()

    async def _upload_attachments(self, page: Page, attachments) -> None:
        if not attachments:
            return
        file_input = page.locator(self.selectors.file_input).first
        if not await file_input.count():
            logger.warning("ExpressAI: no file input found; skipping %d attachment(s)", len(attachments))
            return
        await file_input.set_input_files(
            [{"name": a.name, "mimeType": a.mime, "buffer": a.data} for a in attachments]
        )
        # Give the UI a moment to register/preview the uploads before sending.
        await page.wait_for_timeout(500)

    async def _submit_prompt(self, page: Page, prompt: str) -> None:
        composer = page.locator(self.selectors.prompt_input).first
        await composer.click()
        await composer.fill(prompt)

        # The send button starts disabled and enables once React registers the
        # input. Playwright's click auto-waits for it to become enabled; only
        # fall back to Enter if the button is genuinely absent or never enables.
        send = page.locator(self.selectors.send_button).first
        if await send.count():
            try:
                await send.click(timeout=5000)
                return
            except PlaywrightTimeout:
                logger.debug("Send button never enabled; falling back to Enter")
        await composer.press("Enter")

    async def _await_response(self, page: Page) -> str:
        """Wait for the whole reply to finish, then return its final text.

        We don't stream token-by-token: the rendered markdown (.prose) reflows as
        it generates, so incremental reads duplicate or corrupt content (e.g.
        tool-call blocks). Completion is detected from the send->stop->send
        button toggle — the send button is absent (a stop button) for the entire
        generation, including between blocks (paragraph/table/…), and returns
        only when the reply is fully done. Text stability is a secondary guard.
        """
        deadline = time.monotonic() + self.settings.response_timeout_s
        poll = max(self.settings.poll_interval_s, 0.15)
        send = page.locator(self.selectors.send_button)
        bubbles = page.locator(self.selectors.assistant_message)

        last = ""
        stable_ticks = 0
        while time.monotonic() < deadline:
            generating = await send.count() == 0  # send button is a stop button
            current = (await bubbles.last.inner_text()).strip() if await bubbles.count() else ""
            if not generating and current and current == last:
                stable_ticks += 1
                if stable_ticks >= 2:
                    return current
            else:
                stable_ticks = 0
            last = current
            await asyncio.sleep(poll)

        raise ProviderTimeout("ExpressAI response did not complete in time.")
