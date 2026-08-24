# iMessage MCP sidecar

The bundled `scripts/imessage_mcp.py` exposes a strictly read-only MCP server. It provides `imessage_doctor`, `get_latest_message`, `get_recent_messages`, `search_messages`, and `get_conversation`. It delegates database access to `scripts/imessage_query.py`, which opens SQLite with `mode=ro` and `PRAGMA query_only=ON`.

## Codex-managed stdio

The plugin's `.mcp.json` registers the server. With stdio, Codex starts and stops it automatically. The equivalent plugin entry is:

```toml
[mcp_servers.imessage]
command = "./skills/imessage-reader/scripts/imessage_mcp.py"
cwd = "."
startup_timeout_sec = 20
tool_timeout_sec = 60
```

The process that opens `chat.db` must have macOS Full Disk Access. If the Codex desktop host is not authorized, stdio will still fail because its child inherits the host's access boundary.

## Separately authorized loopback HTTP

Use HTTP when the server must run under a separately authorized process. From the plugin root, start it from an app that has Full Disk Access:

```bash
./skills/imessage-reader/scripts/run_sidecar.sh
```

The equivalent direct command is:

```bash
/usr/bin/python3 ./skills/imessage-reader/scripts/imessage_mcp.py \
  --http --host 127.0.0.1 --port 8765
```

Then configure the MCP client to connect to:

```toml
[mcp_servers.imessage]
url = "http://127.0.0.1:8765/mcp"
startup_timeout_sec = 20
tool_timeout_sec = 60
```

The server refuses non-loopback bind addresses. `IMESSAGE_MCP_TOKEN` can add a bearer token even on loopback. Keep the server read-only and do not expose it through LAN, port forwarding, or `0.0.0.0`.

The optional `IMESSAGE_DB` environment variable can point the sidecar at a deliberately supplied read-only snapshot. Otherwise it reads the Mac-local `~/Library/Messages/chat.db`.
