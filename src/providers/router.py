"""Route a request to a provider.

A single server can front several chat UIs. ``CHAT2API_PROVIDERS`` is an
**ordered** list — the whole configuration:

    CHAT2API_PROVIDERS=expressai,perplexity

There is no "default provider": a model always identifies its own backend, and
the ids advertised on ``/v1/models`` are exactly the ids clients send back.

Two ways a request resolves:

* ``expressai/GPT OSS 120B`` — pinned, the form ``/v1/models`` advertises. That
  provider, that model, no failover (the caller named a specific backend;
  substituting silently would be worse than an error).
* ``GPT OSS 120B`` — unpinned: whichever enabled provider offers that exact
  model. Ambiguity is broken by list order. These may fail over.

An omitted model is an error — with no default provider there is nothing to
guess, and guessing is how a request ends up silently answered by a backend the
caller never chose.

Provider instances are created lazily and cached; their browser tabs are warmed
on first use, so listing a provider you never call costs nothing. All providers
share one browser context (one profile → one login per site).
"""

from __future__ import annotations

import logging

from ..browser import BrowserManager
from ..config import Settings
from .base import BaseChatProvider
from .registry import available_providers, create_provider

logger = logging.getLogger(__name__)


class UnknownModel(Exception):
    """No enabled provider offers the requested model."""


class ModelRequired(Exception):
    """The request named no model, and there is no default to fall back to."""


class ProviderRouter:
    def __init__(self, settings: Settings, browser: BrowserManager):
        self._settings = settings
        self._browser = browser
        registered = available_providers()
        configured = settings.provider_order
        unknown = [name for name in configured if name not in registered]
        if unknown:
            logger.warning("Ignoring unknown providers in CHAT2API_PROVIDERS: %s", unknown)
        # Order is preference, so this is a list, not a set. Empty config falls
        # back to every registered provider rather than leaving the server with
        # nothing to route to.
        self._order = [name for name in configured if name in registered] or list(registered)
        self._instances: dict[str, BaseChatProvider] = {}

    @property
    def settings(self) -> Settings:
        """The active configuration (the API layer reads failover policy here)."""
        return self._settings

    @property
    def enabled(self) -> list[str]:
        """Routable providers, in preference order."""
        return list(self._order)

    def get(self, name: str) -> BaseChatProvider:
        """Return the (cached) provider instance, or raise ``KeyError``.

        Instantiation is cheap and browser-free; tabs warm on first request.
        """
        provider = self._instances.get(name)
        if provider is None:
            if name not in self._order:
                raise KeyError(name)
            provider = create_provider(name, self._settings, self._browser)
            self._instances[name] = provider
        return provider

    def all_providers(self) -> list[BaseChatProvider]:
        return [self.get(name) for name in self._order]

    def resolve(self, model: str) -> tuple[BaseChatProvider, str, bool]:
        """Resolve a model string to ``(provider, bare_model, pinned)``.

        A ``provider/`` prefix only counts when it names an enabled provider, so
        a model id that merely contains a slash isn't mis-parsed. Splitting on
        the first ``/`` only means model names may contain spaces and slashes.

        Raises :class:`UnknownModel` when nothing offers the requested model,
        and :class:`ModelRequired` when none was given — better than silently
        answering from a backend the caller never chose.
        """
        prefix, sep, rest = model.partition("/")
        if sep and prefix in self._order:
            return self.get(prefix), rest, True

        if not model:
            raise ModelRequired()

        for name in self._order:
            provider = self.get(name)
            if provider.supports_model(model):
                return provider, model, False
        raise UnknownModel(model)

    def catalogue(self) -> dict[str, list[str]]:
        """Every routable model, by provider — used to explain a 404."""
        return {provider.name: provider.models for provider in self.all_providers()}
