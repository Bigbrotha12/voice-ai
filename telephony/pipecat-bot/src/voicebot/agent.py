"""Voice agent wiring: LiveKit transport + Voicebox TTS/STT + Ollama LLM.

Run with:
  uv run src/voicebot/agent.py

Requires:
- LiveKit server running (local or cloud)
- Ollama running locally with a model pulled
- Voicebox container running on 17600

Env vars:
- LIVEKIT_URL: ws://localhost:7880 (or wss:// for cloud)
- LIVEKIT_API_KEY, LIVEKIT_API_SECRET: for token generation
- LIVEKIT_ROOM: room name to join
- OLLAMA_MODEL: e.g., "llama3.2" or "mistral"
- VOICEBOX_URL: http://127.0.0.1:17600
- VOICEBOX_PROFILE_ID: voice profile ID
- VOICEBOX_ENGINE: kokoro/luxtts/chatterbox_turbo/etc
"""

from __future__ import annotations

import os
import asyncio
from contextlib import asynccontextmanager

from livekit import api
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.transports.livekit.transport import LiveKitParams, LiveKitTransport
from pipecat.services.ollama.llm import OLLamaLLMService
from pipecat.frames.frames import Frame

from voicebot.tts import VoiceboxTTSService
from voicebot.stt import VoiceboxSTTService


# ─── Config ────────────────────────────────────────────────────────────────

LIVEKIT_URL = os.getenv("LIVEKIT_URL", "ws://localhost:7880")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "devkey")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "secret")
LIVEKIT_ROOM = os.getenv("LIVEKIT_ROOM", "voicebot-room")

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")

VOICEBOX_URL = os.getenv("VOICEBOX_URL", "http://127.0.0.1:17600")
VOICEBOX_PROFILE_ID = os.getenv("VOICEBOX_PROFILE_ID")
VOICEBOX_ENGINE = os.getenv("VOICEBOX_ENGINE", "kokoro")


# ─── Token generation ──────────────────────────────────────────────────────

def generate_token(identity: str = "voicebot") -> str:
    """Generate a LiveKit access token for the bot."""
    token = api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET) \
        .with_identity(identity) \
        .with_name("Voicebot") \
        .with_grants(api.VideoGrants(
            room_join=True,
            room=LIVEKIT_ROOM,
            can_publish=True,
            can_subscribe=True,
            can_publish_data=True,
        )) \
        .to_jwt()
    return token


# ─── Pipeline construction ────────────────────────────────────────────────

def build_pipeline() -> Pipeline:
    """Construct the full voice agent pipeline."""

    # LiveKit transport
    transport = LiveKitTransport(
        url=LIVEKIT_URL,
        token=generate_token(),
        room_name=LIVEKIT_ROOM,
        params=LiveKitParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=24000,
        ),
    )

    # STT: Voicebox Whisper (batch, not streaming)
    stt = VoiceboxSTTService(
        base_url=VOICEBOX_URL,
        model="turbo",
        client_id="voicebot-agent",
    )

    # LLM: Local Ollama
    llm = OLLamaLLMService(
        model=OLLAMA_MODEL,
        base_url=OLLAMA_BASE_URL,
    )

    # TTS: Voicebox streaming
    if not VOICEBOX_PROFILE_ID:
        raise ValueError("VOICEBOX_PROFILE_ID env var required")

    tts = VoiceboxTTSService(
        base_url=VOICEBOX_URL,
        profile_id=VOICEBOX_PROFILE_ID,
        engine=VOICEBOX_ENGINE,
        client_id="voicebot-agent",
    )

    # Pipeline: transport → STT → LLM → TTS → transport
    pipeline = Pipeline([
        transport.input(),
        stt,
        llm,
        tts,
        transport.output(),
    ])

    return pipeline, transport


# ─── Main ──────────────────────────────────────────────────────────────────

async def main():
    pipeline, transport = build_pipeline()

    task = PipelineTask(pipeline)

    # Handle participant events
    @transport.event_handler("on_participant_connected")
    async def on_participant_connected(transport, participant):
        print(f"Participant connected: {participant.identity}")

    @transport.event_handler("on_participant_disconnected")
    async def on_participant_disconnected(transport, participant):
        print(f"Participant disconnected: {participant.identity}")
        await task.cancel()

    print(f"Starting voice agent in room '{LIVEKIT_ROOM}'...")
    print(f"LiveKit: {LIVEKIT_URL}")
    print(f"Ollama: {OLLAMA_MODEL} @ {OLLAMA_BASE_URL}")
    print(f"Voicebox: {VOICEBOX_URL} ({VOICEBOX_ENGINE})")

    runner = PipelineRunner()
    await runner.run(task)


def run():
    """Synchronous entry point for console script."""
    asyncio.run(main())


if __name__ == "__main__":
    run()