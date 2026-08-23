"""Async tests for the Voicebox client against canned transport responses."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from voice_mcp.config import Settings
from voice_mcp.voicebox_client import ModelDownloading, VoiceboxClient, VoiceboxError


def _settings() -> Settings:
    return Settings(
        base_url="http://test",
        client_id="tests",
        default_profile=None,
        say_timeout_seconds=5.0,
        output_dir=Path("/tmp"),
        player="none",
        warmup_ms=0.0,
    )


def _client(handler) -> VoiceboxClient:
    return VoiceboxClient(_settings(), transport=httpx.MockTransport(handler))


def _sse(*events: str) -> bytes:
    return "".join(f"data: {e}\n\n" for e in events).encode()


class TestWatchStatus:
    @pytest.mark.asyncio
    async def test_completes_on_terminal_event(self):
        def handler(request):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse(
                    '{"id":"g1","status":"generating"}',
                    '{"id":"g1","status":"completed","duration":1.5}',
                ),
            )

        event = await _client(handler).watch_status("g1", 5.0)
        assert event["status"] == "completed"
        assert event["duration"] == 1.5

    @pytest.mark.asyncio
    async def test_handles_data_without_space(self):
        def handler(request):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b'data:{"id":"g2","status":"completed"}\n\n',
            )

        event = await _client(handler).watch_status("g2", 5.0)
        assert event["status"] == "completed"

    @pytest.mark.asyncio
    async def test_failed_raises_with_error_payload(self):
        def handler(request):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse('{"id":"g3","status":"failed","error":"out of memory"}'),
            )

        with pytest.raises(VoiceboxError, match="out of memory"):
            await _client(handler).watch_status("g3", 5.0)

    @pytest.mark.asyncio
    async def test_not_found_raises(self):
        def handler(request):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse('{"id":"g4","status":"not_found"}'),
            )

        with pytest.raises(VoiceboxError, match="not found"):
            await _client(handler).watch_status("g4", 5.0)

    @pytest.mark.asyncio
    async def test_early_close_without_terminal_raises(self):
        def handler(request):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse('{"id":"g5","status":"generating"}'),
            )

        with pytest.raises(VoiceboxError, match="closed before terminal"):
            await _client(handler).watch_status("g5", 5.0)

    @pytest.mark.asyncio
    async def test_empty_stream_raises(self):
        def handler(request):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b"",
            )

        with pytest.raises(VoiceboxError, match="no events"):
            await _client(handler).watch_status("g6", 5.0)

    @pytest.mark.asyncio
    async def test_http_error_status(self):
        def handler(request):
            return httpx.Response(404, json={"detail": "nope"})

        with pytest.raises(VoiceboxError, match="HTTP 404"):
            await _client(handler).watch_status("g7", 5.0)


class TestRequestMapping:
    @pytest.mark.asyncio
    async def test_4xx_raises_voicebox_error(self):
        def handler(request):
            return httpx.Response(422, text="validation error")

        with pytest.raises(VoiceboxError, match="HTTP 422"):
            await _client(handler).list_profiles()

    @pytest.mark.asyncio
    async def test_non_json_2xx_raises_voicebox_error(self):
        def handler(request):
            return httpx.Response(200, text="<html>proxy error</html>")

        with pytest.raises(VoiceboxError, match="non-JSON"):
            await _client(handler).list_profiles()

    @pytest.mark.asyncio
    async def test_transport_error_message_is_neutral(self):
        def handler(request):
            raise httpx.ConnectError("refused")

        with pytest.raises(VoiceboxError, match="Request to Voicebox failed"):
            await _client(handler).list_profiles()


class TestTranscribe:
    @pytest.mark.asyncio
    async def test_202_raises_model_downloading(self, tmp_path):
        wav = tmp_path / "a.wav"
        wav.write_bytes(b"RIFF" + b"\x00" * 100)

        def handler(request):
            return httpx.Response(202, json={"detail": {"downloading": True}})

        with pytest.raises(ModelDownloading, match="downloading"):
            await _client(handler).transcribe(str(wav))

    @pytest.mark.asyncio
    async def test_success_returns_json(self, tmp_path):
        wav = tmp_path / "a.wav"
        wav.write_bytes(b"RIFF" + b"\x00" * 100)

        def handler(request):
            return httpx.Response(200, json={"text": "hello", "duration": 1.0})

        result = await _client(handler).transcribe(str(wav))
        assert result["text"] == "hello"

    @pytest.mark.asyncio
    async def test_500_raises(self, tmp_path):
        wav = tmp_path / "a.wav"
        wav.write_bytes(b"RIFF" + b"\x00" * 100)

        def handler(request):
            return httpx.Response(500, text="server exploded")

        with pytest.raises(VoiceboxError, match="HTTP 500"):
            await _client(handler).transcribe(str(wav))
