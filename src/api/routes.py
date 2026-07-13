"""OpenAI-compatible HTTP routes."""

from __future__ import annotations

import logging
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..core.errors import ProviderError
from ..core.messages import flatten_messages
from ..core.tools import (
    TextEvent,
    ToolCallEvent,
    ToolCallParser,
    build_tools_preamble,
)
from ..core.types import ChatMessage, ChatRequest
from ..providers import BaseChatProvider
from . import openai_format as fmt
from .auth import require_api_key
from .schemas import ChatCompletionRequest, ModelCard, ModelList
from .tool_runtime import collect

logger = logging.getLogger(__name__)
router = APIRouter()


def get_provider(request: Request) -> BaseChatProvider:
    return request.app.state.provider


def get_mcp(request: Request):
    """The MCP manager, or None when MCP isn't configured (e.g. in tests)."""
    return getattr(request.app.state, "mcp", None)


@router.get("/health")
async def health(
    deep: bool = False, provider: BaseChatProvider = Depends(get_provider)
) -> dict:
    """Liveness + upstream login state.

    `authenticated` is the cached login state (updated on every request); pass
    `?deep=1` to actively re-probe the provider (navigates a tab).
    """
    authenticated = (
        await provider.check_authentication() if deep else provider.authenticated
    )
    return {
        "status": "ok",
        "provider": provider.name,
        "authenticated": authenticated,
    }


@router.get("/v1/models", dependencies=[Depends(require_api_key)])
async def list_models(provider: BaseChatProvider = Depends(get_provider)) -> ModelList:
    return ModelList(
        data=[ModelCard(id=model, owned_by=provider.name) for model in provider.models]
    )


def validate_model(provider: BaseChatProvider, model: str) -> None:
    """Reject a model the provider doesn't offer (OpenAI ``model_not_found``).

    An empty model means "unspecified" — the caller substitutes the provider's
    default, so it's never rejected here.
    """
    if model and not provider.supports_model(model):
        raise HTTPException(
            status_code=404,
            detail=(
                f"The model {model!r} does not exist for provider {provider.name!r}. "
                f"Available models: {provider.models}."
            ),
        )


@router.post("/v1/chat/completions", dependencies=[Depends(require_api_key)])
async def chat_completions(
    body: ChatCompletionRequest,
    provider: BaseChatProvider = Depends(get_provider),
    mcp=Depends(get_mcp),
):
    # Resolve the model (strip a `:online` suffix). An explicitly-requested
    # model must be one the provider offers — we never try to switch to an
    # unknown one. An omitted model falls back to the provider's default.
    model = body.resolve_model()
    if model:
        validate_model(provider, model)
    else:
        model = provider.default_model
    # Tools come from the client (function tools) plus any configured MCP
    # servers. Chat Completions delegates execution: parsed calls are returned
    # to the client (which owns/executes them), so we only list + inject here.
    # A client that explicitly sends tools to a provider that can't emit tool
    # calls is an error; but globally-configured MCP tools are best-effort — we
    # simply skip them for such providers so the provider (e.g. Perplexity, with
    # its own native search) still answers normally.
    client_tools = [t.model_dump() for t in body.tools or []]
    if client_tools and not provider.supports_tools:
        raise HTTPException(
            status_code=400,
            detail=f"Provider {provider.name!r} does not support tool calls.",
        )
    mcp_tools = mcp.openai_tools() if mcp and mcp.has_tools and provider.supports_tools else []
    tool_defs = client_tools + mcp_tools
    use_tools = bool(tool_defs) and body.tool_choice != "none"

    chat_request = body.to_chat_request()
    chat_request.model = model  # the validated, resolved model the provider selects
    if chat_request.attachments and not provider.supports_attachments:
        raise HTTPException(
            status_code=400,
            detail=f"Provider {provider.name!r} does not support attachments.",
        )
    if chat_request.web_search and not provider.supports_web_search:
        chat_request.web_search = False  # soft-ignore an unsupported enhancement
    if use_tools:
        required = body.tool_choice == "required" or isinstance(body.tool_choice, dict)
        chat_request.messages.insert(
            0,
            ChatMessage(role="system", content=build_tools_preamble(tool_defs, required)),
        )

    completion_id = fmt.new_completion_id()

    if body.stream:
        return StreamingResponse(
            _stream(provider, chat_request, completion_id, model, use_tools),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        content, tool_calls = await collect(provider, chat_request, use_tools)
    except ProviderError as exc:
        return _provider_error(exc)

    prompt = flatten_messages(chat_request.messages)
    return JSONResponse(
        fmt.full_completion(completion_id, model, content, prompt, tool_calls)
    )


async def _stream(
    provider: BaseChatProvider,
    chat_request: ChatRequest,
    completion_id: str,
    model: str,
    use_tools: bool,
) -> AsyncIterator[str]:
    yield fmt.sse(fmt.chunk(completion_id, model, delta={"role": "assistant"}))
    parser = ToolCallParser() if use_tools else None
    tool_index = 0
    saw_tool_call = False

    def render(event) -> str | None:
        nonlocal tool_index, saw_tool_call
        if isinstance(event, ToolCallEvent):
            saw_tool_call = True
            delta = fmt.tool_call_delta(
                tool_index, fmt.new_tool_call_id(), event.name, event.arguments
            )
            tool_index += 1
            return fmt.sse(fmt.chunk(completion_id, model, delta=delta))
        if isinstance(event, TextEvent) and event.text:
            return fmt.sse(
                fmt.chunk(completion_id, model, delta={"content": event.text})
            )
        return None

    try:
        async for delta in provider.generate(chat_request):
            if parser is None:
                if delta:
                    yield fmt.sse(
                        fmt.chunk(completion_id, model, delta={"content": delta})
                    )
                continue
            for event in parser.feed(delta):
                out = render(event)
                if out:
                    yield out
        if parser is not None:
            for event in parser.finish():
                out = render(event)
                if out:
                    yield out
    except ProviderError as exc:
        logger.warning("Provider error during stream: %s", exc)
        yield fmt.sse(fmt.error_payload(str(exc), exc.error_type, exc.status_code))
        yield fmt.SSE_DONE
        return
    except Exception as exc:  # noqa: BLE001 - surface as SSE error, never 500 mid-stream
        logger.exception("Unexpected error during stream")
        yield fmt.sse(fmt.error_payload(str(exc), "internal_error", 500))
        yield fmt.SSE_DONE
        return

    finish_reason = "tool_calls" if saw_tool_call else "stop"
    yield fmt.sse(
        fmt.chunk(completion_id, model, delta={}, finish_reason=finish_reason)
    )
    yield fmt.SSE_DONE


def _provider_error(exc: ProviderError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=fmt.error_payload(str(exc), exc.error_type, exc.status_code),
    )
