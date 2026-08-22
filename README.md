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
│   TTS engines (7) · Whisper STT · REST API :17493   │
│   Built-in MCP (/mcp) — we consume, don't replace   │
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

- [ ] **Phase 1** — Voicebox runtime up (source build with CUDA on Linux/NVIDIA)
- [ ] **Phase 2** — Wire upstream's built-in MCP into opencode; verify end-to-end
- [ ] **Phase 3** — Wrapper MCP server (`services/voice-mcp`): `say`, `voices`, `listen`
- [ ] **Phase 4** — Telephony spike: Pipecat bot + LiveKit SIP + Telnyx trunk
- [ ] **Phase 5** — Conversation dynamics: semantic end-of-turn, barge-in tuning, pre-rendered backchannels, speculative reply pipelining

See `docs/setup.md` for bring-up instructions and `telephony/RESEARCH.md`
for the Phase 4 decision record.

## Repository layout

```
.
├── services/voice-mcp/   # Wrapper MCP server (FastMCP, Python)
├── scripts/              # smoke-test.sh, mcp-inspect.sh
├── docs/                 # setup and operations notes
├── telephony/            # Phase 4 research and future code
└── AGENTS.md             # conventions for agent sessions in this repo
```

## Quick check (once Voicebox is running)

```sh
./scripts/smoke-test.sh
```
