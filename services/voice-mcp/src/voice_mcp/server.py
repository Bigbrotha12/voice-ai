"""Wrapper MCP server exposing high-level voice tools over a local Voicebox.

Tools:
- say(text, ...)      Generate speech via Voicebox, wait for completion (SSE),
                      then play the wav host-side.
- listen(seconds)     Record from the microphone, transcribe via Voicebox
                      Whisper, return the text.
- voices()            List available voice profiles.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from .config import load_settings
from .mic import MicError, record
from .playback import play_file
from .voicebox_client import (
    VoiceboxClient,
    VoiceboxError,
    classify,
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
    """Speak text aloud in one of your Voicebox voices, then play the audio on this machine.

    Use this when you want to tell the user something by voice instead of
    printing it. Generation happens server-side; playback happens host-side
    (the headless container has no speakers). Blocks until audio finishes.

    Args:
        text: What to say. Plain text; keep it conversational. Note: long
            texts go through chunking and can exceed the wait timeout.
        profile: Voice profile name (case-insensitive) or id. Omit to use the
            client's default voice configured in this repo's .env / Voicebox.
        engine: TTS engine override. Known: kokoro (fastest), qwen (delivery
            control), qwen_custom_voice, luxtts, chatterbox, chatterbox_turbo,
            tada. Omit for the profile default.
        language: ISO language hint such as "en" or "de".
        wait: When true (default), waits for generation to finish and plays
            the result aloud. When false, returns the generation id without
            waiting or playing.

    Returns:
        {generation_id, status, audio_path?, played?, play_detail?}.
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
    if not wait:
        return result

    try:
        final = await client.watch_status(gen_id, _settings.say_timeout_seconds)
        result["status"] = final.get("status") or "done"
        if final.get("duration"):
            result["duration"] = final["duration"]
    except (VoiceboxError, TimeoutError) as exc:
        raise RuntimeError(
            f"Spoke but could not confirm completion ({gen_id}): {exc}"
        ) from exc

    if classify(str(result["status"])) == "done":
        wav_path = _settings.output_dir / f"{gen_id}.wav"
        played, detail = await asyncio.to_thread(play_file, _settings, str(wav_path))
        result["audio_path"] = str(wav_path)
        result["played"] = played
        result["play_detail"] = detail
    return result


@mcp.tool
async def listen(seconds: float = 5.0) -> str:
    """Record from this machine's microphone and return the transcript.

    Records for `seconds`, then transcribes via Voicebox (Whisper).
    Note: the very first call may fail while the Whisper model downloads
    (~1.5 GB); wait a minute and retry.

    Args:
        seconds: How long to record, capped at 60.
    """
    client = _get_client()
    try:
        audio_path = await asyncio.to_thread(record, min(max(seconds, 1.0), 60.0))
    except MicError as exc:
        raise RuntimeError(str(exc)) from exc
    try:
        payload = await client.transcribe(audio_path)
    except VoiceboxError as exc:
        raise RuntimeError(str(exc)) from exc
    finally:
        Path(audio_path).unlink(missing_ok=True)
    return str(payload.get("text", ""))


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
    transport = os.environ.get("VOICE_MCP_TRANSPORT", "stdio").lower()
    if transport == "stdio":
        mcp.run()
        return
    # HTTP (streamable) for remote clients - e.g. the pipecat bot's
    # MCPClient. Loopback by default; the endpoint has no auth.
    mcp.run(
        transport=transport,
        host=os.environ.get("VOICE_MCP_HOST", "127.0.0.1"),
        port=int(os.environ.get("VOICE_MCP_PORT", "17601")),
    )


if __name__ == "__main__":
    main()
