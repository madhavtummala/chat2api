"""Helpers for turning OpenAI-style message arrays into a single prompt.

Browser-driven chat UIs only accept one free-text turn at a time, so a
multi-message conversation has to be flattened into a single string. Providers
that maintain their own server-side conversation history can override this.
"""

from __future__ import annotations

from .types import ChatMessage

_ROLE_LABELS = {
    "system": "System",
    "user": "User",
    "assistant": "Assistant",
    "tool": "Tool",
}


def flatten_messages(messages: list[ChatMessage]) -> str:
    """Render a transcript as a labelled plain-text prompt.

    A lone user message is returned verbatim; anything with prior context is
    rendered as ``Role: content`` blocks followed by an ``Assistant:`` cue.
    """
    if len(messages) == 1 and messages[0].role == "user":
        return messages[0].content

    lines: list[str] = []
    for msg in messages:
        label = _ROLE_LABELS.get(msg.role, msg.role.capitalize())
        lines.append(f"{label}: {msg.content}")
    lines.append("Assistant:")
    return "\n\n".join(lines)


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) for usage accounting.

    Avoids a hard tiktoken dependency; good enough for clients that only need
    a plausible number.
    """
    return max(1, len(text) // 4)
