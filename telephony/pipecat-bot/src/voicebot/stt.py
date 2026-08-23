"""Voicebox STT adapter for Pipecat.

Wraps Voicebox's POST /transcribe endpoint as a Pipecat STTService.
Uses the same Whisper backend as Voicebox's dictation feature.

Note: Voicebox's /transcribe is batch-oriented (uploads complete audio file),
not streaming. For live conversation you'd want faster-whisper or Deepgram
instead. This adapter is suitable for dictation-style use cases or testing.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncGenerator

import httpx
from loguru import logger
from pipecat.frames.frames import ErrorFrame, Frame, TranscriptionFrame
from pipecat.services.stt_service import STTService

DEFAULT_BASE_URL = "http://127.0.0.1:17600"


class VoiceboxSTTService(STTService):
    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str = "turbo",
        client_id: str = "pipecat",
        http_client: httpx.AsyncClient | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._base_url = (
            base_url or os.environ.get("VOICEBOX_URL", DEFAULT_BASE_URL)
        ).rstrip("/")
        self._model = model
        self._client_header = {"X-Voicebox-Client-Id": client_id}
        self._http = http_client or httpx.AsyncClient(
            base_url=self._base_url, timeout=httpx.Timeout(120.0)
        )

    async def run_stt(self, audio: bytes, context_id: str) -> AsyncGenerator[Frame | None, None]:
        """Transcribe audio bytes via Voicebox's /transcribe endpoint.

        Note: This is batch transcription, not streaming. The audio parameter
        should be a complete utterance (e.g., from VAD endpointing).
        """
        try:
            # Write audio to temp file for upload
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp.write(audio)
                tmp_path = tmp.name

            try:
                with open(tmp_path, "rb") as f:
                    resp = await self._http.post(
                        "/transcribe",
                        files={"file": f},
                        data={"model": self._model},
                        headers=self._client_header,
                    )

                if resp.status_code == 202:
                    # Model downloading - not ready yet
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
                    yield TranscriptionFrame(text=text, user_id="", timestamp="")
                else:
                    logger.debug(f"{self} Empty transcription")

            finally:
                os.unlink(tmp_path)

        except Exception as exc:
            logger.error(f"{self} Voicebox transcription failed: {exc!r}")
            yield ErrorFrame(error=f"Voicebox transcription failed: {exc!r}")
