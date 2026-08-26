#!/usr/bin/env bash
# Run the voicebot against the k3s cluster voice services (data-only path):
#
#   STT: whisper-stt  (svc/whisper-stt:8000, OpenAI /v1/audio/transcriptions)
#   TTS: glados-tts   (svc/glados-tts:5000,  /v1/audio/speech, wav requested)
#
# Voicebox/GPU is NOT needed for this mode - that is the point. LiveKit and
# the queues-proxy LLM stay as in scripts/stack-up.sh (see docs/setup.md).
#
# ClusterIP services are not host-reachable directly; when the base URLs
# point at 127.0.0.1 the script opens `kubectl port-forward` tunnels itself
# and tears them down on exit.
#
# Usage:
#   ./scripts/cluster-voice-test.sh --probe-only   # stage-0 latency probes,
#                                                  # no bot launch
#   ./scripts/cluster-voice-test.sh                # preflight + run bot
#
# Env overrides:
#   OPENAI_STT_BASE_URL   default http://127.0.0.1:8000
#   PIPER_TTS_BASE_URL    default http://127.0.0.1:5000
#   K8S_NAMESPACE         default productivity
#   OPENAI_STT_MODEL      default whisper-1
#   PIPER_VOICE           default en_US-glados-high
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${K8S_NAMESPACE:-productivity}"
STT_URL="${OPENAI_STT_BASE_URL:-http://127.0.0.1:8000}"
TTS_URL="${PIPER_TTS_BASE_URL:-http://127.0.0.1:5000}"
PROBE_ONLY=0
[ "${1:-}" = "--probe-only" ] && PROBE_ONLY=1

if [ -f "$REPO_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$REPO_ROOT/.env"
  set +a
fi

log() { printf '\033[1;36m[cluster-voice]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[cluster-voice]\033[0m %s\n' "$*" >&2; exit 1; }

url_host() { printf '%s' "$1" | sed -E 's#https?://##; s#[:/].*##'; }
url_port() { printf '%s' "$1" | grep -oE ':[0-9]+' | tr -d ':' || true; }

PORT_FORWARD_PIDS=()
cleanup() {
  for pid in "${PORT_FORWARD_PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT

ensure_reachable() {
  # $1 = url, $2 = svc name, $3 = svc port. Opens a port-forward when the
  # URL targets localhost and nothing answers yet.
  local url="$1" svc="$2" port="$3" host port_cur
  host="$(url_host "$url")"
  port_cur="$(url_port "$url")"; port_cur="${port_cur:-$port}"
  if ! curl -s -o /dev/null --max-time 2 "http://${host}:${port_cur}/"; then
    case "$host" in
      127.0.0.1|localhost)
        log "opening port-forward $svc:${port_cur} (ns=$NAMESPACE)"
        kubectl port-forward -n "$NAMESPACE" "svc/$svc" "${port_cur}:${port}" >/dev/null 2>&1 &
        PORT_FORWARD_PIDS+=("$!")
        # Tunnel binds asynchronously (API-server RTT over the mesh varies);
        # poll instead of a fixed sleep so first requests don't race it.
        for _ in $(seq 1 20); do
          curl -s -o /dev/null --max-time 2 "http://${host}:${port_cur}/" && break
          sleep 0.5
        done
        ;;
      *) die "service unreachable at $url and no tunnel rule for non-local host" ;;
    esac
  fi
}

timed_request() {
  # $1 = label, rest = curl args. Prints "<label>: HTTP <code> <ms>ms",
  # returns non-zero unless HTTP 200. Set CURL_OUT=<path> to keep the
  # response body instead of discarding it.
  local label="$1" out code ms
  shift
  out="$(curl -s -o "${CURL_OUT:-/dev/null}" -w '%{http_code} %{time_total}' --max-time 30 "$@")" || {
    echo "$label: FAILED (connection)"; return 1; }
  code="${out%% *}"
  ms="$(awk -v t="${out#* }" 'BEGIN {printf "%.0f", t * 1000}')"
  echo "$label: HTTP $code ${ms}ms"
  [ "$code" = "200" ]
}

probe() {
  # Closed loop: TTS synthesizes the probe phrase, STT transcribes it - real
  # speech both ways, no mic needed. Format-agnostic TTS request (no
  # response_format) so it works against pre- and post-upgrade deployments.
  local audio="/tmp/opencode/cluster-probe-audio.bin"

  log "TTS probe (glados-tts @ $TTS_URL)"
  CURL_OUT="$audio" timed_request "  speech" -X POST "$TTS_URL/v1/audio/speech" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"piper\",\"input\":\"Cluster voice test, one two three.\",\"voice\":\"${PIPER_VOICE:-en_US-glados-high}\"}" \
    || die "glados-tts unreachable/broken"

  log "STT probe (whisper-stt @ $STT_URL)"
  timed_request "  transcribe" -X POST "$STT_URL/v1/audio/transcriptions" \
    -F "file=@$audio;type=audio/mpeg" \
    -F "model=${OPENAI_STT_MODEL:-whisper-1}" \
    || die "whisper-stt unreachable/broken"

  log "reference: live-turn baseline is STT 173ms / TTS kokoro 40ms (GPU, RESEARCH.md); batch-over-mesh budgets: STT <=600ms, TTS <=300ms"
}

# --- main -----------------------------------------------------------------

command -v kubectl >/dev/null || die "kubectl required (port-forwards to ClusterIP services)"
command -v curl >/dev/null || die "curl required"

log "checking services exist in ns/$NAMESPACE"
kubectl get svc whisper-stt -n "$NAMESPACE" >/dev/null 2>&1 || die "svc/whisper-stt not found in ns/$NAMESPACE"
kubectl get svc glados-tts -n "$NAMESPACE" >/dev/null 2>&1 || die "svc/glados-tts not found in ns/$NAMESPACE"

ensure_reachable "$STT_URL" whisper-stt 8000
ensure_reachable "$TTS_URL" glados-tts 5000

if [ "$PROBE_ONLY" = 1 ]; then
  probe
  exit 0
fi

[ -n "${LIVEKIT_API_KEY:-}" ] && [ -n "${LIVEKIT_API_SECRET:-}" ] || {
  die "LIVEKIT_API_KEY/LIVEKIT_API_SECRET missing (.env) - configured-keys LiveKit has no defaults"
}

log "preflight passed; launching bot with VOICEBOT_STT_PROVIDER=openai VOICEBOT_TTS_PROVIDER=piper"
export VOICEBOT_STT_PROVIDER=openai
export VOICEBOT_TTS_PROVIDER=piper
export OPENAI_STT_BASE_URL="$STT_URL"
export PIPER_TTS_BASE_URL="$TTS_URL"

cd "$REPO_ROOT/telephony/pipecat-bot"
exec uv run voicebot
