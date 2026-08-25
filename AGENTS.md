# AGENTS.md — Voice Agent Stack

Conventions and hard-won context for agent sessions working in this repo.

## What this is

Learning project: open-source voice I/O for AI agents, built on an unmodified
upstream [Voicebox](https://github.com/jamiepine/voicebox) install. We never fork
or patch upstream; everything we own lives in this repo.

## Layer map

| Layer | Where | Notes |
|---|---|---|
| Voicebox runtime | `~/Projects/ai/voicebox-upstream` (sibling dir) | Podman container, pinned tag v0.5.0; owns TTS/kokoro + REST/MCP |
| LiveKit media plane | `docker/livekit.yml` (pinned v1.13.5) | Sibling container, dev mode, loopback-only; bot joins rooms over WebRTC |
| Pipecat agent | `telephony/pipecat-bot/` | Bundled container (`--host-bot` opts out); faster-whisper STT in-process, llama.cpp LLM via queues proxy; tool calling + MCP bridge (`VOICEBOT_MCP_URLS`) |
| Wrapper MCP | `services/voice-mcp/` | FastMCP server exposing `say`, `voices`, `listen`; stdio for agents, `VOICE_MCP_TRANSPORT=http` for the bot |
| Clients | opencode / Claude Code configs | Registered per docs/setup.md |
| Telephony (future) | `telephony/` | LiveKit SIP + Telnyx trunk; turn-taking plan in `telephony/RESEARCH.md` |

## Facts that will bite you

- **Ports.** The podman runtime maps host `17600 -> 17493`; that is the live
  path and the default in configs/scripts. Native `17493` only applies when
  running a source/desktop build. Voicebox REST/MCP stays loopback-only.
  LiveKit is HOST-networked (dev mode): signaling `7880/tcp`, media
  `7881/tcp + 7882/udp` on all interfaces - bridge networking + loopback
  publishes breaks browser ICE (container-IP candidates + filtered mDNS =
  "could not establish pc connection"). The bundled bot reaches it as
  `ws://host.containers.internal:7880`, Voicebox at `http://voicebox:17493`.
- **GPU.** Stock image ships torch cu130; GPU comes from CDI passthrough in
  `docker/ports-override.yml` (`nvidia.com/gpu=all`). Host runs SELinux
  Enforcing - after driver updates, fresh containers need `label=disable`
  (also in that file) or cuInit returns 100 while old processes keep working.
  Same wiring applies to the voicebot container (`docker/voicebot.yml`);
  its faster-whisper CUDA runtime ships as pip wheels in-image. Model caches
  persist via `HF_HOME` pinned to upstream's huggingface volume (bot has its
  own `voicebot-hf-cache`). Verify:
  `podman exec voicebox python -c "import torch; print(torch.cuda.is_available())"`.
- **Headless tradeoffs.** No speaking pill, no dictation, no direct speaker
  playback. Generated audio lands in `voicebox-upstream/output/` (bind mount);
  play host-side with `scripts/play-latest.sh`. Per-client voice bindings are
  desktop-UI-only, so `DEFAULT_PROFILE` in `.env` carries voice selection.
- **No auth, localhost only.** The REST API and MCP endpoint have no bearer token.
  Never expose these ports beyond loopback.
- **llama.cpp is host-independent** (user's homelab compose under
  `~/Documents/homelab/podman/queues/`, not managed by this repo). The bot's
  LLM endpoint is the queues **proxy** (`:9090`=14b, `:9091`=3b, `:9092`=
  coder) - an OpenAI-compatible HTTP facade over RabbitMQ; its worker owns
  llama container lifecycle (cold start, idle shutdown ~10min, GPU model
  swapping). Consequences: model names follow the GGUF resident at the time,
  and the first token after an idle gap can take ~30s (model reload). The
  bundled bot reaches it as `host.containers.internal:9091/v1`. Reboot
  contract: user linger enabled + `podman-restart.service` enabled -
  containers restore at boot ONLY if they were running at shutdown.
- **Backend lifetime.** The API only answers while the Voicebox desktop app (or
  container) is running. "Connection refused" almost always means it isn't up.
- **API shapes (verified against v0.5.0 source).** `/generate` requires
  `profile_id`; responses use `id` (client accepts `generation_id` as fallback).
  `GET /generate/<id>/status` is SSE, not JSON - consume the stream
  (`VoiceboxClient.watch_status`), never `resp.json()` it. `/transcribe`
  multipart field is `file` and answers HTTP 202 while Whisper downloads.
  Statuses: `generating`, `loading_model`, `completed`, `failed` (+ `not_found`
  pseudo-status on the stream).
- **MCP route needs the trailing slash**: `/mcp/` works, bare `/mcp` returns
  405 (Starlette mount path quirk in v0.5.0).
- **`GET /history/<id>`** is a plain-JSON status alternative to SSE - handy for
  one-shot checks.
- **Engine names** for `engine=` args: `qwen`, `qwen_custom_voice`, `luxtts`,
  `chatterbox`, `chatterbox_turbo`, `tada`, `kokoro`. Keep this as a soft list -
  the wrapper accepts any string so new upstream engines don't get rejected.

## Commands

```sh
# Full stack (from this repo; injects scoped CONTAINERS_REGISTRIES_CONF)
./scripts/stack-up.sh            # voicebox + livekit + bot, one compose project
./scripts/stack-up.sh logs       # watch startup; /health when ready
./scripts/stack-up.sh down
./scripts/stack-up.sh --host-bot # skip the bundled bot; run `uv run voicebot` host-side
./scripts/upstream-up.sh         # voicebox-only alternative (same base files)

# Wrapper MCP server
uv sync --directory services/voice-mcp
uv run --env-file ../../.env --directory services/voice-mcp voice-mcp   # stdio MCP

# Verification
./scripts/smoke-test.sh          # REST path: profiles -> speak -> SSE status
./scripts/warmup.sh              # pre-load kokoro/whisper/llama.cpp models after bring-up
./scripts/mcp-inspect.sh         # MCP inspector against the wrapper
./scripts/play-latest.sh         # play newest generated wav host-side
uv run --directory services/voice-mcp pytest -q    # unit tests
uv run --directory telephony/pipecat-bot pytest -q # bot unit tests
```

## Conventions

- Python >= 3.11, managed with uv. Deps only in `services/voice-mcp/pyproject.toml`.
- Secrets/config via `.env` (gitignored); `.env.example` documents every var.
- Branch naming follows global conventions (`feat/`, `fix/`, `chore/`).
- Phase status lives in README.md; keep it current when a phase completes.
- After an upstream `voicebox.speak` completes, play the audio host-side with
  `./scripts/play-latest.sh` - the container has no speakers (our wrapper's
  `say()` plays automatically once registered).
