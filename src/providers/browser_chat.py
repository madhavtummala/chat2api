"""Shared base for browser-driven chat providers.

Driving a chat web UI is mostly generic: navigate, verify login, start a fresh
thread, optionally pick a model / toggle web search / attach files, submit the
prompt, and read the settled reply. All of that lives here, parameterised by a
:class:`Selectors` block. Onboarding a new provider is then usually just:

    class FooProvider(BrowserChatProvider):
        name = "foo"
        selectors = Selectors(prompt_input="…", send_button="…", …)
        def __init__(self, settings, browser):
            super().__init__(settings, browser)
            self.base_url = settings.foo_base_url

Override a method only when a site genuinely differs (e.g. a custom completion
signal or an incognito toggle). Optional selectors left as ``""`` disable that
capability (and its `supports_*` flag should be False).
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from ..core.errors import AuthenticationRequired, ProviderError, ProviderTimeout
from ..core.markdown import html_to_markdown
from ..core.messages import flatten_messages
from ..core.types import ChatRequest
from .base import BaseChatProvider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Selectors:
    """CSS/Playwright selectors for one chat site. All site-specific assumptions
    live here so the rest of the codebase is unaffected when the DOM changes."""

    # -- essential (every provider sets these) ----------------------------
    prompt_input: str = ""          # the composer
    send_button: str = ""           # submit control (Enter is the fallback)
    assistant_message: str = ""     # the reply container; `.last` is the newest
    ready_marker: str = ""          # present once the composer is usable
    login_marker: str = ""          # present on a logged-out / sign-in screen
    logged_in_marker: str = ""      # present only when logged in (e.g. avatar)

    # -- optional (leave "" to disable that capability) -------------------
    new_chat_button: str = ""       # start a fresh thread
    # Present while a reply is generating. If empty, we fall back to "the send
    # button disappeared" (it becomes a stop button) as the generating signal.
    generating_indicator: str = ""
    model_selector: str = ""        # opens the model picker; shows the current model
    model_option: str = "[role='option']:has-text('{model}')"  # {model} substituted
    modal_close: str = ""           # closes a blocking modal (e.g. model picker)
    blocking_overlay: str = ""      # a full-screen overlay that blocks input
    web_search_toggle: str = ""     # web-search on/off button
    web_search_active: str = ""     # matches only when web search is ON
    file_input: str = ""            # hidden <input type=file> for attachments


class BrowserChatProvider(BaseChatProvider):
    #: Real chat boxes hold context across messages, so a persistent-thread
    #: agentic loop can send the system prompt once + deltas (see open_session).
    supports_thread_continuation: bool = True
    #: Root URL of the chat app (subclasses usually set this from settings).
    base_url: str = ""
    #: Site selectors (subclass overrides).
    selectors: Selectors = Selectors()
    #: URL substrings indicating a logged-out / redirected-to-auth state.
    login_url_hints: tuple[str, ...] = ("/login", "/signin", "/sign-in", "/auth", "accounts.")
    #: Whether login is required to chat at all. When True (e.g. ExpressAI), a
    #: logged-out session is an error. When False (e.g. Perplexity), the chat box
    #: works anonymously and login just unlocks more models/credits/features —
    #: we run anyway and only *report* the login state on /health.
    requires_login: bool = True

    def login_help(self) -> str:
        return (
            f"{self.name} is logged out (session expired or missing). Re-authenticate "
            "by running once with CHAT2API_HEADLESS=false and logging in, or provide a "
            "fresh CHAT2API_STORAGE_STATE. Check GET /health for live auth state."
        )

    # -- lifecycle ---------------------------------------------------------
    async def startup(self) -> None:
        # Warm one tab so login state is verified before the first request.
        async with self.browser.acquire(self.name) as lease:
            await self._ensure_ready(lease.page)

    async def _ensure_ready(self, page: Page) -> None:
        """Navigate (if needed) and confirm the chat is usable; record login state.

        We wait for the *composer* (chat usable) rather than judging auth eagerly
        — many SPAs server-render a logged-out shell and resolve auth client-side
        during hydration. On login-*required* sites the composer only appears
        when logged in; on optional-login sites it appears either way, so login
        state is tracked separately and being anonymous is not an error.
        """
        if self.base_url and not page.url.startswith(self.base_url):
            await page.goto(self.base_url, wait_until="domcontentloaded")
        try:
            await page.locator(self.selectors.ready_marker).first.wait_for(
                state="visible", timeout=self.settings.nav_timeout_ms
            )
        except PlaywrightTimeout as exc:
            if self.requires_login and await self._is_logged_out(page):
                self._authenticated = False
                raise AuthenticationRequired(self.login_help()) from exc
            raise ProviderError(
                f"{self.name} chat UI did not become ready; the page layout may have "
                f"changed (update Selectors in providers/{self.name}.py)."
            ) from exc

        # Chat is usable. Now resolve login state (informational for /health).
        if self.requires_login:
            self._authenticated = True  # composer is gated on login -> logged in
        else:
            self._authenticated = await self._logged_in_state(page)
            if self._authenticated is False:
                logger.info(
                    "%s: not logged in — running anonymously (fewer models/credits/features)",
                    self.name,
                )
        await self.enable_incognito(page)  # no-op unless a provider overrides it

    async def _is_logged_out(self, page: Page) -> bool:
        url = page.url.lower()
        if any(hint in url for hint in self.login_url_hints):
            return True
        if not self.selectors.login_marker:
            return False
        try:
            return bool(await page.locator(self.selectors.login_marker).count())
        except PlaywrightError:
            return False

    async def _logged_in_state(self, page: Page) -> bool | None:
        """True if logged in, False if logged out, None if indeterminate."""
        try:
            if self.selectors.logged_in_marker and await page.locator(
                self.selectors.logged_in_marker
            ).count():
                return True
            if await self._is_logged_out(page):
                return False
        except PlaywrightError:
            pass
        return None

    async def check_authentication(self) -> bool | None:
        try:
            async with self.browser.acquire(self.name) as lease:
                await self._ensure_ready(lease.page)
            return self._authenticated
        except AuthenticationRequired:
            return False
        except ProviderError:
            return self._authenticated  # inconclusive; keep last known

    async def _new_conversation(self, page: Page) -> None:
        """Reset the tab to a fresh thread, preserving the logged-in session."""
        btn = page.locator(self.selectors.new_chat_button) if self.selectors.new_chat_button else None
        if btn and await btn.count():
            await btn.first.click()
        elif self.base_url:
            await page.goto(self.base_url, wait_until="domcontentloaded")
        await page.locator(self.selectors.ready_marker).first.wait_for(
            state="visible", timeout=self.settings.nav_timeout_ms
        )

    # -- generation --------------------------------------------------------
    async def generate(self, request: ChatRequest) -> AsyncIterator[str]:
        # A single stateless turn is just a one-message session: a fresh thread
        # with the full transcript in `request`. Delegate so the setup/submit/
        # read logic lives in exactly one place (see BrowserChatSession).
        async with self.open_session() as session:
            async for delta in session.send(request):
                yield delta

    @asynccontextmanager
    async def open_session(self) -> AsyncIterator["BrowserChatSession"]:
        """Borrow one tab for a whole conversation.

        The first :meth:`BrowserChatSession.send` opens a fresh thread and does
        all the per-conversation setup (model, thinking, web-search); subsequent
        sends reuse that same thread and submit only the new message, letting the
        chat box's own context carry the history. Used by the Responses agentic
        loop to send the system prompt once and then deltas; ``generate`` uses it
        for a single stateless turn.
        """
        async with self.browser.acquire(self.name) as lease:
            yield BrowserChatSession(self, lease.page)

    async def select_model(self, page: Page, model: str) -> None:
        """Switch models only when needed: skip if unknown, unsupported, or
        already selected — so we never open the picker unnecessarily. Always
        closes the picker if we opened it."""
        if not model or not self.supports_model(model) or not self.selectors.model_selector:
            return
        picker = page.locator(self.selectors.model_selector).first
        if not await picker.count():
            return
        if model.strip() in (await picker.inner_text()).strip():
            return  # already the selected model
        try:
            await picker.click()
            option = page.locator(self.selectors.model_option.format(model=model)).first
            if await option.count():
                await option.click()
        except PlaywrightError:
            logger.debug("Model switch to %r failed; continuing", model, exc_info=True)
        finally:
            await self._close_modals(page)

    async def _close_modals(self, page: Page) -> None:
        if not self.selectors.blocking_overlay:
            return
        for _ in range(3):
            if not await page.locator(self.selectors.blocking_overlay).count():
                return
            close = page.locator(self.selectors.modal_close).first if self.selectors.modal_close else None
            try:
                if close and await close.count():
                    await close.click(timeout=2000)
                else:
                    await page.keyboard.press("Escape")
            except PlaywrightError:
                break
            await page.wait_for_timeout(300)

    async def _set_thinking(self, page: Page, request: ChatRequest) -> None:
        """Apply the request's reasoning/"thinking" preference. No-op unless a
        provider overrides it — only Perplexity's web UI exposes such a toggle."""

    async def _set_web_search(self, page: Page, enabled: bool) -> None:
        if not self.selectors.web_search_toggle:
            return
        toggle = page.locator(self.selectors.web_search_toggle).first
        if not await toggle.count():
            return
        active = bool(
            self.selectors.web_search_active
            and await page.locator(self.selectors.web_search_active).count()
        )
        if active != enabled:
            await toggle.click()

    async def _upload_attachments(self, page: Page, attachments) -> None:
        if not attachments:
            return
        if not self.selectors.file_input:
            logger.warning("%s: no file_input selector; skipping %d attachment(s)", self.name, len(attachments))
            return
        file_input = page.locator(self.selectors.file_input).first
        if not await file_input.count():
            logger.warning("%s: file input not found; skipping attachments", self.name)
            return
        await file_input.set_input_files(
            [{"name": a.name, "mimeType": a.mime, "buffer": a.data} for a in attachments]
        )
        await page.wait_for_timeout(500)  # let the UI register the uploads

    async def _submit_prompt(self, page: Page, prompt: str) -> None:
        composer = page.locator(self.selectors.prompt_input).first
        await composer.click()
        await composer.fill(prompt)
        # Send buttons are often disabled until input registers; Playwright's
        # click auto-waits for enabled. Fall back to Enter if absent/never-enables.
        if self.selectors.send_button:
            send = page.locator(self.selectors.send_button).first
            if await send.count():
                try:
                    await send.click(timeout=5000)
                    return
                except PlaywrightTimeout:
                    logger.debug("Send button never enabled; falling back to Enter")
        await composer.press("Enter")

    async def _is_generating(self, page: Page) -> bool:
        """Whether a reply is still being generated.

        Uses `generating_indicator` if provided; otherwise falls back to "the
        send button disappeared" (it becomes a stop button during generation).
        """
        try:
            if self.selectors.generating_indicator:
                return bool(await page.locator(self.selectors.generating_indicator).count())
            if self.selectors.send_button:
                return await page.locator(self.selectors.send_button).count() == 0
        except PlaywrightError:
            pass
        return False

    async def _await_response(self, page: Page, previous_count: int = 0) -> str:
        """Wait for the whole reply to finish, then return its final text.

        We buffer rather than stream token-by-token: rendered markdown reflows as
        it generates, so incremental reads duplicate/corrupt content (e.g.
        tool-call blocks). Completion = not generating AND the text has settled.

        ``previous_count`` is how many answer bubbles existed *before* this turn
        was submitted. In a continued thread the prior turns' answers are still
        on the page, so we ignore everything until a *new* bubble appears —
        otherwise we'd immediately return the previous turn's (already-settled)
        reply. For a fresh thread this is 0 and has no effect.
        """
        deadline = time.monotonic() + self.settings.response_timeout_s
        poll = max(self.settings.poll_interval_s, 0.15)
        bubbles = page.locator(self.selectors.assistant_message)

        last = ""
        stable_ticks = 0
        while time.monotonic() < deadline:
            if await bubbles.count() <= previous_count:
                await asyncio.sleep(poll)  # our answer hasn't appeared yet
                continue
            generating = await self._is_generating(page)
            current = await self._reply_text(bubbles)
            if not generating and current and current == last:
                stable_ticks += 1
                if stable_ticks >= 2:
                    return current
            else:
                stable_ticks = 0
            last = current
            await asyncio.sleep(poll)

        raise ProviderTimeout(f"{self.name} response did not complete in time.")

    async def _reply_text(self, bubbles) -> str:
        """Read the newest reply as Markdown.

        We read the answer's ``innerHTML`` and reconstruct Markdown (see
        :func:`html_to_markdown`) rather than ``inner_text`` — the latter drops
        lists, headings, emphasis and inline reference URLs, so the client would
        get flattened prose instead of the formatting the site actually renders.
        Override to strip site-specific UI chrome before conversion."""
        if not await bubbles.count():
            return ""
        return html_to_markdown(await bubbles.last.inner_html())


class BrowserChatSession:
    """A conversation bound to one tab (see ``BrowserChatProvider.open_session``).

    The first :meth:`send` opens a fresh thread and does the per-conversation
    setup; later sends reuse that thread and submit only the message they're
    given, so the chat box's own context supplies the history. All the browser
    work is the provider's — this just tracks whether the thread is open and
    turns Playwright failures into provider errors, exactly like the old
    single-shot ``generate`` did.
    """

    def __init__(self, provider: BrowserChatProvider, page: Page):
        self._p = provider
        self._page = page
        self._started = False

    async def send(self, request: ChatRequest) -> AsyncIterator[str]:
        p, page = self._p, self._page
        prompt = flatten_messages(request.messages)
        try:
            if not self._started:
                await p._ensure_ready(page)
                await p._new_conversation(page)  # fresh thread for this session
                await p.select_model(page, request.model)
                await p._set_thinking(page, request)
                await p._set_web_search(page, request.web_search)
                self._started = True
            # Attachments belong to whatever turn carries them; upload each time.
            await p._upload_attachments(page, request.attachments)
            await p._close_modals(page)  # nothing may block the composer
            bubbles = page.locator(p.selectors.assistant_message)
            previous = await bubbles.count()  # ignore prior turns' answers
            await p._submit_prompt(page, prompt)
            text = await p._await_response(page, previous)
            if text:
                yield text
        except PlaywrightTimeout as exc:
            raise ProviderTimeout(f"Timed out waiting for {p.name}.") from exc
        except PlaywrightError as exc:
            raise ProviderError(f"Browser automation failed: {exc}") from exc
