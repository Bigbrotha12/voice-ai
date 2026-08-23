"""Environment-driven configuration.

Loaded lazily via `load_settings()` so importing the package never requires
env vars to be present (useful for tooling and tests).
"""

import os
from dataclasses import dataclass

DEFAULT_BASE_URL = "http://127.0.0.1:17600"


@dataclass(frozen=True)
class Settings:
    base_url: str
    client_id: str
    default_profile: str | None
    say_timeout_seconds: float


def _float_env(name: str, default: str) -> float:
    raw = os.environ.get(name, default)
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None


def load_settings() -> Settings:
    return Settings(
        base_url=os.environ.get("VOICEBOX_URL", DEFAULT_BASE_URL).rstrip("/"),
        client_id=os.environ.get("VOICEBOX_CLIENT_ID", "voice-mcp"),
        default_profile=os.environ.get("DEFAULT_PROFILE") or None,
        say_timeout_seconds=_float_env("SAY_TIMEOUT_SECONDS", "120"),
    )
