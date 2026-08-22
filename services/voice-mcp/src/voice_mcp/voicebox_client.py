"""Typed async client for the Voicebox REST API.

Endpoint assumptions (to be verified against the live server in Phase 1):
- POST /generate            -> {generation_id|id, status, ...}
- GET  /generate/<id>/status -> {status|state, ...}
- POST /speak               -> same shape as /generate, plays on speakers
- POST /transcribe          -> multipart upload -> {text, duration, language, model}
- GET  /profiles            -> {profiles: [...]} or [...]
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

DONE_STATUSES = {"completed", "complete", "success", "succeeded", "done", "finished"}
FAILED_STATUSES = {"failed", "error", "cancelled", "canceled"}


class VoiceboxError(RuntimeError):
    """Raised for transport errors, non-2xx responses, and failed generations."""


def _extract_generation_id(payload: dict[str, Any]) -> str:
    gen_id = payload.get("generation_id") or payload.get("id")
    if not gen_id:
        raise VoiceboxError(f"No generation id in response: {payload}")
    return str(gen_id)


def _classify(status_value: str) -> str:
    s = status_value.strip().lower()
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
        profile_id: str | None = None,
        engine: str | None = None,
        language: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"text": text}
        if profile_id:
            body["profile_id"] = profile_id
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

    async def status(self, generation_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/generate/{generation_id}/status")

    async def transcribe(self, audio_path: str, model: str = "turbo") -> dict[str, Any]:
        with open(audio_path, "rb") as fh:
            try:
                resp = await self._http.post(
                    "/transcribe", files={"audio": fh}, data={"model": model}
                )
            except httpx.HTTPError as exc:
                raise VoiceboxError(f"Transcribe request failed: {exc!r}") from exc
        if resp.status_code >= 400:
            raise VoiceboxError(f"POST /transcribe -> HTTP {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    async def wait_for_generation(
        self, generation_id: str, timeout_seconds: float, interval_seconds: float
    ) -> dict[str, Any]:
        elapsed = 0.0
        while True:
            payload = await self.status(generation_id)
            raw_status = str(payload.get("status") or payload.get("state") or "")
            state = _classify(raw_status)
            if state == "done":
                return payload
            if state == "failed":
                raise VoiceboxError(
                    f"Generation {generation_id} failed: "
                    f"{payload.get('error') or payload}"
                )
            if elapsed >= timeout_seconds:
                raise TimeoutError(
                    f"Generation {generation_id} still '{raw_status}' after {timeout_seconds:.0f}s"
                )
            await asyncio.sleep(interval_seconds)
            elapsed += interval_seconds
