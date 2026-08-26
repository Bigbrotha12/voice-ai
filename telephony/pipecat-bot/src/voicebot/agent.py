"""Voice agent: LiveKit transport + pluggable TTS/STT + llama.cpp LLM.

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
  VOICEBOT_STT_PROVIDER (local|voicebox|openai), WHISPER_MODEL,
  WHISPER_DEVICE, WHISPER_COMPUTE_TYPE, VOICEBOX_STT_MODEL,
  OPENAI_STT_BASE_URL, OPENAI_STT_MODEL
  VOICEBOT_TTS_PROVIDER (voicebox|piper), PIPER_TTS_BASE_URL,
  PIPER_VOICE, PIPER_SPEED
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from livekit import api
from loguru import logger
from datetime import datetime
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.services.mcp_service import MCPClient
from pipecat.services.ollama.llm import OLLamaLLMService
from pipecat.services.stt_service import STTService
from pipecat.services.whisper.stt import WhisperSTTService
from pipecat.transports.livekit.transport import LiveKitParams, LiveKitTransport
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from voicebot.backchannel import BackchannelInjectorProcessor, BackchannelTriggerProcessor, load_bank
from voicebot.openai_batch_stt import OpenAIBatchSTTService
from voicebot.piper_tts import PiperTTSService
from voicebot.stt import VoiceboxSTTService
from voicebot.tts import VoiceboxTTSService

LIVEKIT_URL = os.getenv("LIVEKIT_URL", "ws://localhost:7880")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")
LIVEKIT_ROOM = os.getenv("LIVEKIT_ROOM", "voicebot-room")

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-3b-instruct")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:19091/v1")

VOICEBOX_URL = os.getenv("VOICEBOX_URL", "http://127.0.0.1:17600")
VOICEBOX_PROFILE_ID = os.getenv("VOICEBOX_PROFILE_ID", "")
VOICEBOX_ENGINE = os.getenv("VOICEBOX_ENGINE", "kokoro")

# STT selection. "local" runs faster-whisper in this process (GPU via the
# [gpu] extra); "voicebox" posts utterances to Voicebox's batch endpoint;
# "openai" posts to any OpenAI-compatible /v1/audio/transcriptions service
# (whisper-stt in the k3s productivity namespace, or OpenAI itself).
STT_PROVIDER = os.getenv("VOICEBOT_STT_PROVIDER", "local")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "deepdml/faster-whisper-large-v3-turbo-ct2")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "auto")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "default")
VOICEBOX_STT_MODEL = os.getenv("VOICEBOX_STT_MODEL", "turbo")
OPENAI_STT_BASE_URL = os.getenv("OPENAI_STT_BASE_URL", "http://127.0.0.1:8000")
OPENAI_STT_MODEL = os.getenv("OPENAI_STT_MODEL", "whisper-1")
OPENAI_STT_LANGUAGE = os.getenv("OPENAI_STT_LANGUAGE", "")

# TTS selection. "voicebox" (default) uses Voicebox profiles; "piper" uses
# the glados-tts service (OpenAI-speech-shaped, wav requested explicitly).
TTS_PROVIDER = os.getenv("VOICEBOT_TTS_PROVIDER", "voicebox")
PIPER_TTS_BASE_URL = os.getenv("PIPER_TTS_BASE_URL", "http://127.0.0.1:5000")
PIPER_VOICE = os.getenv("PIPER_VOICE", "en_US-glados-high")
PIPER_SPEED = float(os.getenv("PIPER_SPEED", "1.0"))

# Backchannel system (RESEARCH.md layer 4). Bank dir: container mounts the
# repo's backchannels/ at /app/bank; host-side dev runs fall back to the
# repo-relative path.
BACKCHANNEL_ENABLED = os.getenv("VOICEBOT_BACKCHANNEL", "1").lower() not in ("0", "false", "no")


def _repo_root() -> Path:
    """Locate the repo root from this file's position (src/voicebot/agent.py)."""
    return Path(__file__).resolve().parents[4]


def _bank_meta(bank_dir: Path) -> dict | None:
    meta_path = bank_dir / ".meta.json"
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _bank_is_stale(bank_dir: Path) -> bool:
    """True when the bank sentinel doesn't match the current VOICEBOX_PROFILE_ID."""
    if not VOICEBOX_PROFILE_ID:
        return False  # no profile set; can't validate
    meta = _bank_meta(bank_dir)
    if meta is None:
        return True  # no sentinel → stale
    return meta.get("profile_id") != VOICEBOX_PROFILE_ID


def _regenerate_bank(bank_dir: Path) -> bool:
    """Invoke backchannel-bank.py for the current profile.

    Best-effort: returns True if the bank has usable clips after the attempt,
    False only when the bank is empty (bot disables backchannels).
    """
    script = _repo_root() / "scripts" / "backchannel-bank.py"
    if not script.exists():
        logger.warning("bank: backchannel-bank.py not found, skipping regeneration")
        return load_bank(bank_dir)
    logger.info(f"bank: profile changed → regenerating for {VOICEBOX_PROFILE_ID}")
    try:
        result = subprocess.run(
            [
                sys.executable, str(script),
                "--profile-id", VOICEBOX_PROFILE_ID,
                "--force",
            ],
            capture_output=True,
            text=True,
            timeout=300,
            env={**os.environ, "VOICEBOX_URL": VOICEBOX_URL},
        )
        if result.returncode != 0:
            logger.warning(f"bank: regeneration failed (rc={result.returncode}): {result.stderr[:200]}")
        else:
            logger.info("bank: regeneration complete")
    except subprocess.TimeoutExpired:
        logger.warning("bank: regeneration timed out after 300s")
    except Exception as exc:
        logger.warning(f"bank: regeneration error: {exc}")
    return load_bank(bank_dir)


def _resolve_bank() -> Path:
    candidates = [
        Path(os.getenv("VOICEBOT_BACKCHANNEL_BANK", "/app/bank")),
        Path("backchannels"),
    ]
    for candidate in candidates:
        if load_bank(candidate) and not _bank_is_stale(candidate):
            return candidate

    # Stale or empty — try regenerating for the current profile.
    for candidate in candidates:
        if load_bank(candidate):
            if _regenerate_bank(candidate):
                return candidate

    return candidates[0]


BANK_DIR = _resolve_bank()

# Tools: native functions + MCP servers. VOICEBOT_MCP_URLS is a comma-
# separated list of streamable-HTTP endpoints; VOICE_MCP_AUTH_TOKEN is the
# bearer those endpoints expect (empty = no auth, loopback-only setups).
MCP_URLS = [u.strip() for u in os.getenv("VOICEBOT_MCP_URLS", "").split(",") if u.strip()]
MCP_AUTH_TOKEN = os.getenv("VOICE_MCP_AUTH_TOKEN", "")

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
    turn critical path). openai: OpenAI-compatible batch transcriptions
    (whisper-stt service; same batch trade-off, one fast in-mesh hop).
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
    if provider == "openai":
        return OpenAIBatchSTTService(
            base_url=OPENAI_STT_BASE_URL,
            model=OPENAI_STT_MODEL,
            language=OPENAI_STT_LANGUAGE or None,
        )
    raise ValueError(
        f"Unknown VOICEBOT_STT_PROVIDER '{provider}' "
        "(expected 'local', 'voicebox' or 'openai')"
    )


def build_tts():
    """Build the configured TTS service.

    voicebox (default): Voicebox profiles via POST /generate/stream.
    piper: glados-tts service via /v1/audio/speech with response_format=wav;
    frames leave the adapter at the file's native rate and the transport
    resamples to audio_out_sample_rate.
    """
    provider = TTS_PROVIDER.strip().lower()
    if provider == "voicebox":
        return VoiceboxTTSService(
            base_url=VOICEBOX_URL,
            profile_id=VOICEBOX_PROFILE_ID,
            engine=VOICEBOX_ENGINE,
            client_id="voicebot-agent",
        )
    if provider == "piper":
        return PiperTTSService(
            base_url=PIPER_TTS_BASE_URL,
            voice=PIPER_VOICE,
            speed=PIPER_SPEED,
        )
    raise ValueError(
        f"Unknown VOICEBOT_TTS_PROVIDER '{provider}' (expected 'voicebox' or 'piper')"
    )


def _native_tool_schemas() -> list[FunctionSchema]:
    """Built-in tools that don't need an MCP server."""
    return [
        FunctionSchema(
            name="get_current_time",
            description="Get the assistant's current local date and time.",
            properties={},
            required=[],
        ),
    ]


async def _register_native_handlers(llm: OLLamaLLMService) -> None:
    async def get_current_time(params):
        await params.result_callback(datetime.now().isoformat(timespec="seconds"))

    llm.register_function("get_current_time", get_current_time)


def _resolvable(url: str) -> bool:
    """True if the URL's hostname resolves from this process.

    The repo .env carries container-oriented MCP URLs (host.containers.
    internal) that cannot resolve on host-side runs; letting them reach the
    MCP client produces an anyio TaskGroup teardown cascade that can kill
    startup. Skip them with a warning instead.
    """
    try:
        host = urlparse(url).hostname
        if not host:
            return False
        socket.getaddrinfo(host, None)
        return True
    except OSError:
        return False


async def setup_tools(llm: OLLamaLLMService) -> tuple[list[FunctionSchema], list[MCPClient]]:
    """Register native handlers + MCP servers; return schemas for LLMContext.

    MCP servers are streamable-HTTP endpoints (VOICEBOT_MCP_URLS, comma-
    separated). A server that's down degrades to a warning, never a crash -
    the conversation continues with whatever tools did register.
    """
    schemas: list[FunctionSchema] = _native_tool_schemas()
    await _register_native_handlers(llm)

    clients: list[MCPClient] = []
    for url in MCP_URLS:
        if not _resolvable(url):
            logger.warning(f"tools: MCP host unresolvable from here, skipping {url}")
            continue
        from mcp.client.session_group import StreamableHttpParameters

        params = StreamableHttpParameters(url=url)
        if MCP_AUTH_TOKEN:
            params.headers = {"Authorization": f"Bearer {MCP_AUTH_TOKEN}"}
        client = MCPClient(server_params=params)
        try:
            await client.start()
            tools = await client.register_tools(llm)
            schemas.extend(tools.standard_tools)
            names = [t.name for t in tools.standard_tools]
            logger.info(f"tools: {len(names)} MCP tools from {url}: {names}")
            clients.append(client)
        except Exception as exc:
            # Degrade, don't crash - but close the half-open session so we
            # don't leak the transport.
            try:
                await client.close()
            except Exception:
                pass
            logger.warning(f"tools: MCP server unreachable, skipping {url}: {exc}")

    logger.info(f"tools: {len(schemas)} functions available to LLM")
    return schemas, clients


def build_latency_observer() -> UserBotLatencyObserver:
    """Per-turn latency logging: user-stop -> bot-speech, plus service TTFBs.

    Phase 5 baseline measurement (RESEARCH.md). Headline number is
    on_latency_measured; the breakdown attributes the turn to user_turn
    (EOU wait + STT finalization), per-service TTFB, and sentence
    aggregation.
    """
    observer = UserBotLatencyObserver()

    @observer.event_handler("on_latency_measured")
    async def _measured(_, latency: float):
        logger.info(f"LATENCY user-stop -> bot-speech: {latency:.2f}s")

    @observer.event_handler("on_latency_breakdown")
    async def _breakdown(_, b):
        parts = []
        if b.user_turn_secs is not None:
            parts.append(f"user_turn {b.user_turn_secs:.2f}s")
        for t in b.ttfb:
            name = t.processor.split("#")[0].removesuffix("Service")
            parts.append(f"{name} ttfb {t.duration_secs:.2f}s")
        if b.text_aggregation:
            parts.append(f"agg {b.text_aggregation.duration_secs:.2f}s")
        for fc in b.function_calls:
            parts.append(f"{fc.function_name} {fc.duration_secs:.2f}s")
        logger.info("LATENCY breakdown: " + " | ".join(parts))

    return observer


async def build_pipeline() -> tuple[Pipeline, LiveKitTransport, LLMContext, list[MCPClient]]:
    tts_provider = TTS_PROVIDER.strip().lower()
    if tts_provider == "voicebox" and not VOICEBOX_PROFILE_ID:
        raise ValueError("VOICEBOX_PROFILE_ID env var required for voicebox TTS")

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
    tts = build_tts()

    llm = OLLamaLLMService(
        base_url=OLLAMA_BASE_URL,
        settings=OLLamaLLMService.Settings(
            model=OLLAMA_MODEL,
            system_instruction=SYSTEM_INSTRUCTION,
        ),
    )

    tool_schemas, mcp_clients = await setup_tools(llm)
    context = LLMContext(tools=tool_schemas)
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

    audio_out_rate = 24000
    backchannel_input: list = []
    backchannel_output: list = []
    if BACKCHANNEL_ENABLED:
        # Layer 4: pre-rendered acknowledgments during long user monologues.
        # Trigger rides the input path (audio energy + VAD state); injector
        # sits right before output and swaps BackchannelFrame -> raw PCM,
        # bypassing LLM+TTS entirely.
        trigger = BackchannelTriggerProcessor(bank_dir=BANK_DIR)
        injector = BackchannelInjectorProcessor(sample_rate=audio_out_rate)
        if trigger.enabled:
            logger.info(f"backchannel: {trigger.clip_count} clips from {BANK_DIR}")
            backchannel_input.append(trigger)
            backchannel_output.append(injector)
        else:
            logger.info("backchannel: bank empty, disabled")

    pipeline = Pipeline(
        [
            transport.input(),
            VADProcessor(vad_analyzer=SileroVADAnalyzer()),
            *backchannel_input,
            stt,
            user_aggregator,
            llm,
            tts,
            *backchannel_output,
            transport.output(),
            assistant_aggregator,
        ]
    )

    return pipeline, transport, context, mcp_clients


async def main():
    pipeline, transport, context, mcp_clients = await build_pipeline()

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        # Framework default is 300s: idle cancel kills the runner -> container
        # exit -> podman restart churn. 30min keeps the bot resident between
        # conversations.
        idle_timeout_secs=1800,
    )
    worker.add_observer(build_latency_observer())

    @transport.event_handler("on_participant_connected")
    async def on_participant_connected(transport, participant_id: str):
        print(f"Participant connected: {participant_id}")

    @transport.event_handler("on_participant_disconnected")
    async def on_participant_disconnected(transport, participant_id: str):
        # No worker.cancel() here: the idle timeout (1800s) owns teardown.
        # Cancelling on empty-room races quick reconnects (refresh/ICE
        # restart) and recycles the container after every goodbye.
        print(f"Participant disconnected: {participant_id}")

    if not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        raise SystemExit(
            "LIVEKIT_API_KEY / LIVEKIT_API_SECRET missing - configured-keys "
            "LiveKit mode has no defaults (see .env.example)"
        )

    print(f"Voice agent joining '{LIVEKIT_ROOM}' @ {LIVEKIT_URL}")
    print(f"LLM: {OLLAMA_MODEL} @ {OLLAMA_BASE_URL}")
    if TTS_PROVIDER.strip().lower() == "piper":
        print(f"TTS: piper/glados-tts @ {PIPER_TTS_BASE_URL} (voice={PIPER_VOICE})")
    else:
        print(f"TTS: Voicebox @ {VOICEBOX_URL} ({VOICEBOX_ENGINE})")
    if STT_PROVIDER.strip().lower() == "openai":
        print(f"STT: openai-batch @ {OPENAI_STT_BASE_URL} (model={OPENAI_STT_MODEL})")
    elif STT_PROVIDER.strip().lower() == "voicebox":
        print(f"STT: voicebox batch @ {VOICEBOX_URL} (model={VOICEBOX_STT_MODEL})")
    else:
        print(f"STT: local faster-whisper (model={WHISPER_MODEL}, device={WHISPER_DEVICE})")

    context.add_message({"role": "developer", "content": SYSTEM_INSTRUCTION})
    await worker.queue_frames([LLMRunFrame()])

    try:
        runner = WorkerRunner()
        await runner.add_workers(worker)
        await runner.run()
    finally:
        for client in mcp_clients:
            try:
                await client.close()
            except Exception:
                pass


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
