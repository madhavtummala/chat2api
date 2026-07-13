"""The stateful /v1/responses endpoint: agentic loop with server-side MCP execution.

State (the accumulated message history) lives in the SessionStore, keyed by
response id. Each model turn resends the full history to a fresh browser turn
(via the provider's stateless ``generate``), so this works for any provider and
reuses the common tab pool. MCP-owned tool calls are executed by us and their
results fed back into the conversation; unknown tool calls halt the loop with
``requires_action`` for the client to handle.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..config import settings
from ..core.errors import ProviderError
from ..core.messages import flatten_messages
from ..core.tools import build_tools_preamble
from ..core.types import ChatMessage, ChatRequest
from ..providers import BaseChatProvider
from . import openai_format as fmt
from . import responses_schemas as rs
from .auth import require_api_key
from .routes import get_mcp, get_provider, validate_model
from .tool_runtime import collect

logger = logging.getLogger(__name__)
router = APIRouter()


def get_sessions(request: Request):
    return request.app.state.sessions


@router.post("/v1/responses", dependencies=[Depends(require_api_key)])
async def create_response(
    body: rs.ResponsesRequest,
    provider: BaseChatProvider = Depends(get_provider),
    mcp=Depends(get_mcp),
    sessions=Depends(get_sessions),
):
    if body.model:
        validate_model(provider, body.model)
    else:
        body.model = provider.default_model
    # Rebuild the conversation: prior history (if continuing) + this turn's input.
    history: list[ChatMessage] = []
    if body.previous_response_id:
        prior = sessions.get(body.previous_response_id)
        if prior is None:
            raise HTTPException(404, f"Unknown previous_response_id {body.previous_response_id!r}")
        history.extend(prior.messages)
    if body.instructions:
        history.append(ChatMessage(role="system", content=body.instructions))
    history.extend(body.input_messages())

    # Client-supplied function tools require tool support; server-configured MCP
    # tools are best-effort and skipped for providers that can't emit tool calls
    # (so e.g. Perplexity still answers with its own native search).
    client_tools = body.function_tools()
    if client_tools and not provider.supports_tools:
        raise HTTPException(
            400, f"Provider {provider.name!r} does not support tool calls."
        )
    tool_defs = client_tools + (
        mcp.openai_tools() if mcp and mcp.has_tools and provider.supports_tools else []
    )

    try:
        output_items, output_text, status = await _run_loop(
            provider, mcp, history, tool_defs, body
        )
    except ProviderError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content=fmt.error_payload(str(exc), exc.error_type, exc.status_code),
        )

    response_id = (
        sessions.create(history, body.model).id if body.store else f"resp_{fmt.uuid.uuid4().hex}"
    )
    prompt = flatten_messages(history)
    response = rs.build_response(
        response_id, body.model, output_items, output_text, status,
        body.previous_response_id, prompt,
    )

    if body.stream:
        return StreamingResponse(
            _stream_response(response, output_text),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return JSONResponse(response)


async def _run_loop(
    provider: BaseChatProvider,
    mcp,
    history: list[ChatMessage],
    tool_defs: list[dict],
    body: rs.ResponsesRequest,
) -> tuple[list[dict], str, str]:
    """Drive model<->tool turns until a final answer (or a client-side tool)."""
    use_tools = bool(tool_defs) and body.tool_choice != "none"
    required = body.tool_choice == "required" or isinstance(body.tool_choice, dict)
    output_items: list[dict] = []
    final_text = ""

    for _ in range(settings.max_agent_turns):
        messages = list(history)
        if use_tools:
            messages.insert(
                0, ChatMessage(role="system", content=build_tools_preamble(tool_defs, required))
            )
        request = ChatRequest(
            messages=messages,
            model=body.model,
            reasoning_effort=body.resolve_reasoning_effort(),
        )
        text, tool_calls = await collect(provider, request, use_tools)
        if text:
            final_text = text

        if not tool_calls:
            if text:
                history.append(ChatMessage(role="assistant", content=text))
            output_items.append(rs.message_item(text))
            return output_items, final_text, "completed"

        # Record the assistant's tool-call turn in the history.
        history.append(ChatMessage(role="assistant", content=_render_calls(text, tool_calls)))

        # Execute all MCP-owned calls concurrently (parallel tool calls run in
        # parallel); a tool we can't run halts the loop for the client.
        owned = [c for c in tool_calls if mcp and mcp.owns(c["function"]["name"])]
        unowned = [c for c in tool_calls if not (mcp and mcp.owns(c["function"]["name"]))]

        results = await asyncio.gather(*(_exec_mcp(mcp, c) for c in owned))
        for call, result in zip(owned, results):
            name = call["function"]["name"]
            output_items.append(rs.mcp_call_item(name, call["function"]["arguments"], result))
            history.append(ChatMessage(role="tool", content=result, name=name))

        if unowned:
            for call in unowned:
                output_items.append(rs.function_call_item(call))
            return output_items, final_text, "requires_action"

    logger.warning("Responses agentic loop hit max_agent_turns")
    output_items.append(rs.message_item(final_text))
    return output_items, final_text, "incomplete"


async def _exec_mcp(mcp, call: dict) -> str:
    name = call["function"]["name"]
    try:
        return await mcp.call_tool(name, json.loads(call["function"]["arguments"] or "{}"))
    except Exception as exc:  # noqa: BLE001 - surface tool errors to the model
        return f"[tool error] {exc}"


def _render_calls(text: str, tool_calls: list[dict]) -> str:
    rendered = "\n".join(
        f"<tool_call>{json.dumps({'name': c['function']['name'], 'arguments': json.loads(c['function']['arguments'] or '{}')})}</tool_call>"
        for c in tool_calls
    )
    return f"{text}\n{rendered}".strip() if text else rendered


async def _stream_response(response: dict, output_text: str) -> AsyncIterator[str]:
    """A pragmatic subset of Responses streaming events."""
    created = {**response, "status": "in_progress", "output": [], "output_text": ""}
    yield _event("response.created", {"response": created})
    for i in range(0, len(output_text), 64):
        yield _event("response.output_text.delta", {"delta": output_text[i:i + 64]})
    yield _event("response.completed", {"response": response})


def _event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
