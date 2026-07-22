"""Perplexity provider — drives https://www.perplexity.ai via Playwright.

All browser-driving logic is inherited from :class:`BrowserChatProvider`; this
file is just Perplexity's `Selectors` + capabilities. Selectors below were
verified against the live perplexity.ai DOM. Re-derive with
`python scripts/inspect_provider.py https://www.perplexity.ai` if the layout
changes.

How Perplexity differs from ExpressAI (all handled transparently):
  * Web search is *native* and always on — there is no toggle to drive, so
    ``supports_web_search`` is False and any ``web_search`` request flag is a
    harmless no-op (search happens regardless).
  * Chats are saved to the account's library by default, so we self-enable
    incognito (see :meth:`enable_incognito`) to keep proxied chats out of it.
  * Tools/MCP work the same as any other provider (text-based ``<tool_call>``
    emulation); a client just wouldn't send a *web-search* tool since it's native.
  * Threads are resumable — the URL changes per conversation and incognito
    sessions persist ~24h — but we don't rely on that: like ExpressAI, every
    request runs statelessly in a fresh thread with the full transcript resent.
"""

from __future__ import annotations

import logging
from weakref import WeakSet

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from ..browser import BrowserManager
from ..config import Settings
from ..core.types import ChatRequest
from .browser_chat import BrowserChatProvider, Selectors

logger = logging.getLogger(__name__)

# Enables incognito ("temporary") sessions so proxied chats aren't saved to the
# account's history / library. Only rendered when logged in (top-right header).
_INCOGNITO_BUTTON = "button[aria-label^='Use incognito']"

_SELECTORS = Selectors(
    # The composer is a Lexical contenteditable (NOT a textarea) with a stable id.
    # aria-placeholder varies ("Ask anything" / "Ask a follow-up" / "Type @ for
    # connectors" when logged in); the id is stable across all, so key off it.
    prompt_input="#ask-input",
    ready_marker="#ask-input",
    # Submit control (arrow-up), present ONLY while the composer has text — an
    # empty composer shows a "Use voice mode" button in its place. Enter is the
    # fallback. NOTE: because Submit is absent when empty, we can't use the
    # "Submit disappeared" trick for completion; we use `generating_indicator`.
    send_button="button[aria-label='Submit']",
    # A Stop button replaces Submit while the answer streams — our completion
    # signal (present == generating). Best-effort label match; if it never
    # matches, completion falls back to text-settling in `_await_response`.
    generating_indicator="button[aria-label*='Stop' i]",
    # Start a fresh thread: the sidebar "New" link (href="/"). Falls back to
    # navigating to base_url if absent.
    new_chat_button="a[aria-label='New']",
    # The answer body: Perplexity renders each answer's markdown in a prose block
    # tagged data-renderer="lm". `.last` picks the newest answer.
    assistant_message=".prose[data-renderer='lm']",
    # Logged-out only: the "Login or sign up" popup and a "Sign In" sidebar button
    # appear when anonymous; both vanish once logged in.
    login_marker="[data-testid='login-modal'], button:has-text('Sign In')",
    # Logged-in only: the sidebar profile avatar (verified against a logged-in
    # session). Drives auth reporting on /health.
    logged_in_marker="img[alt='Profile avatar']",
    # Attachments: hidden multi-file input.
    file_input="input[type='file']",
    # Model picker trigger. Its aria-label is NOT stable — it reads "Model" on
    # the Best default but becomes the model's name once one is picked — so we
    # identify it structurally instead: the composer's only menu-trigger that
    # isn't the mode toggle (has aria-pressed) or the "+" add-files button.
    # The menu it opens is a Radix dropdown of `[role='menuitemradio']` items
    # whose name is a `span[translate='no']`; locked (Max) models are
    # `[role='menuitem']` with a lock and aren't pickable. See select_model.
    model_selector=(
        "[data-ask-input-container='true'] button[aria-haspopup='menu']"
        ":not([aria-pressed]):not([aria-label='Add files or tools'])"
    ),
    # (No Search/Computer mode toggle to drive — search is native/always on.)
)

# Selectable model names live in these radio items; the Max-tier `[role=menuitem]`
# entries (with a lock) are excluded because this account can't pick them.
_MODEL_NAME = "[role='menuitemradio'] span[translate='no']"
# The "Thinking" toggle for reasoning-capable models appears in the same picker
# menu as a `menuitemcheckbox` whose control is a `role=switch` button. It's
# absent for non-reasoning models and `disabled` when a model forces it on.
_THINKING_SWITCH = "[role='menuitemcheckbox'] button[role='switch']"


class PerplexityProvider(BrowserChatProvider):
    name = "perplexity"
    # "Best" is Perplexity's own auto-router and the on-load default. When a
    # client omits `model` we leave it here (no picker interaction); a specific
    # model name switches via the dropdown, exactly like ExpressAI.
    #
    # Static catalogue (the selectable, non-Max models as shown in the picker).
    # Perplexity renames/rotates these periodically — update this tuple when the
    # dropdown changes (`select_model` matches these names exactly in the menu).
    default_model = "Best"
    available_models = (
        "Best",
        "Sonar 2",
        "GPT-5.6 Terra",
        "Gemini 3.1 Pro",
        "Claude Sonnet 5",
        "GLM 5.2",
        "Kimi K2.6",
        "Nemotron 3 Ultra",
    )
    #  - tools: same text-based <tool_call> emulation as every other provider,
    #    so clients can supply their own (custom / MCP) tools.
    #  - web_search: native and always on — there's no toggle to drive.
    #  - attachments: it supports file upload.
    supports_tools = True
    supports_web_search = False
    supports_attachments = True
    # Perplexity's chat box works anonymously; login just unlocks more
    # models/credits/features. So we run either way and only report login state.
    requires_login = False
    selectors = _SELECTORS

    def __init__(self, settings: Settings, browser: BrowserManager):
        super().__init__(settings, browser)
        self.base_url = settings.perplexity_base_url
        # Tabs we've already switched into incognito. `_ensure_ready` (hence
        # enable_incognito) runs on every request, but the toggle is sticky per
        # tab, so we must flip it at most once per tab or we'd toggle it back OFF.
        self._incognito_pages: "WeakSet[Page]" = WeakSet()

    async def enable_incognito(self, page: Page) -> None:
        """Turn on incognito so proxied chats aren't saved to history.

        The control only lives in the home-page header and only when logged in
        (anonymous sessions aren't saved anyway). It's a sticky, session-wide
        toggle with no reliable on/off marker in the DOM, so we click it exactly
        once per tab and assume it starts OFF — the norm for a freshly launched
        profile. If you enable incognito manually in the same profile first, this
        would turn it back off.
        """
        if page in self._incognito_pages:
            return
        if self.authenticated is not True:
            return  # anonymous: no incognito control, and nothing is saved anyway
        try:
            # Briefly wait: the header button renders alongside the composer, and
            # we only get this chance on the home page (thread pages omit it).
            btn = page.locator(_INCOGNITO_BUTTON).first
            await btn.wait_for(state="visible", timeout=5000)
            await btn.click()
            self._incognito_pages.add(page)
            logger.info("perplexity: enabled incognito for this tab")
        except PlaywrightError:
            logger.debug("perplexity: incognito control unavailable", exc_info=True)

    # -- model selection --------------------------------------------------
    # Models are a static catalogue (see available_models); we don't scrape the
    # picker at startup — only switch when a client asks for a specific one.
    async def select_model(self, page: Page, model: str) -> None:
        """Switch the picker to ``model`` (skip if it's already selected).

        Blank/unknown models are left as-is (validate_model already rejects
        explicit unknowns upstream). The menu closes on a successful pick; on
        failure we press Escape so a stray dropdown never blocks the composer.
        """
        if not model or not self.supports_model(model):
            return
        picker = page.locator(self.selectors.model_selector).first
        if not await picker.count():
            return
        if await self._selected_model(picker) == model:
            return  # tab already on this model (handles cross-request reuse/reset)
        try:
            await picker.click()
            # Match the exact model-name span (avoids the item's description text)
            # and click it — Radix selects the enclosing menuitemradio.
            option = page.locator(f'{_MODEL_NAME}:text-is("{model}")').first
            await option.wait_for(state="visible", timeout=4000)
            await option.click()
        except PlaywrightError:
            logger.debug("perplexity: model switch to %r failed", model, exc_info=True)
            try:
                await page.keyboard.press("Escape")
            except PlaywrightError:
                pass

    async def _selected_model(self, picker) -> str:
        """The model the trigger currently shows. It displays the active model's
        name, or "Model" while on the default auto-router — which we normalise
        back to ``default_model`` ("Best") so an omitted request is a no-op."""
        try:
            label = (await picker.inner_text()).strip()
        except PlaywrightError:
            return ""
        return self.default_model if (not label or label.casefold() == "model") else label

    async def _set_thinking(self, page: Page, request: ChatRequest) -> None:
        """Turn the current model's Thinking (reasoning) mode on/off per the
        request's ``reasoning_effort``. No-op when no preference is given, the
        model has no Thinking toggle, or the toggle is forced (disabled)."""
        want = self._wants_thinking(request)
        if want is None:
            return
        picker = page.locator(self.selectors.model_selector).first
        if not await picker.count():
            return
        opened = False
        try:
            await picker.click()
            opened = True
            switch = page.locator(_THINKING_SWITCH).first
            try:
                await switch.wait_for(state="visible", timeout=2500)
            except PlaywrightError:
                logger.debug("perplexity: current model exposes no Thinking toggle")
                return
            if await switch.is_disabled():
                return  # model forces Thinking on/off — nothing to change
            is_on = (await switch.get_attribute("aria-checked")) == "true"
            if is_on != want:
                await switch.click()
                logger.info("perplexity: set thinking=%s", want)
        except PlaywrightError:
            logger.debug("perplexity: setting thinking failed", exc_info=True)
        finally:
            if opened:
                try:
                    await page.keyboard.press("Escape")  # dismiss the menu
                except PlaywrightError:
                    pass

    @staticmethod
    def _wants_thinking(request: ChatRequest) -> bool | None:
        """Map ``reasoning_effort`` to a desired Thinking state (None = leave)."""
        effort = (request.reasoning_effort or "").strip().lower()
        if not effort:
            return None
        return effort not in ("none", "minimal")

    # Reading the answer is fully generic (see BrowserChatProvider._reply_text):
    # the base reads the prose block's innerHTML and converts it to Markdown, so
    # Perplexity's inline citation chips (`<span class="citation"><a href>…`)
    # survive as `[label](url)` links with no provider-specific handling needed.
