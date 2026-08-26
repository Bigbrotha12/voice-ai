# Voice Agent Stack

A learning project building an open-source alternative to ElevenLabs-style voice I/O
for AI agents, on top of [Voicebox](https://github.com/jamiepine/voicebox).

Agents get two primitives: **speak** (TTS through cloned/preset voices) and **listen**
(STT through Whisper) — exposed over MCP so any MCP-aware agent (opencode, Claude Code,
Cursor) can use them. A later phase adds telephony so agents can hold real phone calls.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│ Layer 1 — Voicebox runtime (upstream, unmodified)   │
│   TTS engines (7) · Whisper STT · REST/MCP :17600   │
│   Headless podman container (built-in MCP at /mcp)  │
├─────────────────────────────────────────────────────┤
│ Layer 2 — Our wrapper MCP server (learning core)    │
│   services/voice-mcp: say() · listen() · voices()   │
│   Typed REST client → Voicebox                      │
├─────────────────────────────────────────────────────┤
│ Layer 3 — Clients: opencode / Claude Code / custom  │
├─────────────────────────────────────────────────────┤
│ Layer 4 (later) — Telephony                         │
│   Pipecat agent + LiveKit SIP + Telnyx trunk        │
└─────────────────────────────────────────────────────┘
```

## Phases

- [x] **Phase 1** — Voicebox runtime up (podman container, CUDA via CDI passthrough)
- [x] **Phase 2** — Upstream MCP wired into opencode; speak verified end-to-end
- [x] **Phase 3** — Wrapper MCP server live: `say` (auto-play), `voices`, `listen` (mic → Whisper)
- [x] **Phase 4** — Telephony agent: LiveKit transport + Voicebox TTS + local faster-whisper STT + llama.cpp LLM (`telephony/pipecat-bot/`)
- [~] **Phase 5** — Conversation dynamics: semantic end-of-turn (smart-turn v3, done), barge-in tuning, pre-rendered backchannels, speculative reply pipelining

See `docs/setup.md` for bring-up instructions and `telephony/RESEARCH.md`
for the Phase 4 decision record.

## Repository layout

```
.
├── opencode.json           # registers upstream MCP + our wrapper for opencode
├── services/voice-mcp/     # Wrapper MCP server (FastMCP, Python)
├── docker/                 # compose overrides: ports/GPU, LiveKit, bot service
├── scripts/                # stack-up.sh, upstream-up.sh, smoke-test.sh, warmup.sh, ...
├── docs/                   # setup and operations notes
├── telephony/              # pipecat-bot (LiveKit voice agent) + research notes
└── AGENTS.md               # conventions for agent sessions in this repo
```

## Quick check (once Voicebox is running)

```sh
./scripts/smoke-test.sh
```
