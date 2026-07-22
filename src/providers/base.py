"""The provider contract.

A provider adapts one chat web UI to a uniform streaming interface. It receives
a normalised :class:`ChatRequest` and yields **plain text deltas** (the newly
generated substring since the previous yield). The API layer is solely
responsible for wrapping those deltas into OpenAI chat-completion chunks, so
providers never need to know about the OpenAI wire format.

To add a new provider:
    1. Subclass :class:`BaseChatProvider`.
    2. Implement ``generate`` (and optionally ``startup``/``shutdown``).
    3. Register it in ``src/providers/registry.py``.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import AsyncIterator

from ..browser import BrowserManager
from ..config import Settings
from ..core.types import ChatRequest

logger = logging.getLogger(__name__)


class BaseChatProvider(ABC):
    #: Stable identifier used for provider selection and the `owned_by` field.
    name: str = "base"
    #: Model id reported when a client does not request a specific one.
    default_model: str = "default"
    #: Seed/fallback model ids. The *live* list (see :meth:`models`) may be
    #: discovered from the site at startup via :meth:`list_models`.
    available_models: tuple[str, ...] = ("default",)
    #: Whether this provider can emulate OpenAI tool calls via prompt injection
    #: + text parsing. When False, requests carrying ``tools`` are rejected.
    supports_tools: bool = False
    #: Whether the UI has a web-search toggle we can drive.
    supports_web_search: bool = False
    #: Whether the UI accepts file attachments.
    supports_attachments: bool = False
    #: Whether a conversation can continue in one persistent thread — i.e. the
    #: backend holds context between messages, so an agentic loop can send the
    #: system prompt once and then only the new (delta) messages, instead of
    #: re-flattening the whole transcript each turn. True for real chat boxes
    #: (see :class:`BrowserChatProvider`); False for stateless backends like
    #: Google AI Mode, which reconstruct context from a full replay every time.
    supports_thread_continuation: bool = False

    def __init__(self, settings: Settings, browser: BrowserManager):
        self.settings = settings
        self.browser = browser
        self._models: list[str] = list(self.available_models)
        self._authenticated: bool | None = None  # None until first checked

    async def startup(self) -> None:
        """Optional one-time setup (navigate, verify login, warm caches)."""

    async def shutdown(self) -> None:
        """Optional teardown."""

    # -- auth --------------------------------------------------------------
    @property
    def authenticated(self) -> bool | None:
        """Last known login state (None = not yet checked). Updated on each
        request and surfaced on ``/health`` so logouts can be monitored."""
        return getattr(self, "_authenticated", None)

    async def check_authentication(self) -> bool | None:
        """Actively re-verify the login state (may navigate). Default: the last
        cached value; providers override to probe the live UI."""
        return self.authenticated

    # -- models ------------------------------------------------------------
    @property
    def models(self) -> list[str]:
        """The current model catalogue exposed on ``/v1/models``."""
        return getattr(self, "_models", None) or list(self.available_models)

    def supports_model(self, model: str) -> bool:
        return model in self.models

    async def list_models(self) -> list[str]:
        """Discover the models the chat UI offers.

        Default: the static :attr:`available_models`. Override to scrape the
        provider website's model picker (may use ``self.browser``).
        """
        return list(self.available_models)

    async def refresh_models(self) -> None:
        """Populate the live catalogue from :meth:`list_models` (best-effort)."""
        try:
            discovered = await self.list_models()
        except Exception:
            logger.warning("Model discovery failed for %s; keeping defaults", self.name)
            return
        if discovered:
            self._models = list(dict.fromkeys(discovered))  # dedupe, keep order

    async def select_model(self, page, model: str) -> None:
        """Switch the chat UI to ``model`` before submitting a prompt.

        Default: no-op (single-model UIs). Override for UIs with a model
        picker (e.g. AI Studio / ChatGPT) to click the relevant option.
        """

    async def enable_incognito(self, page) -> None:
        """Ensure proxied chats aren't saved into the human's chat history.

        Default: no-op — for UIs that are already ephemeral (e.g. ExpressAI).
        Override for providers (e.g. ChatGPT) that persist chats unless a
        temporary-chat toggle is set.
        """

    # -- generation --------------------------------------------------------
    @abstractmethod
    def generate(self, request: ChatRequest) -> AsyncIterator[str]:
        """Yield incremental text deltas for ``request``.

        Implemented as an ``async def`` with ``yield`` (an async generator).
        Raise a :class:`~src.core.errors.ProviderError` subclass on failure.
        """
        raise NotImplementedError
