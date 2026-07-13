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
    """A normalised chat-completion request handed to a provider."""

    messages: list[ChatMessage]
    model: str
    # A stable identifier used to route repeated requests to the same browser
    # conversation/tab (falls back to a fresh conversation when None).
    conversation_id: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False
    # Provider-specific capabilities, normalised from the OpenAI-compatible body.
    web_search: bool = False
    attachments: list[Attachment] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @property
    def last_user_message(self) -> str:
        for msg in reversed(self.messages):
            if msg.role == "user":
                return msg.content
        return self.messages[-1].content if self.messages else ""
