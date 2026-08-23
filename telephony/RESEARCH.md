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

1. Phase 4 baseline call with Pipecat defaults (Silero + timeout EOU +
   interruption gates + fully streaming chain) - comes free.
2. Measure baseline latency, then swap in Smart Turn EOU.
3. Enable preemptive generation once EOU is trustworthy.
4. Build the pre-rendered backchannel bank + trigger component (our code);
   doubles as the latency-mask filler for layer 5.
5. Treat speech-to-speech as an optional study layer; revisit only when a
   trigger in its section fires.

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

**Remaining Phase 4 work**:
- STT adapter (wrap Voicebox `/transcribe` or use faster-whisper directly)
- Transport integration (Daily/LiveKit/LiveKit SIP)
- LLM integration (any Pipecat-compatible service)
- End-to-end agent loop with VAD + turn-taking
