#!/usr/bin/env bash
# Bring the upstream Voicebox container stack up/down from OUR repo.
# Injects:
#   CONTAINERS_REGISTRIES_CONF  -> scoped registries config so podman
#                                  resolves short base-image names without
#                                  a TTY prompt (no global podman changes)
#   docker/ports-override.yml   -> remaps host port 17493 -> 17600 (v0.5.0
#                                  compose predates upstream's own remap),
#                                  rootless bind-mount fixes, persistent
#                                  model cache via HF_HOME, SELinux fix
#   docker/cpu-override.yml     -> (--cpu only) drops CDI GPU passthrough;
#                                  torch falls back to CPU at startup
#
# Usage:
#   ./scripts/upstream-up.sh              # build + start detached (GPU)
#   ./scripts/upstream-up.sh --cpu        # same, but CPU-only runtime
#   ./scripts/upstream-up.sh [--cpu] logs # follow startup logs
#   ./scripts/upstream-up.sh [--cpu] down # stop and remove
#
# Mode switches recreate the container; profiles/generations/model caches
# live in named volumes and survive the flip either direction.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPSTREAM_DIR="${VOICEBOX_UPSTREAM_DIR:-$HOME/Projects/ai/voicebox-upstream}"
export CONTAINERS_REGISTRIES_CONF="$REPO_ROOT/docker/registries.conf"

[ -d "$UPSTREAM_DIR" ] || {
  echo "Upstream checkout not found at $UPSTREAM_DIR" >&2
  echo "Clone it first (docs/setup.md step 1), or set VOICEBOX_UPSTREAM_DIR." >&2
  exit 1
}

CPU_MODE=0
ARGS=()
for arg in "$@"; do
  case "$arg" in
    --cpu) CPU_MODE=1 ;;
    *) ARGS+=("$arg") ;;
  esac
done
ACTION="${ARGS[0]:-up}"

compose() {
  cd "$UPSTREAM_DIR"
  local files=(-f "$UPSTREAM_DIR/docker-compose.yml" -f "$REPO_ROOT/docker/ports-override.yml")
  [ "$CPU_MODE" = 1 ] && files+=(-f "$REPO_ROOT/docker/cpu-override.yml")
  podman-compose "${files[@]}" "$@"
}

case "$ACTION" in
  up)
    compose up -d --build
    ;;
  logs)
    compose logs -f
    ;;
  down)
    compose down
    ;;
  *)
    echo "Unknown command: $ACTION (use up | logs | down; optional --cpu flag)" >&2
    exit 1
    ;;
esac
