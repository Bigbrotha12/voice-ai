# Setup

Bringing the stack up in order. Phase 1 is the only heavyweight step.

## 1. Voicebox runtime (upstream, podman container)

Primary path: headless container from a pinned upstream checkout. No host
toolchain needed beyond podman (+ podman-compose). GPU comes via CDI
passthrough (see below) - the stock image ships CUDA-enabled torch.

```sh
git clone https://github.com/jamiepine/voicebox.git ~/Projects/ai/voicebox-upstream
cd ~/Projects/ai/voicebox-upstream
git checkout v0.5.0        # pinned upstream version; bump deliberately, record in AGENTS.md

./scripts/upstream-up.sh   # from THIS repo; first build takes several minutes
```

Note: `upstream-up.sh` injects a scoped `CONTAINERS_REGISTRIES_CONF`
(`docker/registries.conf` in this repo) so podman resolves the short
base-image names in upstream's Dockerfile non-interactively - your global
podman config stays untouched.

## 1b. Full stack bundle (voicebox + livekit + bot)

`scripts/stack-up.sh` wraps everything in ONE compose project (same `-f`
stacking pattern), so all services share a network (`ws://livekit:7880`,
`http://voicebox:17493` internally) and one up/down/logs lifecycle:

```sh
./scripts/stack-up.sh                # voicebox + livekit + bot (GPU)
./scripts/stack-up.sh --host-bot     # iterate on agent code: run it on the host
./scripts/stack-up.sh logs           # follow everything
./scripts/stack-up.sh down           # stop all three
```

Details:
- **livekit** (`docker/livekit.yml`, pinned v1.13.5): dev mode - no auth,
  accepts any devkey/secret-signed token. HOST-networked (not bridge): ICE
  candidates must point at reachable interfaces, and loopback-only port
  publishes leave the browser with container-IP candidates it cannot reach
  ("could not establish pc connection"). Ports: `7880/tcp` signaling,
  `7881/tcp + 7882/udp` media, all interfaces. Browsers connect to
  `ws://localhost:7880`; the bot uses `host.containers.internal`.
- **voicebot** (`docker/voicebot.yml` + `telephony/pipecat-bot/Containerfile`):
  multi-stage uv build with faster-whisper GPU wheels; CDI GPU passthrough +
  `label=disable` like voicebox. Requires `VOICEBOX_PROFILE_ID` in `.env`
  (restart-loops with a clear error otherwise). First start downloads the
  turbo CT2 model (~1.5GB) into the `voicebot-hf-cache` volume.
- The bot reaches llama.cpp at `host.containers.internal:19091` - start
  `llama-server` on the host separately; it is NOT part of this stack.
- Restart coupling is per-service (separate containers), unlike a fused
  image. Rebuild just the bot after code edits:
  `podman-compose ... up -d --build voicebot`, or rerun `stack-up.sh`.
- `--cpu` mode still applies only to voicebox; the bot keeps its own GPU
  wheels regardless (drop the `[gpu]` extra in the Containerfile for a
  fully CPU stack).

Verify:

```sh
curl http://127.0.0.1:17600/health
curl http://127.0.0.1:17600/profiles
podman ps --format "{{.Names}} {{.Status}}"   # voicebox, livekit, voicebot
```

Then **create or import a voice profile via the web UI** at
<http://127.0.0.1:17600> (Profiles -> New -> drop a reference sample) -
the smoke test and `/speak` need at least one profile.

Container facts that matter:
- Host port is **17600** (container-internal stays 17493); bound to loopback only.
- Generated audio lands in `~/Projects/ai/voicebox-upstream/output/` (bind
  mount). Play host-side: `./scripts/play-latest.sh`.
- Headless = no speaking pill, no dictation hotkey. Accepted tradeoff.
- Models download lazily on first use into named volumes (persist across
  restarts); `/transcribe` answers HTTP 202 while Whisper downloads.

### GPU

No custom image needed: the stock build ships torch **cu130**. GPU access is
just CDI passthrough, already wired in `docker/ports-override.yml`
(`nvidia.com/gpu=all`; requires nvidia-container-toolkit + `/etc/cdi/nvidia.yaml`).
Verify with:

```sh
podman exec voicebox python -c "import torch; print(torch.cuda.is_available())"
curl -s http://127.0.0.1:17600/health | grep -o '"gpu_available":[a-z]*'
```

### Alternative: source/desktop build

Only if you want the desktop app features (speaking pill, dictation hotkey,
per-client binding UI): install Rust + just + Tauri system deps, then
`just setup && just dev`; API then lives on **17493** and `.env` must point there.
See upstream linux-install docs. Not required for anything in this repo.

## 2. Baseline wiring (upstream's built-in MCP)

Before touching our wrapper, prove plumbing with the built-in MCP server.

Claude Code:

```sh
claude mcp add voicebox --transport http \
  --url http://127.0.0.1:17600/mcp/ \
  --header "X-Voicebox-Client-Id: claude-code"
```

opencode (project-level `opencode.json`):

```json
{
  "mcp": {
    "voicebox": {
      "type": "remote",
      "url": "http://127.0.0.1:17600/mcp/",
      "headers": { "X-Voicebox-Client-Id": "opencode" }
    }
  }
}
```

Then ask the agent to call `voicebox.list_profiles`, then `voicebox.speak`.
Per-client voice bindings show up under Voicebox -> Settings -> MCP.

Both servers stay enabled in `opencode.json` on purpose: `voicebox.*` is the
upstream raw tool surface, while our wrapper's `say`/`listen`/`voices` add
host playback and mic capture on top. If you ever test wrapper changes in
isolation, temporarily disable the upstream entry so tool calls are
unambiguous.

## 3. Wrapper MCP server (ours)

```sh
uv sync --directory services/voice-mcp
cp .env.example .env            # adjust VOICEBOX_URL if using docker port 17600
uv run --env-file ../../.env --directory services/voice-mcp voice-mcp
```

Note: uv resolves `--env-file` relative to `--directory`, hence `../../.env`.

Inspect tools without an agent host:

```sh
./scripts/mcp-inspect.sh        # npx @modelcontextprotocol/inspector over stdio
```

REST-level check independent of MCP:

```sh
./scripts/smoke-test.sh         # profiles -> speak -> SSE status -> PASS/FAIL
```

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `VOICEBOX_URL` | `http://127.0.0.1:17600` | Backend base URL |
| `VOICEBOX_CLIENT_ID` | `voice-mcp` | Client identity for per-client bindings |
| `DEFAULT_PROFILE` | unset | Fallback voice when callers omit `profile` |
| `SAY_TIMEOUT_SECONDS` | `120` | Max seconds to watch the SSE status stream |
| `VOICEBOX_OUTPUT_DIR` | `~/Projects/ai/voicebox-upstream/output` | Where generated wavs land |
| `VOICEBOX_PLAYER` | `auto` | Host player: `auto`, `none`, or paplay/ffplay/mpv |
| `VOICEBOX_WARMUP_MS` | `250` | Silence burst before playback to wake idle sinks |
| `VOICEBOX_MIC_DEVICE` | auto-detect | Input source override for `listen()` |

## API shapes (verified against upstream v0.5.0 source)

- `/speak` and `/generate` return `GenerationResponse` with field `id`
  (`generation_id` is kept as a fallback key in the client).
- `GET /generate/<id>/status` is a **Server-Sent Events** stream
  (`text/event-stream`): emits `data: {...}` immediately, then ~1/s until
  `completed`/`failed`, then closes. It is NOT a JSON polling endpoint.
  `not_found` is a pseudo-status meaning the id is unknown.
- `/generate` requires `profile_id` (404 without it); `/speak` resolves a
  profile by name/id, then per-client binding, then global default.
- `/transcribe` multipart field is `file` (not `audio`); returns **HTTP 202**
  while the Whisper model downloads (~1.5 GB first use) - retry after it
  finishes.
- Status values: `generating`, `loading_model`, `completed`, `failed`.
