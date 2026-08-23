#!/usr/bin/env bash
# Bring the upstream Voicebox container stack up/down from OUR repo.
# Injects:
#   CONTAINERS_REGISTRIES_CONF  -> scoped registries config so podman
#                                  resolves short base-image names without
#                                  a TTY prompt (no global podman changes)
#   docker/ports-override.yml   -> remaps host port 17493 -> 17600 (v0.5.0
#                                  compose predates upstream's own remap)
#
# Usage:
#   ./scripts/upstream-up.sh            # build + start detached
#   ./scripts/upstream-up.sh logs       # follow startup logs
#   ./scripts/upstream-up.sh down       # stop and remove
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPSTREAM_DIR="${VOICEBOX_UPSTREAM_DIR:-$HOME/Projects/ai/voicebox-upstream}"
export CONTAINERS_REGISTRIES_CONF="$REPO_ROOT/docker/registries.conf"

compose() {
  cd "$UPSTREAM_DIR"
  podman-compose \
    -f "$UPSTREAM_DIR/docker-compose.yml" \
    -f "$REPO_ROOT/docker/ports-override.yml" \
    "$@"
}

case "${1:-up}" in
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
    echo "Unknown command: $1 (use up | logs | down)" >&2
    exit 1
    ;;
esac
