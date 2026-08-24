#!/bin/sh
set -eu

skill_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
port=${IMESSAGE_MCP_PORT:-8765}
exec /usr/bin/python3 "$skill_dir/imessage_mcp.py" --http --host 127.0.0.1 --port "$port"
