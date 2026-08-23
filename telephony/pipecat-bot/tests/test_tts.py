"""Tests for VoiceboxTTSService against canned HTTP + real WAV bytes."""

from __future__ import annotations

import io
import wave

import httpx
import pytest

from voicebot.tts import VoiceboxTTSService, parse_wav_header


def make_wav(pcm: bytes, sample_rate: int = 24000, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def make_service(handler) -> VoiceboxTTSService:
    return VoiceboxTTSService(
        base_url="http://test",
        profile_id="p1",
        engine="kokoro",
        http_client=httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(handler)
        ),
        sample_rate=24000,
    )


async def collect(service: VoiceboxTTSService, text="hi") -> list:
    return [f async for f in service.run_tts(text, context_id="ctx-1")]


class TestParseWavHeader:
    def test_standard_44_byte_header(self):
        wav = make_wav(b"\x01\x02")
        offset, rate, ch = parse_wav_header(wav)
        assert offset == 44
        assert rate == 24000
        assert ch == 1

    def test_extra_chunks_before_data(self):
        wav = make_wav(b"\x01") + b"JUNKJUNK"
        offset, rate, _ = parse_wav_header(make_wav(b"\x01"))
        assert offset == 44 and rate == 24000
        assert b"JUNK" not in wav[:offset]

    def test_truncated_raises(self):
        with pytest.raises(ValueError):
            parse_wav_header(b"RIFF")

    def test_not_riff_raises(self):
        with pytest.raises(ValueError):
            parse_wav_header(b"OOGA" + b"\x00" * 60)


class TestRunTts:
    @pytest.mark.asyncio
    async def test_yields_pcm_stripped_of_header(self):
        pcm = bytes(range(256)) * 4
        wav = make_wav(pcm)

        def handler(request):
            assert request.headers["X-Voicebox-Client-Id"] == "pipecat"
            import json

            body = json.loads(request.content)
            assert body == {
                "text": "hi",
                "profile_id": "p1",
                "engine": "kokoro",
            }
            return httpx.Response(
                200,
                headers={"content-type": "audio/wav"},
                content=wav,
            )

        frames = [f for f in await collect(make_service(handler)) if type(f).__name__ == "TTSAudioRawFrame"]
        audio = b"".join(f.audio for f in frames)
        assert audio == pcm
        rates = {f.sample_rate for f in frames}
        assert rates == {24000}

    @pytest.mark.asyncio
    async def test_http_error_yields_error_frame(self):
        def handler(request):
            return httpx.Response(500, text="boom")

        frames = await collect(make_service(handler))
        assert any(type(f).__name__ == "ErrorFrame" for f in frames)

    @pytest.mark.asyncio
    async def test_transport_error_yields_error_frame(self):
        def handler(request):
            raise httpx.ConnectError("refused")

        frames = await collect(make_service(handler))
        assert any(type(f).__name__ == "ErrorFrame" for f in frames)

    @pytest.mark.asyncio
    async def test_headerless_response_yields_error_frame(self):
        def handler(request):
            return httpx.Response(200, content=b"\x00" * 100000)

        frames = await collect(make_service(handler))
        assert any(type(f).__name__ == "ErrorFrame" for f in frames)
