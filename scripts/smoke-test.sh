#!/usr/bin/env bash
# REST smoke test against a running Voicebox: profiles -> generate -> poll.
#
# Usage:
#   ./scripts/smoke-test.sh
#   VOICEBOX_URL=http://127.0.0.1:17600 SMOKE_TEXT="Hallo Welt" ./scripts/smoke-test.sh
set -euo pipefail

BASE_URL="${VOICEBOX_URL:-http://127.0.0.1:17493}"
TEXT="${SMOKE_TEXT:-Voicebox smoke test successful.}"
TIMEOUT_SECONDS="${SMOKE_TIMEOUT:-180}"

say() { printf '\n== %s ==\n' "$*"; }

say "Checking profiles at $BASE_URL"
curl -fsS "$BASE_URL/profiles" -o /tmp/vb-profiles.json
python3 - <<'PY'
import json

with open("/tmp/vb-profiles.json") as fh:
    data = json.load(fh)
profiles = data.get("profiles") if isinstance(data, dict) else data
print(f"OK: {len(profiles or [])} voice profile(s) available")
PY

say "Submitting generation"
payload=$(python3 -c 'import json,os,sys; print(json.dumps({"text": os.environ["SMOKE_TEXT"], "language": "en"}))')
gen_json=$(SMOKE_TEXT="$TEXT" curl -fsS -X POST "$BASE_URL/generate" \
  -H 'Content-Type: application/json' \
  -H 'X-Voicebox-Client-Id: smoke-test' \
  --data "$payload")
GEN_ID=$(printf '%s' "$gen_json" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("generation_id") or d.get("id") or "")')
[ -n "$GEN_ID" ] || { echo "FAIL: no generation id in response: $gen_json"; exit 1; }
echo "Generation id: $GEN_ID"

say "Polling status (timeout ${TIMEOUT_SECONDS}s)"
elapsed=0
while :; do
  status_json=$(curl -fsS "$BASE_URL/generate/$GEN_ID/status")
  raw=$(printf '%s' "$status_json" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("status") or d.get("state") or "unknown")')
  lower=$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]')
  echo "  [${elapsed}s] $raw"
  case "$lower" in
    complet*|success*|done*|finish*)
      say "PASS: generation completed"
      printf '%s\n' "$status_json"
      exit 0
      ;;
    fail*|error*|cancel*)
      echo "FAIL: generation ended with status '$raw'"
      printf '%s\n' "$status_json"
      exit 1
      ;;
  esac
  if [ "$elapsed" -ge "$TIMEOUT_SECONDS" ]; then
    echo "FAIL: still '$raw' after ${TIMEOUT_SECONDS}s"
    exit 1
  fi
  sleep 2
  elapsed=$((elapsed + 2))
done
