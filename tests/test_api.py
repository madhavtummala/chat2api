import json

from src.core.errors import ProviderTimeout

from .conftest import FakeProvider, make_app
from fastapi.testclient import TestClient


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["provider"] == "fake"
    assert "authenticated" in body  # None until a request checks it


def test_list_models(client):
    data = client.get("/v1/models").json()
    assert data["object"] == "list"
    assert {m["id"] for m in data["data"]} == {"fake/fake-1", "fake/fake-2"}
    assert all(m["owned_by"] == "fake" for m in data["data"])


def test_non_streaming_completion(client):
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "fake-1", "messages": [{"role": "user", "content": "hi"}]},
    )
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "Hello world"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["total_tokens"] > 0


def test_streaming_completion(client):
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "fake-1",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
    ) as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = [
            line[len("data: "):]
            for line in resp.iter_lines()
            if line.startswith("data: ")
        ]

    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events if e != "[DONE]"]
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
    content = "".join(
        c["choices"][0]["delta"].get("content", "") for c in chunks
    )
    assert content == "Hello world"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_content_parts_array_is_accepted(client):
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "fake-1",
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "hi"}],
                }
            ],
        },
    )
    assert resp.status_code == 200


def test_provider_error_non_stream_returns_status():
    app = make_app(FakeProvider(deltas=[], error=ProviderTimeout("boom")))
    client = TestClient(app)
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "fake-1", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 504
    assert resp.json()["error"]["type"] == "timeout"


WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get weather for a city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}

# A provider reply that decides to call a tool, streamed in awkward fragments.
TOOL_DELTAS = [
    "Let me check. <tool_",
    'call>{"name": "get_weather", ',
    '"arguments": {"city": "Paris"}}</tool_call>',
]


def _tool_client():
    return TestClient(make_app(FakeProvider(deltas=TOOL_DELTAS)))


def test_tool_call_non_streaming():
    resp = _tool_client().post(
        "/v1/chat/completions",
        json={
            "model": "fake-1",
            "messages": [{"role": "user", "content": "weather in Paris?"}],
            "tools": [WEATHER_TOOL],
        },
    )
    body = resp.json()
    choice = body["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] == "Let me check."
    call = choice["message"]["tool_calls"][0]
    assert call["function"]["name"] == "get_weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "Paris"}
    assert call["id"].startswith("call_")


def test_tool_call_streaming():
    with _tool_client().stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "fake-1",
            "stream": True,
            "messages": [{"role": "user", "content": "weather?"}],
            "tools": [WEATHER_TOOL],
        },
    ) as resp:
        chunks = [
            json.loads(line[len("data: "):])
            for line in resp.iter_lines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]

    # Text content never leaks a partial "<tool_" fragment.
    content = "".join(
        c["choices"][0]["delta"].get("content", "") for c in chunks
    )
    assert content == "Let me check. "
    tool_deltas = [
        c["choices"][0]["delta"]["tool_calls"][0]
        for c in chunks
        if c["choices"][0]["delta"].get("tool_calls")
    ]
    assert len(tool_deltas) == 1
    assert tool_deltas[0]["function"]["name"] == "get_weather"
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


def test_tools_rejected_when_provider_unsupported():
    provider = FakeProvider()
    provider.supports_tools = False
    client = TestClient(make_app(provider))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "fake-1",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [WEATHER_TOOL],
        },
    )
    assert resp.status_code == 400


def test_tool_choice_none_skips_tool_parsing():
    # With tool_choice="none" we should not parse tool calls out of the text.
    client = TestClient(make_app(FakeProvider(deltas=TOOL_DELTAS)))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "fake-1",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [WEATHER_TOOL],
            "tool_choice": "none",
        },
    )
    body = resp.json()
    assert body["choices"][0]["finish_reason"] == "stop"
    assert "tool_calls" not in body["choices"][0]["message"]
    assert "<tool_call>" in body["choices"][0]["message"]["content"]


def test_auth_enforced_when_keys_configured(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "api_keys", "secret", raising=False)
    client = TestClient(make_app(FakeProvider()))

    assert client.get("/v1/models").status_code == 401
    ok = client.get("/v1/models", headers={"Authorization": "Bearer secret"})
    assert ok.status_code == 200
