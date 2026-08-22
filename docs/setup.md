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
just setup   # python venv + all deps; first run downloads model deps too
just dev     # starts backend (:17493) + desktop app
```

Verify:

```sh
curl http://127.0.0.1:17493/profiles
```

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
| `SAY_TIMEOUT_SECONDS` | `120` | Max wait for generation completion |
| `POLL_INTERVAL_SECONDS` | `1.0` | Status polling cadence |

## Unverified assumptions (tighten after Phase 1 runs)

- Generation id field: we accept both `generation_id` and `id`.
- Completion detection matches loosely on `completed/success/done` prefixes;
  failure on `failed/error/cancelled`.
- `/transcribe` multipart field name is `audio`.
