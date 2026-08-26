# Telephony research / decision record (Phase 4)

Status: direction chosen, not started. Do not begin until Phases 1-3 are done.

## Decision

**Pipecat + LiveKit SIP + Telnyx SIP trunk.**

## Why this stack

- **Pipecat** (open source, Python): voice-agent framework with pluggable
  LLM/TTS/STT services and transports. We subclass its `TTSService` to call
  Voicebox's REST API, so the phone bot uses the same cloned voices as our MCP layer.
- **LiveKit + LiveKit SIP** (self-hostable): WebRTC media plane plus a SIP
  gateway that bridges rooms to real phone numbers.
- **Telnyx**: the trunk. Real PSTN calls require carrier interconnect,
  number provisioning, E911, and STIR/SHAKEN - all regulated, all impractical
  to self-provision. Telnyx rents that plumbing at ~$0.003-0.01/min. Twilio is
  the managed alternative; Jambonz is an alternative CPaaS layer.

Everything above the trunk stays ours/self-hosted; only the regulated last mile is rented.

## Proposed shape

```
Pipecat agent ── WebSocket ── LiveKit room <── LiveKit SIP <── Telnyx trunk <── PSTN
     │                                            (inbound + outbound)
     ├─ LLM: pluggable (local or API)
     ├─ STT: Deepgram or local faster-whisper
     └─ TTS: VoiceboxTTSService -> POST /generate/stream (fresh httpx client,
        not the MCP wrapper - its say/watch-status path is poll-oriented)
```

## Conversation dynamics (turn-taking)

A phone call feels human when the agent handles interruptions and acknowledges
the listener while keeping replies fast. Five technologies stack up to that;
they land around Phases 4-5.

### 1. VAD - who is speaking right now

- **Silero VAD**: open source (~1MB ONNX model), real-time on CPU, the de-facto
  standard. Ships as Pipecat's default input analyzer - zero work for us.
- Knows only speech-vs-silence; everything below adds meaning.

### 2. End-of-turn detection - when the user is done

Silence length alone cannot distinguish "um, hold on..." from a finished answer.

| Approach | Tradeoff |
|---|---|
| Fixed timeout (300-800ms) | Simple; sluggish or cuts people off |
| Semantic EOU models | Watch partial ASR transcripts + audio, predict completion |
| Full-duplex speech models | Emergent behavior, but black-box |

Concrete options: Pipecat **Smart Turn** (open source, self-hostable),
LiveKit **TurnDetector** plugin, Deepgram Semantic End-of-Turn (managed).
Default plan: self-host Smart Turn; revisit managed options if quality lags.

### 3. Barge-in - letting the user interrupt the agent

Two requirements:
- **Echo cancellation**, else the agent hears its own TTS echoed back from the
  callee's speakerphone and keeps interrupting itself. Note: carrier-side AEC
  (G.168) fixes hybrid/network echo, NOT this acoustic coupling, and WebRTC
  AEC3 only helps WebRTC endpoints - for PSTN callees the mitigation is
  agent-side (AEC/noise-cancellation plugin on the received stream, VAD tuning).
- **Local instant cancellation** (<300ms perceived): on VAD firing during agent
  speech, kill TTS playback and flush the pipeline immediately - never wait on
  a server round trip. Both Pipecat and LiveKit Agents expose this as
  first-class configuration (`allow_interruptions`, input/output gates).

### 4. Backchanneling - acknowledgment fillers while the user talks

Research name: **backchannel prediction**. Generating "mm-hmm"/"right"/"go on"
is trivial; predicting *when* is the whole game. Too often sounds deranged -
fire at clause boundaries during long user monologues, keyed off prosody cues
(rising pitch invites acknowledgment, energy dips mark safe insertion points).

Latency reality: no batch TTS engine produces sub-100ms clips live. Our plan:
- Pre-render a **backchannel bank** per voice profile (several takes each;
  expressive variants via Chatterbox Turbo `[laugh]`/`[sigh]` paralinguistic
  tags).
- Own a small **trigger component**: watches ASR partials + VAD state, fires a
  clip into the same output path. Start heuristic (>5s continuous user speech
  AND pitch-energy dip), upgradeable to prosody classifiers later.
- This is genuinely our code, not framework plumbing - good learning surface.

### 5. Speculative reply planning - closing the 200 ms gap

Measured human conversations leave ~200 ms between one person finishing and
the next starting. Planning a reply takes longer than that alone, so people
start composing while the other person is still talking. An agent that waits
for silence, then thinks, then speaks is already seconds behind. Countermeasures,
roughly in adoption order:

- **Stream every stage.** Streaming ASR emits partial transcripts continuously;
  streaming LLM yields first tokens right at EOU; streaming TTS speaks
  sentence-by-sentence instead of waiting for the full reply. End-to-end
  latency collapses to EOU + first-token + first-audio-chunk instead of the
  sum of complete stage times. Cascaded frameworks do this natively - it is
  the default shape of a Pipecat/LiveKit pipeline, not an add-on.
- **Preemptive generation.** Kick off the LLM on *partial* transcripts before
  EOU declares completion; discard or restart if the transcript shifts
  materially. LiveKit Agents ships this as preemptive generation. Costs wasted
  tokens on revisions, saves hundreds of milliseconds of perceived latency.
- **Latency-mask fillers.** A turn-boundary acknowledgment ("mm-hm...", "okay
  so...") plays instantly while the real answer generates behind it. Reuses
  the layer-4 backchannel bank for a second purpose - and it mirrors what
  humans actually do ("uh, let me think").
- **Warm paths.** Models stay loaded, KV caches warm, glue audio pre-rendered,
  fastest engine (kokoro) assigned to live turns. Cold-start is the silent
  latency killer. Cross-reference Known risk 1: Voicebox's batch queue means
  live-turn TTS likely needs kokoro inside the app or standalone Kokoro.
- **Early semantic commitment** (advanced, optional). Where intent is
  predictable from a partial utterance ("What's your name?" half-finished),
  template answers can be pre-selected before EOU fires.

Budget sanity: strong agents target <=600-800 ms end-to-end
(EOU ~100-200 + first token ~150-300 + audio TTFB ~100-200). Full-duplex
speech models (optional layer below) attack this architecturally - they plan
while listening by construction - which is exactly why they remain the
long-term alternative to all these pipeline tricks.

### Adoption order in our stack

1. ~~Phase 4 baseline call with Pipecat defaults~~ **done**.
2. ~~Measure baseline latency, then swap in Smart Turn EOU~~ **done** -
   smart-turn v3 was already the framework default (now pinned explicitly);
   baseline table above. Every service leg at/below budget.
3. ~~Enable preemptive generation~~ **skipped** - first token is 60ms warm;
   there is no LLM wait left to mask.
4. **Backchannel bank + trigger component** **built** (2026-08-25):
   `scripts/backchannel-bank.py` renders expressive chatterbox_turbo takes
   into `backchannels/` (mounted read-only into the bot); `voicebot.backchannel`
   fires clips on an energy-dip heuristic during monologues >5s
   (`VOICEBOT_BACKCHANNEL_*` env tuning; kill switch `VOICEBOT_BACKCHANNEL=0`).
   One clip per dip episode + cooldown; bypasses LLM+TTS entirely
   (single pipeline hop). Upgrade path: prosody classifier replacing the
   energy heuristic.
5. Treat speech-to-speech as an optional study layer; revisit only when a
   trigger in its section fires.

Adjacent knobs not yet exercised: barge-in threshold tuning (browser AEC is
free; matters more for SIP), pipecat `FilterIncompleteUserTurnStrategies`
for incomplete-utterance handling.

## Optional layer: speech-to-speech models (deferred)

Speech-to-speech (S2S / duplex) models take audio in and emit audio out
through a single network - no ASR -> text -> LLM -> TTS round trip, so
prosody, pace, and emotion survive, and turn-taking plus backchanneling
(layers 2-4) emerge architecturally instead of from pipeline plumbing.

Landscape:
- **Managed**: OpenAI gpt-realtime, Amazon Nova Sonic, Google Gemini Live -
  all moved to this design in 2025.
- **Open source**: Moshi (Kyutai) is the reference implementation;
  nothing else is production-mature yet.
- **Voicebox roadmap** lists "end-to-end speech LLMs" (Moshi, GLM-4-Voice,
  Qwen2.5 Omni) - if it ships, this collapses into the app we already run.

Why deferred, not dismissed:
1. **Cloned voices are the core goal.** Managed S2S locks us to provider
   voice catalogs; Voicebox profiles cannot ride along.
2. **Cost per minute** vs local GPU inference on hardware we already own.
3. **Control/transparency.** No transcript trail, weaker debugging and
   compliance story for real phone calls.
4. **Learning value.** The pipeline keeps every stage visible and swappable,
   which is the point of this repo.

Revisit triggers:
- Voicebox ships native duplex/speech-native support.
- A cloned-voice conditioning path appears on an open S2S model.
- Pipeline latency plateaus above the conversational threshold even after
  layers 2-5 are tuned.

## Known risks

1. **Latency.** Voicebox is batch-oriented (chunking, crossfade, serial queue),
   not streaming-first. Even `POST /generate/stream` sends the first WAV byte
   only after FULL synthesis - TTFB is the full batch time, so it does not
   rescue live turns. Turn-based calls will work; barge-in/realtime may not.
   Mitigation ladder: kokoro engine inside Voicebox -> standalone Kokoro in the
   call path -> accept Voicebox only for non-realtime call flows (voicemail,
   summaries) and use streaming TTS for live turns.
2. **Compliance.** Outbound AI calls carry TCPA/consent obligations; read
   upstream RESPONSIBLE_USE.md before any live dialing. Test against our own numbers first.
3. **Ops weight.** Self-hosted LiveKit adds infra. Managed LiveKit Cloud exists
   as a fallback if self-hosting stalls.

## Measured latency baseline (2026-08-25, RTX 4060 Ti, warm models)

Component benchmarks via `scripts/latency-bench.sh`; live per-turn numbers
via the bot's `UserBotLatencyObserver` (LATENCY log lines, every turn).

| leg | measured | budget (layer 5) | verdict |
|---|---|---|---|
| STT decode - faster-whisper turbo, in-process GPU | 173ms / 2.8s utterance (16x RT) | - | realtime-grade |
| STT batch path (Voicebox /transcribe, comparison) | ~14s round trip | - | retired |
| LLM first token via queues proxy (Qwen3-8B, warm) | 60ms | 150-300ms | beats budget |
| LLM 80-token stream total | 440ms | - | - |
| TTS kokoro short / medium | 40ms / 130ms | 100-200ms | beats budget |

Consequence: with every service leg at or under budget, **preemptive
generation is skipped** - there is no LLM wait left to hide. Phase 5 effort
goes to conversation dynamics instead: backchannel bank + trigger,
barge-in tuning, and optionally pipecat's FilterIncompleteUserTurnStrategies
for incomplete-utterance handling.

## Cluster voice services probe (2026-08-26, data-only k3s path)

Via `scripts/cluster-voice-test.sh --probe-only` (kubectl port-forward,
closed loop: glados-tts output feeds whisper-stt input). Warm steady-state:

| leg | measured | budget | verdict |
|---|---|---|---|
| TTS glados-tts (piper CPU, wav response_format live) | ~1000-1150ms | 300ms | over - piper subprocess dominates; transcode was not the cost |
| STT whisper-stt (small.en int8 CPU, 2s utterance) | ~730-860ms | 600ms | near - model-size/thread tuning or in-cluster placement |

End-to-end single turn via room driver (2026-08-26): **user-stop →
bot-speech 2.12s** = user_turn 0.93s (batch STT + EOU) + LLM ttfb 0.15s +
Piper ttfb ~1.0s. Long-monologue driving shows the known batch pathology:
transcripts land after smart-turn fires on clause dips -> early/misaligned
turn releases and repeated LLM runs (13 TTS gens in one 26.6s monologue).
Verdict: works as data-only demo path; conversational-grade needs faster
STT/TTS legs (in-cluster hop, piper warm process, smaller/faster whisper
model) or Voicebox GPU legs for live turns.

## Measured TTS latency (2026-08-23, RTX 4060 Ti, GPU, warm models)

Via `scripts/tts-benchmark.py` against `POST /generate/stream`
(total time to full wav in memory; TTFB == total since synthesis is batch):

| engine | short (~50 chars) | medium (~240 chars) | verdict for live turns |
|---|---|---|---|
| kokoro | 0.04s | 0.15s | realtime-grade - default live-turn engine |
| luxtts | 0.11s | 0.19s | realtime-grade |
| chatterbox_turbo | 0.78s | 2.31s | turn-boundary use only (expressive tags) |
| qwen | n/a | n/a | BLOCKED: triton JIT needs a C compiler absent from the runtime image; revisit only if delivery control becomes a hard requirement |

This retires open question 3 and downgrades Known risk 1 substantially:
with kokoro at 40ms, Voicebox itself can serve live turns - standalone
Kokoro is no longer the assumed fallback.

## Open questions (answer before starting Phase 4)

- Inbound or outbound first? (Outbound is simpler; inbound needs number provisioning.)
- Which country for the number? Affects pricing/regulation.
- ~~Latency budget for "acceptable conversation"?~~ MEASURED 2026-08 (see table above): kokoro/luxtts are realtime-grade on GPU. Remaining budget question is end-to-end (VAD+LLM), not TTS.
- Trunk budget cap for the learning phase?

## Phase 4 groundwork (2026-08-23)

**TTS adapter complete**: `telephony/pipecat-bot/src/voicebot/tts.py` — `VoiceboxTTSService` wraps `POST /generate/stream`, parses WAV header, yields raw PCM frames. 8 unit tests pass. See `telephony/pipecat-bot/README.md` for usage.

**STT adapter complete**: `telephony/pipecat-bot/src/voicebot/stt.py` — `VoiceboxSTTService` wraps `POST /transcribe` (batch Whisper transcription). 5 unit tests pass. Note: batch-oriented, not streaming - suitable for dictation-style use cases.

**Remaining Phase 4 work**:
- ~~Transport integration (Daily/LiveKit/LiveKit SIP)~~ done: LiveKit transport
- ~~LLM integration (any Pipecat-compatible service)~~ done: llama.cpp via OLLamaLLMService
- ~~End-to-end agent loop with VAD + turn-taking~~ done: see README

## Latency tuning round 1 (2026-08-25)

Adoption-order progress against the "Conversation dynamics" section above:

1. **Baseline streaming chain**: already the pipecat default shape. Done.
2. **Smart Turn EOU**: adopted. Pipecat 1.7.0 ships smart-turn v3.2 as a
   *bundled* ONNX model and makes it the default user-turn stop strategy
   (`default_user_turn_stop_strategies()`). Pinned explicitly in
   `agent.py` so the dependency stays visible.
3. **Preemptive generation**: deferred - no framework support in pipecat
   1.7.0 (LiveKit Agents has it; pipecat does not). Requires custom
   partial-transcript -> LLM restart plumbing. Revisit if turn latency is
   still above budget after measuring with the current stack.
4. **Backchannel bank + trigger**: not started (Phase 5).
5. **Speech-to-speech**: still deferred per triggers above.

**STT moved in-process (2026-08-25)**. The batch `/transcribe` path put full
upload + decode latency on the turn critical path, and Smart Turn's stop
strategy waits for the final transcript before firing - so STT speed gates
end-of-turn. Default is now pipecat's `WhisperSTTService` (faster-whisper /
CTranslate2) running inside the bot process, GPU via optional nvidia wheels
(preloaded through ctypes since CTranslate2 dlopens them by soname). The
Voicebox batch adapter remains selectable via `VOICEBOT_STT_PROVIDER=voicebox`
(useful when VRAM is tight or for CPU-only Voicebox runtimes - note upstream
whisper small/turbo crash on `--cpu`; use `base` there).

**Warm paths**: `scripts/warmup.sh` warms kokoro + whisper + llama.cpp after
bring-up so first turns don't pay model cold-start.

### llama.cpp server flags (recommended)

`llama-small` (the bot's endpoint, 19091) runs CPU-resident (`-ngl 0`) by
design - it coexists with voicebox/14B on the GPU without eviction logic.
For that container the useful additions are `--jinja`, `--mlock`, and
`--cache-type-k q8_0 --cache-type-v q8_0`; flash-attn only applies if a
model is moved onto the GPU (`-ngl > 0`). Verify any flag actually engaged
in server startup logs; llama.cpp falls back silently in some cases.
