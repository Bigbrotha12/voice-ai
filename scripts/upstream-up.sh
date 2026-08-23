#!/usr/bin/env bash
# Bring the upstream Voicebox container stack up/down from OUR repo,
# injecting a scoped CONTAINERS_REGISTRIES_CONF so podman resolves the
# short base-image names in upstream's Dockerfile without a TTY prompt
# (and without touching your global podman config).
#
# Usage:
#   ./scripts/upstream-up.sh            # build + start detached
#   ./scripts/upstream-up.sh logs       # follow startup logs
#   ./scripts/upstream-up.sh down       # stop and remove
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPSTREAM_DIR="${VOICEBOX_UPSTREAM_DIR:-$HOME/Projects/ai/voicebox-upstream}"

export CONTAINERS_REGISTRIES_CONF="$REPO_ROOT/docker/registries.conf"

case "${1:-up}" in
  up)
    cd "$UPSTREAM_DIR"
    exec podman-compose up -d --build
    ;;
  logs)
    cd "$UPSTREAM_DIR"
    exec podman-compose logs -f
    ;;
  down)
    cd "$UPSTREAM_DIR"
    exec podman-compose down
    ;;
  *)
    echo "Unknown command: $1 (use up | logs | down)" >&2
    exit 1
    ;;
esac
