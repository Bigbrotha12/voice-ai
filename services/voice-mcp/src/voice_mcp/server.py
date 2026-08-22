"""Wrapper MCP server exposing high-level voice tools over a local Voicebox.

Tools:
- say(text, ...)      Speak through Voicebox (plays on speakers) and wait for
                      the generation to finish before returning.
- listen(seconds)     STT from the microphone (not implemented yet; will fail
                      until mic capture lands).
- voices()            List available voice profiles.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from .config import load_settings
from .mic import MicError, record
from .voicebox_client import (
    VoiceboxClient,
    VoiceboxError,
    extract_generation_id,
)

KNOWN_ENGINES = (
    "qwen, qwen_custom_voice, luxtts, chatterbox, chatterbox_turbo, tada, kokoro"
)

mcp = FastMCP("voice-agent")

_settings = None
_client: VoiceboxClient | None = None


def _get_client() -> VoiceboxClient:
    global _settings, _client
    if _client is None:
        _settings = load_settings()
        _client = VoiceboxClient(_settings)
    return _client


@mcp.tool
async def say(
    text: str,
    profile: str | None = None,
    engine: str | None = None,
    language: str | None = None,
    wait: bool = True,
) -> dict[str, Any]:
    """Speak text aloud in one of your Voicebox voices.

    Use this when you want to tell the user something by voice instead of
    printing it. The audio plays on the user's speakers and an on-screen pill
    shows that it is playing.

    Args:
        text: What to say. Plain text; keep it conversational. Note: long
            texts go through chunking and can exceed the wait timeout.
        profile: Voice profile name (case-insensitive) or id. Omit to use the
            client's default voice configured in this repo's .env / Voicebox.
        engine: TTS engine override. Known: kokoro (fastest), qwen (delivery
            control), qwen_custom_voice, luxtts, chatterbox, chatterbox_turbo,
            tada. Omit for the profile default.
        language: ISO language hint such as "en" or "de".
        wait: When true (default), follows the server-sent status stream until
            generation completes and includes final status in the result.
            When false, returns the generation id immediately.

    Returns:
        {generation_id, status} so you can confirm delivery.
    """
    client = _get_client()
    try:
        response = await client.speak(
            text=text, profile=profile or _settings.default_profile,
            engine=engine, language=language,
        )
    except VoiceboxError as exc:
        raise RuntimeError(str(exc)) from exc

    gen_id = extract_generation_id(response)
    result: dict[str, Any] = {
        "generation_id": gen_id,
        "status": response.get("status", "submitted"),
    }
    if wait:
        try:
            final = await client.watch_status(gen_id, _settings.say_timeout_seconds)
            result["status"] = final.get("status") or "done"
            if final.get("duration"):
                result["duration"] = final["duration"]
        except (VoiceboxError, TimeoutError) as exc:
            raise RuntimeError(
                f"Spoke but could not confirm completion ({gen_id}): {exc}"
            ) from exc
    return result


@mcp.tool
async def listen(seconds: float = 5.0) -> str:
    """Record from the microphone and return the transcript.

    NOT IMPLEMENTED yet - currently always fails. Will record from the default
    input device and return the transcribed text.

    Args:
        seconds: How long to record, capped at 60.
    """
    client = _get_client()
    try:
        audio_path = record(min(seconds, 60.0))
        payload = await client.transcribe(audio_path)
        return str(payload.get("text", ""))
    except (MicError, VoiceboxError) as exc:
        raise RuntimeError(str(exc)) from exc


@mcp.tool
async def voices() -> dict[str, Any]:
    """List available voice profiles (cloned + preset).

    Returns:
        {profiles: [{id, name, ...}], count} - pass a `name` to `say(profile=...)`.
    """
    try:
        profiles = await _get_client().list_profiles()
    except VoiceboxError as exc:
        raise RuntimeError(str(exc)) from exc
    return {"profiles": profiles, "count": len(profiles)}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
