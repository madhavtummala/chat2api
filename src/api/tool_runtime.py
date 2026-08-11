"""Shared tool plumbing for the Chat Completions and Responses endpoints.

Which tools a request may use, and how a delta stream is turned into text +
tool calls, must be identical across both endpoints — so both live here rather
than being reimplemented per route.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from fastapi import HTTPException

from ..core.tools import Event, TextEvent, ToolCallEvent, ToolCallParser
from ..providers import BaseChatProvider
from . import openai_format as fmt


def resolve_tools(
    provider: BaseChatProvider,
    mcp,
    client_tools: list[dict],
    tool_choice: Any,
) -> tuple[list[dict], bool, bool]:
    """Tools available for one request, as ``(tool_defs, use_tools, required)``.

    Client-supplied function tools against a provider that can't emit tool calls
    is a client error. Globally-configured MCP tools are best-effort by contrast
    — we simply skip them for such providers, so e.g. Perplexity still answers
    normally with its own native search.
    """
    if client_tools and not provider.supports_tools:
        raise HTTPException(
            status_code=400,
            detail=f"Provider {provider.name!r} does not support tool calls.",
        )
    mcp_tools = mcp.openai_tools() if mcp and mcp.has_tools and provider.supports_tools else []
    tool_defs = client_tools + mcp_tools
    use_tools = bool(tool_defs) and tool_choice != "none"
    # "required" or a named-function choice both mean the model must call a tool.
    required = tool_choice == "required" or isinstance(tool_choice, dict)
    return tool_defs, use_tools, required


async def parse_events(
    stream: AsyncIterator[str], use_tools: bool
) -> AsyncIterator[Event]:
    """Turn a stream of text deltas into text / tool-call events.

    ``stream`` is any async iterator of deltas — ``provider.generate(...)`` for a
    stateless turn, or ``session.send(...)`` for a continued thread. With
    ``use_tools`` False the output is passed through verbatim, so a reply that
    merely mentions the sentinel is never mangled into a phantom call.
    """
    if not use_tools:
        async for delta in stream:
            if delta:
                yield TextEvent(delta)
        return
    parser = ToolCallParser()
    async for delta in stream:
        for event in parser.feed(delta):
            yield event
    for event in parser.finish():
        yield event


async def collect(
    stream: AsyncIterator[str], use_tools: bool
) -> tuple[str, list[dict]]:
    """Drain a whole (non-streaming) generation into ``(text, tool_calls)``.

    ``tool_calls`` are OpenAI-shaped dicts (id/type/function).
    """
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    async for event in parse_events(stream, use_tools):
        if isinstance(event, ToolCallEvent):
            tool_calls.append(
                {
                    "id": fmt.new_tool_call_id(),
                    "type": "function",
                    "function": {"name": event.name, "arguments": event.arguments},
                }
            )
        else:
            text_parts.append(event.text)

    return "".join(text_parts).strip(), tool_calls
