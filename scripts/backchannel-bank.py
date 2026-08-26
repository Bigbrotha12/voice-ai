#!/usr/bin/env python3
"""Pre-render the backchannel clip bank (RESEARCH.md layer 4).

Generates short acknowledgment clips ("mm-hmm", "right", "go on", ...) per
voice profile via Voicebox's /generate/stream.

Output: BANK_DIR/<phrase>__take<N>.wav, mono s16le 24kHz - consumed by
voicebot.backchannel.BackchannelInjectorProcessor.

A .meta.json sentinel is written after successful generation so the bot
can detect stale banks (profile changed) and regenerate automatically.

Usage:
  ./scripts/backchannel-bank.py                          # kokoro, default profile
  ./scripts/backchannel-bank.py --engine kokoro --profile "Bella"
  ./scripts/backchannel-bank.py --engine chatterbox_turbo --profile "Benchmark Clone"
  ./scripts/backchannel-bank.py --profile-id abc123 --force
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import struct
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path

import httpx

BASE_URL = os.environ.get("VOICEBOX_URL", "http://127.0.0.1:17600").rstrip("/")
BANK_DIR = Path(os.environ.get("VOICEBOT_BACKCHANNEL_BANK", "backchannels"))
CLIENT_ID = "backchannel-bank"

# Paralinguistic tags — only supported by chatterbox_turbo.
_TAG_RE = __import__("re").compile(r"\[laugh\]|\[sigh\]", __import__("re").IGNORECASE)

# Base phrases; tags are stripped for engines that don't support them.
PHRASES = [
    "mm-hmm",
    "mm-hmm [sigh]",
    "right.",
    "go on.",
    "I see.",
    "yeah?",
    "uh-huh",
    "interesting. [laugh]",
]
TARGET_RATE = 24000


def normalize_to_s16_mono_24k(wav_bytes: bytes) -> bytes:
    """Decode any pcm wav into canonical mono s16le TARGET_RATE wav bytes."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise RuntimeError(f"unexpected sample width {width * 8}-bit")
    samples = list(struct.unpack(f"<{len(raw) // 2}h", raw))
    if channels > 1:
        samples = [
            sum(samples[i : i + channels]) // channels for i in range(0, len(samples), channels)
        ]
    if rate != TARGET_RATE:
        out_len = int(len(samples) * TARGET_RATE / rate)
        samples = [samples[min(len(samples) - 1, int(i * rate / TARGET_RATE))] for i in range(out_len)]
    peak = max(1, max(abs(s) for s in samples))
    if peak < 2000:  # too quiet to be usable as an acknowledgment
        raise RuntimeError("clip nearly silent")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TARGET_RATE)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return buf.getvalue()


async def _probe(client: httpx.AsyncClient, pid: str, engine: str) -> bool:
    try:
        async with client.stream(
            "POST",
            "/generate/stream",
            json={"text": ".", "profile_id": pid, "engine": engine},
            headers={"X-Voicebox-Client-Id": CLIENT_ID},
        ) as r:
            return r.status_code == 200
    except Exception:
        return False


async def _ensure_model(client: httpx.AsyncClient, pid: str, engine: str) -> None:
    """Trigger model download via /generate (stream refuses while downloading),
    poll until done."""
    gen = await client.post(
        "/generate",
        json={"text": "Testing.", "profile_id": pid, "engine": engine},
        headers={"X-Voicebox-Client-Id": CLIENT_ID},
    )
    gen.raise_for_status()
    gen_id = gen.json()["id"]
    print(f"downloading/loading {engine} model (gen {gen_id})...", flush=True)
    deadline = asyncio.get_event_loop().time() + 1800
    while asyncio.get_event_loop().time() < deadline:
        status = (await client.get(f"/history/{gen_id}")).json().get("status")
        if status == "completed":
            return
        if status == "failed":
            raise SystemExit(f"bootstrap generation failed: {status}")
        await asyncio.sleep(5)
    raise SystemExit("bootstrap timed out after 30 min")


async def pick_profile(client: httpx.AsyncClient, name: str | None, profile_id: str | None, engine: str) -> tuple[str, str]:
    """Return (profile_id, profile_name) for the selected profile.

    *profile_id* takes precedence over *name* when both are given.
    """
    resp = await client.get("/profiles")
    profiles = resp.json()
    if isinstance(profiles, dict):
        profiles = profiles.get("profiles", [])

    if profile_id:
        for p in profiles:
            if p.get("id") == profile_id:
                return p["id"], p.get("name", p["id"])
        raise SystemExit(f"profile id '{profile_id}' not found")

    if name:
        for p in profiles:
            if p.get("name", "").lower() == name.lower():
                return p["id"], p.get("name", name)
        raise SystemExit(f"profile '{name}' not found")

    # Try clone-named profiles first, then everything else.
    ordered = [p for p in profiles if "clone" in p.get("name", "").lower()]
    ordered += [p for p in profiles if p not in ordered]
    for p in ordered:
        if await _probe(client, p["id"], engine):
            return p["id"], p.get("name", p["id"])

    # All probes failed - the model may not be downloaded yet. Bootstrap on
    # the first candidate and retry.
    target = ordered[0] if ordered else None
    if not target:
        raise SystemExit("no voice profiles found - create one first")
    await _ensure_model(client, target["id"], engine)
    if await _probe(client, target["id"], engine):
        return target["id"], target.get("name", target["id"])
    raise SystemExit(f"no profile accepted {engine} after bootstrap")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="kokoro", help="TTS engine (default: kokoro)")
    ap.add_argument("--profile", default=None, help="profile name (default: probe)")
    ap.add_argument("--profile-id", default=None, help="profile id (overrides --profile)")
    ap.add_argument("--takes", type=int, default=2, help="takes per phrase variant")
    ap.add_argument("--force", action="store_true", help="re-render existing clips")
    args = ap.parse_args()

    engine = args.engine.strip().lower()
    has_tags = engine not in ("chatterbox", "chatterbox_turbo")

    # Strip paralinguistic tags for engines that don't support them.
    phrases = [_TAG_RE.sub("", p).strip() for p in PHRASES] if has_tags else list(PHRASES)
    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique_phrases: list[str] = []
    for p in phrases:
        if p not in seen:
            seen.add(p)
            unique_phrases.append(p)
    phrases = unique_phrases

    BANK_DIR.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=httpx.Timeout(300)) as client:
        profile_id, profile_name = await pick_profile(client, args.profile, args.profile_id, engine)
        print(f"profile: {profile_name} ({profile_id}) engine: {engine}")

        rendered = skipped = failed = 0
        for phrase in phrases:
            for take in range(1, args.takes + 1):
                stem = phrase.replace(" ", "-").rstrip("-.")
                out = BANK_DIR / f"{stem}__t{take}.wav"
                if out.exists() and not args.force:
                    skipped += 1
                    continue
                try:
                    async with client.stream(
                        "POST",
                        "/generate/stream",
                        json={"text": phrase, "profile_id": profile_id, "engine": engine},
                        headers={"X-Voicebox-Client-Id": CLIENT_ID},
                    ) as resp:
                        if resp.status_code != 200:
                            body = (await resp.aread()).decode(errors="replace")[:120]
                            print(f"FAIL {out.name}: HTTP {resp.status_code} {body}")
                            failed += 1
                            continue
                        wav_bytes = b""
                        async for chunk in resp.aiter_bytes():
                            wav_bytes += chunk
                    out.write_bytes(normalize_to_s16_mono_24k(wav_bytes))
                    rendered += 1
                    print(f"ok {out.name}")
                except Exception as exc:
                    print(f"FAIL {out.name}: {exc}")
                    failed += 1

        # Write sentinel so the bot can detect stale banks.
        meta = {
            "engine": engine,
            "profile_id": profile_id,
            "profile_name": profile_name,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        (BANK_DIR / ".meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    total = len(list(BANK_DIR.glob("*.wav")))
    print(f"\nbank: {total} clips in {BANK_DIR} ({rendered} rendered, {skipped} kept, {failed} failed)")
    sys.exit(0 if total > 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
