"""Tests for OpenAIBatchSTTService against canned HTTP responses."""

from __future__ import annotations

import httpx
import pytest

from voicebot.openai_batch_stt import OpenAIBatchSTTService


def make_service(handler) -> OpenAIBatchSTTService:
    return OpenAIBatchSTTService(
        base_url="http://test",
        model="whisper-1",
        http_client=httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(handler)
        ),
    )


async def collect(service: OpenAIBatchSTTService, audio: bytes = b"fake-audio") -> list:
    return [f async for f in service.run_stt(audio)]


class TestRunStt:
    def test_run_stt_matches_pipecat_contract(self):
        """run_stt(audio) - Pipecat calls it with only the audio arg."""
        import inspect

        from pipecat.services.stt_service import STTService

        params = list(inspect.signature(OpenAIBatchSTTService.run_stt).parameters.values())
        base_params = list(inspect.signature(STTService.run_stt).parameters.values())
        assert [p.name for p in params[1:]] == [p.name for p in base_params[1:]]

    @pytest.mark.asyncio
    async def test_posts_multipart_to_transcriptions(self):
        def handler(request):
            assert request.url.path == "/v1/audio/transcriptions"
            content = request.content
            assert b'name="file"' in content
            assert b"name=\"model\"" in content and b"whisper-1" in content
            assert b"audio.wav" in content
            return httpx.Response(200, json={"text": "cluster hello"})

        frames = await collect(make_service(handler))
        transcription_frames = [f for f in frames if type(f).__name__ == "TranscriptionFrame"]
        assert len(transcription_frames) == 1
        assert transcription_frames[0].text == "cluster hello"

    @pytest.mark.asyncio
    async def test_400_no_speech_yields_no_frame(self):
        def handler(request):
            return httpx.Response(400, json={"detail": "No speech detected"})

        frames = await collect(make_service(handler))
        assert not [f for f in frames if type(f).__name__ == "TranscriptionFrame"]
        assert not [f for f in frames if type(f).__name__ == "ErrorFrame"]

    @pytest.mark.asyncio
    async def test_empty_text_yields_no_frame(self):
        def handler(request):
            return httpx.Response(200, json={"text": ""})

        frames = await collect(make_service(handler))
        transcription_frames = [f for f in frames if type(f).__name__ == "TranscriptionFrame"]
        assert len(transcription_frames) == 0

    @pytest.mark.asyncio
    async def test_500_yields_error_frame(self):
        def handler(request):
            return httpx.Response(500, json={"detail": "transcription failed: boom"})

        frames = await collect(make_service(handler))
        error_frames = [f for f in frames if type(f).__name__ == "ErrorFrame"]
        assert len(error_frames) == 1
        assert "500" in error_frames[0].error

    @pytest.mark.asyncio
    async def test_connection_error_yields_error_frame(self):
        def handler(request):
            raise httpx.ConnectError("refused")

        frames = await collect(make_service(handler))
        error_frames = [f for f in frames if type(f).__name__ == "ErrorFrame"]
        assert len(error_frames) == 1
