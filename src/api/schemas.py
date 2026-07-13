"""OpenAI-compatible request/response models (the public wire format)."""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..core.types import Attachment, ChatMessage, ChatRequest

_DATA_URL = re.compile(r"^data:([^;,]*?)(;base64)?,(.*)$", re.S)


def decode_data_url(url: str | None, default_name: str) -> Attachment | None:
    """Decode a ``data:`` URL into an Attachment (remote URLs are skipped)."""
    if not url:
        return None
    m = _DATA_URL.match(url)
    if not m:
        return None
    mime = m.group(1) or "application/octet-stream"
    payload = m.group(3)
    data = base64.b64decode(payload) if m.group(2) else payload.encode("utf-8")
    ext = mime.split("/")[-1].split("+")[0] if "/" in mime else "bin"
    name = default_name if "." in default_name else f"{default_name}.{ext}"
    return Attachment(name=name, mime=mime, data=data)


# ---- Tool definitions (client -> us) ------------------------------------
class FunctionDef(BaseModel):
    name: str
    description: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)


class Tool(BaseModel):
    type: Literal["function"] = "function"
    function: FunctionDef


# ---- Tool calls (in the transcript / our response) ----------------------
class FunctionCall(BaseModel):
    name: str
    arguments: str = "{}"  # OpenAI encodes arguments as a JSON *string*


class ToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: FunctionCall


class Message(BaseModel):
    role: str
    # Content may be a plain string or the OpenAI "parts" array; we accept both.
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    # Present on prior assistant turns that called tools, and on tool results.
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None

    def text(self) -> str:
        if self.content is None:
            return ""
        if isinstance(self.content, str):
            return self.content
        parts = [
            part.get("text", "")
            for part in self.content
            if isinstance(part, dict) and part.get("type") in ("text", "input_text")
        ]
        return "".join(parts)

    def attachments(self) -> list[Attachment]:
        """Decode file/image content parts into uploadable attachments."""
        if not isinstance(self.content, list):
            return []
        out: list[Attachment] = []
        for i, part in enumerate(self.content):
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind in ("image_url", "input_image"):
                url = (part.get("image_url") or {}).get("url") if isinstance(
                    part.get("image_url"), dict
                ) else part.get("image_url")
                att = decode_data_url(url, f"image-{i}")
            elif kind in ("file", "input_file"):
                f = part.get("file") if isinstance(part.get("file"), dict) else part
                att = decode_data_url(
                    f.get("file_data") or f.get("file_url"),
                    f.get("filename") or f"file-{i}",
                )
            else:
                att = None
            if att:
                out.append(att)
        return out

    def render(self) -> str:
        """Flatten this message (incl. any tool calls/results) to plain text.

        Prior assistant tool calls are re-rendered in the same ``<tool_call>``
        format we ask the model to emit, and tool results are labelled, so a
        multi-turn tool loop stays coherent when flattened into one prompt.
        """
        body = self.text()
        if self.tool_calls:
            rendered = "\n".join(
                f"<tool_call>{json.dumps({'name': tc.function.name, 'arguments': _loads(tc.function.arguments)})}</tool_call>"
                for tc in self.tool_calls
            )
            body = f"{body}\n{rendered}".strip() if body else rendered
        if self.role == "tool":
            who = f" for {self.name}" if self.name else ""
            body = f"[tool result{who}] {body}"
        return body


class ChatCompletionRequest(BaseModel):
    model: str = ""  # empty -> use the provider's default model
    messages: list[Message] = Field(min_length=1)
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    tools: list[Tool] | None = None
    # "none" | "auto" | "required" | {"type":"function","function":{"name":...}}
    tool_choice: str | dict[str, Any] | None = None
    # Web search, accepted from any OpenAI-compatible spelling:
    #   - `web_search_options` (native OpenAI field)   -> presence enables it
    #   - `model` suffix `:online` (OpenRouter convention)
    #   - `web_search` (vendor extension via extra_body)
    web_search_options: dict[str, Any] | None = None
    web_search: bool | None = None
    # Reasoning/"thinking" effort (OpenAI-standard). For providers with a thinking
    # toggle (Perplexity): "minimal"/"none" disables, any other value enables,
    # absent leaves the model default untouched.
    reasoning_effort: str | None = None
    # Optional: route repeated requests to the same browser conversation/tab.
    conversation_id: str | None = None
    user: str | None = None

    def wants_tools(self) -> bool:
        return bool(self.tools) and self.tool_choice != "none"

    def resolve_model(self) -> str:
        return re.sub(r":online$", "", self.model, flags=re.IGNORECASE)

    def resolve_web_search(self) -> bool:
        if self.web_search is not None:
            return self.web_search
        if self.web_search_options is not None:
            return True
        return self.model.lower().endswith(":online")

    def collect_attachments(self) -> list[Attachment]:
        # Attachments belong to the current turn -> the last user message.
        for msg in reversed(self.messages):
            if msg.role == "user":
                return msg.attachments()
        return []

    def to_chat_request(self) -> ChatRequest:
        return ChatRequest(
            messages=[
                ChatMessage(role=m.role, content=m.render(), name=m.name)
                for m in self.messages
            ],
            model=self.resolve_model(),
            conversation_id=self.conversation_id or self.user,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            stream=self.stream,
            web_search=self.resolve_web_search(),
            reasoning_effort=self.reasoning_effort,
            attachments=self.collect_attachments(),
        )


class ModelCard(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int = 0
    owned_by: str = "chat2api"


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelCard]


def _loads(arguments: str) -> Any:
    try:
        return json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return arguments
