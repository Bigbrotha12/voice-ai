"""Tests for the backchannel trigger/injector processors."""

from __future__ import annotations

import random
import struct
import wave
from pathlib import Path

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
    TTSAudioRawFrame,
)

from voicebot.backchannel import (
    BackchannelFrame,
    BackchannelInjectorProcessor,
    BackchannelTriggerProcessor,
    ClipBag,
    load_bank,
    load_clip_pcm,
)


def make_wav(path: Path, rate: int = 24000, seconds: float = 0.3) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x01\x02" * int(rate * seconds))


@pytest.fixture
def bank(tmp_path: Path) -> Path:
    for i in range(3):
        make_wav(tmp_path / f"clip{i}.wav")
    return tmp_path


class TestClipBag:
    def test_draw_cycles_without_immediate_repeat(self):
        clips = [Path(f"c{i}") for i in range(3)]
        bag = ClipBag(clips, rng=random.Random(42))
        drawn = [bag.draw() for _ in range(6)]
        # 2 full cycles; within each cycle no repeats
        for cycle in (drawn[:3], drawn[3:]):
            assert len(set(cycle)) == 3
        assert set(drawn) == set(clips)

    def test_empty_bag_draws_none(self):
        assert ClipBag([]).draw() is None


class TestLoadClip:
    def test_load_bank_sorted_wavs_only(self, tmp_path: Path):
        make_wav(tmp_path / "b.wav")
        make_wav(tmp_path / "a.wav")
        (tmp_path / "notes.txt").write_text("x")
        assert [p.name for p in load_bank(tmp_path)] == ["a.wav", "b.wav"]

    def test_resamples_to_target_rate(self, tmp_path: Path):
        make_wav(tmp_path / "c.wav", rate=48000, seconds=1.0)
        pcm, rate = load_clip_pcm(tmp_path / "c.wav", target_rate=24000)
        assert rate == 24000
        assert len(pcm) == 24000 * 2  # mono s16

    def test_stereo_downmix(self, tmp_path: Path):
        p = tmp_path / "s.wav"
        with wave.open(str(p), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(struct.pack("<400h", *([100, -100] * 200)))
        pcm, rate = load_clip_pcm(p, 24000)
        samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
        assert rate == 24000
        assert all(s == 0 for s in samples)  # L+R cancels


class TestTrigger:
    """Floor-clock semantics: clock starts on BotStoppedSpeakingFrame."""

    def make_trigger(self, bank_dir, **kwargs) -> BackchannelTriggerProcessor:
        kwargs.setdefault("min_speech_secs", 0.0)
        kwargs.setdefault("dip_hold_ms", 0)
        kwargs.setdefault("cooldown_secs", 0.0)
        return BackchannelTriggerProcessor(
            bank_dir=bank_dir,
            sample_rate=16000,
            **kwargs,
        )

    async def feed_dip(self, trig, loud_chunks=8, quiet_chunks=8, handover=True):
        """Optionally hand over the floor, loud speech, then a quiet dip."""
        fired = []

        async def capture(frame):
            fired.append(frame)

        trig.queue_frame = capture  # type: ignore[method-assign]
        if handover:
            await trig.process_frame(BotStoppedSpeakingFrame(), None)
        loud = b"\x40\x06" * 800  # ~1600 amplitude blocks
        quiet = b"\x01\x00" * 800
        for _ in range(loud_chunks):
            await trig.process_frame(InputAudioRawFrame(loud, 16000, 1), None)
        for _ in range(quiet_chunks):
            await trig.process_frame(InputAudioRawFrame(quiet, 16000, 1), None)
        return fired

    @pytest.mark.asyncio
    async def test_fires_on_dip_after_floor_hold(self, bank):
        trig = self.make_trigger(bank)
        fired = await self.feed_dip(trig)
        assert len(fired) == 1
        assert isinstance(fired[0], BackchannelFrame)

    @pytest.mark.asyncio
    async def test_one_clip_per_dip_episode(self, bank):
        trig = self.make_trigger(bank)
        await self.feed_dip(trig)  # arms off after fire
        # continued silence without energy recovery must not refire
        quiet = b"\x01\x00" * 800
        more = []
        orig = trig.queue_frame

        async def capture(frame):
            more.append(frame)

        trig.queue_frame = capture  # type: ignore[method-assign]
        for _ in range(10):
            await trig.process_frame(InputAudioRawFrame(quiet, 16000, 1), None)
        assert more == []

    @pytest.mark.asyncio
    async def test_no_fire_below_min_speech(self, bank):
        trig = self.make_trigger(bank, min_speech_secs=999)
        fired = await self.feed_dip(trig)
        assert fired == []

    @pytest.mark.asyncio
    async def test_cooldown_blocks_refire_after_recovery(self, bank):
        trig = self.make_trigger(bank, cooldown_secs=60)
        first = await self.feed_dip(trig)
        assert len(first) == 1
        second = await self.feed_dip(trig)  # energy recovered -> armed, but cooldown holds
        assert second == []

    @pytest.mark.asyncio
    async def test_bot_speaking_suppresses(self, bank):
        trig = self.make_trigger(bank)
        await trig.process_frame(BotStartedSpeakingFrame(), None)  # floor never starts
        fired = await self.feed_dip(trig, handover=False)
        assert fired == []

    @pytest.mark.asyncio
    async def test_disabled_with_empty_bank(self, tmp_path):
        trig = self.make_trigger(tmp_path)  # no wavs
        assert not trig.enabled
        fired = await self.feed_dip(trig)
        assert fired == []

    @pytest.mark.asyncio
    async def test_process_frame_dispatch_reaches_ingest(self, bank):
        """Regression: _ingest_audio must be awaited via process_frame, not
        just when called directly (live failure caught by RuntimeWarning)."""
        import warnings

        trig = self.make_trigger(bank)
        fired = []

        async def capture(frame):
            fired.append(frame)

        trig.queue_frame = capture  # type: ignore[method-assign]
        await trig.process_frame(BotStoppedSpeakingFrame(), None)

        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)  # un-awaited coroutines fail loudly
            for _ in range(12):
                await trig.process_frame(InputAudioRawFrame(b"\x40\x06" * 800, 16000, 1), None)
            for _ in range(12):
                await trig.process_frame(InputAudioRawFrame(b"\x01\x00" * 800, 16000, 1), None)

        assert len(fired) == 1


class TestInjector:
    @pytest.mark.asyncio
    async def test_converts_backchannel_to_tts_frame(self, bank):
        inj = BackchannelInjectorProcessor(sample_rate=24000)
        pushed = []

        async def capture(f, d=None):
            pushed.append(f)

        inj.push_frame = capture  # type: ignore[method-assign]
        clip = sorted(bank.glob("*.wav"))[0]
        await inj.process_frame(BackchannelFrame(clip), None)
        audio = [f for f in pushed if isinstance(f, TTSAudioRawFrame)]
        assert len(audio) == 1
        assert audio[0].sample_rate == 24000
        assert len(audio[0].audio) > 1000

    @pytest.mark.asyncio
    async def test_passes_other_frames_through(self, bank):
        inj = BackchannelInjectorProcessor()
        frame = InputAudioRawFrame(b"\x00\x01", 16000, 1)
        pushed = []

        async def capture(f, d=None):
            pushed.append(f)

        inj.push_frame = capture  # type: ignore[method-assign]
        await inj.process_frame(frame, None)
        assert pushed == [frame]
