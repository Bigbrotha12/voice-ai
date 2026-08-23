"""Tests for VoiceboxSTTService against canned HTTP responses."""

from __future__ import annotations

import httpx
import pytest

from voicebot.stt import VoiceboxSTTService


def make_service(handler) -> VoiceboxSTTService:
    return VoiceboxSTTService(
        base_url="http://test",
        model="turbo",
        http_client=httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(handler)
        ),
    )


async def collect(service: VoiceboxSTTService, audio: bytes = b"fake-audio") -> list:
    return [f async for f in service.run_stt(audio, context_id="ctx-1")]


class TestRunStt:
    @pytest.mark.asyncio
    async def test_yields_transcription_frame(self):
        def handler(request):
            assert request.headers["X-Voicebox-Client-Id"] == "pipecat"
            assert request.url.path == "/transcribe"
            assert "file" in request.content.decode(errors="ignore") or b"file" in request.content
            return httpx.Response(
                200,
                json={"text": "hello world", "duration": 1.5, "language": "en"},
            )

        frames = await collect(make_service(handler))
        transcription_frames = [f for f in frames if type(f).__name__ == "TranscriptionFrame"]
        assert len(transcription_frames) == 1
        assert transcription_frames[0].text == "hello world"

    @pytest.mark.asyncio
    async def test_empty_text_yields_no_frame(self):
        def handler(request):
            return httpx.Response(200, json={"text": "", "duration": 1.0})

        frames = await collect(make_service(handler))
        transcription_frames = [f for f in frames if type(f).__name__ == "TranscriptionFrame"]
        assert len(transcription_frames) == 0

    @pytest.mark.asyncio
    async def test_202_yields_error_frame(self):
        def handler(request):
            return httpx.Response(202, json={"detail": "model downloading"})

        frames = await collect(make_service(handler))
        error_frames = [f for f in frames if type(f).__name__ == "ErrorFrame"]
        assert len(error_frames) == 1
        assert "downloading" in error_frames[0].error.lower()

    @pytest.mark.asyncio
    async def test_http_error_yields_error_frame(self):
        def handler(request):
            return httpx.Response(500, text="internal error")

        frames = await collect(make_service(handler))
        error_frames = [f for f in frames if type(f).__name__ == "ErrorFrame"]
        assert len(error_frames) == 1
        assert "500" in error_frames[0].error

    @pytest.mark.asyncio
    async def test_transport_error_yields_error_frame(self):
        def handler(request):
            raise httpx.ConnectError("connection refused")

        frames = await collect(make_service(handler))
        error_frames = [f for f in frames if type(f).__name__ == "ErrorFrame"]
        assert len(error_frames) == 1
