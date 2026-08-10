"""Internal, provider-facing data types.

These are deliberately decoupled from the OpenAI wire schema (see
``src/api/schemas.py``). Providers consume a :class:`ChatRequest` and yield
plain text deltas; the API layer owns all OpenAI-shaped (de)serialisation.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class ChatMessage:
    role: str
    content: str
    name: str | None = None


@dataclass(slots=True)
class Attachment:
    """A file to upload into the chat UI (decoded from an OpenAI content part)."""

    name: str
    mime: str
    data: bytes


@dataclass(slots=True)
class ChatRequest:
    """A normalised chat-completion request handed to a provider.

    Deliberately narrow: it carries only what a browser-driven provider can act
    on. Sampling controls (``temperature``, ``max_tokens``) are accepted at the
    API boundary for OpenAI compatibility but stop there — a chat web UI exposes
    no way to set them, so plumbing them further would only imply they work.
    """

    messages: list[ChatMessage]
    model: str
    # Provider-specific capabilities, normalised from the OpenAI-compatible body.
    web_search: bool = False
    # Reasoning/"thinking" effort (OpenAI `reasoning_effort`). None = leave the
    # model default; "minimal"/"none" = off; any other value = on. Only providers
    # with a thinking toggle (Perplexity) act on it.
    reasoning_effort: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
