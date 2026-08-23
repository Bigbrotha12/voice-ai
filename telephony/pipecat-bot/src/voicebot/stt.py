"""Voicebox STT adapter for Pipecat.

Wraps Voicebox's POST /transcribe endpoint as a Pipecat STTService.

Built on SegmentedSTTService: mic audio is buffered while VAD reports
speech, and run_stt receives one complete utterance as a WAV container -
exactly what Voicebox's multipart endpoint (librosa/libsndfile) expects.
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

DEFAULT_BASE_URL = "http://127.0.0.1:17600"


class VoiceboxSTTService(SegmentedSTTService):
    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str = "turbo",
        client_id: str = "pipecat",
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
            base_url or os.environ.get("VOICEBOX_URL", DEFAULT_BASE_URL)
        ).rstrip("/")
        self._model = model
        self._client_header = {"X-Voicebox-Client-Id": client_id}
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
                "/transcribe",
                files={"file": ("audio.wav", audio, "audio/wav")},
                data={"model": self._model},
                headers=self._client_header,
            )

            if resp.status_code == 202:
                msg = "Whisper model is downloading; try again in a minute"
                logger.warning(f"{self} {msg}")
                yield ErrorFrame(error=msg)
                return

            if resp.status_code != 200:
                body = resp.text[:300]
                logger.error(f"{self} Voicebox HTTP {resp.status_code}: {body}")
                yield ErrorFrame(error=f"Voicebox HTTP {resp.status_code}: {body}")
                return

            data = resp.json()
            text = data.get("text", "").strip()

            if text:
                logger.debug(f"{self} Transcribed: {text[:50]}...")
                user_id = getattr(self, "_user_id", "") or ""
                yield TranscriptionFrame(
                    text=text, user_id=user_id, timestamp=str(int(time.time() * 1000))
                )
            else:
                logger.debug(f"{self} Empty transcription")

        except Exception as exc:
            logger.error(f"{self} Voicebox transcription failed: {exc!r}")
            yield ErrorFrame(error=f"Voicebox transcription failed: {exc!r}")
