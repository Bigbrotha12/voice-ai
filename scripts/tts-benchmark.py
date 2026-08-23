#!/usr/bin/env python
"""Measure Voicebox synthesis latency per engine (Phase 4 planning data).

Hits POST /generate/stream - the same endpoint a telephony TTS adapter would
use - and reports TTFB and total time. First call per engine pays model
load/download; a warmup request absorbs that before timings are taken.

Usage:
  uv run --env-file ../../.env --directory services/voice-mcp \
      python ../../scripts/tts-benchmark.py [--engines kokoro,luxtts] [--samples 3]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time

import httpx

BASE_URL = os.environ.get("VOICEBOX_URL", "http://127.0.0.1:17600").rstrip("/")
CLIENT_ID = "tts-benchmark"

TEXTS = {
    "short": "Sure, one moment while I check that for you.",
    "medium": (
        "Thanks for calling. I looked into your account and everything appears "
        "to be in order. Your latest invoice was paid on Friday, and there is "
        "nothing else pending on your side right now."
    ),
}

DEFAULT_ENGINES = ["kokoro", "luxtts", "chatterbox_turbo", "qwen"]


async def resolve_profile_id(client: httpx.AsyncClient, name_or_none: str | None) -> str | None:
    resp = await client.get("/profiles")
    resp.raise_for_status()
    profiles = resp.json()
    if isinstance(profiles, dict):
        profiles = profiles.get("profiles", [])
    if not profiles:
        print("No voice profiles found - create one first (docs/setup.md step 1)", file=sys.stderr)
        raise SystemExit(1)
    if name_or_none:
        for p in profiles:
            if p.get("name", "").lower() == name_or_none.lower():
                return p["id"]
        raise SystemExit(f"profile '{name_or_none}' not found")
    return profiles[0]["id"]


async def timed_stream(
    client: httpx.AsyncClient, profile_id: str, engine: str, text: str
) -> tuple[float, float]:
    """Returns (ttfb_seconds, total_seconds) for one /generate/stream call."""
    t0 = time.perf_counter()
    ttfb = None
    async with client.stream(
        "POST",
        "/generate/stream",
        json={"text": text, "profile_id": profile_id, "engine": engine},
        headers={"X-Voicebox-Client-Id": CLIENT_ID},
    ) as resp:
        if resp.status_code != 200:
            body = (await resp.aread()).decode(errors="replace")[:200]
            raise RuntimeError(f"{engine}: HTTP {resp.status_code}: {body}")
        ttfb = time.perf_counter() - t0
        async for _ in resp.aiter_bytes():
            pass
    return ttfb, time.perf_counter() - t0


def fmt_row(engine: str, length: str, samples: list[tuple[float, float]]) -> str:
    tt = [t for _, t in samples]
    fb = [f for f, _ in samples if f is not None]
    med = statistics.median(tt)
    p95 = sorted(tt)[max(0, int(len(tt) * 0.95) - 1)]
    return (
        f"| {engine} | {length} | {len(samples)} "
        f"| {min(fb):.2f} | {med:.2f} | {p95:.2f} | {min(tt):.2f} |"
    )


async def ensure_model(
    client: httpx.AsyncClient, profile_id: str, engine: str
) -> None:
    """Trigger a model download via /generate (the stream endpoint refuses to).

    Polls /history/<id> until the bootstrap generation finishes.
    """
    gen = await client.post(
        "/generate",
        json={"text": TEXTS["short"], "profile_id": profile_id, "engine": engine},
        headers={"X-Voicebox-Client-Id": CLIENT_ID},
    )
    gen.raise_for_status()
    gen_id = gen.json()["id"]
    print(f"{engine}: downloading/loading model (gen {gen_id})...", flush=True)
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        hist = await client.get(f"/history/{gen_id}")
        status = hist.json().get("status")
        if status == "completed":
            return
        if status == "failed":
            raise RuntimeError(f"{engine}: bootstrap generation failed: {hist.text[:200]}")
        await asyncio.sleep(3)
    raise RuntimeError(f"{engine}: bootstrap timed out after 30 min")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engines", default=",".join(DEFAULT_ENGINES))
    ap.add_argument("--profile", default=None, help="profile name; defaults to first")
    ap.add_argument("--samples", type=int, default=3)
    args = ap.parse_args()

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=httpx.Timeout(600.0)) as client:
        profile_id = await resolve_profile_id(client, args.profile)
        print(f"profile_id: {profile_id}")

        rows: list[str] = []
        for engine in [e.strip() for e in args.engines.split(",") if e.strip()]:
            try:
                # Warmup: absorb model load outside the measurements.
                await timed_stream(client, profile_id, engine, TEXTS["short"])
            except Exception as exc:
                if "not downloaded yet" not in str(exc):
                    print(f"\n{engine}: SKIPPED ({exc})")
                    continue
                try:
                    await ensure_model(client, profile_id, engine)
                    await timed_stream(client, profile_id, engine, TEXTS["short"])
                except Exception as exc2:
                    print(f"\n{engine}: SKIPPED ({exc2})")
                    continue
            for length, text in TEXTS.items():
                samples = [
                    await timed_stream(client, profile_id, engine, text)
                    for _ in range(args.samples)
                ]
                rows.append(fmt_row(engine, length, samples))

    print("\n| engine | text | n | ttfb_min | total_med | total_p95 | total_min |")
    print("|---|---|---|---|---|---|---|")
    for row in rows:
        print(row)


if __name__ == "__main__":
    asyncio.run(main())
