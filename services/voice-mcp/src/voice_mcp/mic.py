"""Microphone capture for the listen() tool.

Records N seconds from the default input device to a temp wav, hands the
path back for VoiceboxClient.transcribe(); the CALLER owns cleanup.

Design notes / constraints:
- Linux audio stack ambiguity handled pragmatically: prefer ffmpeg with the
  pulse backend (works on PipeWire and PulseAudio via compat), fall back to
  arecord (raw ALSA). Device = VOICEBOX_MIC_DEVICE env override, else
  `pactl get-default-source`, else pulse "default".
- Fixed-duration capture, no VAD endpointing yet - callers should pick a
  sane duration. Tracked as a Phase 3 follow-up.
- No microphone present, missing tools, or empty recordings all raise
  MicError with actionable text.
- Temp files are removed on failure; success leaves deletion to the caller.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class MicError(RuntimeError):
    pass


def _resolve_device() -> str | None:
    dev = os.environ.get("VOICEBOX_MIC_DEVICE")
    if dev:
        return dev
    pactl = shutil.which("pactl")
    if pactl is None:
        raise MicError(
            "pactl not found - cannot detect the default input source; "
            "set VOICEBOX_MIC_DEVICE explicitly"
        )
    try:
        result = subprocess.run(
            [pactl, "get-default-source"],
            capture_output=True, text=True, timeout=5,
        )
    except subprocess.SubprocessError as exc:
        raise MicError(f"failed to detect default input source via pactl: {exc}") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise MicError(
            "no default input source reported by pactl - is a microphone connected?"
        )
    return result.stdout.strip()


def record(seconds: float) -> str:
    """Record `seconds` of audio from the mic; returns the temp wav path."""
    fd, path = tempfile.mkstemp(prefix="voice-mcp-listen-", suffix=".wav")
    os.close(fd)
    wav = Path(path)

    ffmpeg = shutil.which("ffmpeg")
    try:
        if ffmpeg:
            device = _resolve_device()
            cmd = [
                ffmpeg, "-loglevel", "error", "-y",
                "-f", "pulse", "-i", device,
                "-t", f"{seconds:.2f}", "-ac", "1", "-ar", "16000",
                path,
            ]
        elif shutil.which("arecord"):
            cmd = [
                "arecord", "-q",
                "-d", str(max(1, math.ceil(seconds))),
                "-r", "16000", "-c", "1", "-f", "S16_LE",
                path,
            ]
        else:
            raise MicError("listen() needs ffmpeg or arecord; neither found on PATH")

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=seconds + 15,
            )
        except subprocess.TimeoutExpired as exc:
            raise MicError(f"recording timed out after {seconds:.0f}s") from exc
        if result.returncode != 0:
            raise MicError(
                f"recording failed ({cmd[0]}): {result.stderr.strip()[-200:]}"
            )
        if wav.stat().st_size <= 44:
            raise MicError("empty recording - check the selected input device")
    except BaseException:
        wav.unlink(missing_ok=True)
        raise

    return str(wav)
