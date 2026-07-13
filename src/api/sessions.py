"""Server-side conversation state for the Responses API.

We keep the accumulated message history ourselves (keyed by response id) rather
than relying on the chat UI to persist threads. This mirrors OpenAI's
``store`` / ``previous_response_id`` model and works for every provider,
including stateless ones like Google AI Mode.
"""

from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

from ..core.types import ChatMessage


@dataclass(slots=True)
class StoredResponse:
    id: str
    messages: list[ChatMessage]
    model: str
    created: int = field(default_factory=lambda: int(time.time()))


class SessionStore:
    """In-memory, bounded (LRU) store of conversation histories."""

    def __init__(self, max_entries: int = 2000):
        self._entries: "OrderedDict[str, StoredResponse]" = OrderedDict()
        self._max = max_entries

    def get(self, response_id: str) -> StoredResponse | None:
        entry = self._entries.get(response_id)
        if entry is not None:
            self._entries.move_to_end(response_id)
        return entry

    def create(self, messages: list[ChatMessage], model: str) -> StoredResponse:
        response_id = f"resp_{uuid.uuid4().hex}"
        entry = StoredResponse(id=response_id, messages=messages, model=model)
        self._entries[response_id] = entry
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)  # evict oldest
        return entry

    def __len__(self) -> int:
        return len(self._entries)
