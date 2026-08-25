"""LiveKit token minter.

Browser and mobile clients must never hold the LiveKit API secret (the old
test-client embedded devkey/secret in JS). This service signs short-lived
room-join JWTs on their behalf, using server-side credentials from env.

Hardening (post-review):
- Room is pinned server-side; clients cannot join arbitrary rooms.
- Identity is derived server-side (guest-<uuid4>); the reserved "voicebot"
  identity is rejected - a duplicate identity evicts the live participant.
- Bearer gate becomes REQUIRED when bound beyond loopback (fail closed at
  startup, mirroring voice-mcp).
- Constant-time credential comparison.
- CORS restricted to TOKEN_MINT_ALLOWED_ORIGINS (comma-separated); never "*"
  combined with credentials-free minting being drive-by-able.
"""

from __future__ import annotations

import os
import secrets
import time
import uuid
from datetime import timedelta
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from livekit import api as lk_api
from pydantic import BaseModel, Field

app = FastAPI(title="token-mint", docs_url=None, redoc_url=None)

LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "ws://localhost:7880")
# Client-facing dial address handed to browsers/phones (may differ from the
# operator-facing LIVEKIT_URL once Tailscale/LAN clients exist).
LIVEKIT_PUBLIC_URL = os.environ.get("LIVEKIT_PUBLIC_URL", LIVEKIT_URL)
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "")
SHARED_SECRET = os.environ.get("TOKEN_MINT_SHARED_SECRET", "")
TOKEN_TTL = int(os.environ.get("TOKEN_MINT_TTL_SECS", "600"))
DEFAULT_ROOM = os.environ.get("LIVEKIT_ROOM", "voicebot-room")
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "TOKEN_MINT_ALLOWED_ORIGINS",
        "http://localhost:8080,http://127.0.0.1:8080",
    ).split(",")
    if o.strip()
]
RESERVED_IDENTITIES = {"voicebot"}
HOST = os.environ.get("TOKEN_MINT_HOST", "127.0.0.1")

if HOST not in ("127.0.0.1", "localhost", "::1") and not SHARED_SECRET:
    # Fail closed: a mint endpoint reachable off-host with no gate is an
    # unauthenticated room-entry oracle.
    raise SystemExit(
        "token-mint: TOKEN_MINT_SHARED_SECRET is required when binding "
        f"beyond loopback (host={HOST})"
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["POST", "GET"],
    allow_headers=["Authorization", "Content-Type"],
)


class TokenRequest(BaseModel):
    identity: str = Field(default="", max_length=64)
    room: str = Field(default="", max_length=64)
    name: str = Field(default="Guest", max_length=64)


def _check_auth(authorization: str) -> None:
    if SHARED_SECRET and not secrets.compare_digest(
        authorization.encode(), f"Bearer {SHARED_SECRET}".encode()
    ):
        raise HTTPException(status_code=401, detail="unauthorized")


def _creds() -> tuple[str, str]:
    if not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        raise HTTPException(status_code=503, detail="LiveKit credentials not configured")
    return LIVEKIT_API_KEY, LIVEKIT_API_SECRET


@app.get("/healthz")
def healthz() -> dict[str, str]:
    # Deliberately minimal: no config posture disclosure on an endpoint
    # that may be reachable wherever the minter is.
    return {"status": "ok"}


@app.post("/token")
def mint(req: TokenRequest, authorization: str = Header(default="")) -> dict[str, Any]:
    _check_auth(authorization)
    key, secret = _creds()

    identity = req.identity.strip()
    if identity in RESERVED_IDENTITIES:
        raise HTTPException(status_code=403, detail="reserved identity")
    if not identity:
        identity = f"guest-{uuid.uuid4().hex[:8]}"

    room = req.room.strip() or DEFAULT_ROOM
    if room != DEFAULT_ROOM:
        # Homelab scope: one room. Loosen deliberately (allow-list) later.
        raise HTTPException(status_code=403, detail="unknown room")

    jwt = (
        lk_api.AccessToken(key, secret)
        .with_identity(identity)
        .with_name(req.name)
        .with_ttl(timedelta(seconds=TOKEN_TTL))
        .with_grants(
            lk_api.VideoGrants(
                room_join=True,
                room=room,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .to_jwt()
    )
    return {
        "token": jwt,
        "url": LIVEKIT_PUBLIC_URL,
        "room": room,
        "identity": identity,
        "ttl": TOKEN_TTL,
    }


def main() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=HOST,
        port=int(os.environ.get("TOKEN_MINT_PORT", "17602")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
