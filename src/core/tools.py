"""Text-based tool-call emulation.

Chat UIs don't expose a model's native function-calling channel, so we instruct
the model (via a prompt preamble) to emit tool calls as a sentinel-wrapped JSON
block in its visible reply, then parse them back out of the streamed text:

    <tool_call>{"name": "get_weather", "arguments": {"city": "Paris"}}</tool_call>

:class:`ToolCallParser` is a streaming parser: feed it text deltas and it yields
``TextEvent``/``ToolCallEvent`` objects, holding back only the minimum needed so
a tag split across two deltas is never leaked as content.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterator

OPEN = "<tool_call>"
CLOSE = "</tool_call>"


@dataclass(slots=True)
class TextEvent:
    text: str


@dataclass(slots=True)
class ToolCallEvent:
    name: str
    arguments: str  # JSON-encoded string, per the OpenAI wire format


Event = TextEvent | ToolCallEvent


def build_tools_preamble(tools: list[dict[str, Any]], required: bool) -> str:
    lines = [
        "You have access to the tools listed below.",
        "To call a tool, output a block in EXACTLY this format and nothing else:",
        f'{OPEN}{{"name": "<tool_name>", "arguments": {{<json-args>}}}}{CLOSE}',
        "You may emit several such blocks. If no tool is needed, reply normally.",
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
