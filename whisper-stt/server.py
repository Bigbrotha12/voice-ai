"""GPU-accelerated OpenAI-compatible STT service.

Exposes POST /v1/audio/transcriptions matching the OpenAI Whisper API
shape. Uses faster-whisper on GPU via CDI passthrough for ~200ms turns
(vs ~800ms on CPU through the k3s cluster).

Designed for podman host-side deployment (docker/whisper-stt.yml) with
nvidia.com/gpu=all CDI device. The bot's OpenAIBatchSTTService speaks
this API directly.
"""

from __future__ import annotations

import ctypes
import io
import os
import tempfile
import time
from pathlib import Path

import httpx
import numpy as np
import soundfile as sf
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse
from loguru import logger

MODEL_SIZE = os.getenv("WHISPER_MODEL", "deepdml/faster-whisper-large-v3-turbo-ct2")
DEVICE = os.getenv("WHISPER_DEVICE", "cuda")
COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "float16")

app = FastAPI(title="whisper-stt", version="1.0.0")


def _preload_cuda_libs() -> None:
    """Make faster-whisper GPU inference work from a plain pip install.

    CTranslate2 dlopens libcublas/libcudnn by soname at CUDA init; the pip
    wheels ship them under site-packages/nvidia/*/lib, which is not on the
    loader path. Preloading with RTLD_GLOBAL satisfies it.
    """
    try:
        import nvidia.cublas
        import nvidia.cudnn
    except ImportError:
        logger.debug("nvidia pip wheels not installed - skipping CUDA lib preload")
        return

    pending: list[Path] = []
    for module in (nvidia.cublas, nvidia.cudnn):
        for pkg_path in module.__path__:
            pending.extend(sorted((Path(pkg_path) / "lib").glob("lib*.so*")))

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
        pending = remaining
        if not progressed:
            break
    logger.debug(f"CUDA libs preloaded: {loaded}")


# Lazy-loaded model (first request triggers download + GPU init).
_model = None


def _get_model():
    global _model
    if _model is None:
        _preload_cuda_libs()
        from faster_whisper import WhisperModel

        t0 = time.monotonic()
        logger.info(f"loading {MODEL_SIZE} on {DEVICE} ({COMPUTE_TYPE})...")
        _model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE)
        logger.info(f"model ready in {time.monotonic() - t0:.1f}s")
    return _model


@app.get("/healthz")
def healthz():
    return {"status": "ok", "model": MODEL_SIZE, "device": DEVICE}


@app.post("/v1/audio/transcriptions")
async def transcribe(
    file: UploadFile = File(...),
    model: str = Form(default="whisper-1"),
    language: str | None = Form(default=None),
):
    t0 = time.monotonic()
    audio_bytes = await file.read()
    if not audio_bytes:
        return JSONResponse(status_code=400, content={"error": "empty upload"})

    # Decode to numpy via soundfile (handles WAV, MP3, OGG, FLAC, WebM).
    try:
        data, sample_rate = sf.read(io.BytesIO(audio_bytes), dtype="float32")
    except Exception as exc:
        return JSONResponse(status_code=400, content={"error": f"decode failed: {exc}"})

    # Mono mixdown.
    if data.ndim > 1:
        data = data.mean(axis=1)

    # Resample to 16 kHz if needed (faster-whisper expects 16k).
    if sample_rate != 16000:
        target_len = int(len(data) * 16000 / sample_rate)
        indices = np.linspace(0, len(data) - 1, target_len).astype(int)
        data = data[indices]

    # Transcribe.
    whisper = _get_model()
    segments, info = whisper.transcribe(
        data,
        language=language,
        beam_size=3,
        vad_filter=True,
    )
    text = " ".join(seg.text.strip() for seg in segments).strip()

    elapsed = time.monotonic() - t0
    if not text:
        logger.debug(f"no speech detected ({elapsed:.2f}s)")
        return JSONResponse(status_code=400, content={"error": "no speech detected"})

    logger.debug(f"transcribed ({elapsed:.2f}s): {text[:60]}...")
    return {"text": text}
