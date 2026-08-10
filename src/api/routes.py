"""OpenAI-compatible HTTP routes."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..core.errors import AuthenticationRequired, ProviderError, ProviderTimeout
from ..core.messages import flatten_messages
from ..core.tools import (
    TextEvent,
    ToolCallEvent,
    ToolCallParser,
    build_tools_preamble,
)
from ..core.types import ChatMessage, ChatRequest
from ..providers import BaseChatProvider, ProviderRouter
from . import openai_format as fmt
from .auth import require_api_key
from .schemas import ChatCompletionRequest, ModelCard, ModelList
from .tool_runtime import collect

logger = logging.getLogger(__name__)
router = APIRouter()


def get_router(request: Request) -> ProviderRouter:
    return request.app.state.router


def resolve_provider(
    provider_router: ProviderRouter, model: str
) -> tuple[BaseChatProvider, str]:
    """Map a (possibly ``provider/``-prefixed) model to (provider, bare_model)."""
    name, bare_model = provider_router.split(model)
    try:
        return provider_router.get(name), bare_model
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown provider {name!r}.") from None


def get_mcp(request: Request):
    """The MCP manager, or None when MCP isn't configured (e.g. in tests)."""
    return getattr(request.app.state, "mcp", None)


Attempt = tuple[BaseChatProvider, str]


def build_attempts(
    provider_router: ProviderRouter,
    primary: BaseChatProvider,
    model: str,
    explicit: bool,
    use_tools: bool,
    chat_request: ChatRequest,
) -> list[Attempt]:
    """Ordered ``(provider, model)`` attempts for one request.

    Browser automation fails for reasons that are frequently *not* the client's
    fault and often not even persistent: a site ships a DOM change, a tab wedges,
    a session drops. Since we already front several interchangeable chat UIs,
    the cheapest availability win is to try another one rather than 500.

    We never do this when the caller pinned a backend with `provider/model` —
    they asked for that specific model and silently answering from a different
    one would be worse than an error. Fallbacks run on their own default model
    (the requested id is meaningless elsewhere) and only if they support every
    capability this request needs.
    """
    attempts: list[Attempt] = [(primary, model)]
    config = getattr(provider_router, "settings", None)
    if explicit or not getattr(config, "enable_failover", False):
        return attempts
    # Same provider once more: the failed tab was recycled on the way out, so
    # this retry runs on a fresh one — enough to absorb a transient DOM flake.
    attempts.append((primary, model))
    for other in provider_router.all_providers():
        if other.name == primary.name:
            continue
        if use_tools and not other.supports_tools:
            continue
        if chat_request.attachments and not other.supports_attachments:
            continue
        attempts.append((other, other.default_model))
    return attempts


class Served:
    """Records which attempt actually answered, for reporting back to the client.

    Failover must not be silent: a caller who asked for the default and got a
    different backend needs to see that in the response ``model``, otherwise
    they cannot tell a fallback answer from a primary one.
    """

    def __init__(self, provider_name: str, model: str):
        self.provider_name = provider_name
        self.model = model
        self.substituted = False

    def record(self, provider: BaseChatProvider, model: str) -> None:
        self.substituted = provider.name != self.provider_name
        self.provider_name, self.model = provider.name, model

    def label(self, requested: str) -> str:
        """The model id to report: qualified only when a fallback answered."""
        return f"{self.provider_name}/{self.model}" if self.substituted else requested


async def generate_with_failover(
    attempts: list[Attempt], chat_request: ChatRequest, served: Served | None = None
) -> AsyncIterator[str]:
    """Run ``attempts`` in order until one produces a reply.

    Fall-through is only legal *before the first delta*: once the client has seen
    part of an answer, switching backends would splice two different replies
    together, so a mid-stream failure is always raised.
    """
    exhausted: set[str] = set()
    produced = False
    last: ProviderError | None = None

    for provider, model in attempts:
        if provider.name in exhausted:
            continue
        try:
            async for delta in provider.generate(replace(chat_request, model=model)):
                if not produced and served is not None:
                    served.record(provider, model)
                produced = True
                yield delta
            return
        except ProviderError as exc:
            if produced:
                raise
            last = exc
            # A timeout means the site is slow (another attempt costs a second
            # full timeout); a logged-out session won't fix itself. Neither is
            # worth retrying on the *same* provider — move on to a different one.
            if isinstance(exc, (ProviderTimeout, AuthenticationRequired)):
                exhausted.add(provider.name)
            logger.warning("Provider %r failed: %s", provider.name, exc)

    assert last is not None  # attempts is non-empty, so we either returned or failed
    raise last


@router.get("/health")
async def health(
    request: Request,
    deep: bool = False,
    provider_router: ProviderRouter = Depends(get_router),
) -> JSONResponse:
    """Liveness + per-provider login state.

    Returns **503** when the browser is unrecoverably down, so a container
    healthcheck can restart us; a browser that merely died and will be
    relaunched still reports 200 (see ``BrowserManager.healthy``). Being logged
    out is *not* unhealthy — that needs a human at the noVNC session, not a
    restart, and restarting would only interrupt them.

    Each provider's `authenticated` is its cached login state (updated on every
    request); pass `?deep=1` to actively re-probe every provider (navigates a
    tab per provider, warming any that are still cold). Keep `deep` out of
    automated healthchecks: it competes with real traffic for pooled tabs.
    """
    providers: dict[str, bool | None] = {}
    for provider in provider_router.all_providers():
        providers[provider.name] = (
            await provider.check_authentication() if deep else provider.authenticated
        )
    default = provider_router.default_name
    browser = getattr(request.app.state, "browser", None)
    healthy = browser.healthy if browser is not None else True
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ok" if healthy else "unavailable",
            "browser": "up" if (browser is None or browser.is_alive) else "down",
            "provider": default,
            "authenticated": providers.get(default),
            "providers": providers,
        },
    )


@router.get("/v1/models", dependencies=[Depends(require_api_key)])
async def list_models(provider_router: ProviderRouter = Depends(get_router)) -> ModelList:
    # Advertise every routable model as `provider/model` so clients can pick a
    # backend explicitly. Bare (unprefixed) models still work against the default.
    return ModelList(
        data=[
            ModelCard(id=f"{provider.name}/{model}", owned_by=provider.name)
            for provider in provider_router.all_providers()
            for model in provider.models
        ]
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
    provider_router: ProviderRouter = Depends(get_router),
    mcp=Depends(get_mcp),
):
    # Resolve the model (strip a `:online` suffix), then route on any
    # `provider/` prefix. An explicitly-requested model must be one the resolved
    # provider offers — we never switch to an unknown one. An omitted model
    # falls back to that provider's default.
    requested = body.resolve_model()
    provider, model = resolve_provider(provider_router, requested)
    # `split` only yields a provider name when the string really carried that
    # prefix, so this distinguishes "route me to X" from "use the default".
    explicit = requested.startswith(f"{provider.name}/")
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
    attempts = build_attempts(
        provider_router, provider, model, explicit, use_tools, chat_request
    )
    served = Served(provider.name, model)

    if body.stream:
        return StreamingResponse(
            _stream(attempts, chat_request, completion_id, model, use_tools, served),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        content, tool_calls = await collect(
            generate_with_failover(attempts, chat_request, served), use_tools
        )
    except ProviderError as exc:
        return _provider_error(exc)

    prompt = flatten_messages(chat_request.messages)
    return JSONResponse(
        fmt.full_completion(
            completion_id, served.label(model), content, prompt, tool_calls
        )
    )


async def _stream(
    attempts: list[Attempt],
    chat_request: ChatRequest,
    completion_id: str,
    model: str,
    use_tools: bool,
    served: Served,
) -> AsyncIterator[str]:
    yield fmt.sse(fmt.chunk(completion_id, model, delta={"role": "assistant"}))
    parser = ToolCallParser() if use_tools else None
    tool_index = 0
    saw_tool_call = False

    def render(event) -> str | None:
        nonlocal tool_index, saw_tool_call
        served_model = served.label(model)
        if isinstance(event, ToolCallEvent):
            saw_tool_call = True
            delta = fmt.tool_call_delta(
                tool_index, fmt.new_tool_call_id(), event.name, event.arguments
            )
            tool_index += 1
            return fmt.sse(fmt.chunk(completion_id, served_model, delta=delta))
        if isinstance(event, TextEvent) and event.text:
            return fmt.sse(
                fmt.chunk(completion_id, served_model, delta={"content": event.text})
            )
        return None

    try:
        async for delta in generate_with_failover(attempts, chat_request, served):
            if parser is None:
                if delta:
                    yield fmt.sse(
                        fmt.chunk(
                            completion_id, served.label(model), delta={"content": delta}
                        )
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
        fmt.chunk(
            completion_id, served.label(model), delta={}, finish_reason=finish_reason
        )
    )
    yield fmt.SSE_DONE


def _provider_error(exc: ProviderError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=fmt.error_payload(str(exc), exc.error_type, exc.status_code),
    )
