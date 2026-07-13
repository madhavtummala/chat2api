import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.mcp_bridge import McpManager, McpServerSpec

from .conftest import FakeProvider, make_app

SERVER = Path(__file__).parent / "assets" / "mock_mcp_server.py"


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


async def test_lists_tools_as_openai_defs(mcp):
    assert mcp.has_tools
    names = {t["function"]["name"] for t in mcp.openai_tools()}
    assert names == {"mock__echo", "mock__add"}
    echo = next(t for t in mcp.openai_tools() if t["function"]["name"] == "mock__echo")
    assert "text" in echo["function"]["parameters"]["properties"]


async def test_calls_tool_and_routes_by_label(mcp):
    assert mcp.owns("mock__echo")
    assert not mcp.owns("other__x")
    assert await mcp.call_tool("mock__echo", {"text": "hi"}) == "echo: hi"
    assert await mcp.call_tool("mock__add", {"a": 2, "b": 3}) == "5"


async def test_mcp_tools_injected_and_delegated_in_chat_completions(mcp):
    # Model chooses to call an MCP-advertised tool; chat completions delegates it.
    deltas = [
        "One sec.",
        '<tool_call>{"name": "mock__echo", "arguments": {"text": "hi"}}</tool_call>',
    ]
    app = make_app(FakeProvider(deltas=deltas))
    app.state.mcp = mcp
    client = TestClient(app)

    resp = client.post(
        "/v1/chat/completions",
        json={"model": "fake-1", "messages": [{"role": "user", "content": "hi"}]},
    )
    choice = resp.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    call = choice["message"]["tool_calls"][0]
    assert call["function"]["name"] == "mock__echo"
    assert json.loads(call["function"]["arguments"]) == {"text": "hi"}
