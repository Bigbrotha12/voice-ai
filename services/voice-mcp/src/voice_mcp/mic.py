"""Placeholder for microphone capture (Phase 3 follow-up).

Planned approach: shell out to ffmpeg/arecord, write to a temp wav, hand the
path to VoiceboxClient.transcribe(), then delete the temp file.

Design constraints to settle before implementing:
- Linux audio stack ambiguity: PipeWire vs PulseAudio vs raw ALSA default
  device. Needs runtime detection and a documented prerequisite in setup.md.
- No device selection yet; "default input" is a footgun on multi-device hosts.
- Fixed-duration recording with no VAD endpointing: either clips the user or
  records dead air. Consider VAD-gated recording later.
- Must handle "no microphone present" and mic permission errors cleanly.
- Temp files must be removed even on failure.
"""

class MicError(RuntimeError):
    pass


def record(seconds: float) -> str:
    raise MicError(
        "Mic capture is not implemented yet; tracked as a Phase 3 follow-up."
    )
