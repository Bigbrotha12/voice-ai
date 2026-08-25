"""Voice agent: LiveKit transport + Voicebox TTS + local faster-whisper STT + llama.cpp LLM.

Pipeline (pipecat 1.7.0 turn architecture):

  transport.input() -> VAD -> STT(segmented) -> user_aggregator
    -> llm -> tts -> transport.output() -> assistant_aggregator

The LLMContextAggregatorPair is what turns transcriptions into LLM runs
and bot text into context - omitting it means nothing downstream of STT
ever fires.

Latency notes (see telephony/RESEARCH.md):
- STT defaults to in-process faster-whisper (no HTTP round trip, partials
  available later); VOICEBOT_STT_PROVIDER=voicebox restores the batch
  /transcribe path.
- End-of-turn uses LocalSmartTurnAnalyzerV3 (semantic EOU, bundled with
  pipecat and its default stop strategy - pinned here for visibility).

Run:
  uv run voicebot

Env:
  LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, LIVEKIT_ROOM
  OLLAMA_MODEL, OLLAMA_BASE_URL (llama.cpp OpenAI-compatible endpoint)
  VOICEBOX_URL, VOICEBOX_PROFILE_ID, VOICEBOX_ENGINE
  VOICEBOT_STT_PROVIDER, WHISPER_MODEL, WHISPER_DEVICE,
  WHISPER_COMPUTE_TYPE, VOICEBOX_STT_MODEL
"""

from __future__ import annotations

import asyncio
import ctypes
import os
from pathlib import Path

from livekit import api
from loguru import logger
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
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
from pipecat.services.stt_service import STTService
from pipecat.services.whisper.stt import WhisperSTTService
from pipecat.transports.livekit.transport import LiveKitParams, LiveKitTransport
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
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

# STT selection. "local" runs faster-whisper in this process (GPU via the
# [gpu] extra); "voicebox" posts utterances to Voicebox's batch endpoint.
STT_PROVIDER = os.getenv("VOICEBOT_STT_PROVIDER", "local")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "deepdml/faster-whisper-large-v3-turbo-ct2")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "auto")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "default")
VOICEBOX_STT_MODEL = os.getenv("VOICEBOX_STT_MODEL", "turbo")

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


def _preload_cuda_libs() -> None:
    """Make faster-whisper GPU inference work from a plain venv.

    CTranslate2 dlopens libcublas/libcudnn by soname at CUDA init; the pip
    wheels ship them under site-packages/nvidia/*/lib, which is not on the
    loader path. Preloading with RTLD_GLOBAL satisfies it without requiring
    LD_LIBRARY_PATH to be set before process start. No-op when the [gpu]
    extra is not installed (CPU-only setups).
    """
    try:
        import nvidia.cublas
        import nvidia.cudnn
    except ImportError:
        logger.debug("nvidia pip wheels not installed - skipping CUDA lib preload")
        return

    # Namespace packages (__file__ is None): resolve lib dirs via __path__.
    pending: list[Path] = []
    for module in (nvidia.cublas, nvidia.cudnn):
        for pkg_path in module.__path__:
            pending.extend(sorted((Path(pkg_path) / "lib").glob("lib*.so*")))

    # Interdependent libs (libcublas needs libcublasLt first) may fail on an
    # earlier pass; loop until a pass makes no progress.
    loaded = 0
    while pending:
        remaining: list[Path] = []
        progressed = False
        for so in pending:
            try:
                ctypes.CDLL(str(so), mode=ctypes.RTLD_GLOBAL)
                loaded += 1
                progressed = True
            except OSError as exc:
                logger.debug(f"CUDA preload deferred {so.name}: {exc}")
                remaining.append(so)
        if not progressed:
            break
        pending = remaining
    logger.debug(f"Preloaded CUDA libs: {loaded}")


def build_stt() -> STTService:
    """Build the configured STT service.

    local (default): faster-whisper in this process - no HTTP round trip,
    lowest utterance latency. voicebox: Voicebox's batch POST /transcribe
    (shared model cache, no extra VRAM, but full-decode latency on the
    turn critical path).
    """
    provider = STT_PROVIDER.strip().lower()
    if provider == "local":
        _preload_cuda_libs()
        return WhisperSTTService(
            settings=WhisperSTTService.Settings(
                model=WHISPER_MODEL,
                language=None,  # auto-detect
            ),
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE_TYPE,
        )
    if provider == "voicebox":
        return VoiceboxSTTService(
            base_url=VOICEBOX_URL,
            model=VOICEBOX_STT_MODEL,
            client_id="voicebot-agent",
        )
    raise ValueError(
        f"Unknown VOICEBOT_STT_PROVIDER '{provider}' (expected 'local' or 'voicebox')"
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

    stt = build_stt()

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
        user_params=LLMUserAggregatorParams(
            user_turn_strategies=UserTurnStrategies(
                # LocalSmartTurnAnalyzerV3 is already pipecat's default stop
                # strategy; pinned here so the semantic-EOU dependency is
                # visible and swappable (RESEARCH.md layer 2). It decides
                # completion from audio, then waits for the final transcript -
                # which is why in-process STT matters for turn latency.
                stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())],
            ),
        ),
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
    print(f"TTS: Voicebox @ {VOICEBOX_URL} ({VOICEBOX_ENGINE})")
    print(f"STT: {STT_PROVIDER} (model={WHISPER_MODEL if STT_PROVIDER == 'local' else VOICEBOX_STT_MODEL})")

    context.add_message({"role": "developer", "content": SYSTEM_INSTRUCTION})
    await worker.queue_frames([LLMRunFrame()])

    runner = WorkerRunner()
    await runner.add_workers(worker)
    await runner.run()


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
