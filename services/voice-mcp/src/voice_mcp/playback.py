"""Host-side playback of generated audio.

The containerized Voicebox cannot reach speakers; the wrapper runs on the
host, so it owns playback. Preference order (auto): ffplay, paplay, mpv.
Set VOICEBOX_PLAYER=none to disable.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .config import Settings

PLAYER_COMMANDS: dict[str, list[str]] = {
    "ffplay": ["ffplay", "-autoexit", "-nodisp", "-loglevel", "error"],
    "paplay": ["paplay"],
    "mpv": ["mpv", "--no-video", "--really-quiet"],
}

PLAY_TIMEOUT_SECONDS = 600.0


def resolve_player(player_setting: str, has_binary) -> str | None:
    """Pick the player command name; None means 'do not play'.

    has_binary(name) -> bool is injected for testability.
    """
    if player_setting == "none":
        return None
    if player_setting != "auto":
        return player_setting if has_binary(player_setting) else None
    for name in PLAYER_COMMANDS:
        if has_binary(name):
            return name
    return None


def play_file(settings: Settings, wav_path) -> tuple[bool, str]:
    """Play a wav host-side, blocking until finished. Returns (played, detail)."""
    path = str(wav_path)
    if not Path(path).exists():
        return False, f"audio file not found: {path}"

    player = resolve_player(settings.player, lambda n: shutil.which(n) is not None)
    if player is None:
        return False, "no audio player available (tried ffplay/paplay/mpv or VOICEBOX_PLAYER setting)"

    cmd = [*PLAYER_COMMANDS[player], path]
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
        return False, f"{player} exited {result.returncode}: {result.stderr.decode(errors='replace')[:200]}"
    return True, f"played via {player}"
