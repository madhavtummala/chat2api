"""Shared helpers for running a generation and extracting text + tool calls.

Used by both the Chat Completions route and the Responses agentic loop so the
tool-parsing behaviour is identical across endpoints.
"""

from __future__ import annotations

from ..core.tools import TextEvent, ToolCallEvent, ToolCallParser
from ..core.types import ChatRequest
from ..providers import BaseChatProvider
from . import openai_format as fmt


async def collect(
    provider: BaseChatProvider, chat_request: ChatRequest, use_tools: bool
) -> tuple[str, list[dict]]:
    """Drain a full (non-streaming) generation into (text, tool_calls).

    ``tool_calls`` are OpenAI-shaped dicts (id/type/function). When
    ``use_tools`` is False the model output is returned verbatim as text.
    """
    parser = ToolCallParser() if use_tools else None
    text_parts: list[str] = []
    tool_calls: list[dict] = []

    def handle(event) -> None:
        if isinstance(event, ToolCallEvent):
            tool_calls.append(
                {
                    "id": fmt.new_tool_call_id(),
                    "type": "function",
                    "function": {"name": event.name, "arguments": event.arguments},
                }
            )
        elif isinstance(event, TextEvent):
            text_parts.append(event.text)

    async for delta in provider.generate(chat_request):
        if parser is None:
            text_parts.append(delta)
        else:
            for event in parser.feed(delta):
                handle(event)
    if parser is not None:
        for event in parser.finish():
            handle(event)

    return "".join(text_parts).strip(), tool_calls
