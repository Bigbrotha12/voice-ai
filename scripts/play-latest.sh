#!/usr/bin/env bash
# Play the most recently generated WAV from the Voicebox output bind mount.
# The headless container cannot reach your speakers; audio files land here.
set -euo pipefail

OUT_DIR="${VOICEBOX_OUTPUT_DIR:-$HOME/Projects/ai/voicebox-upstream/output}"

latest=$(find "$OUT_DIR" -maxdepth 1 -name '*.wav' -printf '%T@ %p\n' 2>/dev/null \
  | sort -rn | head -1 | cut -d' ' -f2-)

[ -n "$latest" ] || { echo "No .wav files in $OUT_DIR yet"; exit 1; }
echo "Playing: $latest"
exec ffplay -autoexit -nodisp -loglevel error "$latest"
