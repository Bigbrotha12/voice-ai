"""OpenAI-compatible batch STT adapter for Pipecat.

Wraps POST /v1/audio/transcriptions (whisper-stt service in the k3s
"productivity" namespace, or any OpenAI-compatible transcription endpoint)
as a Pipecat STTService.

Built on SegmentedSTTService like VoiceboxSTTService: mic audio is buffered
while VAD reports speech, and run_stt receives one complete utterance as a
WAV container - which the endpoint ffmpeg-normalizes to 16 kHz mono before
faster-whisper inference.

Batch trade-off (same as VoiceboxSTTService): no partial transcripts, so
smart-turn EOU waits for the full round trip. Select with
VOICEBOT_STT_PROVIDER=openai.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncGenerator

import httpx
from loguru import logger
from pipecat.frames.frames import ErrorFrame, Frame, TranscriptionFrame
from pipecat.services.settings import STTSettings
from pipecat.services.stt_service import SegmentedSTTService

DEFAULT_BASE_URL = "http://127.0.0.1:8000"


class OpenAIBatchSTTService(SegmentedSTTService):
    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str = "whisper-1",
        sample_rate: int = 16000,
        http_client: httpx.AsyncClient | None = None,
        **kwargs,
    ) -> None:
        super().__init__(
            sample_rate=sample_rate,
            settings=STTSettings(model=model, language=None),
            **kwargs,
        )
        self._base_url = (
            base_url or os.environ.get("OPENAI_STT_BASE_URL", DEFAULT_BASE_URL)
        ).rstrip("/")
        self._model = model
        self._http = http_client or httpx.AsyncClient(
            base_url=self._base_url, timeout=httpx.Timeout(120.0)
        )

    async def cleanup(self):
        await super().cleanup()
        await self._http.aclose()

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        """Transcribe one complete utterance (WAV bytes from segmentation)."""
        try:
            resp = await self._http.post(
                "/v1/audio/transcriptions",
                files={"file": ("audio.wav", audio, "audio/wav")},
                data={"model": self._model},
            )

            # whisper-stt answers 400 for both empty uploads and "no speech
            # detected" - neither should error the pipeline; VAD occasionally
            # hands over near-silent segments.
            if resp.status_code == 400:
                logger.debug(f"{self} No speech detected ({resp.text[:120]})")
                return

            if resp.status_code != 200:
                body = resp.text[:300]
                logger.error(f"{self} STT HTTP {resp.status_code}: {body}")
                yield ErrorFrame(error=f"STT HTTP {resp.status_code}: {body}")
                return

            text = resp.json().get("text", "").strip()

            if text:
                logger.debug(f"{self} Transcribed: {text[:50]}...")
                user_id = getattr(self, "_user_id", "") or ""
                yield TranscriptionFrame(
                    text=text, user_id=user_id, timestamp=str(int(time.time() * 1000))
                )
            else:
                logger.debug(f"{self} Empty transcription")

        except Exception as exc:
            logger.error(f"{self} STT transcription failed: {exc!r}")
            yield ErrorFrame(error=f"STT transcription failed: {exc!r}")
