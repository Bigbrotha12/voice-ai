#!/usr/bin/env bash
# Warm up lazy-loaded models so the first real turn doesn't pay cold-start:
#   1. kokoro TTS   - one short /speak generation
#   2. Whisper STT  - transcribe the wav produced by step 1
#   3. llama.cpp    - one-token completion (skipped if endpoint is down)
#
# Run right after ./scripts/upstream-up.sh (and after starting llama-server).
#
# Usage:
#   ./scripts/warmup.sh
#   SMOKE_PROFILE="Morgan" OLLAMA_BASE_URL=http://127.0.0.1:19091/v1 ./scripts/warmup.sh
set -euo pipefail

BASE_URL="${VOICEBOX_URL:-http://127.0.0.1:17600}"
PROFILE="${SMOKE_PROFILE:-${DEFAULT_PROFILE:-}}"
OUTPUT_DIR="${VOICEBOX_OUTPUT_DIR:-$HOME/Projects/ai/voicebox-upstream/output}"
STT_MODEL="${VOICEBOX_STT_MODEL:-turbo}"
LLM_URL="${OLLAMA_BASE_URL:-http://127.0.0.1:19091/v1}"
LLM_MODEL="${OLLAMA_MODEL:-qwen2.5-3b-instruct}"
WARM_TIMEOUT="${WARM_TIMEOUT:-120}"

stage() { printf '== %s ==\n' "$*"; }

stage "TTS warmup ($BASE_URL)"
export ENGINE="${VOICEBOX_ENGINE:-kokoro}"

# Candidate profiles: explicit override first, then everything the server
# knows. First hit wins - kokoro rejects cloned-voice profiles (HTTP 400),
# so a blind "first profile" pick can fail.
mapfile -t CANDIDATES < <(
  [ -n "$PROFILE" ] && printf '%s\n' "$PROFILE"
  curl -fsS --max-time 15 "$BASE_URL/profiles" | python3 -c '
import json, sys

data = json.load(sys.stdin)
profiles = data.get("profiles") if isinstance(data, dict) else data
for prof in profiles or []:
    name = prof.get("name") or prof.get("id", "")
    if name:
        print(name)'
)

gen_submit_s=""
gen_id=""
for candidate in "${CANDIDATES[@]}"; do
  payload=$(PROFILE="$candidate" python3 -c '
import json, os
body = {"text": "Warmup complete.", "engine": os.environ.get("ENGINE", "kokoro")}
profile = os.environ.get("PROFILE", "")
if profile:
    body["profile"] = profile
print(json.dumps(body))')
  resp=$(curl -sS --max-time 30 -X POST "$BASE_URL/speak" \
    -H 'Content-Type: application/json' \
    -H 'X-Voicebox-Client-Id: warmup' \
    -w '\n%{time_total}' \
    --data "$payload" 2>/dev/null) || continue
  candidate_time=$(printf '%s' "$resp" | tail -1)
  candidate_id=$(printf '%s' "$resp" | head -n -1 | python3 -c 'import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get("id") or d.get("generation_id") or "")
except Exception:
    print("")')
  if [ -n "$candidate_id" ]; then
    gen_id="$candidate_id"
    gen_submit_s="$candidate_time"
    PROFILE="$candidate"
    echo "Using profile: $candidate"
    break
  fi
done
[ -n "$gen_id" ] || { echo "FAIL: no profile accepted engine '$ENGINE'"; exit 1; }

t0=$(date +%s.%N)
for _ in $(seq 1 $((WARM_TIMEOUT * 4))); do
  status=$(curl -fsS --max-time 5 "$BASE_URL/history/$gen_id" | python3 -c 'import sys, json; print(json.load(sys.stdin).get("status", ""))' || true)
  case "$status" in
    completed) break ;;
    failed) echo "FAIL: generation failed"; exit 1 ;;
  esac
  sleep 0.25
done
t1=$(date +%s.%N)
printf 'OK: tts submit %.2fs + synth %.2fs\n' "$gen_submit_s" "$(echo "$t1 $t0" | awk '{printf "%.2f", $1-$2}')"

stage "STT warmup (whisper $STT_MODEL)"
latest_wav=$(ls -t "$OUTPUT_DIR"/*.wav 2>/dev/null | head -1 || true)
[ -n "$latest_wav" ] || { echo "FAIL: no wav found in $OUTPUT_DIR"; exit 1; }
transcribe_s=$(curl -fsS --max-time "$WARM_TIMEOUT" -X POST "$BASE_URL/transcribe" \
  -F "file=@$latest_wav;type=audio/wav" \
  -F "model=$STT_MODEL" \
  -H 'X-Voicebox-Client-Id: warmup' \
  -o /dev/null \
  -w '%{time_total}')
printf 'OK: stt %.2fs (%s)\n' "$transcribe_s" "$(basename "$latest_wav")"

stage "LLM warmup ($LLM_MODEL)"
if llm_s=$(curl -fsS --max-time "$WARM_TIMEOUT" -X POST "$LLM_URL/chat/completions" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$LLM_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":1,\"cache_prompt\":true}" \
    -o /dev/null \
    -w '%{time_total}' 2>/dev/null); then
  printf 'OK: llm first token %.2fs\n' "$llm_s"
else
  echo "SKIP: llama.cpp not reachable at $LLM_URL"
fi

echo "Warmup complete."
