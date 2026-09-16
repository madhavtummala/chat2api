import json

from fastapi.testclient import TestClient

from src.api.audit import (
    AuditMiddleware,
    audit_request,
    format_entry,
    format_report,
    normalize_body,
)

from .conftest import FakeProvider, make_app


def categories(audit):
    return dict(audit.by_category())


def test_tool_schemas_are_attributed_per_tool_and_server():
    body = {
        "model": "fake-1",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "mcp__notion__search",
                    "description": "x" * 500,
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {"name": "Read", "description": "read a file"},
            },
        ],
    }
    audit = audit_request(body)
    cats = categories(audit)
    assert cats["tools/mcp:notion"] > 500
    assert 0 < cats["tools/builtin"] < 200
    assert audit.tool_count == 2
    # The preamble really is in the prompt, so it must be counted in the total.
    assert audit.prompt_chars > cats["tools/mcp:notion"]


def test_tool_choice_none_means_no_preamble():
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "Read"}}],
        "tool_choice": "none",
    }
    assert not any(c.startswith("tools/") for c in categories(audit_request(body)))


def test_injections_are_split_out_of_their_carrier_message():
    body = {
        "messages": [
            {
                "role": "user",
                "content": "do a thing<system-reminder>" + "s" * 800 + "</system-reminder>",
            }
        ]
    }
    cats = categories(audit_request(body))
    assert cats["injected/system-reminder"] > 800
    assert cats["user-messages"] < 50  # only "do a thing" is left


def test_skill_payloads_get_their_own_category():
    body = {
        "messages": [
            {"role": "user", "content": "<command-name>dataviz</command-name>" + "x" * 100}
        ]
    }
    assert "skills/commands" in categories(audit_request(body))


def test_tool_output_is_attributed_per_tool():
    body = {
        "messages": [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "1",
                        "type": "function",
                        "function": {"name": "Bash", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "name": "Bash", "tool_call_id": "1", "content": "o" * 900},
        ]
    }
    cats = categories(audit_request(body))
    assert cats["tool-output/Bash"] > 900
    assert cats["assistant/tool-calls"] > 0


def test_tool_output_is_named_from_the_call_it_answers():
    # Most clients omit `name` on a tool result and identify it by tool_call_id.
    body = {
        "messages": [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "walbot__list_accounts", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "o" * 500},
        ]
    }
    cats = categories(audit_request(body))
    assert cats["tool-output/walbot__list_accounts"] > 500
    assert not any(c.endswith("/unknown") for c in cats)


def test_namespaced_tools_are_grouped_by_their_prefix():
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {"type": "function", "function": {"name": "walbot__place_orders", "description": "d"}},
            {"type": "function", "function": {"name": "walbot__list_accounts", "description": "d"}},
            {"type": "function", "function": {"name": "web_search", "description": "d"}},
        ],
    }
    cats = categories(audit_request(body))
    assert "tools/ns:walbot" in cats and "tools/builtin" in cats
    assert cats["tools/ns:walbot"] > cats["tools/builtin"]


def test_long_system_prompt_is_split_at_its_headings():
    body = {
        "messages": [
            {"role": "system", "content": "# Alpha\n" + "a" * 3000 + "\n# Beta\n" + "b" * 50},
            {"role": "user", "content": "hi"},
        ]
    }
    labels = [s.label for s in audit_request(body).segments if s.category == "system-prompt"]
    assert any("Alpha" in label for label in labels)
    assert any("Beta" in label for label in labels)


def test_segments_account_for_the_whole_prompt():
    body = {
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
        ],
        "tools": [{"type": "function", "function": {"name": "Read", "description": "d"}}],
    }
    audit = audit_request(body)
    assert sum(s.chars for s in audit.segments) == audit.prompt_chars


def test_responses_body_is_normalised_to_messages():
    body = normalize_body(
        {"instructions": "be brief", "input": [{"role": "user", "content": "hi"}]}
    )
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert audit_request(body, path="/v1/responses").message_count == 2


def test_report_flags_a_prompt_over_the_limit():
    audit = audit_request({"messages": [{"role": "user", "content": "x" * 200}]})
    assert "OVER LIMIT" in format_report(audit, limit_chars=100)
    assert "OVER LIMIT" not in format_report(audit, limit_chars=100_000)


def test_middleware_logs_and_persists_without_disturbing_the_response(tmp_path, caplog):
    app = make_app(FakeProvider())
    app.add_middleware(AuditMiddleware, audit_dir=tmp_path, limit_chars=10)
    client = TestClient(app)

    with caplog.at_level("INFO"):
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "fake-1", "messages": [{"role": "user", "content": "hi" * 40}]},
        )

    assert resp.json()["choices"][0]["message"]["content"] == "Hello world"
    assert "prompt audit" in caplog.text and "OVER LIMIT" in caplog.text
    records = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert records[0]["prompt_chars"] == 80
    assert len(list((tmp_path / "bodies").glob("*.json"))) == 1


def test_entry_carries_the_exact_prompt_next_to_its_analysis():
    audit = audit_request(
        {
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "hello"},
            ]
        }
    )
    # What the provider types into the composer, verbatim.
    assert audit.prompt == "System: be brief\n\nUser: hello\n\nAssistant:"
    entry = format_entry(audit)
    assert audit.prompt in entry
    assert "by category:" in entry
    assert "prompt sent to the provider" in entry


def test_prompts_log_grows_one_entry_per_request(tmp_path):
    app = make_app(FakeProvider())
    app.add_middleware(AuditMiddleware, audit_dir=tmp_path)
    client = TestClient(app)

    for text in ("first question", "second question"):
        client.post(
            "/v1/chat/completions",
            json={"model": "fake-1", "messages": [{"role": "user", "content": text}]},
        )

    log = (tmp_path / "prompts.log").read_text()
    assert log.count("prompt sent to the provider") == 2
    assert "first question" in log and "second question" in log


def test_middleware_serves_the_request_even_when_the_body_is_unparseable(tmp_path):
    app = make_app(FakeProvider())
    app.add_middleware(AuditMiddleware, audit_dir=tmp_path)
    client = TestClient(app)

    resp = client.post(
        "/v1/chat/completions",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 422  # FastAPI's own error, not a middleware crash
