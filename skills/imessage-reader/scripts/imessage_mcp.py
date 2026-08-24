#!/usr/bin/env python3
"""Read-only MCP sidecar for the local macOS Messages database.

The default transport is stdio, which is suitable when an MCP client launches
this process. Pass --http to run a loopback Streamable-HTTP endpoint from a
separately authorized process (for example Terminal or a LaunchAgent).

No message mutation tools are exposed. The database work is delegated to the
read-only imessage_query.py helper in this directory.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


SERVER_NAME = "imessage-reader"
SERVER_VERSION = "0.2.0"
PROTOCOL_VERSION = "2025-06-18"
SCRIPT_DIR = Path(__file__).resolve().parent
QUERY_HELPER = SCRIPT_DIR / "imessage_query.py"
MAX_TOOL_TIMEOUT = 60


TOOLS: list[dict[str, Any]] = [
    {
        "name": "imessage_doctor",
        "description": "Check read-only access to the local macOS Messages database without returning message bodies.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_latest_message",
        "description": "Return the single newest locally synced Messages record, read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {"metadata_only": {"type": "boolean", "default": False}},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_recent_messages",
        "description": "Return a small, filtered set of recent locally synced Messages records, read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                "contact": {"type": "string"},
                "from_date": {"type": "string", "description": "YYYY-MM-DD"},
                "to_date": {"type": "string", "description": "YYYY-MM-DD"},
                "metadata_only": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "search_messages",
        "description": "Search decoded message text within a bounded, locally synced Messages history window, read-only.",
        "inputSchema": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                "contact": {"type": "string"},
                "from_date": {"type": "string", "description": "YYYY-MM-DD"},
                "to_date": {"type": "string", "description": "YYYY-MM-DD"},
                "metadata_only": {"type": "boolean", "default": False},
                "max_scan": {"type": "integer", "minimum": 1, "maximum": 20000, "default": 5000},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_conversation",
        "description": "Return a bounded set of messages for a contact or chat label, read-only.",
        "inputSchema": {
            "type": "object",
            "required": ["contact"],
            "properties": {
                "contact": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 50},
                "from_date": {"type": "string", "description": "YYYY-MM-DD"},
                "to_date": {"type": "string", "description": "YYYY-MM-DD"},
                "metadata_only": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
    },
]


def json_rpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def json_rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def server_info() -> dict[str, str]:
    return {"name": SERVER_NAME, "version": SERVER_VERSION}


def initialize_result(params: dict[str, Any] | None) -> dict[str, Any]:
    requested = (params or {}).get("protocolVersion")
    protocol = requested if requested in {PROTOCOL_VERSION, "2024-11-05"} else PROTOCOL_VERSION
    return {
        "protocolVersion": protocol,
        "capabilities": {"tools": {}},
        "serverInfo": server_info(),
        "instructions": (
            "This server is strictly read-only. It accesses only the local "
            "macOS Messages database and never sends, edits, deletes, or "
            "exports messages."
        ),
    }


def validate_bool(arguments: dict[str, Any], name: str) -> bool:
    value = arguments.get(name, False)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def validate_limit(arguments: dict[str, Any], default: int, maximum: int = 50) -> int:
    value = arguments.get("limit", default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"limit must be an integer from 1 to {maximum}")
    return value


def optional_string(arguments: dict[str, Any], name: str) -> str | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def query_command(tool_name: str, arguments: dict[str, Any]) -> list[str]:
    command = [sys.executable, str(QUERY_HELPER)]
    if tool_name == "imessage_doctor":
        return command + ["doctor"]

    metadata_only = validate_bool(arguments, "metadata_only")
    common: list[str] = []
    contact = optional_string(arguments, "contact")
    from_date = optional_string(arguments, "from_date")
    to_date = optional_string(arguments, "to_date")
    if contact:
        common += ["--contact", contact]
    if from_date:
        common += ["--from", from_date]
    if to_date:
        common += ["--to", to_date]
    if metadata_only:
        common.append("--metadata-only")

    if tool_name == "get_latest_message":
        return command + ["recent", "--limit", "1"] + common
    if tool_name == "get_recent_messages":
        return command + ["recent", "--limit", str(validate_limit(arguments, 20))] + common
    if tool_name == "search_messages":
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        max_scan = arguments.get("max_scan", 5000)
        if isinstance(max_scan, bool) or not isinstance(max_scan, int) or not 1 <= max_scan <= 20000:
            raise ValueError("max_scan must be an integer from 1 to 20000")
        return command + [
            "search",
            query,
            "--limit",
            str(validate_limit(arguments, 20)),
            "--max-scan",
            str(max_scan),
        ] + common
    if tool_name == "get_conversation":
        if not isinstance(contact, str) or not contact.strip():
            raise ValueError("contact is required")
        conversation_filters: list[str] = []
        if from_date:
            conversation_filters += ["--from", from_date]
        if to_date:
            conversation_filters += ["--to", to_date]
        if metadata_only:
            conversation_filters.append("--metadata-only")
        return command + [
            "conversation",
            "--contact",
            contact,
            "--limit",
            str(validate_limit(arguments, 50)),
        ] + conversation_filters
    raise ValueError(f"Unknown tool: {tool_name}")


def run_tool(tool_name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    command = query_command(tool_name, arguments)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=MAX_TOOL_TIMEOUT,
            check=False,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return {"error": "The read-only Messages query timed out."}, True
    except OSError as exc:
        return {"error": f"Could not start the read-only query helper: {exc}"}, True

    output = completed.stdout.strip()
    if output:
        try:
            value = json.loads(output)
        except json.JSONDecodeError:
            value = {"error": "The query helper returned invalid JSON."}
    else:
        value = {"error": completed.stderr.strip() or "The query helper returned no output."}
    return value, completed.returncode != 0


def tool_call_result(name: str, arguments: Any) -> dict[str, Any]:
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be an object")
    value, is_error = run_tool(name, arguments)
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(value, ensure_ascii=False, indent=2),
            }
        ],
        "isError": is_error,
    }


def dispatch(request: dict[str, Any]) -> dict[str, Any] | None:
    request_id = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}
    if not isinstance(params, dict):
        return json_rpc_error(request_id, -32602, "params must be an object")

    if method == "initialize":
        return json_rpc_result(request_id, initialize_result(params))
    if method in {"notifications/initialized", "notifications/cancelled"}:
        return None
    if method == "ping":
        return json_rpc_result(request_id, {})
    if method == "tools/list":
        return json_rpc_result(request_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or name not in {tool["name"] for tool in TOOLS}:
            return json_rpc_error(request_id, -32602, "Unknown tool")
        try:
            result = tool_call_result(name, params.get("arguments", {}))
        except ValueError as exc:
            return json_rpc_error(request_id, -32602, str(exc))
        return json_rpc_result(request_id, result)
    return json_rpc_error(request_id, -32601, f"Method not found: {method}")


def write_message(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def run_stdio() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("JSON-RPC request must be an object")
            response = dispatch(request)
            if response is not None:
                write_message(response)
        except (json.JSONDecodeError, ValueError) as exc:
            write_message(json_rpc_error(None, -32700, str(exc)))
    return 0


class MCPHTTPHandler(BaseHTTPRequestHandler):
    server_version = "imessage-reader-mcp/0.2.0"

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("[imessage-mcp] " + (format % args) + "\n")

    def authorized(self) -> bool:
        expected = os.environ.get("IMESSAGE_MCP_TOKEN")
        if not expected:
            return True
        supplied = self.headers.get("Authorization", "")
        expected_header = "Bearer " + expected
        return secrets.compare_digest(supplied, expected_header)

    def send_json(self, status: int, value: dict[str, Any], session_id: str) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Mcp-Session-Id", session_id)
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/mcp":
            self.send_error(404)
            return
        if not self.authorized():
            self.send_error(401)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                raise ValueError("invalid request size")
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            response = dispatch(request)
        except (ValueError, json.JSONDecodeError) as exc:
            response = json_rpc_error(None, -32700, str(exc))
        if response is None:
            self.send_response(202)
            self.end_headers()
            return
        session_id = self.headers.get("Mcp-Session-Id") or "imessage-" + base64.urlsafe_b64encode(
            hashlib.sha256(os.urandom(16)).digest()[:12]
        ).decode("ascii").rstrip("=")
        self.send_json(200, response, session_id)

    def do_GET(self) -> None:  # noqa: N802
        self.send_error(405, "This sidecar uses POST-only Streamable HTTP")

    def do_DELETE(self) -> None:  # noqa: N802
        self.send_error(405)


def run_http(host: str, port: int) -> int:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("Refusing to bind outside loopback")
    server = ThreadingHTTPServer((host, port), MCPHTTPHandler)
    print(f"imessage MCP sidecar listening on http://{host}:{port}/mcp", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--http", action="store_true", help="Run loopback Streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1", help="Loopback bind address")
    parser.add_argument("--port", type=int, default=8765, help="Loopback port")
    args = parser.parse_args()
    if args.http:
        return run_http(args.host, args.port)
    return run_stdio()


if __name__ == "__main__":
    raise SystemExit(main())
