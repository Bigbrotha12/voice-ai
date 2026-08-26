#!/usr/bin/env python3
"""Join the LiveKit room and publish speech-with-pauses as fake mic audio.

Automated end-to-end exercise for the voicebot pipeline (backchannel trigger
+ latency observer) without a human speaker. Builds a monologue from a
Voicebox-generated wav: sentence chunks separated by short dips (clause
boundaries, ~600ms) and one long stretch first so the trigger's >5s
continuous-speech requirement is met.

Run from telephony/pipecat-bot:  uv run ../../scripts/room-talk.py
Requires: stack up + bot in room. Watch `podman logs voicebot` for
LATENCY / backchannel lines.
"""

from __future__ import annotations

import asyncio
import os
import sys
import wave
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "telephony" / "pipecat-bot" / "src"))

# Credentials live in the repo .env (configured-keys LiveKit mode); only
# fall back to process env so CI/shell overrides still win.
if not os.environ.get("LIVEKIT_API_KEY") and (_REPO / ".env").exists():
    for line in (_REPO / ".env").read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

os.environ.setdefault("LIVEKIT_URL", "ws://localhost:7880")

import numpy as np  # noqa: E402
from livekit import api, rtc  # noqa: E402

RATE = 16000


def token(identity: str = "room-talk") -> str:
    return (
        api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
        .with_identity(identity)
        .with_grants(api.VideoGrants(room_join=True, room=os.environ.get("LIVEKIT_ROOM", "voicebot-room")))
        .to_jwt()
    )


def load_wav_16k_mono(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        rate, n = w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    pcm = np.frombuffer(raw, dtype=np.int16)
    if w.getnchannels() > 1:
        pcm = pcm[:: w.getnchannels()]
    if rate != RATE:
        idx = (np.arange(int(len(pcm) * RATE / rate)) * rate / RATE).astype(int)
        pcm = pcm[np.clip(idx, 0, len(pcm) - 1)]
    return pcm.astype(np.int16)


def build_monologue(wav_dir: Path) -> np.ndarray:
    """Long first stretch (>5s), then clause-boundary dips between chunks."""
    silence_ms = lambda ms: np.zeros(int(RATE * ms / 1000), dtype=np.int16)  # noqa: E731
    wavs = sorted(wav_dir.glob("*.wav")) or []
    if not wavs:
        raise SystemExit(f"no wavs under {wav_dir} - run ./scripts/warmup.sh first")
    chunks = [load_wav_16k_mono(p) for p in wavs[:4]]
    # pad chunk 1+2 into a single >5s stretch by repeating if needed
    long_stretch = np.concatenate([chunks[0], silence_ms(150), chunks[1 % len(chunks)]])
    while long_stretch.shape[0] < RATE * 6:
        long_stretch = np.concatenate([long_stretch, chunks[2 % len(chunks)], silence_ms(120)])
    parts = [long_stretch]
    for c in chunks[1:]:
        parts += [silence_ms(320), c]  # clause-boundary dip (must stay under VAD stop_secs)
    parts.append(silence_ms(1200))  # final pause -> turn release
    return np.concatenate(parts)


async def main() -> None:
    wav_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "Projects/ai/voicebox-upstream/output"
    mono = build_monologue(wav_dir)
    print(f"streaming {mono.shape[0] / RATE:.1f}s of audio @ {RATE}Hz")

    room = rtc.Room()
    await room.connect(os.environ["LIVEKIT_URL"], token())
    print("connected:", room.name)

    source = rtc.AudioSource(RATE, 1)
    track = rtc.LocalAudioTrack.create_audio_track("fake-mic", source)
    opts = rtc.TrackPublishOptions()
    opts.source = rtc.TrackSource.SOURCE_MICROPHONE
    await room.local_participant.publish_track(track, opts)

    frame_bytes = mono.tobytes()
    samples_total = mono.shape[0]
    chunk = int(RATE * 0.02)  # 20ms frames
    off = 0
    while off < samples_total:
        piece = frame_bytes[off * 2 : min(off + chunk, samples_total) * 2]
        f = rtc.AudioFrame(
            data=piece,
            sample_rate=RATE,
            num_channels=1,
            samples_per_channel=len(piece) // 2,
        )
        await source.capture_frame(f)
        off += chunk
        await asyncio.sleep(0.02)

    await asyncio.sleep(8)  # let the bot finish its reply
    await room.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
