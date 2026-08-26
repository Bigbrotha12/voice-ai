#!/usr/bin/env bash
# Single interface for the full voice stack: voicebox + livekit (+ voicebot),
# one podman-compose project so everything shares voicebox-net and one
# up/down/logs lifecycle. llama.cpp stays host-native and is NOT managed here.
#
# Compose file stack (same pattern as upstream-up.sh):
#   $UPSTREAM/docker-compose.yml      base: voicebox service, network, volumes
#   docker/ports-override.yml         17600 remap, rootless fixes, GPU CDI
#   [docker/cpu-override.yml]         (--cpu) drops GPU passthrough
#   docker/livekit.yml                LiveKit media plane (dev mode, loopback)
#   [docker/voicebot.yml]             pipecat bot (skipped with --host-bot)
#
# The repo .env is sourced first so ${VARS} in the override files resolve;
# voicebot.yml then overrides VOICEBOX_URL/LIVEKIT_URL with internal service
# names for the bot container.
#
# Usage:
#   ./scripts/stack-up.sh                     # build + start all three (GPU)
#   ./scripts/stack-up.sh --cpu               # CPU-only voicebox (whisper base!)
#   ./scripts/stack-up.sh --host-bot          # voicebox + livekit only; run the
#                                             # bot via `uv run voicebot` on host
#   ./scripts/stack-up.sh [--cpu] logs        # follow logs (all services)
#   ./scripts/stack-up.sh [--cpu] down        # stop and remove everything
#
# Note: --host-bot only affects `up`; logs/down always include the full set
# so a previously-bundled bot container is never orphaned.
#
# Mode switches recreate containers; profiles/generations/model caches live
# in named volumes and survive either direction.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPSTREAM_DIR="${VOICEBOX_UPSTREAM_DIR:-$HOME/Projects/ai/voicebox-upstream}"
export CONTAINERS_REGISTRIES_CONF="$REPO_ROOT/docker/registries.conf"
export VOICEBOT_CONTEXT="$REPO_ROOT/telephony/pipecat-bot"
export VOICEBOT_BANK_DIR="$REPO_ROOT/backchannels"
export REPO_ROOT

[ -d "$UPSTREAM_DIR" ] || {
  echo "Upstream checkout not found at $UPSTREAM_DIR" >&2
  echo "Clone it first (docs/setup.md step 1), or set VOICEBOX_UPSTREAM_DIR." >&2
  exit 1
}

if [ -f "$REPO_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$REPO_ROOT/.env"
  set +a
fi

# Fail fast on required credentials. Shell-side assertions instead of
# ${VAR:?} guards inside compose fragments: this podman-compose version
# silently mangles those in list-form entries.
: "${LIVEKIT_API_KEY:?set LIVEKIT_API_KEY in .env (openssl rand -hex 12)}"
: "${LIVEKIT_API_SECRET:?set LIVEKIT_API_SECRET in .env (openssl rand -base64 32)}"

CPU_MODE=0
HOST_BOT=0
FORCE=0
ARGS=()
for arg in "$@"; do
  case "$arg" in
    --cpu) CPU_MODE=1 ;;
    --host-bot) HOST_BOT=1 ;;
    # Force container recreation even when compose sees no config change -
    # needed after code-only image rebuilds (podman-compose skips recreate
    # on unchanged config hashes).
    --force-recreate) FORCE=1 ;;
    *) ARGS+=("$arg") ;;
  esac
done
ACTION="${ARGS[0]:-up}"

files=(-f "$UPSTREAM_DIR/docker-compose.yml" -f "$REPO_ROOT/docker/ports-override.yml")
[ "$CPU_MODE" = 1 ] && files+=(-f "$REPO_ROOT/docker/cpu-override.yml")
files+=(-f "$REPO_ROOT/docker/livekit.yml")
files+=(-f "$REPO_ROOT/docker/token-mint.yml")
if [ "$ACTION" != "up" ] || [ "$HOST_BOT" = 0 ]; then
  files+=(-f "$REPO_ROOT/docker/voicebot.yml")
fi

compose() {
  cd "$UPSTREAM_DIR"
  podman-compose "${files[@]}" "$@"
}

case "$ACTION" in
  up)
    compose up -d --build $([ "$FORCE" = 1 ] && echo --force-recreate)
    echo
    echo "Stack up. Voicebox REST/MCP: http://127.0.0.1:17600 (loopback)"
    echo "LiveKit signaling: ws://127.0.0.1:7880 | bot room join via ws://livekit:7880"
    echo "Warm models before first use: ./scripts/warmup.sh"
    ;;
  logs)
    compose logs -f
    ;;
  down)
    compose down
    ;;
  *)
    echo "Unknown command: $ACTION (use up | logs | down; optional --cpu / --host-bot flags)" >&2
    exit 1
    ;;
esac
