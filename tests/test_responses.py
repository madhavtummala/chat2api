import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.responses_routes import router as responses_router
from src.api.routes import router as main_router
from src.api.sessions import SessionStore
from src.core.types import ChatRequest
from src.mcp_bridge import McpManager, McpServerSpec
from src.providers.base import BaseChatProvider

from .conftest import FakeProvider, FakeRouter

SERVER = Path(__file__).parent / "assets" / "mock_mcp_server.py"


class ToolThenAnswerProvider(BaseChatProvider):
    """Calls a tool on the first turn, then answers using the tool result."""

    name = "loopfake"
    supports_tools = True
    available_models = ("loopfake",)

    def __init__(self):
        pass

    async def generate(self, request: ChatRequest):
        tool_result = next((m.content for m in request.messages if m.role == "tool"), None)
        if tool_result is not None:
            yield f"The tool said: {tool_result}"
        else:
            yield '<tool_call>{"name": "mock__echo", "arguments": {"text": "ping"}}</tool_call>'


class SessionProvider(BaseChatProvider):
    """A chat-box provider that continues one thread (supports_thread_continuation).

    Records the messages handed to each ``send`` so a test can assert the loop
    sends the full context on turn 0 and only the delta afterwards.
    """

    name = "sessionfake"
    supports_tools = True
    supports_thread_continuation = True
    available_models = ("sessionfake",)

    def __init__(self):
        self.sends: list[list[tuple[str, str]]] = []

    @asynccontextmanager
    async def open_session(self):
        yield _FakeSession(self)

    async def generate(self, request):  # unused on the continuation path
        yield ""


class _FakeSession:
    def __init__(self, provider: SessionProvider):
        self._p = provider
        self._turn = 0

    async def send(self, request):
        self._p.sends.append([(m.role, m.content) for m in request.messages])
        turn, self._turn = self._turn, self._turn + 1
        if turn == 0:
            yield '<tool_call>{"name": "mock__echo", "arguments": {"text": "ping"}}</tool_call>'
        else:
            tool = next((c for r, c in self._p.sends[-1] if r == "tool"), None)
            yield f"The tool said: {tool}"


def make_app(provider, mcp=None) -> FastAPI:
    app = FastAPI()
    app.include_router(main_router)
    app.include_router(responses_router)
    app.state.router = FakeRouter(provider)
    app.state.sessions = SessionStore()
    if mcp is not None:
        app.state.mcp = mcp
    return app


def client_for(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def mcp():
    manager = McpManager(
        [McpServerSpec(label="mock", transport="stdio", command=sys.executable, args=[str(SERVER)])]
    )
    await manager.startup()
    try:
        yield manager
    finally:
        await manager.shutdown()


async def test_simple_response_no_tools():
    async with client_for(make_app(FakeProvider(deltas=["Hello ", "world"]))) as client:
        body = (await client.post("/v1/responses", json={"model": "m", "input": "hi"})).json()
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert body["id"].startswith("resp_")
    assert body["output_text"] == "Hello world"
    assert body["output"][0]["type"] == "message"
    assert body["output"][0]["content"][0]["text"] == "Hello world"


async def test_previous_response_id_carries_history():
    app = make_app(FakeProvider(deltas=["ok"]))
    async with client_for(app) as client:
        first = (await client.post("/v1/responses", json={"model": "m", "input": "my name is Alice"})).json()
        second = (
            await client.post(
                "/v1/responses",
                json={"model": "m", "input": "what is my name?", "previous_response_id": first["id"]},
            )
        ).json()

    assert second["previous_response_id"] == first["id"]
    stored = [(m.role, m.content) for m in app.state.sessions.get(second["id"]).messages]
    assert ("user", "my name is Alice") in stored
    assert ("user", "what is my name?") in stored


async def test_unknown_response_id_is_404():
    async with client_for(make_app(FakeProvider())) as client:
        resp = await client.post(
            "/v1/responses", json={"model": "m", "input": "hi", "previous_response_id": "resp_nope"}
        )
    assert resp.status_code == 404


async def test_agentic_loop_executes_mcp_tool(mcp):
    async with client_for(make_app(ToolThenAnswerProvider(), mcp=mcp)) as client:
        body = (
            await client.post("/v1/responses", json={"model": "loopfake", "input": "use the tool"})
        ).json()

    assert body["status"] == "completed"
    types = [item["type"] for item in body["output"]]
    assert "mcp_call" in types and "message" in types
    mcp_call = next(i for i in body["output"] if i["type"] == "mcp_call")
    assert mcp_call["name"] == "mock__echo"
    assert mcp_call["output"] == "echo: ping"
    assert body["output_text"] == "The tool said: echo: ping"


async def test_continuation_sends_system_prompt_once_then_deltas(mcp):
    """A chat-box provider holds one thread: turn 0 gets the system prompt +
    history, turn 1 gets only the appended tool result (not the whole transcript
    nor the model's own tool-call turn)."""
    provider = SessionProvider()
    async with client_for(make_app(provider, mcp=mcp)) as client:
        body = (
            await client.post(
                "/v1/responses", json={"model": "sessionfake", "input": "use the tool"}
            )
        ).json()

    assert body["status"] == "completed"
    assert body["output_text"] == "The tool said: echo: ping"
    # Two turns were sent into the one thread.
    assert len(provider.sends) == 2
    # Turn 0: full context — the tools preamble (system) + the user input.
    roles_0 = [r for r, _ in provider.sends[0]]
    assert "system" in roles_0 and "user" in roles_0
    # Turn 1: only the delta — the tool result, nothing re-sent.
    assert provider.sends[1] == [("tool", "echo: ping")]


async def test_unknown_tool_requires_action():
    provider = FakeProvider(deltas=['<tool_call>{"name": "client_fn", "arguments": {}}</tool_call>'])
    async with client_for(make_app(provider)) as client:
        body = (
            await client.post(
                "/v1/responses",
                json={
                    "model": "m",
                    "input": "do it",
                    "tools": [{"type": "function", "function": {"name": "client_fn", "parameters": {}}}],
                },
            )
        ).json()
    assert body["status"] == "requires_action"
    fc = next(i for i in body["output"] if i["type"] == "function_call")
    assert fc["name"] == "client_fn"


async def test_streaming_response_events():
    async with client_for(make_app(FakeProvider(deltas=["Hi ", "there"]))) as client:
        async with client.stream(
            "POST", "/v1/responses", json={"model": "m", "input": "hi", "stream": True}
        ) as resp:
            events, deltas = [], []
            async for line in resp.aiter_lines():
                if line.startswith("event: "):
                    events.append(line[len("event: "):])
                elif line.startswith("data: "):
                    payload = json.loads(line[len("data: "):])
                    if "delta" in payload:
                        deltas.append(payload["delta"])

    assert events[0] == "response.created"
    assert events[-1] == "response.completed"
    assert "".join(deltas) == "Hi there"
