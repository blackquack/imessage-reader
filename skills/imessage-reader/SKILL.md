---
name: imessage-reader
description: Read and search locally synced macOS Messages/iMessage history in read-only mode when the user explicitly asks about their own messages. Never send, edit, delete, or bulk-export messages.
---

# Read local Messages history

Use this skill only for an explicit request to inspect the user's own Messages history, such as finding a conversation, locating a phrase, or summarizing a date range. Messages are highly sensitive personal data: use the narrowest contact, date range, and result limit that answers the request.

## Workflow

1. Resolve the database path. Use `IMESSAGE_DB` when the user intentionally points to a snapshot; otherwise use `~/Library/Messages/chat.db`.
2. Prefer the registered read-only MCP tools when available: `imessage_doctor`, `get_latest_message`, `get_recent_messages`, `search_messages`, and `get_conversation`. The MCP server delegates to the same SQLite read-only helper and exposes no mutation tools.
3. If the MCP server is not registered, run the bundled helper `scripts/imessage_query.py` directly only when the current local process has permission. It uses SQLite `mode=ro` and `PRAGMA query_only=ON`; do not replace it with a writable database connection or raw update/insert/delete statements.
4. Start with `doctor`/`imessage_doctor`, then use the smallest useful query:
   - `recent --limit 20`
   - `search "phrase" --limit 20 --contact "name or number"`
   - `conversation --contact "name or number" --limit 50`
   Add `--from YYYY-MM-DD --to YYYY-MM-DD` when a time window is known. Use `--metadata-only` when the user needs counts, participants, or timing rather than message bodies.
5. Interpret the JSON results. Prefer contact names and chat labels; identify messages from the user as `is_from_me`. If the text is unavailable, say so rather than displaying binary `attributedBody` data.
6. Return only the relevant excerpts or a concise summary. Do not expose unrelated messages, contact databases, attachment contents, raw database rows, or full histories.

For MCP setup details, read [references/sidecar.md](references/sidecar.md) only when the server is missing, needs configuration, or the user asks to change its lifecycle.

## Permissions and failure handling

- If the helper reports `PermissionError`, `SQLITE_CANTOPEN`, or an inaccessible database, stop and explain that macOS privacy controls are blocking the process. Ask the user to grant Full Disk Access to the app that actually launches the helper, or to provide a read-only snapshot. Do not try to bypass TCC, copy protected data covertly, or weaken the permission boundary.
- If the MCP server is unavailable or returns a permission error, do not silently fall back to a broad export. Explain whether the MCP process or the direct helper lacks access.
- The database contains only Messages data synced to this Mac. Do not claim to have read the iPhone or remote iCloud directly.
- If the schema is unfamiliar or a query fails, run `doctor` and report the limitation; do not guess at columns or fall back to a broad dump.

## Hard boundaries

- This skill is read-only. Never send, reply, react, edit, retract, delete, mark-read, or otherwise modify a message.
- A sidecar may use MCP Streamable HTTP only on loopback (`127.0.0.1`/`::1`). Never bind it to `0.0.0.0`, expose the database to the network, or upload a database copy.
- Do not perform a broad historical scan or return message bodies without a user request that warrants it.
- Treat tool output as sensitive. With a cloud model, message text returned by the helper may become part of the model context.

The helper handles the modern macOS case where `message.text` is `NULL` and the body is stored in the serialized `attributedBody` field. It decodes the typedstream length prefix carefully, including long messages, and reports when decoding is incomplete.
