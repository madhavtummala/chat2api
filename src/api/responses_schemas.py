"""OpenAI Responses API wire models + output-item builders (a pragmatic subset)."""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..core.messages import estimate_tokens
from ..core.types import ChatMessage


class ResponsesRequest(BaseModel):
    model: str = ""  # empty -> use the provider's default model
    # Either a plain string or a list of input items ({"role","content"}).
    input: str | list[dict[str, Any]]
    stream: bool = False
    # Loosely typed: function tools ({"type":"function",...}) are honoured;
    # other entries (e.g. MCP) are ignored here — MCP tools come from config.
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    previous_response_id: str | None = None
    store: bool = True
    instructions: str | None = None
    max_output_tokens: int | None = None
    # Reasoning/"thinking" effort: OpenAI sends `reasoning: {"effort": ...}`;
    # some clients send it flat as `reasoning_effort`. Both are accepted.
    reasoning: dict[str, Any] | None = None
    reasoning_effort: str | None = None

    def resolve_reasoning_effort(self) -> str | None:
        if self.reasoning_effort:
            return self.reasoning_effort
        if isinstance(self.reasoning, dict):
            effort = self.reasoning.get("effort")
            return effort if isinstance(effort, str) else None
        return None

    def input_messages(self) -> list[ChatMessage]:
        if isinstance(self.input, str):
            return [ChatMessage(role="user", content=self.input)]
        messages: list[ChatMessage] = []
        for item in self.input:
            role = item.get("role", "user")
            messages.append(ChatMessage(role=role, content=_content_text(item.get("content"))))
        return messages

    def function_tools(self) -> list[dict]:
        return [t for t in (self.tools or []) if t.get("type") == "function"]


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for part in content:
        if isinstance(part, dict) and "text" in part:
            parts.append(part["text"])
    return "".join(parts)


# ---- output-item builders -----------------------------------------------
def message_item(text: str) -> dict:
    return {
        "type": "message",
        "id": f"msg_{uuid.uuid4().hex}",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def mcp_call_item(name: str, arguments: str, output: str) -> dict:
    return {
        "type": "mcp_call",
        "id": f"mcp_{uuid.uuid4().hex}",
        "name": name,
        "arguments": arguments,
        "output": output,
    }


def function_call_item(call: dict) -> dict:
    return {
        "type": "function_call",
        "id": f"fc_{uuid.uuid4().hex}",
        "call_id": call["id"],
        "name": call["function"]["name"],
        "arguments": call["function"]["arguments"],
    }


def build_response(
    response_id: str,
    model: str,
    output_items: list[dict],
    output_text: str,
    status: str,
    previous_response_id: str | None,
    prompt: str,
) -> dict:
    input_tokens = estimate_tokens(prompt)
    output_tokens = estimate_tokens(output_text)
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "model": model,
        "output": output_items,
        "output_text": output_text,
        "previous_response_id": previous_response_id,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }
