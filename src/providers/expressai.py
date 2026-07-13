"""ExpressAI provider — drives https://app.expressai.com via Playwright.

All the browser-driving logic lives in :class:`BrowserChatProvider`; this file
is just ExpressAI's `Selectors` + capabilities. The CSS selectors are tuned
against the live site — re-run `scripts/inspect_provider.py expressai` if the
DOM changes.
"""

from __future__ import annotations

from ..browser import BrowserManager
from ..config import Settings
from .browser_chat import BrowserChatProvider, Selectors

_SELECTORS = Selectors(
    prompt_input="textarea[placeholder='Ask anything']",
    ready_marker="textarea[placeholder='Ask anything']",
    # The send button is replaced by a stop button while generating (so this
    # selector matching nothing == generating), and returns when the reply is
    # done — that toggle is the completion signal (see _is_generating fallback).
    send_button="button[title='Send message']",
    new_chat_button="button[title='New Chat']",
    # Assistant bubbles are left-aligned (`mr-auto` .prose); the user turn is
    # neither, so this uniquely matches AI replies.
    assistant_message=".mr-auto .prose",
    # Logged-out: composer is disabled with a "Please sign in…" placeholder + a
    # "Sign in with ExpressVPN" button. (We avoid input[type=password]: the
    # *logged-in* page uses one to unlock the encrypted-history vault.)
    login_marker="textarea[placeholder*='sign in' i], button:has-text('Sign in with ExpressVPN')",
    # Model picker: chevron button opens a modal; options are role=button rows.
    model_selector="button:has(svg.lucide-chevron-down)",
    model_option="[role='button']:has-text('{model}')",
    modal_close=".fixed.inset-0 button:has(svg.lucide-x)",
    blocking_overlay=".fixed.inset-0",
    # Web-search "on" state is the teal text colour (no aria-pressed); the
    # aria-pressed clause keeps the test mock working.
    web_search_toggle="[data-testid='web-search-btn']",
    web_search_active=(
        "[data-testid='web-search-btn'][class*='#0F866C'], "
        "[data-testid='web-search-btn'][aria-pressed='true']"
    ),
    file_input="input[type='file'][data-testid='file-input']",
)


class ExpressAIProvider(BrowserChatProvider):
    name = "expressai"
    # Seed catalogue (display names double as ids so select_model matches the
    # dropdown options). ExpressAI is incognito by default, so enable_incognito
    # stays the base no-op.
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
    selectors = _SELECTORS

    def __init__(self, settings: Settings, browser: BrowserManager):
        super().__init__(settings, browser)
        self.base_url = settings.expressai_base_url
