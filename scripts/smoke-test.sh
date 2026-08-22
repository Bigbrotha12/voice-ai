#!/usr/bin/env bash
# REST smoke test against a running Voicebox: profiles -> speak -> SSE status.
#
# Requires a voice profile to exist (create one in the Voicebox app first).
#
# Usage:
#   ./scripts/smoke-test.sh
#   VOICEBOX_URL=http://127.0.0.1:17600 SMOKE_PROFILE="Morgan" ./scripts/smoke-test.sh
set -euo pipefail

BASE_URL="${VOICEBOX_URL:-http://127.0.0.1:17493}"
TEXT="${SMOKE_TEXT:-Voicebox smoke test successful.}"
PROFILE="${SMOKE_PROFILE:-}"
TIMEOUT_SECONDS="${SMOKE_TIMEOUT:-180}"

say() { printf '\n== %s ==\n' "$*"; }

profiles_file=$(mktemp)
trap 'rm -f "$profiles_file"' EXIT

say "Checking profiles at $BASE_URL"
curl -fsS --max-time 15 "$BASE_URL/profiles" -o "$profiles_file"
python3 - "$profiles_file" <<'PY'
import json
import sys

with open(sys.argv[1]) as fh:
    data = json.load(fh)
profiles = data.get("profiles") if isinstance(data, dict) else data
count = len(profiles or [])
if count == 0:
    print("FAIL: no voice profiles - create one in the Voicebox app first")
    sys.exit(1)
print(f"OK: {count} voice profile(s) available")
PY

say "Submitting speak request"
payload=$(SMOKE_TEXT="$TEXT" SMOKE_PROFILE="$PROFILE" python3 -c 'import json, os
body = {"text": os.environ["SMOKE_TEXT"]}
profile = os.environ.get("SMOKE_PROFILE")
if profile:
    body["profile"] = profile
print(json.dumps(body))')
[ -n "$payload" ] || { echo "FAIL: payload build failed"; exit 1; }
gen_json=$(curl -fsS --max-time 30 -X POST "$BASE_URL/speak" \
  -H 'Content-Type: application/json' \
  -H 'X-Voicebox-Client-Id: smoke-test' \
  --data "$payload")
GEN_ID=$(printf '%s' "$gen_json" | python3 -c 'import sys, json; d = json.load(sys.stdin); print(d.get("id") or d.get("generation_id") or "")')
[ -n "$GEN_ID" ] || { echo "FAIL: no generation id in response: $gen_json"; exit 1; }
echo "Generation id: $GEN_ID"

say "Watching SSE status (timeout ${TIMEOUT_SECONDS}s)"
# The stream closes itself once the generation reaches a terminal state.
curl -fsS --max-time "$TIMEOUT_SECONDS" "$BASE_URL/generate/$GEN_ID/status" \
  | sed -n 's/^data: //p' \
  | python3 -c '
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        continue
    status = event.get("status", "unknown")
    print("  " + status, flush=True)
    if status == "not_found":
        print("FAIL: generation id unknown to server"); sys.exit(1)
    if status == "failed":
        print("FAIL: " + str(event.get("error"))); sys.exit(1)
    if status == "completed":
        print("\nPASS: generation completed"); sys.exit(0)
print("FAIL: stream closed without a terminal status"); sys.exit(1)
'
