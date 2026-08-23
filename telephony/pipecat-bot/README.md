# voicebot — Pipecat adapter for Voicebox TTS

A minimal Pipecat service that wraps Voicebox's REST API (`POST /generate/stream`) as a TTS source, enabling Voicebox's cloned/preset voices inside Pipecat voice-agent pipelines.

## What works

- **`VoiceboxTTSService`** (`src/voicebot/tts.py`) — Pipecat `TTSService` subclass
  - Streams WAV from Voicebox, parses RIFF header, yields raw PCM frames
  - Configurable: `base_url`, `profile_id`, `engine`, `client_id`, `chunk_size`
  - Falls back to `VOICEBOX_URL` / `VOICEBOX_PROFILE_ID` env vars
  - 8 unit tests (header parsing, PCM extraction, error paths)

## What's needed next

This is a **TTS adapter only**. To run a full voice agent you need:

1. **Transport** — Daily, LiveKit, or self-hosted LiveKit SIP (see `telephony/RESEARCH.md`)
2. **LLM** — any Pipecat-compatible LLM service (OpenAI, Anthropic, local)
3. **STT** — Deepgram, faster-whisper, or Voicebox's `/transcribe` (not yet wrapped)
4. **VAD** — Pipecat's built-in Silero VAD (included in `[silero]` extra)

## Usage

```python
from pipecat.pipeline import Pipeline
from pipecat.services.openai.llm import OpenAILLMService
from voicebot.tts import VoiceboxTTSService

tts = VoiceboxTTSService(
    base_url="http://127.0.0.1:17600",
    profile_id="your-profile-id",
    engine="kokoro",
)

pipeline = Pipeline([
    # ... VAD, STT, LLM, tts, transport
])
```

## Latency

Measured on RTX 4060 Ti (see `telephony/RESEARCH.md`):
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
