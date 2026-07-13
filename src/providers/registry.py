"""Provider registry and factory.

Providers register themselves here by name so the server can instantiate the
one selected via ``CHAT2API_PROVIDER`` without importing concrete classes at
the call site.
"""

from __future__ import annotations

from typing import Type

from ..browser import BrowserManager
from ..config import Settings
from .base import BaseChatProvider
from .expressai import ExpressAIProvider
from .google_aimode import GoogleAIModeProvider
from .perplexity import PerplexityProvider

_REGISTRY: dict[str, Type[BaseChatProvider]] = {}


def register(provider_cls: Type[BaseChatProvider]) -> Type[BaseChatProvider]:
    _REGISTRY[provider_cls.name] = provider_cls
    return provider_cls


def available_providers() -> list[str]:
    return sorted(_REGISTRY)


def create_provider(
    name: str, settings: Settings, browser: BrowserManager
) -> BaseChatProvider:
    try:
        provider_cls = _REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"Unknown provider {name!r}. Available: {available_providers()}"
        ) from None
    return provider_cls(settings, browser)


# -- built-in providers ---------------------------------------------------
register(ExpressAIProvider)
register(GoogleAIModeProvider)
register(PerplexityProvider)
