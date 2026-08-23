"""Voicebox TTS adapter for Pipecat.

Wraps POST /generate/stream (Voicebox REST) as a Pipecat TTSService.

Note: the endpoint returns a complete WAV after full batch synthesis
(TTFB == total synthesis time), so this is a batch-TTS adapter, not a
streaming one. Measured on RTX 4060 Ti: kokoro ~40ms for short turns,
fast enough for live conversation anyway. See telephony/RESEARCH.md.
"""

from __future__ import annotations

import os
import struct
from collections.abc import AsyncGenerator

import httpx
from loguru import logger
from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
from pipecat.services.tts_service import TTSService

DEFAULT_BASE_URL = "http://127.0.0.1:17600"


def parse_wav_header(data: bytes) -> tuple[int, int, int]:
    """Parse a RIFF/WAVE header; return (pcm_offset, sample_rate, channels).

    Raises ValueError if `data` does not yet contain a complete 'data'
    chunk header, or on malformed input.
    """
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE stream")
    pos = 12
    sample_rate: int | None = None
    channels: int | None = None
    while True:
        if len(data) < pos + 8:
            raise ValueError("incomplete wav header")
        chunk_id = data[pos : pos + 4]
        (chunk_size,) = struct.unpack_from("<I", data, pos + 4)
        if chunk_id == b"fmt " and len(data) >= pos + 8 + 16:
            channels = struct.unpack_from("<H", data, pos + 10)[0]
            sample_rate = struct.unpack_from("<I", data, pos + 12)[0]
        elif chunk_id == b"data":
            if sample_rate is None:
                raise ValueError("data chunk before fmt chunk")
            return pos + 8, sample_rate, channels or 1
        pos += 8 + chunk_size + (chunk_size % 2)


class VoiceboxTTSService(TTSService):
    def __init__(
        self,
        *,
        base_url: str | None = None,
        profile_id: str | None = None,
        engine: str = "kokoro",
        client_id: str = "pipecat",
        http_client: httpx.AsyncClient | None = None,
        chunk_size: int = 8192,
        **kwargs,
    ) -> None:
        # NOTE: base TTSService attributes (self.sample_rate / self.chunk_size)
        # are 0 until pipeline setup - do not rely on them here.
        super().__init__(**kwargs)
        self._chunk_size = max(1, int(chunk_size))
        self._base_url = (
            base_url or os.environ.get("VOICEBOX_URL", DEFAULT_BASE_URL)
        ).rstrip("/")
        self._profile_id = profile_id or os.environ.get("VOICEBOX_PROFILE_ID", "")
        self._engine = engine
        self._client_header = {"X-Voicebox-Client-Id": client_id}
        self._http = http_client or httpx.AsyncClient(
            base_url=self._base_url, timeout=httpx.Timeout(120.0)
        )

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        try:
            async with self._http.stream(
                "POST",
                "/generate/stream",
                json={
                    "text": text,
                    "profile_id": self._profile_id,
                    "engine": self._engine,
                },
                headers=self._client_header,
            ) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode(errors="replace")[:300]
                    logger.error(f"{self} Voicebox HTTP {resp.status_code}: {body}")
                    yield ErrorFrame(error=f"Voicebox HTTP {resp.status_code}: {body}")
                    return

                await self.start_tts_usage_metrics(text)

                buf = b""
                header_parsed = False
                rate = 24000
                first_frame = True
                async for chunk in resp.aiter_bytes(self._chunk_size):
                    buf += chunk
                    if not header_parsed:
                        try:
                            offset, wav_rate, _channels = parse_wav_header(buf)
                        except ValueError:
                            if len(buf) > 65536:
                                msg = "Voicebox response missing WAV header"
                                logger.error(f"{self} {msg}")
                                yield ErrorFrame(error=msg)
                                return
                            continue
                        buf = buf[offset:]
                        rate = wav_rate
                        header_parsed = True

                    while len(buf) >= self._chunk_size:
                        piece, buf = buf[: self._chunk_size], buf[self._chunk_size :]
                        if first_frame:
                            await self.stop_ttfb_metrics()
                            first_frame = False
                        yield TTSAudioRawFrame(piece, rate, 1, context_id=context_id)

                if not header_parsed:
                    msg = "Voicebox response ended without WAV header"
                    logger.error(f"{self} {msg}")
                    yield ErrorFrame(error=msg)
                    return
                if buf:
                    if first_frame:
                        await self.stop_ttfb_metrics()
                    yield TTSAudioRawFrame(buf, rate, 1, context_id=context_id)
        except Exception as exc:
            logger.error(f"{self} Voicebox synthesis failed: {exc!r}")
            yield ErrorFrame(error=f"Voicebox synthesis failed: {exc!r}")
