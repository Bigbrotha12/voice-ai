"""Placeholder for microphone capture (Phase 3 follow-up).

Plan: shell out to ffmpeg/arecord to record N seconds from the default input
device into a temp wav, then hand it to VoiceboxClient.transcribe(). Kept in
its own module so the MCP tool surface stays stable while the implementation
lands.
"""

class MicError(RuntimeError):
    pass


def record(seconds: float) -> str:
    raise NotImplementedError(
        "Mic capture is not implemented yet; tracked as a Phase 3 follow-up."
    )
