"""Piper TTS adapter for Pipecat (glados-tts service, k3s "productivity").

Wraps POST /v1/audio/speech (OpenAI-speech-shaped) with
response_format="wav" as a Pipecat TTSService. Batch synthesis: the full
WAV arrives after piper finishes; frames are yielded at the file's native
rate and the transport resamples to audio_out_sample_rate
(pipecat base_output resamples every incoming frame - verified).

Requires glados-tts >= response_format support (defaults to mp3 without it;
this adapter always asks for wav).
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator

import httpx
from loguru import logger
from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService

from voicebot.tts import MAX_HEADER_SEARCH_BYTES, parse_wav_header

DEFAULT_BASE_URL = "http://127.0.0.1:5000"
DEFAULT_VOICE = "en_US-glados-high"


class PiperTTSService(TTSService):
    def __init__(
        self,
        *,
        base_url: str | None = None,
        voice: str | None = None,
        speed: float = 1.0,
        model: str = "piper",
        http_client: httpx.AsyncClient | None = None,
        chunk_size: int = 8192,
        **kwargs,
    ) -> None:
        self._voice = voice or os.environ.get("PIPER_VOICE", DEFAULT_VOICE)
        self._speed = max(0.1, float(speed))
        self._model = model
        super().__init__(
            settings=TTSSettings(
                model=model,
                voice=self._voice,
                language=None,
            ),
            **kwargs,
        )
        self._chunk_size = max(1, int(chunk_size))
        self._base_url = (
            base_url or os.environ.get("PIPER_TTS_BASE_URL", DEFAULT_BASE_URL)
        ).rstrip("/")
        self._http = http_client or httpx.AsyncClient(
            base_url=self._base_url, timeout=httpx.Timeout(120.0)
        )

    async def cleanup(self):
        await super().cleanup()
        await self._http.aclose()

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        try:
            async with self._http.stream(
                "POST",
                "/v1/audio/speech",
                json={
                    "model": self._model,
                    "input": text,
                    "voice": self._voice,
                    "speed": self._speed,
                    "response_format": "wav",
                },
            ) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode(errors="replace")[:300]
                    logger.error(f"{self} Piper HTTP {resp.status_code}: {body}")
                    yield ErrorFrame(error=f"Piper HTTP {resp.status_code}: {body}")
                    return

                await self.start_tts_usage_metrics(text)

                buf = b""
                header_parsed = False
                rate = 22050
                channels = 1
                first_frame = True
                produced_audio = False
                async for chunk in resp.aiter_bytes(self._chunk_size):
                    buf += chunk
                    if not header_parsed:
                        try:
                            offset, wav_rate, wav_channels = parse_wav_header(buf)
                        except ValueError:
                            if len(buf) > MAX_HEADER_SEARCH_BYTES:
                                msg = "Piper response missing WAV header"
                                logger.error(f"{self} {msg}")
                                yield ErrorFrame(error=msg)
                                return
                            continue
                        buf = buf[offset:]
                        rate = wav_rate
                        channels = wav_channels
                        header_parsed = True

                    while len(buf) >= self._chunk_size:
                        piece, buf = buf[: self._chunk_size], buf[self._chunk_size :]
                        if first_frame:
                            await self.stop_ttfb_metrics()
                            first_frame = False
                        produced_audio = True
                        yield TTSAudioRawFrame(piece, rate, channels, context_id=context_id)

                if not header_parsed:
                    msg = "Piper response ended without WAV header"
                    logger.error(f"{self} {msg}")
                    yield ErrorFrame(error=msg)
                    return
                if buf:
                    if first_frame:
                        await self.stop_ttfb_metrics()
                    produced_audio = True
                    yield TTSAudioRawFrame(buf, rate, channels, context_id=context_id)
                if not produced_audio:
                    msg = "Piper returned a valid WAV header with no audio data"
                    logger.error(f"{self} {msg}")
                    if first_frame:
                        await self.stop_ttfb_metrics()
                    yield ErrorFrame(error=msg)
        except Exception as exc:
            logger.error(f"{self} Piper synthesis failed: {exc!r}")
            yield ErrorFrame(error=f"Piper synthesis failed: {exc!r}")
