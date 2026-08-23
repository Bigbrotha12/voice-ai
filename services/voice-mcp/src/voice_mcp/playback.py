"""Host-side playback of generated audio.

The containerized Voicebox cannot reach speakers; the wrapper runs on the
host, so it owns playback.

Two latency gotchas handled here:
- Player preference (auto): paplay spawns and opens the device fastest,
  ffplay/mpv pay SDL/probing startup cost.
- Sink wake-up: idle sinks (especially USB headsets) suspend and eat the
  first ~1s of audio while resuming. Before real playback we push a short
  silence clip through the same player, wait VOICEBOX_WARMUP_MS, then play
  the real thing. Set VOICEBOX_WARMUP_MS=0 to disable.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .config import Settings

PLAYER_COMMANDS: dict[str, list[str]] = {
    "paplay": ["paplay"],
    "ffplay": ["ffplay", "-autoexit", "-nodisp", "-loglevel", "error"],
    "mpv": ["mpv", "--no-video", "--really-quiet"],
}

PLAY_TIMEOUT_SECONDS = 600.0
WARMUP_SILENCE_SECONDS = "0.2"

_warmup_path: Path | None = None


def resolve_player(player_setting: str, has_binary) -> str | None:
    """Pick the player command name; None means 'do not play'.

    has_binary(name) -> bool is injected for testability.
    """
    if player_setting == "none":
        return None
    if player_setting != "auto":
        if player_setting in PLAYER_COMMANDS and has_binary(player_setting):
            return player_setting
        return None
    for name in PLAYER_COMMANDS:
        if has_binary(name):
            return name
    return None


def _run(cmd: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            cmd, timeout=PLAY_TIMEOUT_SECONDS, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
    except subprocess.TimeoutExpired:
        return False, f"playback timed out after {PLAY_TIMEOUT_SECONDS:.0f}s"
    except OSError as exc:
        return False, f"player failed to start: {exc}"
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace")[:200]
        return False, f"{cmd[0]} exited {result.returncode}: {detail}"
    return True, ""


def _warmup_clip() -> Path | None:
    """A short silence wav, generated once per process, to wake the sink."""
    global _warmup_path
    if _warmup_path is not None and _warmup_path.exists():
        return _warmup_path
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return None
    path = Path(tempfile.gettempdir()) / f"voice-mcp-warmup-{WARMUP_SILENCE_SECONDS}s.wav"
    ok, _ = _run([
        ffmpeg, "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono",
        "-t", WARMUP_SILENCE_SECONDS, str(path),
    ])
    _warmup_path = path if ok else None
    return _warmup_path


def play_file(settings: Settings, wav_path) -> tuple[bool, str]:
    """Play a wav host-side, blocking until finished. Returns (played, detail)."""
    path = str(wav_path)
    if not Path(path).exists():
        return False, f"audio file not found: {path}"

    player = resolve_player(settings.player, lambda n: shutil.which(n) is not None)
    if player is None:
        return False, "no audio player available (tried paplay/ffplay/mpv or VOICEBOX_PLAYER setting)"

    cmd = [*PLAYER_COMMANDS[player], path]

    # Warm the sink so its wake-up latency doesn't clip the real audio's head.
    if settings.warmup_ms > 0:
        clip = _warmup_clip()
        if clip is not None:
            _run([*PLAYER_COMMANDS[player], str(clip)])
            time.sleep(settings.warmup_ms / 1000.0)

    played, err = _run(cmd)
    detail = f"played via {player}" if played else err
    return played, detail
