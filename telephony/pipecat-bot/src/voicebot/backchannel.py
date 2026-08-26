"""Backchannel system: pre-rendered acknowledgment clips during user monologues.

Two cooperating frame processors (RESEARCH.md conversation dynamics layer 4):

  BackchannelTriggerProcessor - sits on the input path, watches audio energy
    continuously. Tracks a *floor clock* (time since the bot last spoke);
    when the user has held the floor for `min_speech_secs` and energy shows
    a sustained dip (clause-boundary proxy), emits a BackchannelFrame for a
    random clip from the pre-rendered bank.

  BackchannelInjectorProcessor - sits directly before transport.output,
    converts BackchannelFrame into TTSAudioRawFrame (raw PCM) so the clip
    rides the same output path as synthesized speech. Bypasses LLM+TTS:
    latency is one pipeline hop.

Why not gate on VAD speaking state: Silero declares stop after ~200ms of
silence, so a monologue is a burst of short utterances. VAD segmentation
says "turn ended" exactly where backchannels belong (brief pauses between
clauses). The floor clock + continuous ingestion sidesteps that.

The bank itself is pre-rendered per profile by scripts/backchannel-bank.py
(chatterbox_turbo expressive takes). Clips are WAV, mono s16le, resampled
to the transport output rate on injection.
"""

from __future__ import annotations

import os
import random
import struct
import time
import wave
from collections import deque
from pathlib import Path

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InputAudioRawFrame,
    TTSAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class BackchannelFrame(Frame):
    """Request to play one pre-rendered clip. `path` points at a bank wav."""

    def __init__(self, path: str | Path):
        super().__init__()
        self.path = Path(path)


def load_clip_pcm(path: Path, target_rate: int) -> tuple[bytes, int]:
    """Read a bank wav and return (mono s16 pcm resampled to target_rate, rate).

    Linear resample is fine for sub-second acknowledgment clips.
    """
    with wave.open(str(path), "rb") as w:
        channels = w.getnchannels()
        width = w.getsampwidth()
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())

    if width != 2:
        raise ValueError(f"{path.name}: expected 16-bit pcm, got {width * 8}-bit")
    if channels > 1:
        samples = struct.unpack(f"<{len(raw) // 2}h", raw)
        raw = struct.pack(
            f"<{len(samples) // channels}h",
            *[sum(samples[i : i + channels]) // channels for i in range(0, len(samples), channels)],
        )

    if rate == target_rate:
        return raw, rate
    # Naive decimation/interpolation by integer-ish ratio.
    count = len(raw) // 2
    src = struct.unpack(f"<{count}h", raw)
    out_len = int(count * target_rate / rate)
    idx = [min(count - 1, int(i * rate / target_rate)) for i in range(out_len)]
    return struct.pack(f"<{out_len}h", *(src[i] for i in idx)), target_rate


def load_bank(bank_dir: str | Path) -> list[Path]:
    """Collect playable clips (non-recursive) from the bank directory."""
    bank = Path(os.path.expanduser(str(bank_dir)))
    if not bank.is_dir():
        return []
    return sorted(p for p in bank.iterdir() if p.suffix.lower() == ".wav")


class ClipBag:
    """Shuffled no-immediate-repeat sampler over the bank."""

    def __init__(self, clips: list[Path], rng: random.Random | None = None):
        self._clips = list(clips)
        self._rng = rng or random.Random()
        self._bag: list[Path] = []

    def __len__(self) -> int:
        return len(self._clips)

    def draw(self) -> Path | None:
        if not self._clips:
            return None
        if not self._bag:
            self._bag = self._clips[:]
            self._rng.shuffle(self._bag)
        return self._bag.pop()


class BackchannelTriggerProcessor(FrameProcessor):
    """Fires BackchannelFrame while the user holds the floor through pauses.

    The trigger tracks a *floor clock* - time since the bot last finished
    speaking. When the floor has been the user's for `min_floor_secs`,
    current RMS sits below `dip_ratio` of the recent 2s peak for
    `dip_hold_ms`, and cooldown allows, one clip fires. The arm latch
    re-engages only after energy recovers, giving one clip per dip episode.
    Accumulators reset when the bot starts speaking (turn handed over).
    """

    def __init__(
        self,
        *,
        bank_dir: str | Path,
        sample_rate: int = 16000,
        min_speech_secs: float | None = None,
        dip_ratio: float | None = None,
        dip_hold_ms: int | None = None,
        cooldown_secs: float | None = None,
        noise_floor: float | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        env = os.environ.get

        def cfg(explicit, key: str, default: str) -> float:
            # Explicit 0 must win over env/default - don't use `or` here.
            return float(env(key, default) if explicit is None else explicit)

        self._min_floor_secs = cfg(min_speech_secs, "VOICEBOT_BACKCHANNEL_MIN_SPEECH", "5")
        self._dip_ratio = cfg(dip_ratio, "VOICEBOT_BACKCHANNEL_DIP_RATIO", "0.45")
        self._cooldown_secs = cfg(cooldown_secs, "VOICEBOT_BACKCHANNEL_COOLDOWN", "8")
        self._noise_floor = cfg(noise_floor, "VOICEBOT_BACKCHANNEL_NOISE_FLOOR", "150")
        hold = cfg(dip_hold_ms, "VOICEBOT_BACKCHANNEL_DIP_HOLD_MS", "200")
        self._dip_hold_ms = int(hold)

        self._win_samples = max(1, int(sample_rate * 120 / 1000))
        self._acc_sq = 0
        self._acc_n = 0
        self._recent: deque[tuple[float, int]] = deque()  # (rms, timestamp_ms)

        self._floor_start: float | None = None  # monotonic when bot stopped speaking
        self._last_fire: float | None = None
        self._dip_since: float | None = None
        self._armed = True  # re-arms only after energy recovers post-fire

        clips = load_bank(bank_dir)
        self._bag = ClipBag(clips)
        self._enabled = bool(clips)
        if not self._enabled:
            logger.debug("backchannel: empty bank, trigger disabled")

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def clip_count(self) -> int:
        return len(self._bag)

    def _recent_peak(self) -> float:
        # Pruning happens on ingest; this is a pure read.
        return max((r for r, _ in self._recent), default=0.0)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, BotStartedSpeakingFrame):
            self._floor_start = None
            self._dip_since = None
            self._armed = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._floor_start = time.monotonic()
        elif isinstance(frame, InputAudioRawFrame):
            await self._ingest_audio(frame)

        await self.push_frame(frame, direction)

    async def _ingest_audio(self, frame: InputAudioRawFrame) -> None:
        audio = frame.audio
        count = len(audio) // 2
        if count == 0:
            return
        samples = struct.unpack(f"<{count}h", audio[: count * 2])
        self._acc_sq += sum(s * s for s in samples)
        self._acc_n += count
        if self._acc_n < self._win_samples:
            return
        rms = (self._acc_sq / self._acc_n) ** 0.5
        self._acc_sq = 0
        self._acc_n = 0
        now_ms = time.monotonic_ns() // 1_000_000
        # Prune on ingest (not lazily in _maybe_fire): during bot speech the
        # fire path early-returns and the ring would grow unboundedly.
        cutoff = now_ms - 2000
        while self._recent and self._recent[0][1] < cutoff:
            self._recent.popleft()
        self._recent.append((rms, now_ms))
        await self._maybe_fire(rms)

    async def _maybe_fire(self, rms: float) -> None:
        if not self._enabled or self._floor_start is None:
            return
        floor_secs = time.monotonic() - self._floor_start
        if floor_secs < self._min_floor_secs:
            return
        if self._last_fire and (time.monotonic() - self._last_fire) < self._cooldown_secs:
            return

        peak = self._recent_peak()
        dipping = peak > self._noise_floor and rms < self._dip_ratio * peak
        now = time.monotonic_ns() // 1_000_000
        if dipping:
            if not self._armed:
                return
            if self._dip_since is None:
                self._dip_since = now
            if now - self._dip_since >= self._dip_hold_ms:
                clip = self._bag.draw()
                self._dip_since = None
                self._armed = False  # one clip per dip episode
                if clip:
                    logger.info(
                        f"backchannel: firing '{clip.name}' after {floor_secs:.1f}s of user floor"
                    )
                    self._last_fire = time.monotonic()
                    await self.queue_frame(BackchannelFrame(clip))
        else:
            self._dip_since = None
            if rms >= self._dip_ratio * max(peak, self._noise_floor):
                self._armed = True  # speech energy recovered -> re-arm


class BackchannelInjectorProcessor(FrameProcessor):
    """Converts BackchannelFrame into raw PCM on the output path.

    Drops clips that arrive while the bot is speaking: the LLM reply can
    start between trigger-fire and playback, and interleaving PCM mid-reply
    garbles both. One dropped acknowledgment is cheaper than overlap.
    """

    def __init__(self, *, sample_rate: int = 24000, **kwargs):
        super().__init__(**kwargs)
        self._rate = sample_rate
        self._bot_speaking = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
        elif isinstance(frame, BackchannelFrame):
            if self._bot_speaking:
                logger.debug("backchannel: dropping clip, bot holds the floor")
                await self.push_frame(frame, direction)
                return
            try:
                pcm, rate = load_clip_pcm(frame.path, self._rate)
                await self.push_frame(TTSAudioRawFrame(pcm, rate, 1))
                await self.push_frame(frame, direction)  # keep observer flow intact
                return
            except Exception as exc:
                logger.warning(f"backchannel: failed to load {frame.path}: {exc}")

        await self.push_frame(frame, direction)
