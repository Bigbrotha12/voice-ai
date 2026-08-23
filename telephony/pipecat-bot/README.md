# voicebot — Pipecat voice agent using Voicebox TTS/STT

A complete Pipecat voice agent with Voicebox as the TTS/STT backend, LiveKit for real-time transport, and Ollama for local LLM inference.

## What works

- **`VoiceboxTTSService`** (`src/voicebot/tts.py`) — Pipecat `TTSService` subclass
  - Streams WAV from Voicebox, parses RIFF header, yields raw PCM frames
  - Configurable: `base_url`, `profile_id`, `engine`, `client_id`, `chunk_size`
  - Falls back to `VOICEBOX_URL` / `VOICEBOX_PROFILE_ID` env vars
  - 8 unit tests (header parsing, PCM extraction, error paths)

- **`VoiceboxSTTService`** (`src/voicebot/stt.py`) — Pipecat `STTService` subclass
  - Batch transcription via Voicebox's `/transcribe` endpoint (Whisper backend)
  - Configurable: `base_url`, `model` (turbo/base/small/medium/large), `client_id`
  - Falls back to `VOICEBOX_URL` env var
  - 5 unit tests (transcription, empty text, error paths)
  - Note: batch-oriented, not streaming - suitable for dictation-style use cases

- **`agent.py`** — Complete voice agent wiring:
  - LiveKit transport for real-time audio
  - Voicebox TTS + STT
  - Ollama local LLM
  - Silero VAD (built into Pipecat)

## Quick start

### Prerequisites

1. **Voicebox** running on `http://127.0.0.1:17600` (GPU-enabled podman container)
2. **LiveKit server** — local dev server or LiveKit Cloud
3. **llama.cpp server** running on `http://localhost:19091/v1` (Qwen2.5-3B-Instruct)

### Start dependencies

```bash
# Voicebox (from repo root)
./scripts/upstream-up.sh

# llama.cpp server (already running in podman as 'llama-small')
# If not running: docker run --gpus all -p 19091:8080 ghcr.io/ggml-org/llama.cpp:server-cuda13 -m /models/Qwen2.5-3B-Instruct-Q4_K_M.gguf --host 0.0.0.0 --port 8080

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
export OLLAMA_MODEL=qwen2.5-3b-instruct
```

### Run the agent

```bash
cd telephony/pipecat-bot
uv sync
uv run voicebot
```

The agent joins the LiveKit room and waits for participants. Connect a LiveKit client (web, mobile, SIP) to talk to it.

## Web test client

Open `test-client.html` in a browser (or serve it):

```bash
cd telephony/pipecat-bot
python3 -m http.server 8080
# Then open http://localhost:8080/test-client.html
```

Enter the same room name (`voicebot-room`), click **Join Room**, allow microphone access, and speak. The bot will respond via your speakers.

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  LiveKit    │────▶│ Voicebox    │────▶│   Ollama    │────▶│ Voicebox    │
│  Transport  │     │   STT       │     │   LLM       │     │   TTS       │
│  (audio)    │     │ (Whisper)   │     │ (llama3.2)  │     │ (kokoro)    │
└─────────────┘     └─────────────┘     └─────────────┘     └─────────────┘
       ▲                                                                   │
       └───────────────────────────────────────────────────────────────────┘
                              LiveKit Transport
```

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

- Replace batch STT with streaming (faster-whisper or Deepgram) for true real-time
- Add LiveKit SIP + Telnyx trunk for telephony
- Add conversation memory / context management
- Add function calling / tool use via Ollama