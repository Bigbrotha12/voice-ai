"""Wrapper MCP server exposing high-level voice tools over a local Voicebox.

Tools:
- say(text, ...)      Speak through Voicebox (plays on speakers) and wait for
                      the generation to finish before returning.
- listen(seconds)     STT from the microphone (stub until mic capture lands).
- voices()            List available voice profiles.
"""

from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP

from .config import load_settings
from .mic import MicError, record
from .voicebox_client import VoiceboxClient, VoiceboxError

Engine = Literal[
    "qwen",
    "qwen_custom_voice",
    "luxtts",
    "chatterbox",
    "chatterbox_turbo",
    "tada",
    "kokoro",
]

mcp = FastMCP("voice-agent")

_settings = load_settings()
_client = VoiceboxClient(_settings)


def _resolve_profile(profile: str | None) -> str | None:
    return profile or _settings.default_profile


@mcp.tool
async def say(
    text: str,
    profile: str | None = None,
    engine: Engine | None = None,
    language: str | None = None,
    wait: bool = True,
) -> dict[str, Any]:
    """Speak text aloud in one of your Voicebox voices.

    Use this when you want to tell the user something by voice instead of
    printing it. The audio plays on the user's speakers and an on-screen pill
    shows that it is playing.

    Args:
        text: What to say. Plain text; keep it conversational.
        profile: Voice profile name (case-insensitive) or id. Omit to use the
            client's default voice configured in this repo's .env / Voicebox.
        engine: TTS engine override (e.g. kokoro is fastest, qwen has delivery
            control). Omit for the profile default.
        language: ISO language hint such as "en" or "de".
        wait: When true (default), polls until generation completes and
            includes final status in the result. When false, returns the
            generation id immediately.

    Returns:
        {generation_id, status, profile} so you can confirm delivery.
    """
    try:
        response = await _client.speak(
            text=text, profile=_resolve_profile(profile), engine=engine, language=language
        )
    except (VoiceboxError, TimeoutError) as exc:
        raise RuntimeError(str(exc)) from exc

    gen_id = response.get("generation_id") or response.get("id")
    result: dict[str, Any] = {
        "generation_id": str(gen_id) if gen_id else None,
        "status": response.get("status", "submitted"),
        "profile": _resolve_profile(profile),
    }
    if wait and result["generation_id"]:
        try:
            final = await _client.wait_for_generation(
                result["generation_id"],
                timeout_seconds=_settings.say_timeout_seconds,
                interval_seconds=_settings.poll_interval_seconds,
            )
            result["status"] = final.get("status") or final.get("state") or "done"
        except (VoiceboxError, TimeoutError) as exc:
            raise RuntimeError(
                f"Spoke but could not confirm completion ({result['generation_id']}): {exc}"
            ) from exc
    return result


@mcp.tool
async def listen(seconds: float = 5.0) -> str:
    """Record from the microphone and return the transcript.

    Args:
        seconds: How long to record, capped at 60.
    """
    try:
        audio_path = record(min(seconds, 60.0))
        return str(await _client.transcribe(audio_path))
    except (MicError, VoiceboxError) as exc:
        raise RuntimeError(str(exc)) from exc


@mcp.tool
async def voices() -> dict[str, Any]:
    """List available voice profiles (cloned + preset).

    Returns:
        {profiles: [{id, name, ...}], count} - pass a `name` to `say(profile=...)`.
    """
    try:
        profiles = await _client.list_profiles()
    except VoiceboxError as exc:
        raise RuntimeError(str(exc)) from exc
    return {"profiles": profiles, "count": len(profiles)}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
