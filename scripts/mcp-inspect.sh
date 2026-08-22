#!/usr/bin/env bash
# Open the MCP inspector against the wrapper server (stdio).
# Requires: node/npx on PATH and `uv sync` run in services/voice-mcp.
set -euo pipefail

cd "$(dirname "$0")/../services/voice-mcp"
env_args=()
if [ -f ../../.env ]; then
  env_args=(--env-file ../../.env)
fi
exec npx @modelcontextprotocol/inspector uv run "${env_args[@]}" voice-mcp
