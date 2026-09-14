from src.core.tools import (
    OPEN,
    CLOSE,
    TextEvent,
    ToolCallEvent,
    ToolCallParser,
    build_tools_preamble,
    normalize_tool_calls,
)


def _run(chunks: list[str]) -> list:
    parser = ToolCallParser()
    events: list = []
    for chunk in chunks:
        events.extend(parser.feed(chunk))
    events.extend(parser.finish())
    return events


def _text(events) -> str:
    return "".join(e.text for e in events if isinstance(e, TextEvent))


def _calls(events) -> list[ToolCallEvent]:
    return [e for e in events if isinstance(e, ToolCallEvent)]


def test_plain_text_passes_through():
    events = _run(["hello ", "world"])
    assert _text(events) == "hello world"
    assert _calls(events) == []


def test_single_tool_call_extracted():
    blob = 'Sure.' + OPEN + '{"name": "get_weather", "arguments": {"city": "Paris"}}' + CLOSE
    events = _run([blob])
    assert _text(events) == "Sure."
    (call,) = _calls(events)
    assert call.name == "get_weather"
    assert call.arguments == '{"city": "Paris"}'


def test_tag_split_across_deltas_is_not_leaked():
    # Split right in the middle of the opening tag.
    full = OPEN + '{"name": "f", "arguments": {}}' + CLOSE
    chunks = [full[i:i + 3] for i in range(0, len(full), 3)]
    events = _run(chunks)
    assert _text(events) == ""  # no partial "<tool_" leaked as content
    (call,) = _calls(events)
    assert call.name == "f"
    assert call.arguments == "{}"


def test_multiple_tool_calls():
    blob = (
        OPEN + '{"name": "a", "arguments": {"x": 1}}' + CLOSE
        + " and "
        + OPEN + '{"name": "b", "arguments": {}}' + CLOSE
    )
    events = _run([blob])
    calls = _calls(events)
    assert [c.name for c in calls] == ["a", "b"]
    assert _text(events) == " and "


def test_malformed_json_is_surfaced_as_text_not_dropped():
    blob = OPEN + "not json" + CLOSE
    events = _run([blob])
    assert _calls(events) == []
    assert _text(events) == blob


def test_arguments_serialised_as_json_string():
    events = _run([OPEN + '{"name": "n", "arguments": {"a": [1, 2], "b": "x"}}' + CLOSE])
    (call,) = _calls(events)
    # Arguments must be a JSON *string*, per OpenAI's wire format.
    assert isinstance(call.arguments, str)
    assert '"a": [1, 2]' in call.arguments


def test_preamble_lists_tool_names():
    preamble = build_tools_preamble(
        [{"function": {"name": "get_weather", "description": "d", "parameters": {}}}],
        required=True,
    )
    assert "get_weather" in preamble
    assert OPEN in preamble
    assert "MUST" in preamble


def _normalized(text: str, names=("web_search",)) -> list:
    return _run([normalize_tool_calls(text, set(names))])


def test_prefixed_call_notation_becomes_a_call():
    events = _normalized('call:web_search{"query": "why did XSD move today"}')
    assert _text(events) == ""
    (call,) = _calls(events)
    assert call.name == "web_search"
    assert call.arguments == '{"query": "why did XSD move today"}'


def test_prefixed_calls_run_together_are_all_recovered():
    blob = (
        'call:web_search{"query": "why did XSD move today 2026-09-14"}'
        'call:web_search{"query": "IBIT price move 2026-09-14"}'
        'call:web_search{"query": "IAU gold price move 2026-09-14"}'
    )
    events = _normalized(blob)
    assert _text(events) == ""
    assert [c.name for c in _calls(events)] == ["web_search"] * 3


def test_prefixed_call_with_narration_and_parens():
    events = _normalized('Let me look.\ncall: web_search({"query": "gold"})')
    assert _text(events).strip() == "Let me look."
    (call,) = _calls(events)
    assert call.arguments == '{"query": "gold"}'


def test_prefixed_call_for_unadvertised_tool_stays_prose():
    text = 'call:rm_rf{"path": "/"}'
    events = _normalized(text)
    assert _text(events) == text
    assert _calls(events) == []
