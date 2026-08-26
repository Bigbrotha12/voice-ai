"""Tests for the STT provider factory in voicebot.agent."""

from __future__ import annotations

import pytest
from pipecat.services.whisper.stt import WhisperSTTService as RealWhisperSTTService

from voicebot import agent
from voicebot.stt import VoiceboxSTTService


@pytest.fixture
def no_cuda_preload(monkeypatch):
    """Keep the factory from touching nvidia wheels during tests."""
    calls = []
    monkeypatch.setattr(agent, "_preload_cuda_libs", lambda: calls.append(1))
    return calls


class TestBuildStt:
    def test_local_provider_builds_faster_whisper(self, monkeypatch, no_cuda_preload):
        created = []

        class FakeWhisper:
            Settings = RealWhisperSTTService.Settings

            def __init__(self, **kwargs):
                created.append(self)
                self.kwargs = kwargs

        monkeypatch.setattr(agent, "STT_PROVIDER", "local")
        monkeypatch.setattr(agent, "WHISPER_MODEL", "small")
        monkeypatch.setattr(agent, "WHISPER_DEVICE", "cpu")
        monkeypatch.setattr(agent, "WHISPER_COMPUTE_TYPE", "int8")
        monkeypatch.setattr(agent, "WhisperSTTService", FakeWhisper)

        service = agent.build_stt()

        assert service is created[0]
        kwargs = service.kwargs
        assert kwargs["device"] == "cpu"
        assert kwargs["compute_type"] == "int8"
        # language=None keeps auto-detection parity with the voicebox path.
        assert kwargs["settings"].language is None
        assert kwargs["settings"].model == "small"
        assert len(no_cuda_preload) == 1

    def test_voicebox_provider_builds_voicebox_service(self, monkeypatch, no_cuda_preload):
        monkeypatch.setattr(agent, "STT_PROVIDER", "voicebox")
        monkeypatch.setattr(agent, "VOICEBOX_URL", "http://test:17600")
        monkeypatch.setattr(agent, "VOICEBOX_STT_MODEL", "base")

        service = agent.build_stt()

        assert isinstance(service, VoiceboxSTTService)
        assert service._model == "base"
        assert no_cuda_preload == []

    def test_unknown_provider_raises(self, monkeypatch, no_cuda_preload):
        monkeypatch.setattr(agent, "STT_PROVIDER", "deepgram")

        with pytest.raises(ValueError, match="VOICEBOT_STT_PROVIDER"):
            agent.build_stt()


class TestMcpHostGuard:
    def test_resolvable_localhost(self):
        assert agent._resolvable("http://localhost:17601/mcp") is True
        assert agent._resolvable("http://127.0.0.1:17601/mcp") is True

    def test_unresolvable_host(self):
        # .invalid is guaranteed NXDOMAIN (RFC 2606).
        assert agent._resolvable("http://no-such-host.invalid:17601/mcp") is False

    def test_missing_host(self):
        assert agent._resolvable("not-a-url") is False

    @pytest.mark.asyncio
    async def test_setup_tools_skips_unresolvable_without_network(self, monkeypatch):
        class FakeLLM:
            def register_function(self, *args, **kwargs):
                pass

        monkeypatch.setattr(agent, "MCP_URLS", ["http://no-such-host.invalid:17601/mcp"])
        monkeypatch.setattr(agent, "MCP_AUTH_TOKEN", "")

        schemas, clients = await agent.setup_tools(FakeLLM())

        assert clients == []
        assert len(schemas) == 1  # the native get_current_time schema only
