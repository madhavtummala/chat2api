"""Text-based tool-call emulation.

Chat UIs don't expose a model's native function-calling channel, so we instruct
the model (via a prompt preamble) to emit tool calls as a sentinel-wrapped JSON
block in its visible reply, then parse them back out of the streamed text:

    ⟦tool_call⟧{"name": "get_weather", "arguments": {"city": "Paris"}}⟦/tool_call⟧

:class:`ToolCallParser` is a streaming parser: feed it text deltas and it yields
``TextEvent``/``ToolCallEvent`` objects, holding back only the minimum needed so
a tag split across two deltas is never leaked as content.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Collection, Iterator

# Deliberately NOT angle brackets. A chat UI renders the reply as HTML, and an
# unknown element like `<tool_call>` is sanitised away before it reaches the DOM
# — the tags vanish in transit and the call arrives as bare JSON prose, with
# nothing downstream able to tell it was ever a call. These delimiters survive
# the round trip verbatim and are rare enough not to collide with real prose.
OPEN = "⟦tool_call⟧"
CLOSE = "⟦/tool_call⟧"


@dataclass(slots=True)
class TextEvent:
    text: str


@dataclass(slots=True)
class ToolCallEvent:
    name: str
    arguments: str  # JSON-encoded string, per the OpenAI wire format


Event = TextEvent | ToolCallEvent


def render_tool_calls(text: str, calls: list[dict[str, Any]]) -> str:
    """Re-render OpenAI-shaped tool calls back into the sentinel format.

    The inverse of :class:`ToolCallParser`: an assistant turn that called tools
    has to go back into the transcript looking exactly like what we asked the
    model to emit, or a multi-turn tool loop stops making sense once flattened
    into a single prompt. Used both for a client's prior turns (``/v1/chat/
    completions``) and for the ones the Responses loop records itself, so the
    sentinels live in one place and cannot drift apart.
    """
    rendered = "\n".join(
        f"{OPEN}{json.dumps({'name': c['function']['name'], 'arguments': _loads(c['function']['arguments'])})}{CLOSE}"
        for c in calls
    )
    return f"{text}\n{rendered}".strip() if text else rendered


def _loads(arguments: str) -> Any:
    """Decode an arguments JSON string, passing malformed input through as-is."""
    try:
        return json.loads(arguments or "{}")
    except (json.JSONDecodeError, TypeError):
        return arguments


#: A fenced code block, with or without a language tag.
_FENCE = re.compile(r"```[a-zA-Z]*\n(.*?)```", re.DOTALL)


def normalize_tool_calls(text: str, names: Collection[str]) -> str:
    """Rewrite calls the model wrote in its *own* notation into our sentinels.

    Instruction-tuned models are heavily trained to present a function call as a
    fenced ``json`` block, or as a bare JSON object after a sentence of
    narration, and a mid-size model will often do that no matter how the
    preamble asks. Those replies carry a perfectly good call that we would
    otherwise hand back as prose — the client sees the model "describing" a
    call it actually made.

    Rewriting is gated on ``names``: only an object naming a tool this request
    actually advertised becomes a call. That is what keeps a reply which merely
    *discusses* JSON from being mangled into a phantom call. Text already
    carrying a sentinel is returned untouched — the model complied, and a second
    interpretation could only make it worse.
    """
    if not names or OPEN in text:
        return text

    def _call(obj: Any) -> str | None:
        if not isinstance(obj, dict) or obj.get("name") not in names:
            return None
        return OPEN + json.dumps(
            {"name": obj["name"], "arguments": obj.get("arguments", {})}
        ) + CLOSE

    # Unwrap fences whose whole body is a call, so the backticks don't survive
    # as stray text once the call inside them is lifted out.
    def _unfence(match: re.Match) -> str:
        try:
            obj = json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            return match.group(0)
        return _call(obj) or match.group(0)

    text = _FENCE.sub(_unfence, text)
    if OPEN in text:
        return text

    # Then any bare object sitting in prose. raw_decode finds where each one
    # ends, so a call embedded mid-sentence is lifted without guessing.
    decoder = json.JSONDecoder()
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] != "{":
            out.append(text[i])
            i += 1
            continue
        try:
            obj, end = decoder.raw_decode(text, i)
        except ValueError:
            out.append(text[i])
            i += 1
            continue
        rendered = _call(obj)
        if rendered is None:
            out.append(text[i])
            i += 1
        else:
            out.append(rendered)
            i = end
    return "".join(out)


def build_tools_preamble(tools: list[dict[str, Any]], required: bool) -> str:
    lines = [
        "You have access to the tools listed below.",
        "To call a tool, output a block in EXACTLY this format and nothing else:",
        f'{OPEN}{{"name": "<tool_name>", "arguments": {{<json-args>}}}}{CLOSE}',
        "You may emit several such blocks. If no tool is needed, reply normally.",
        "Do NOT put the block in a markdown code fence, and do NOT describe the "
        "call in prose — emit the raw block exactly as shown above.",
        "",
        "Available tools:",
    ]
    for tool in tools:
        fn = tool.get("function", tool)
        name = fn.get("name", "")
        desc = fn.get("description", "") or ""
        params = fn.get("parameters") or {}
        lines.append(f"- {name}: {desc}".rstrip())
        lines.append(f"  parameters (JSON Schema): {json.dumps(params)}")
    if required:
        lines.append("")
        lines.append("You MUST respond with a tool call.")
    return "\n".join(lines)


class ToolCallParser:
    def __init__(self) -> None:
        self._buf = ""
        self._inside = False  # currently between OPEN and CLOSE

    def feed(self, chunk: str) -> list[Event]:
        self._buf += chunk
        return list(self._drain(final=False))

    def finish(self) -> list[Event]:
        return list(self._drain(final=True))

    def _drain(self, final: bool) -> Iterator[Event]:
        while True:
            if not self._inside:
                idx = self._buf.find(OPEN)
                if idx == -1:
                    if final:
                        if self._buf:
                            yield TextEvent(self._buf)
                            self._buf = ""
                        return
                    # Hold back a suffix that might be the start of OPEN.
                    hold = _partial_suffix(self._buf, OPEN)
                    cut = len(self._buf) - hold
                    if cut > 0:
                        yield TextEvent(self._buf[:cut])
                        self._buf = self._buf[cut:]
                    return
                if idx > 0:
                    yield TextEvent(self._buf[:idx])
                self._buf = self._buf[idx + len(OPEN):]
                self._inside = True
            else:
                cidx = self._buf.find(CLOSE)
                if cidx == -1:
                    if final:
                        # Unterminated block — surface it verbatim, don't drop it.
                        yield TextEvent(OPEN + self._buf)
                        self._buf = ""
                        self._inside = False
                    return
                raw = self._buf[:cidx]
                self._buf = self._buf[cidx + len(CLOSE):]
                self._inside = False
                event = _parse_call(raw)
                # Malformed JSON — surface verbatim so nothing is silently lost.
                yield event if event else TextEvent(OPEN + raw + CLOSE)


def _parse_call(raw: str) -> ToolCallEvent | None:
    try:
        data = json.loads(raw.strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "name" not in data:
        return None
    args = data.get("arguments", {})
    if not isinstance(args, str):
        args = json.dumps(args)
    return ToolCallEvent(name=str(data["name"]), arguments=args)


def _partial_suffix(buf: str, token: str) -> int:
    """Length of the longest suffix of ``buf`` that is a proper prefix of ``token``."""
    for k in range(min(len(buf), len(token) - 1), 0, -1):
        if buf.endswith(token[:k]):
            return k
    return 0
