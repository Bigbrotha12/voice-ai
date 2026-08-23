"""Unit tests for the pure helpers in voicebox_client.

Fixture shapes verified against upstream v0.5.0 backend/routes source.
"""

import pytest

from voice_mcp.voicebox_client import (
    ModelDownloading,
    VoiceboxError,
    classify,
    extract_generation_id,
)


class TestExtractGenerationId:
    def test_id_primary(self):
        assert extract_generation_id({"id": "abc"}) == "abc"

    def test_generation_id_fallback(self):
        assert extract_generation_id({"generation_id": "xyz"}) == "xyz"

    def test_prefers_id_over_generation_id(self):
        assert extract_generation_id({"id": "a", "generation_id": "b"}) == "a"

    def test_missing_raises(self):
        with pytest.raises(Exception, match="No generation id"):
            extract_generation_id({"status": "ok"})


class TestClassify:
    @pytest.mark.parametrize("status", ["completed", "success", "done", "Completed"])
    def test_done_family(self, status):
        assert classify(status) == "done"

    @pytest.mark.parametrize("status", ["failed", "error", "cancelled", "canceled"])
    def test_failed_family(self, status):
        assert classify(status) == "failed"

    def test_not_found_is_terminal_failure_class(self):
        assert classify("not_found") == "not_found"

    @pytest.mark.parametrize("status", ["generating", "loading_model", "queued", ""])
    def test_pending_family(self, status):
        assert classify(status) == "pending"


def test_model_downloading_is_voicebox_error():
    assert issubclass(ModelDownloading, VoiceboxError)
