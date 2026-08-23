"""Voice agent: LiveKit transport + Voicebox TTS/STT + llama.cpp LLM.

Pipeline (pipecat 1.7.0 turn architecture):

  transport.input() -> VAD -> STT(segmented) -> user_aggregator
    -> llm -> tts -> transport.output() -> assistant_aggregator

The LLMContextAggregatorPair is what turns transcriptions into LLM runs
and bot text into context - omitting it means nothing downstream of STT
ever fires.

Run:
  uv run voicebot

Env:
  LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, LIVEKIT_ROOM
  OLLAMA_MODEL, OLLAMA_BASE_URL (llama.cpp OpenAI-compatible endpoint)
  VOICEBOX_URL, VOICEBOX_PROFILE_ID, VOICEBOX_ENGINE
"""

from __future__ import annotations

import asyncio
import os

from livekit import api
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.services.ollama.llm import OLLamaLLMService
from pipecat.transports.livekit.transport import LiveKitParams, LiveKitTransport
from pipecat.workers.runner import WorkerRunner

from voicebot.stt import VoiceboxSTTService
from voicebot.tts import VoiceboxTTSService

LIVEKIT_URL = os.getenv("LIVEKIT_URL", "ws://localhost:7880")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "devkey")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "secret")
LIVEKIT_ROOM = os.getenv("LIVEKIT_ROOM", "voicebot-room")

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-3b-instruct")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:19091/v1")

VOICEBOX_URL = os.getenv("VOICEBOX_URL", "http://127.0.0.1:17600")
VOICEBOX_PROFILE_ID = os.getenv("VOICEBOX_PROFILE_ID", "")
VOICEBOX_ENGINE = os.getenv("VOICEBOX_ENGINE", "kokoro")

SYSTEM_INSTRUCTION = (
    "You are a helpful assistant in a live voice conversation. "
    "Your responses are spoken aloud, so keep them brief and conversational - "
    "no emojis, lists, or markdown."
)


def generate_token(identity: str = "voicebot") -> str:
    return (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name("Voicebot")
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=LIVEKIT_ROOM,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .to_jwt()
    )


def build_pipeline() -> tuple[Pipeline, LiveKitTransport, LLMContext]:
    if not VOICEBOX_PROFILE_ID:
        raise ValueError("VOICEBOX_PROFILE_ID env var required")

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

    stt = VoiceboxSTTService(
        base_url=VOICEBOX_URL,
        model="turbo",
        client_id="voicebot-agent",
    )

    llm = OLLamaLLMService(
        base_url=OLLAMA_BASE_URL,
        settings=OLLamaLLMService.Settings(
            model=OLLAMA_MODEL,
            system_instruction=SYSTEM_INSTRUCTION,
        ),
    )

    tts = VoiceboxTTSService(
        base_url=VOICEBOX_URL,
        profile_id=VOICEBOX_PROFILE_ID,
        engine=VOICEBOX_ENGINE,
        client_id="voicebot-agent",
    )

    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            VADProcessor(vad_analyzer=SileroVADAnalyzer()),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    return pipeline, transport, context


async def main():
    pipeline, transport, context = build_pipeline()

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
    )

    @transport.event_handler("on_participant_connected")
    async def on_participant_connected(transport, participant_id: str):
        print(f"Participant connected: {participant_id}")

    @transport.event_handler("on_participant_disconnected")
    async def on_participant_disconnected(transport, participant_id: str):
        print(f"Participant disconnected: {participant_id}")
        if not transport.get_participants():
            await worker.cancel()

    print(f"Voice agent joining '{LIVEKIT_ROOM}' @ {LIVEKIT_URL}")
    print(f"LLM: {OLLAMA_MODEL} @ {OLLAMA_BASE_URL}")
    print(f"Voicebox: {VOICEBOX_URL} ({VOICEBOX_ENGINE})")

    context.add_message({"role": "developer", "content": SYSTEM_INSTRUCTION})
    await worker.queue_frames([LLMRunFrame()])

    runner = WorkerRunner()
    await runner.add_workers(worker)
    await runner.run()


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
