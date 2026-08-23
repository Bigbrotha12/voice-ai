# AGENTS.md — Voice Agent Stack

Conventions and hard-won context for agent sessions working in this repo.

## What this is

Learning project: open-source voice I/O for AI agents, built on an unmodified
upstream [Voicebox](https://github.com/jamiepine/voicebox) install. We never fork
or patch upstream; everything we own lives in this repo.

## Layer map

| Layer | Where | Notes |
|---|---|---|
| Voicebox runtime | `~/Projects/ai/voicebox-upstream` (sibling dir) | Podman container, pinned tag v0.5.0; owns TTS/STT/REST/MCP |
| Wrapper MCP | `services/voice-mcp/` | FastMCP server exposing `say`, `voices`, `listen` |
| Clients | opencode / Claude Code configs | Registered per docs/setup.md |
| Telephony (future) | `telephony/` | Pipecat + LiveKit SIP + Telnyx trunk; turn-taking plan in `telephony/RESEARCH.md` |

## Facts that will bite you

- **Ports.** The podman runtime maps host `17600 -> 17493`; that is the live
  path and the default in configs/scripts. Native `17493` only applies when
  running a source/desktop build.
- **Headless tradeoffs.** No speaking pill, no dictation, no direct speaker
  playback. Generated audio lands in `voicebox-upstream/output/` (bind mount);
  play host-side with `scripts/play-latest.sh`. Per-client voice bindings are
  desktop-UI-only, so `DEFAULT_PROFILE` in `.env` carries voice selection.
- **No auth, localhost only.** The REST API and MCP endpoint have no bearer token.
  Never expose these ports beyond loopback.
- **Backend lifetime.** The API only answers while the Voicebox desktop app (or
  container) is running. "Connection refused" almost always means it isn't up.
- **API shapes (verified against v0.5.0 source).** `/generate` requires
  `profile_id`; responses use `id` (client accepts `generation_id` as fallback).
  `GET /generate/<id>/status` is SSE, not JSON - consume the stream
  (`VoiceboxClient.watch_status`), never `resp.json()` it. `/transcribe`
  multipart field is `file` and answers HTTP 202 while Whisper downloads.
  Statuses: `generating`, `loading_model`, `completed`, `failed` (+ `not_found`
  pseudo-status on the stream).
- **Engine names** for `engine=` args: `qwen`, `qwen_custom_voice`, `luxtts`,
  `chatterbox`, `chatterbox_turbo`, `tada`, `kokoro`. Keep this as a soft list -
  the wrapper accepts any string so new upstream engines don't get rejected.

## Commands

```sh
# Upstream runtime (from this repo; injects scoped CONTAINERS_REGISTRIES_CONF)
./scripts/upstream-up.sh        # build + start detached (first build: several minutes)
./scripts/upstream-up.sh logs   # watch startup; /health when ready
./scripts/upstream-up.sh down

# Wrapper MCP server
uv sync --directory services/voice-mcp
uv run --env-file .env --directory services/voice-mcp voice-mcp   # stdio MCP

# Verification
./scripts/smoke-test.sh         # REST path: profiles -> speak -> SSE status
./scripts/mcp-inspect.sh        # MCP inspector against the wrapper
./scripts/play-latest.sh        # play newest generated wav host-side
uv run --directory services/voice-mcp pytest -q   # unit tests
```

## Conventions

- Python >= 3.11, managed with uv. Deps only in `services/voice-mcp/pyproject.toml`.
- Secrets/config via `.env` (gitignored); `.env.example` documents every var.
- Branch naming follows global conventions (`feat/`, `fix/`, `chore/`).
- Phase status lives in README.md; keep it current when a phase completes.
