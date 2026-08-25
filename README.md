# iMessage Reader

Read locally synced macOS Messages history in read-only mode through a Codex plugin and local MCP server.

## Features

- `imessage_doctor` checks local access and database health.
- `get_latest_message`, `get_recent_messages`, `search_messages`, and `get_conversation` provide narrow queries.
- SQLite is opened read-only with `PRAGMA query_only=ON`.
- No tool sends, edits, deletes, marks read, or bulk-exports messages.
- The default MCP transport is stdio. An optional HTTP sidecar is restricted to loopback.

## Privacy and safety

This repository contains source code and plugin metadata only. It does not contain a Messages database, message text, attachments, credentials, or access tokens.

On your MacBook, grant the ChatGPT desktop app Full Disk Access in System Settings > Privacy & Security > Full Disk Access. If the app is listed as Codex, enable that app instead. The app that actually launches the MCP process must have this permission.

At runtime, the helper reads the Mac-local `~/Library/Messages/chat.db`. Message text returned by an explicit query is sensitive and may become part of the Codex conversation context. Use the smallest query and result limit that answers the request.

The process that opens the database must have macOS Full Disk Access. The optional HTTP sidecar must remain on `127.0.0.1` or `::1`; do not expose it to a LAN, port forwarding, or `0.0.0.0`. `IMESSAGE_MCP_TOKEN` can add bearer-token protection when using HTTP.

## Layout

- `.codex-plugin/plugin.json` defines the plugin.
- `.mcp.json` registers the local MCP server.
- `skills/imessage-reader/SKILL.md` defines the safe read-only workflow.
- `skills/imessage-reader/scripts/imessage_query.py` performs read-only SQLite queries.
- `skills/imessage-reader/scripts/imessage_mcp.py` exposes the MCP tools.
- `skills/imessage-reader/scripts/run_sidecar.sh` starts the optional loopback HTTP sidecar.

## Local checks

From the repository root:

```bash
python3 -m py_compile skills/imessage-reader/scripts/imessage_query.py skills/imessage-reader/scripts/imessage_mcp.py
sh -n skills/imessage-reader/scripts/run_sidecar.sh
```
