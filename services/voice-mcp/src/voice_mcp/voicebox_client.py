"""Typed async client for the Voicebox REST API.

Verified against upstream backend/routes on main (2026-08):
- POST /generate            -> GenerationResponse {id, status, ...}; profile_id REQUIRED (404 without it)
- POST /speak               -> GenerationResponse; profile resolves name-or-id, then bindings, then global default
- GET  /generate/<id>/status -> SSE stream (text/event-stream): yields `data: {...}` immediately, then ~1/s
                                until status in (completed|failed), then closes. Pseudo-status: not_found.
- POST /generate/stream     -> WAV bytes, but only after FULL synthesis (TTFB = full batch time)
- POST /transcribe          -> multipart field `file`; HTTP 202 while the Whisper model downloads (~1.5GB first use)
- GET  /profiles            -> list response
- Status values observed upstream: generating, loading_model, completed, failed
"""

from __future__ import annotations

import json
from typing import Any

import httpx

DONE_STATUSES = {"completed", "complete", "success", "succeeded", "done", "finished"}
FAILED_STATUSES = {"failed", "error", "cancelled", "canceled"}


class VoiceboxError(RuntimeError):
    """Raised for transport errors, non-2xx responses, and failed generations."""


class ModelDownloading(VoiceboxError):
    """Raised when /transcribe answers 202 because Whisper is downloading."""


def extract_generation_id(payload: dict[str, Any]) -> str:
    gen_id = payload.get("id") or payload.get("generation_id")
    if not gen_id:
        raise VoiceboxError(f"No generation id in response: {payload}")
    return str(gen_id)


def classify(status_value: str) -> str:
    s = status_value.strip().lower()
    if s == "not_found":
        return "not_found"
    if any(s.startswith(d) for d in DONE_STATUSES):
        return "done"
    if any(s.startswith(f) for f in FAILED_STATUSES):
        return "failed"
    return "pending"


class VoiceboxClient:
    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.base_url,
            headers={"X-Voicebox-Client-Id": settings.client_id},
            # SSE status events fire ~1/s, so a 30s read timeout is safe here.
            timeout=httpx.Timeout(30.0),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            resp = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise VoiceboxError(
                f"Cannot reach Voicebox at {self._settings.base_url} "
                f"(is the app running?): {exc!r}"
            ) from exc
        if resp.status_code == 202:
            raise ModelDownloading(
                f"Whisper model is downloading; try again in a minute: {resp.text[:300]}"
            )
        if resp.status_code >= 400:
            raise VoiceboxError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    async def list_profiles(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/profiles")
        profiles = data.get("profiles") if isinstance(data, dict) else data
        return profiles or []

    async def generate(
        self,
        text: str,
        profile_id: str,
        engine: str | None = None,
        language: str | None = None,
    ) -> dict[str, Any]:
        """profile_id is REQUIRED upstream; prefer speak() unless you have an id."""
        body: dict[str, Any] = {"text": text, "profile_id": profile_id}
        if engine:
            body["engine"] = engine
        if language:
            body["language"] = language
        return await self._request("POST", "/generate", json=body)

    async def speak(
        self,
        text: str,
        profile: str | None = None,
        engine: str | None = None,
        language: str | None = None,
    ) -> dict[str, Any]:
        """Speak-and-play variant; resolves profile name-or-id like MCP speak."""
        body: dict[str, Any] = {"text": text}
        if profile:
            body["profile"] = profile
        if engine:
            body["engine"] = engine
        if language:
            body["language"] = language
        return await self._request("POST", "/speak", json=body)

    async def watch_status(
        self, generation_id: str, timeout_seconds: float
    ) -> dict[str, Any]:
        """Consume the SSE status stream until terminal state; return the last event.

        The stream closes itself after completed/failed. `not_found` is a
        pseudo-status meaning the generation id is unknown -> immediate failure.
        """
        try:
            async with self._http.stream(
                "GET", f"/generate/{generation_id}/status", timeout=timeout_seconds
            ) as resp:
                if resp.status_code >= 400:
                    raise VoiceboxError(
                        f"GET /generate/{generation_id}/status -> HTTP {resp.status_code}"
                    )
                last: dict[str, Any] | None = None
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[len("data:"):].strip())
                    except json.JSONDecodeError:
                        continue
                    last = event
                    state = classify(str(event.get("status", "")))
                    if state == "done":
                        return event
                    if state == "failed":
                        raise VoiceboxError(
                            f"Generation {generation_id} failed: "
                            f"{event.get('error') or event}"
                        )
                    if state == "not_found":
                        raise VoiceboxError(f"Generation {generation_id} not found on server")
                if last is None:
                    raise VoiceboxError(
                        f"Status stream for {generation_id} closed with no events"
                    )
                return last
        except httpx.HTTPError as exc:
            raise VoiceboxError(
                f"Cannot reach Voicebox at {self._settings.base_url}: {exc!r}"
            ) from exc

    async def transcribe(self, audio_path: str, model: str = "turbo") -> dict[str, Any]:
        with open(audio_path, "rb") as fh:
            try:
                resp = await self._http.post(
                    "/transcribe", files={"file": fh}, data={"model": model}
                )
            except httpx.HTTPError as exc:
                raise VoiceboxError(f"Transcribe request failed: {exc!r}") from exc
        if resp.status_code == 202:
            raise ModelDownloading(
                f"Whisper model is downloading; try again in a minute: {resp.text[:300]}"
            )
        if resp.status_code >= 400:
            raise VoiceboxError(f"POST /transcribe -> HTTP {resp.status_code}: {resp.text[:500]}")
        return resp.json()
