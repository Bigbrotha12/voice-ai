#!/usr/bin/env bash
# Open the MCP inspector against the wrapper server (stdio).
# Requires: node/npx on PATH and `uv sync` run in services/voice-mcp.
set -euo pipefail

cd "$(dirname "$0")/../services/voice-mcp"
exec npx @modelcontextprotocol/inspector uv run voice-mcp
