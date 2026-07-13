"""Builders that turn provider text deltas into OpenAI chat-completion wire objects."""

from __future__ import annotations

import json
import time
import uuid

from ..core.messages import estimate_tokens


def new_completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex}"


def new_tool_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:24]}"


def chunk(completion_id: str, model: str, *, delta: dict, finish_reason=None) -> dict:
    """A single ``chat.completion.chunk`` object."""
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def full_completion(
    completion_id: str,
    model: str,
    content: str,
    prompt: str,
    tool_calls: list[dict] | None = None,
) -> dict:
    """A non-streaming ``chat.completion`` object with estimated usage."""
    prompt_tokens = estimate_tokens(prompt)
    completion_tokens = estimate_tokens(content)
    message: dict = {"role": "assistant", "content": content or None}
    finish_reason = "stop"
    if tool_calls:
        message["tool_calls"] = tool_calls
        finish_reason = "tool_calls"
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {"index": 0, "message": message, "finish_reason": finish_reason}
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def tool_call_delta(index: int, call_id: str, name: str, arguments: str) -> dict:
    """A streaming ``delta.tool_calls`` fragment (one whole call per chunk)."""
    return {
        "tool_calls": [
            {
                "index": index,
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        ]
    }


def sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


SSE_DONE = "data: [DONE]\n\n"


def error_payload(message: str, err_type: str, status_code: int) -> dict:
    return {
        "error": {
            "message": message,
            "type": err_type,
            "code": status_code,
        }
    }
