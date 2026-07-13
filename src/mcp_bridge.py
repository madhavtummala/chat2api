"""MCP client bridge.

Connects to one or more MCP servers (stdio or streamable-HTTP), lists their
tools, and exposes them as OpenAI function definitions for prompt injection.
It can also execute a tool (used by the stateful Responses agentic loop).

Tool names are namespaced ``<server_label>__<tool_name>`` so tools from
different servers never collide and calls can be routed back to the right
server. Named ``mcp_bridge`` (not ``mcp``) to avoid shadowing the SDK package.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger(__name__)

SEP = "__"


@dataclass(slots=True)
class McpServerSpec:
    label: str
    transport: str = "stdio"  # "stdio" | "http"
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    url: str | None = None
    headers: dict[str, str] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "McpServerSpec":
        return cls(
            label=data["label"],
            transport=data.get("transport", "stdio"),
            command=data.get("command"),
            args=list(data.get("args", [])),
            env=data.get("env"),
            url=data.get("url"),
            headers=data.get("headers"),
        )


def load_specs(config_path: str | None) -> list[McpServerSpec]:
    """Load MCP server specs from a JSON file: ``{"servers": [ ... ]}``."""
    if not config_path:
        return []
    path = Path(config_path)
    if not path.exists():
        # The default (mcp.json) is optional — absence just means "no MCP".
        logger.debug("MCP config %s not present; no MCP servers loaded", path)
        return []
    data = json.loads(path.read_text())
    return [McpServerSpec.from_dict(s) for s in data.get("servers", [])]


class McpManager:
    def __init__(self, specs: list[McpServerSpec]):
        self._specs = specs
        self._sessions: dict[str, ClientSession] = {}
        self._tools: dict[str, list] = {}  # label -> [mcp.types.Tool]
        # The connections' anyio cancel scopes are task-bound, so all of
        # open/keep-alive/close happens inside one long-lived `_run` task.
        self._runner: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()

    async def startup(self) -> None:
        if not self._specs:
            return
        # Streamable-HTTP servers that don't support server-initiated messages
        # (e.g. Parallel) make the client log a benign GET-stream 405 reconnect
        # loop; quiet that and the per-request httpx INFO chatter.
        logging.getLogger("mcp.client.streamable_http").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        self._runner = asyncio.create_task(self._run())
        await self._ready.wait()

    async def _run(self) -> None:
        try:
            async with AsyncExitStack() as stack:
                for spec in self._specs:
                    try:
                        session = await self._connect(stack, spec)
                        await session.initialize()
                        tools = (await session.list_tools()).tools
                        self._sessions[spec.label] = session
                        self._tools[spec.label] = tools
                        logger.info("MCP %r connected: %d tool(s)", spec.label, len(tools))
                    except Exception:
                        logger.exception("MCP %r failed to connect", spec.label)
                self._ready.set()
                await self._stop.wait()  # hold sessions open until shutdown
        finally:
            self._ready.set()  # unblock startup even if every connection failed

    async def _connect(self, stack: AsyncExitStack, spec: McpServerSpec) -> ClientSession:
        if spec.transport == "stdio":
            params = StdioServerParameters(
                command=spec.command or "", args=spec.args, env=spec.env
            )
            read, write = await stack.enter_async_context(stdio_client(params))
        elif spec.transport == "http":
            read, write, _ = await stack.enter_async_context(
                streamablehttp_client(spec.url or "", headers=spec.headers)
            )
        else:
            raise ValueError(f"Unknown MCP transport {spec.transport!r}")
        return await stack.enter_async_context(ClientSession(read, write))

    @property
    def has_tools(self) -> bool:
        return any(self._tools.values())

    def openai_tools(self) -> list[dict]:
        """Advertised tools as OpenAI function definitions (namespaced names)."""
        defs: list[dict] = []
        for label, tools in self._tools.items():
            for tool in tools:
                defs.append(
                    {
                        "type": "function",
                        "function": {
                            "name": f"{label}{SEP}{tool.name}",
                            "description": tool.description or "",
                            "parameters": tool.inputSchema or {},
                        },
                    }
                )
        return defs

    def owns(self, qualified_name: str) -> bool:
        label = qualified_name.split(SEP, 1)[0]
        return label in self._sessions

    async def call_tool(self, qualified_name: str, arguments: dict | None) -> str:
        label, _, name = qualified_name.partition(SEP)
        session = self._sessions.get(label)
        if session is None:
            raise KeyError(f"No MCP server for tool {qualified_name!r}")
        result = await session.call_tool(name, arguments or {})
        text = "".join(
            c.text for c in result.content if getattr(c, "type", None) == "text"
        )
        if result.isError:
            return f"[tool error] {text}"
        return text

    async def shutdown(self) -> None:
        self._stop.set()
        if self._runner is not None:
            try:
                await self._runner
            except Exception:
                logger.exception("MCP runner shutdown error")
            self._runner = None
        self._sessions.clear()
        self._tools.clear()
