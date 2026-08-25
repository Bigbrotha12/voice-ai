# voicebot — Pipecat voice agent using Voicebox TTS + local Whisper STT

A complete Pipecat voice agent with Voicebox as the TTS backend, faster-whisper
(in-process) or Voicebox batch as the STT backend, LiveKit for real-time transport, and llama.cpp for local LLM inference.

## What works

- **`VoiceboxTTSService`** (`src/voicebot/tts.py`) — Pipecat `TTSService` subclass
  - Streams WAV from Voicebox, parses RIFF header, yields raw PCM frames
  - Configurable: `base_url`, `profile_id`, `engine`, `client_id`, `chunk_size`
  - Falls back to `VOICEBOX_URL` / `VOICEBOX_PROFILE_ID` env vars
  - 8 unit tests (header parsing, PCM extraction, error paths)

- **Local faster-whisper STT** (default) — pipecat's `WhisperSTTService`
  - Runs in-process (no HTTP round trip on the turn critical path)
  - GPU via the `[gpu]` optional extra (nvidia cublas/cudnn wheels, preloaded
    by `agent.py`); CPU-only installs work without it
  - Model/device/compute configurable via `WHISPER_MODEL` / `WHISPER_DEVICE` /
    `WHISPER_COMPUTE_TYPE`

- **`VoiceboxSTTService`** (`src/voicebot/stt.py`) — Pipecat `STTService` subclass
  - Batch transcription via Voicebox's `/transcribe` endpoint (Whisper backend);
    select with `VOICEBOT_STT_PROVIDER=voicebox`, model via `VOICEBOX_STT_MODEL`
    (**use `base` when the Voicebox runtime runs `--cpu` — small/turbo crash there**)
  - Configurable: `base_url`, `model`, `client_id`; falls back to `VOICEBOX_URL` env var
  - 5 unit tests (transcription, empty text, error paths)
  - Note: batch-oriented, not streaming - suitable for dictation-style use cases

- **Semantic end-of-turn** — `LocalSmartTurnAnalyzerV3` (pipecat's bundled
  smart-turn model; pinned explicitly in `agent.py`)

- **`agent.py`** — Complete voice agent wiring:
  - LiveKit transport for real-time audio
  - Voicebox TTS + pluggable STT (local faster-whisper | Voicebox batch)
  - llama.cpp LLM (OpenAI-compatible endpoint)
  - Silero VAD (built into Pipecat)

## Quick start

### Prerequisites

1. **Voicebox** running on `http://127.0.0.1:17600` (GPU-enabled podman container)
2. **LiveKit server** — local dev server or LiveKit Cloud
3. **LLM**: the queues proxy (OpenAI-compatible facade over RabbitMQ) on
   `http://localhost:9091/v1` for model.3b. Its worker starts/stops llama.cpp
   containers on demand - the first token after an idle gap can take ~30s.
   Direct llama endpoints (19090/19091) are managed by that worker; don't
   point the bot at them unless you also manage lifecycle yourself.

### Start dependencies

```bash
# Voicebox (from repo root)
./scripts/stack-up.sh

# LLM: queues proxy + worker (homelab stack, host-native)
# cd ~/Documents/homelab/podman/queues && podman-compose up -d
# llama.cpp containers start on demand when tasks arrive.

# LiveKit local dev server (Docker)
docker run --rm -p 7880:7880 -p 7881:7881 -p 7882:7882/udp \
  livekit/livekit-server --dev --bind 0.0.0.0
```

### Configure environment

```bash
export VOICEBOX_URL=http://127.0.0.1:17600
export VOICEBOX_PROFILE_ID=<your-profile-id>  # from voicebox.voices()
export VOICEBOX_ENGINE=kokoro
export LIVEKIT_URL=ws://localhost:7880
export LIVEKIT_API_KEY=devkey
export LIVEKIT_API_SECRET=secret
export LIVEKIT_ROOM=voicebot-room
export OLLAMA_MODEL=Qwen3-8B-Q4_K_M.gguf
export OLLAMA_BASE_URL=http://localhost:9091/v1   # queues proxy -> model.3b

# STT (optional overrides; defaults to local faster-whisper)
export VOICEBOT_STT_PROVIDER=local            # local | voicebox
export WHISPER_MODEL=deepdml/faster-whisper-large-v3-turbo-ct2
export WHISPER_DEVICE=auto                    # auto | cpu | cuda
```

### Run the agent

```bash
cd telephony/pipecat-bot
uv sync --extra gpu   # drop --extra gpu for CPU-only STT
uv run voicebot
```

The agent joins the LiveKit room and waits for participants. Connect a LiveKit client (web, mobile, SIP) to talk to it.

## Web test client

Open `test-client.html` in a browser (or serve it):

```bash
cd telephony/pipecat-bot
python3 -m http.server 8080 --bind 127.0.0.1
# Then open http://localhost:8080/test-client.html
```

Enter the same room name (`voicebot-room`), click **Join Room**, allow microphone access, and speak. The bot will respond via your speakers.

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  LiveKit    │────▶│ faster-     │────▶│  llama.cpp  │────▶│ Voicebox    │
│  Transport  │     │ whisper STT │     │ LLM (Qwen)  │     │ TTS (kokoro)│
│  (audio)    │     │ (in-process)│     │             │     │             │
└─────────────┘     └─────────────┘     └─────────────┘     └─────────────┘
       ▲                                                                   │
       └───────────────────────────────────────────────────────────────────┘
                             LiveKit Transport
```

STT runs in this process by default; `VOICEBOT_STT_PROVIDER=voicebox` swaps the
first box for Voicebox's batch `/transcribe` endpoint.

## Latency (RTX 4060 Ti)

Measured in `telephony/RESEARCH.md`:
- **kokoro**: 40ms (short) / 150ms (medium) — realtime-grade
- **luxtts**: 110ms / 190ms
- **chatterbox_turbo**: 780ms / 2.3s — expressive but not live-turn viable

Kokoro is the default for live conversation.

## Testing

```sh
cd telephony/pipecat-bot
uv sync
uv run pytest
```

## Next steps

- ~~Replace batch STT with streaming (faster-whisper or Deepgram) for true real-time~~ done: in-process faster-whisper is the default
- Add preemptive generation on partial transcripts (no framework support in pipecat 1.7.0; custom work)
- Add LiveKit SIP + Telnyx trunk for telephony
- Add conversation memory / context management
- Add function calling / tool use via Ollama
