"""Tests for PiperTTSService against canned HTTP + real WAV bytes."""

from __future__ import annotations

import inspect
import io
import json
import wave

import httpx
import pytest

from voicebot.piper_tts import PiperTTSService


def make_wav(pcm: bytes, sample_rate: int = 22050, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def make_service(handler, chunk_size: int = 8192) -> PiperTTSService:
    return PiperTTSService(
        base_url="http://test",
        voice="en_US-glados-high",
        speed=1.0,
        http_client=httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(handler)
        ),
        chunk_size=chunk_size,
        sample_rate=24000,
    )


async def collect(service: PiperTTSService, text="hi") -> list:
    return [f async for f in service.run_tts(text, context_id="ctx-1")]


class TestRunTts:
    def test_run_tts_matches_pipecat_contract(self):
        """run_tts(text, context_id) - Pipecat calls it with these two args."""
        params = list(inspect.signature(PiperTTSService.run_tts).parameters.values())
        from pipecat.services.tts_service import TTSService

        base_params = list(inspect.signature(TTSService.run_tts).parameters.values())
        assert [p.name for p in params[1:]] == [p.name for p in base_params[1:]]

    @pytest.mark.asyncio
    async def test_requests_wav_and_yields_native_rate_frames(self):
        pcm = bytes(range(256)) * 4
        wav = make_wav(pcm, sample_rate=22050)

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert request.url.path == "/v1/audio/speech"
            assert body["model"] == "piper"
            assert body["voice"] == "en_US-glados-high"
            assert body["response_format"] == "wav"
            return httpx.Response(200, content=wav, headers={"content-type": "audio/wav"})

        frames = await collect(make_service(handler))
        audio_frames = [f for f in frames if type(f).__name__ == "TTSAudioRawFrame"]
        assert audio_frames, "expected at least one audio frame"
        joined = b"".join(f.audio for f in audio_frames)
        assert joined == pcm
        # native piper rate rides on the frames; transport resamples to 24k
        assert all(f.sample_rate == 22050 for f in audio_frames)

    @pytest.mark.asyncio
    async def test_header_split_across_chunks(self):
        pcm = bytes(range(256)) * 4
        wav = make_wav(pcm)

        def handler(request):
            return httpx.Response(
                200,
                content=wav,
                headers={"content-type": "audio/wav"},
            )

        frames = [
            f
            for f in await collect(make_service(handler, chunk_size=7))
            if type(f).__name__ == "TTSAudioRawFrame"
        ]
        assert b"".join(f.audio for f in frames) == pcm

    @pytest.mark.asyncio
    async def test_error_status_yields_error_frame(self):
        def handler(request):
            return httpx.Response(404, json={"detail": "Voice 'x' not found"})

        frames = await collect(make_service(handler))
        error_frames = [f for f in frames if type(f).__name__ == "ErrorFrame"]
        assert len(error_frames) == 1
        assert "404" in error_frames[0].error

    @pytest.mark.asyncio
    async def test_mp3_body_yields_error_frame(self):
        """A server without response_format support returns mp3 - not RIFF."""
        def handler(request):
            return httpx.Response(
                200, content=b"ID3\x04fake-mp3-bytes" * 8, headers={"content-type": "audio/mpeg"}
            )

        frames = await collect(make_service(handler))
        error_frames = [f for f in frames if type(f).__name__ == "ErrorFrame"]
        assert len(error_frames) == 1
        assert "WAV header" in error_frames[0].error

    @pytest.mark.asyncio
    async def test_connection_error_yields_error_frame(self):
        def handler(request):
            raise httpx.ConnectError("refused")

        frames = await collect(make_service(handler))
        error_frames = [f for f in frames if type(f).__name__ == "ErrorFrame"]
        assert len(error_frames) == 1
