# Setup

Bringing the stack up in order. Phase 1 is the only heavyweight step.

## 1. Voicebox runtime (upstream, podman container)

Primary path: headless container from a pinned upstream checkout. No host
toolchain needed beyond podman (+ podman-compose). GPU comes later via a
CUDA overlay we own; start CPU-only - kokoro/luxtts engines are CPU-fast.

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

Verify:

```sh
curl http://127.0.0.1:17600/health
curl http://127.0.0.1:17600/profiles
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

### GPU overlay (follow-up, not yet built)

The stock image installs CPU-only PyTorch. When needed, add `docker/Dockerfile.cuda`
(replicating upstream's pip stage with `--extra-index-url .../cu126`) plus a
compose override passing CDI device `nvidia.com/gpu=all` - nvidia-container-toolkit
and `/etc/cdi/nvidia.yaml` already exist on this machine.

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
