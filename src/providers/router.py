"""Route requests to a provider based on a ``provider/model`` model string.

A single server can front several chat UIs at once. Clients pick the backend by
prefixing the model with the provider name, e.g. ``perplexity/Gemini 3.1 Pro``
or ``expressai/GPT OSS 120B``. A bare model (no ``provider/`` prefix) goes to the
configured default provider, so existing single-provider clients keep working.

Provider instances are created lazily and cached; the browser tabs behind each
are warmed on first use (see :class:`~src.browser.manager.BrowserManager`), so
enabling a provider you never call costs nothing. All providers share one
browser context (one profile → one login per site).
"""

from __future__ import annotations

import logging

from ..browser import BrowserManager
from ..config import Settings
from .base import BaseChatProvider
from .registry import available_providers, create_provider

logger = logging.getLogger(__name__)


class ProviderRouter:
    def __init__(self, settings: Settings, browser: BrowserManager):
        self._settings = settings
        self._browser = browser
        registered = set(available_providers())
        allow = settings.enabled_provider_set
        unknown = allow - registered
        if unknown:
            logger.warning("Ignoring unknown providers in CHAT2API_PROVIDERS: %s", sorted(unknown))
        # Empty allowlist => every registered provider is routable.
        self._enabled = (allow & registered) if allow else set(registered)
        self._default = settings.provider
        self._enabled.add(self._default)  # the default is always routable
        self._instances: dict[str, BaseChatProvider] = {}

    @property
    def default_name(self) -> str:
        return self._default

    @property
    def enabled(self) -> list[str]:
        return sorted(self._enabled)

    def get(self, name: str) -> BaseChatProvider:
        """Return the (cached) provider instance, or raise ``KeyError``.

        Instantiation is cheap and browser-free; tabs warm on first request.
        """
        provider = self._instances.get(name)
        if provider is None:
            if name not in self._enabled:
                raise KeyError(name)
            provider = create_provider(name, self._settings, self._browser)
            self._instances[name] = provider
        return provider

    def all_providers(self) -> list[BaseChatProvider]:
        return [self.get(name) for name in self.enabled]

    def split(self, model: str) -> tuple[str, str]:
        """Split ``provider/model`` into ``(provider_name, bare_model)``.

        Only splits when the prefix is an *enabled* provider; otherwise the whole
        string is treated as a model for the default provider (so a model name
        that happens to contain ``/`` isn't mis-parsed). Splits on the first
        ``/`` only, so model names may contain spaces and further slashes.
        """
        prefix, sep, rest = model.partition("/")
        if sep and prefix in self._enabled:
            return prefix, rest
        return self._default, model
