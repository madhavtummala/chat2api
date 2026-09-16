"""Per-request prompt accounting: what is actually filling the prompt.

A browser-driven chat UI takes one free-text turn, and the site caps how long
that turn may be (~100k characters on the composers we drive). An agent client
blows past that cap without ever showing why: its system prompt, every MCP
server's tool schemas, injected skill text and a handful of fat tool outputs all
end up concatenated into the same string by :func:`flatten_messages`.

This module takes an inbound request apart and attributes every character of the
prompt we are about to send, so the biggest contributor is visible rather than
guessed at. It is read-only: :class:`AuditMiddleware` never alters the request or
the response, so an audited server behaves exactly like an unaudited one.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..core.messages import flatten_messages
from ..core.tools import build_tools_preamble
from ..core.types import ChatMessage
from .schemas import ChatCompletionRequest

logger = logging.getLogger(__name__)

#: Claude-Code-style out-of-band injections (memory, skill hints, reminders).
#: They ride inside an otherwise ordinary message, so they have to be carved out
#: before the surrounding text can be measured honestly.
_REMINDER = re.compile(r"<system-reminder>(.*?)</system-reminder>", re.S)
#: A slash-command / skill payload, which clients wrap in these tags.
_COMMAND = re.compile(
    r"<command-(?:name|message|args|contents)>(.*?)</command-\w+>", re.S
)
#: A markdown heading, used to split one huge system prompt into named sections.
_HEADING = re.compile(r"^(#{1,3})\s+(.+)$", re.M)
#: `mcp__<server>__<tool>` — the conventional name for a tool reached over MCP.
_MCP_TOOL = re.compile(r"^mcp__([^_]+(?:_[^_]+)*?)__")
#: Any other `<namespace>__<tool>` prefix. Clients namespace a whole server's
#: tools this way without the `mcp__` convention (`walbot__place_orders`), and
#: those tools are exactly the ones you disconnect as a group — so they get
#: grouped as one, rather than disappearing into a flat "builtin" bucket.
_NAMESPACED_TOOL = re.compile(r"^([A-Za-z0-9]+)__")


@dataclass(slots=True)
class Segment:
    """One attributed slice of the prompt."""

    category: str  # coarse bucket, e.g. "tools/mcp:notion"
    label: str  # the specific thing, e.g. "notion-create-pages"
    chars: int


@dataclass
class Audit:
    request_id: str
    path: str
    model: str
    stream: bool
    body_bytes: int
    prompt_chars: int
    # The exact string the provider types into the composer — what the site's
    # length cap applies to. Kept verbatim so the log shows the request we made,
    # not a reconstruction of it.
    prompt: str
    message_count: int
    tool_count: int
    segments: list[Segment] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def by_category(self) -> list[tuple[str, int]]:
        totals: dict[str, int] = {}
        for seg in self.segments:
            totals[seg.category] = totals.get(seg.category, 0) + seg.chars
        return sorted(totals.items(), key=lambda kv: -kv[1])

    def largest(self, n: int = 12) -> list[Segment]:
        return sorted(self.segments, key=lambda s: -s.chars)[:n]

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "timestamp": self.timestamp,
            "path": self.path,
            "model": self.model,
            "stream": self.stream,
            "body_bytes": self.body_bytes,
            "prompt_chars": self.prompt_chars,
            "message_count": self.message_count,
            "tool_count": self.tool_count,
            "categories": dict(self.by_category()),
            "segments": [
                {"category": s.category, "label": s.label, "chars": s.chars}
                for s in self.segments
            ],
        }


# ---- normalisation -------------------------------------------------------
def normalize_body(body: dict[str, Any]) -> dict[str, Any]:
    """Coerce a Responses-API body into the Chat-Completions shape.

    Both endpoints end up in the same flattened prompt, so auditing them through
    one code path keeps the numbers comparable — and means a client that switches
    endpoints doesn't silently drop out of the log.
    """
    if "messages" in body:
        return body
    if "input" not in body:
        return body
    messages: list[dict[str, Any]] = []
    if body.get("instructions"):
        messages.append({"role": "system", "content": body["instructions"]})
    raw = body["input"]
    if isinstance(raw, str):
        messages.append({"role": "user", "content": raw})
    else:
        for item in raw or []:
            if isinstance(item, dict):
                messages.append(
                    {"role": item.get("role", "user"), "content": item.get("content")}
                )
    out = dict(body)
    out["messages"] = messages or [{"role": "user", "content": ""}]
    out.pop("input", None)
    return out


# ---- segmentation --------------------------------------------------------
def _tool_bucket(name: str) -> str:
    """Group a tool by where it came from: one bucket per server/namespace."""
    if match := _MCP_TOOL.match(name):
        return f"tools/mcp:{match.group(1)}"
    if match := _NAMESPACED_TOOL.match(name):
        return f"tools/ns:{match.group(1)}"
    return "tools/builtin"


def _snippet(text: str, width: int = 60) -> str:
    """A one-line handle for a block of text, for the label column."""
    line = " ".join(text.strip().split())
    return line[:width] + ("…" if len(line) > width else "")


def _split_injections(text: str, origin: str) -> tuple[str, list[Segment]]:
    """Carve out reminders/skill payloads, returning (remainder, segments).

    These blocks are the ones clients inject without the user ever seeing them,
    which is exactly why they need their own line in the report.
    """
    segments: list[Segment] = []

    def take(pattern: re.Pattern[str], category: str) -> None:
        nonlocal text
        for match in pattern.finditer(text):
            inner = match.group(1)
            segments.append(
                Segment(category, f"{origin}: {_snippet(inner)}", len(match.group(0)))
            )
        text = pattern.sub("", text)

    take(_COMMAND, "skills/commands")
    take(_REMINDER, "injected/system-reminder")
    return text, segments


def _split_sections(text: str, category: str, origin: str) -> list[Segment]:
    """Split a long prompt into its markdown sections.

    A 40k-character system prompt is one number nobody can act on; the same
    prompt broken at its headings shows which half of it is worth trimming.
    Short text (or text with no headings) stays a single segment.
    """
    headings = list(_HEADING.finditer(text))
    if len(headings) < 2 or len(text) < 2_000:
        return [Segment(category, origin, len(text))] if text.strip() else []

    segments: list[Segment] = []
    preamble = text[: headings[0].start()]
    if preamble.strip():
        segments.append(Segment(category, f"{origin} (preamble)", len(preamble)))
    for i, match in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        segments.append(
            Segment(category, f"{origin} § {_snippet(match.group(2), 48)}",
                    end - match.start())
        )
    return segments


def _call_names(messages: list[Any]) -> dict[str, str]:
    """Map ``tool_call_id`` -> tool name, from the assistant turns that called.

    A tool result is the fattest thing in an agent prompt, so it has to be
    attributed to the tool that produced it. Most clients leave ``name`` off the
    result message and identify it only by ``tool_call_id``, which means the
    name has to be recovered from the call it answers.
    """
    return {
        call.id: call.function.name
        for msg in messages
        for call in (msg.tool_calls or [])
    }


def _message_segments(index: int, msg: Any, call_names: dict[str, str]) -> list[Segment]:
    """Attribute one message's rendered text."""
    role = msg.role
    origin = f"msg[{index}] {role}"
    text = msg.render()
    text, segments = _split_injections(text, origin)

    if role == "system":
        segments += _split_sections(text, "system-prompt", origin)
    elif role == "tool":
        name = msg.name or call_names.get(msg.tool_call_id or "", "unknown")
        segments.append(Segment(f"tool-output/{name}", f"{origin} {name}", len(text)))
    elif role == "assistant":
        calls = msg.tool_calls or []
        category = "assistant/tool-calls" if calls else "assistant/text"
        segments.append(Segment(category, origin, len(text)))
    else:
        segments += _split_sections(text, f"{role}-messages", origin)
    return segments


def _tool_segments(tool_defs: list[dict[str, Any]], required: bool) -> list[Segment]:
    """Attribute the tools preamble, per tool, exactly as it is rendered.

    Tool schemas are the contributor clients least expect to pay for: they are
    sent as structured JSON and never appear in the conversation, yet we render
    every one of them into the prompt text. Measuring each tool's own rendered
    block (rather than the raw JSON) is what makes the number comparable to the
    rest of the prompt.
    """
    if not tool_defs:
        return []
    segments = [
        Segment(
            "tools/preamble-boilerplate",
            "tool-call instructions",
            len(build_tools_preamble([], required)),
        )
    ]
    for tool in tool_defs:
        fn = tool.get("function", tool)
        name = fn.get("name", "?")
        # The delta between "preamble with this tool" and "preamble without any"
        # is precisely the characters this tool costs, newline included.
        rendered = len(build_tools_preamble([tool], required)) - len(
            build_tools_preamble([], required)
        )
        segments.append(Segment(_tool_bucket(name), name, rendered))
    return segments


def audit_request(
    body: dict[str, Any],
    *,
    path: str = "/v1/chat/completions",
    request_id: str | None = None,
    extra_tools: Iterable[dict[str, Any]] = (),
) -> Audit:
    """Break one inbound request into attributed prompt segments.

    ``extra_tools`` are the tools the server itself adds (MCP tools loaded from
    config), which the client never sent but still pays for in the prompt.
    """
    body = normalize_body(body)
    req = ChatCompletionRequest.model_validate(body)
    chat = req.to_chat_request()

    tool_defs = [t.model_dump() for t in req.tools or []] + list(extra_tools)
    use_tools = bool(tool_defs) and req.tool_choice != "none"
    required = req.tool_choice == "required" or isinstance(req.tool_choice, dict)

    messages = list(chat.messages)
    if use_tools:
        preamble = build_tools_preamble(tool_defs, required)
        messages.insert(0, ChatMessage(role="system", content=preamble))
    prompt = flatten_messages(messages)

    segments: list[Segment] = []
    if use_tools:
        segments += _tool_segments(tool_defs, required)
    call_names = _call_names(req.messages)
    for i, msg in enumerate(req.messages):
        segments += _message_segments(i, msg, call_names)
    for att in chat.attachments:
        segments.append(Segment("attachments", att.name, len(att.data)))

    # flatten_messages adds role labels and blank lines between turns; that is
    # real prompt spend, so it gets a line rather than quietly skewing the rest.
    accounted = sum(s.chars for s in segments if s.category != "attachments")
    if prompt and (overhead := len(prompt) - accounted) > 0:
        segments.append(Segment("envelope", "role labels + separators", overhead))

    return Audit(
        request_id=request_id or uuid.uuid4().hex[:12],
        path=path,
        model=req.model,
        stream=req.stream,
        body_bytes=len(json.dumps(body)),
        prompt_chars=len(prompt),
        prompt=prompt,
        message_count=len(req.messages),
        tool_count=len(tool_defs),
        segments=segments,
    )


# ---- reporting -----------------------------------------------------------
def format_report(audit: Audit, limit_chars: int = 0, top: int = 12) -> str:
    """A human-readable breakdown, biggest contributor first."""
    total = audit.prompt_chars or 1
    lines = [
        f"── prompt audit {audit.request_id} · {audit.path} · model={audit.model or '(default)'}",
        f"   prompt: {audit.prompt_chars:,} chars (~{audit.prompt_chars // 4:,} tokens)"
        f" · {audit.message_count} messages · {audit.tool_count} tools",
    ]
    if limit_chars and audit.prompt_chars > limit_chars:
        lines.append(
            f"   !! OVER LIMIT by {audit.prompt_chars - limit_chars:,} chars "
            f"(limit {limit_chars:,})"
        )
    lines.append("   by category:")
    for category, chars in audit.by_category():
        lines.append(f"     {chars:>9,}  {100 * chars / total:5.1f}%  {category}")
    lines.append(f"   largest {top} items:")
    for seg in audit.largest(top):
        lines.append(
            f"     {seg.chars:>9,}  {100 * seg.chars / total:5.1f}%  "
            f"{seg.category} · {seg.label}"
        )
    return "\n".join(lines)


RULE = "═" * 78


def format_entry(audit: Audit, limit_chars: int = 0, top: int = 12) -> str:
    """One log entry: when, the analysis, and the prompt itself, verbatim.

    The console report is a summary you skim; this is the record you go back to.
    Keeping the composed prompt next to its own breakdown in a single entry is
    what lets you read a number ("42k in tool-output/Bash") and immediately see
    the text behind it.
    """
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(audit.timestamp))
    return "\n".join(
        [
            RULE,
            f"{when}  {audit.request_id}  {audit.path}  "
            f"model={audit.model or '(default)'}  stream={audit.stream}",
            RULE,
            format_report(audit, limit_chars, top),
            f"── prompt sent to the provider ({audit.prompt_chars:,} chars) "
            + "─" * 20,
            audit.prompt,
            f"── end {audit.request_id} " + "─" * 50,
            "",
        ]
    )


# ---- middleware ----------------------------------------------------------
class AuditMiddleware:
    """ASGI middleware that audits every JSON POST and logs the breakdown.

    Written against the raw ASGI interface rather than ``BaseHTTPMiddleware``
    because the latter buffers responses, which would break SSE streaming — the
    whole point of this server is that deltas reach the client as they arrive.
    """

    def __init__(
        self,
        app,
        *,
        audit_dir: str | Path,
        save_bodies: bool = True,
        limit_chars: int = 100_000,
        paths: tuple[str, ...] = ("/v1/chat/completions", "/v1/responses"),
    ) -> None:
        self.app = app
        self.dir = Path(audit_dir)
        self.save_bodies = save_bodies
        self.limit_chars = limit_chars
        self.paths = paths

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") not in self.paths:
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []

        async def receive_and_capture():
            message = await receive()
            if message["type"] == "http.request":
                chunks.append(message.get("body", b""))
                if not message.get("more_body"):
                    self._record(scope["path"], b"".join(chunks))
            return message

        await self.app(scope, receive_and_capture, send)

    def _record(self, path: str, raw: bytes) -> None:
        # An audit failure must never take a request down with it: this is
        # observability, and a malformed or unexpected body is exactly the case
        # worth logging rather than raising on.
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                return
            audit = audit_request(body, path=path)
            logger.info("\n%s", format_report(audit, self.limit_chars))
            self._persist(audit, body)
        except Exception:
            logger.exception("Prompt audit failed (request served normally)")

    def _persist(self, audit: Audit, body: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with (self.dir / "audit.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(audit.to_dict()) + "\n")
        with (self.dir / "prompts.log").open("a", encoding="utf-8") as fh:
            fh.write(format_entry(audit, self.limit_chars))
        if self.save_bodies:
            stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(audit.timestamp))
            bodies = self.dir / "bodies"
            bodies.mkdir(parents=True, exist_ok=True)
            (bodies / f"{stamp}-{audit.request_id}.json").write_text(
                json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8"
            )
