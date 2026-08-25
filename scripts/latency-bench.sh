#!/usr/bin/env bash
# Component-level latency benchmarks for the voice stack (Phase 5 baseline).
# Complements the live per-turn logging in the bot (LATENCY log lines):
#   1. STT  - faster-whisper (in-process, GPU) vs Voicebox batch /transcribe
#   2. LLM  - first-token + total via the queues proxy, realistic turn prompt
#   3. TTS  - delegates to scripts/tts-benchmark.py (kokoro)
#
# Usage:
#   ./scripts/latency-bench.sh            # all three
#   ./scripts/latency-bench.sh stt llm    # subset
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOT_DIR="$REPO_ROOT/telephony/pipecat-bot"
WAV="${BENCH_WAV:-$(ls -t "$HOME/Projects/ai/voicebox-upstream/output/"*.wav 2>/dev/null | head -1 || true)}"
PROXY_URL="${OLLAMA_BASE_URL:-http://127.0.0.1:9091/v1}"
LLM_MODEL="${OLLAMA_MODEL:-Qwen3-8B-Q4_K_M.gguf}"
STT_MODEL="${WHISPER_MODEL:-deepdml/faster-whisper-large-v3-turbo-ct2}"

stage() { printf '\n== %s ==\n' "$*"; }

run_stt() {
  [ -n "$WAV" ] || { echo "SKIP stt: no wav found in voicebox output dir"; return 0; }
  stage "STT: $STT_MODEL on $(basename "$WAV")"
  cd "$BOT_DIR"
  uv run python - "$WAV" "$STT_MODEL" <<'PY'
import sys, time
import numpy as np

path, model_name = sys.argv[1], sys.argv[2]
from voicebot.agent import _preload_cuda_libs
_preload_cuda_libs()
from faster_whisper import WhisperModel

pcm = np.fromfile(path, dtype=np.uint16)[44:].view(np.int16)  # skip RIFF hdr
audio = pcm.astype(np.float32) / 32768.0
dur = len(audio) / 16000
model = WhisperModel(model_name, device="cuda", compute_type="default")

model.transcribe(np.zeros(1600, dtype=np.float32), language="en")  # CUDA warmup
times = []
for _ in range(3):
    t0 = time.perf_counter()
    segments, _ = model.transcribe(audio, language="en", beam_size=1)
    text = "".join(s.text for s in segments).strip()
    times.append(time.perf_counter() - t0)
best = min(times)
print(f"utterance {dur:.1f}s -> decode {best*1000:.0f}ms (rtf {dur/best:.1f}x): '{text[:60]}'")
PY
  stage "STT: Voicebox batch /transcribe (comparison)"
  curl -s -X POST "${VOICEBOX_URL:-http://127.0.0.1:17600}/transcribe" \
    -F "file=@$WAV;type=audio/wav" -F "model=${VOICEBOX_STT_MODEL:-turbo}" \
    -o /dev/null -w "round-trip: %{time_total}s\n"
}

run_llm() {
  stage "LLM: $LLM_MODEL via proxy (streaming TTFB, ~400 tok prompt)"
  python3 - "$PROXY_URL" "$LLM_MODEL" <<'PY'
import json, sys, time
import urllib.request

base, model = sys.argv[1], sys.argv[2]
history = [{"role": "system", "content": "You are a helpful assistant in a live voice conversation. Keep answers short and conversational."}]
for i in range(6):
    history.append({"role": "user" if i % 2 == 0 else "assistant", "content": f"Message number {i} with some filler context to make the prompt realistic." * 3})
history.append({"role": "user", "content": "In one sentence: what did I just ask you?"})

body = json.dumps({"model": model, "messages": history, "max_tokens": 80, "stream": True}).encode()
req = urllib.request.Request(f"{base}/chat/completions", data=body,
                             headers={"Content-Type": "application/json"})

for attempt in ("warmup", "measured"):
    t0 = time.perf_counter(); ttfb = None; tokens = 0
    with urllib.request.urlopen(req, timeout=120) as resp:
        for raw in resp:
            if raw.startswith(b"data:") and b"[DONE]" not in raw:
                if ttfb is None:
                    ttfb = time.perf_counter() - t0
                tokens += 1
    total = time.perf_counter() - t0
    if attempt == "measured":
        print(f"first token {ttfb:.2f}s | stream finished {total:.2f}s ({tokens} chunks)")
PY
}

for arg in "${@:-stt llm tts}"; do
  case "$arg" in
    stt) run_stt ;;
    llm) run_llm ;;
    tts) stage "TTS: kokoro via scripts/tts-benchmark.py"; \
         uv run --env-file "$REPO_ROOT/.env" --directory "$REPO_ROOT/services/voice-mcp" \
           python "$REPO_ROOT/scripts/tts-benchmark.py" --engines kokoro --samples 3 ;;
    *) echo "unknown component: $arg" >&2; exit 1 ;;
  esac
done
