"""Minimal MCP (Model Context Protocol) implementation for InboxHero.

The Model Context Protocol is a JSON-RPC 2.0 style protocol between an
agent (client) and a capability server. Real MCP servers run over stdio
or an HTTP transport; this module implements both the wire protocol and
an in-process transport so the demo works with zero external dependencies.

Supported methods:
    initialize          → returns server info + capabilities
    tools/list          → returns [{name, description, input_schema}]
    tools/call          → executes a registered tool, returns {content, is_error}

Any tool registered with @tool becomes reachable through either transport,
so the same tool surface is used by the agent loop, by unit tests, and by
an external MCP client if you launch `python -m inboxhero.mcp`.
"""
from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: Callable[..., Any]
    # Irreversible tools require a gate (see agent.gate).
    irreversible: bool = False
    # Category is used for /tools/list grouping + trace tagging.
    category: str = "misc"


class ToolRegistry:
    """Holds the set of tools the MCP server exposes."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        if name not in self._tools:
            raise KeyError(f"unknown tool: {name}")
        return self._tools[name]

    def list(self) -> list[dict]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
                "category": t.category,
                "irreversible": t.irreversible,
            }
            for t in self._tools.values()
        ]


def tool(
    registry: ToolRegistry,
    *,
    name: str,
    description: str,
    input_schema: dict,
    irreversible: bool = False,
    category: str = "misc",
) -> Callable:
    """Decorator that registers a python function as an MCP tool."""

    def wrap(fn: Callable) -> Callable:
        registry.register(
            ToolSpec(
                name=name,
                description=description,
                input_schema=input_schema,
                handler=fn,
                irreversible=irreversible,
                category=category,
            )
        )
        return fn

    return wrap


@dataclass
class MCPServer:
    """JSON-RPC 2.0 server that dispatches to a ToolRegistry.

    Kept transport-agnostic: `handle(request_dict) -> response_dict`.
    The stdio_main() function below drives it over stdin/stdout, and
    InProcessMCPClient calls handle() directly.
    """

    registry: ToolRegistry
    server_name: str = "inboxhero"
    server_version: str = "1.0.0"
    _initialized: bool = field(default=False, init=False)

    def handle(self, req: dict) -> dict:
        rpc_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}
        try:
            if method == "initialize":
                self._initialized = True
                result = {
                    "serverInfo": {"name": self.server_name, "version": self.server_version},
                    "capabilities": {"tools": {}, "memory": {}},
                }
            elif method == "tools/list":
                result = {"tools": self.registry.list()}
            elif method == "tools/call":
                tool_name = params["name"]
                arguments = params.get("arguments") or {}
                spec = self.registry.get(tool_name)
                out = spec.handler(**arguments)
                result = {"content": out, "is_error": False, "tool": tool_name}
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32601, "message": f"method not found: {method}"},
                }
            return {"jsonrpc": "2.0", "id": rpc_id, "result": result}
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {
                    "code": -32000,
                    "message": str(exc),
                    "data": {"trace": traceback.format_exc()},
                },
            }


class InProcessMCPClient:
    """A client that talks to the server via direct dict calls.

    This is the transport the agent loop uses by default so the demo has
    zero moving parts, but the wire format is byte-for-byte identical to
    what the stdio server produces.
    """

    def __init__(self, server: MCPServer) -> None:
        self.server = server
        self._id = 0
        # Handshake so tools/list works before the agent starts.
        self._call("initialize", {})

    def _call(self, method: str, params: dict) -> dict:
        self._id += 1
        resp = self.server.handle(
            {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        )
        if "error" in resp:
            raise RuntimeError(f"MCP error: {resp['error']['message']}")
        return resp["result"]

    def list_tools(self) -> list[dict]:
        return self._call("tools/list", {})["tools"]

    def call_tool(self, name: str, **arguments: Any) -> Any:
        return self._call("tools/call", {"name": name, "arguments": arguments})["content"]


def stdio_main(server: MCPServer) -> None:  # pragma: no cover - manual smoke test
    """Run the server as an MCP-compliant JSON-RPC stdio process."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            print(json.dumps({"jsonrpc": "2.0", "error": {"code": -32700, "message": str(exc)}}), flush=True)
            continue
        print(json.dumps(server.handle(req)), flush=True)
