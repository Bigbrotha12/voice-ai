"""Tests for host-side playback player resolution."""

from voice_mcp.playback import PLAYER_COMMANDS, resolve_player


def test_auto_prefers_ffplay():
    order = []
    assert resolve_player("auto", lambda n: (order.append(n), True)[1]) == "ffplay"
    assert order[0] == "ffplay"


def test_auto_falls_through_to_paplay():
    def has(name):
        return name == "paplay"

    assert resolve_player("auto", has) == "paplay"


def test_auto_none_available():
    assert resolve_player("auto", lambda n: False) is None


def test_disabled():
    assert resolve_player("none", lambda n: True) is None


def test_explicit_missing_binary():
    assert resolve_player("mpv", lambda n: False) is None


def test_all_known_players_have_commands():
    assert set(PLAYER_COMMANDS) >= {"ffplay", "paplay", "mpv"}
