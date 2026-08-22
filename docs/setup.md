# Setup

Bringing the stack up in order. Phase 1 is the only heavyweight step.

## 1. Voicebox runtime (upstream, source build)

Linux has no prebuilt binaries, so we build from source. NVIDIA GPU => CUDA
PyTorch backend comes with the source build (the default Docker image is CPU-only).

Prerequisites: git, Rust (`rustup`), Bun, just (`cargo install just`),
Tauri Linux system deps (<https://v2.tauri.app/start/prerequisites/>).

```sh
git clone https://github.com/jamiepine/voicebox.git ~/Projects/ai/voicebox-upstream
cd ~/Projects/ai/voicebox-upstream
git checkout v0.5.0        # pinned upstream version; bump deliberately, record in AGENTS.md
just setup   # python venv + all deps; first run downloads model deps too
just dev     # starts backend (:17493) + desktop app
```

Verify:

```sh
curl http://127.0.0.1:17493/profiles
```

Then **create or import a voice profile in the app** (Profiles -> New -> drop
a reference sample) - the smoke test and `/speak` need at least one profile.

Notes:
- The REST API/MCP endpoint only answer while the desktop app is running.
- Docker alternative: `docker compose up` in the upstream checkout serves a
  headless CPU build at host port **17600** - no speakers, no speaking pill,
  so prefer the source build for our use case.
- If generation fails with CUDA errors, see upstream
  `docs/content/docs/overview/troubleshooting.mdx`.

## 2. Baseline wiring (upstream's built-in MCP)

Before touching our wrapper, prove plumbing with the built-in MCP server.

Claude Code:

```sh
claude mcp add voicebox --transport http \
  --url http://127.0.0.1:17493/mcp \
  --header "X-Voicebox-Client-Id: claude-code"
```

opencode (project-level `opencode.json`):

```json
{
  "mcp": {
    "voicebox": {
      "type": "remote",
      "url": "http://127.0.0.1:17493/mcp",
      "headers": { "X-Voicebox-Client-Id": "opencode" }
    }
  }
}
```

Then ask the agent to call `voicebox.list_profiles`, then `voicebox.speak`.
Per-client voice bindings show up under Voicebox -> Settings -> MCP.

Note: once our wrapper is also registered, agents see both `voicebox.speak`
and our `say` - disable the upstream server when testing ours to avoid
confusing double-voice behavior.

## 3. Wrapper MCP server (ours)

```sh
uv sync --directory services/voice-mcp
cp .env.example .env            # adjust VOICEBOX_URL if using docker port 17600
uv run --env-file .env --directory services/voice-mcp voice-mcp
```

Inspect tools without an agent host:

```sh
./scripts/mcp-inspect.sh        # npx @modelcontextprotocol/inspector over stdio
```

REST-level check independent of MCP:

```sh
./scripts/smoke-test.sh         # profiles -> generate -> poll -> PASS/FAIL
```

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `VOICEBOX_URL` | `http://127.0.0.1:17493` | Backend base URL |
| `VOICEBOX_CLIENT_ID` | `voice-mcp` | Client identity for per-client bindings |
| `DEFAULT_PROFILE` | unset | Fallback voice when callers omit `profile` |
| `SAY_TIMEOUT_SECONDS` | `120` | Max seconds to watch the SSE status stream |

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
