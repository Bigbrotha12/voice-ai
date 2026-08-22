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
     └─ TTS: VoiceboxTTSService -> http://127.0.0.1:17493 (our wrapper's client code)
```

## Known risks

1. **Latency.** Voicebox is batch-oriented (chunking, crossfade, serial queue),
   not streaming-first. Turn-based calls will work; barge-in/realtime may not.
   Mitigation ladder: kokoro engine inside Voicebox -> standalone Kokoro in the
   call path -> accept Voicebox only for non-realtime call flows (voicemail,
   summaries) and use streaming TTS for live turns.
2. **Compliance.** Outbound AI calls carry TCPA/consent obligations; read
   upstream RESPONSIBLE_USE.md before any live dialing. Test against our own numbers first.
3. **Ops weight.** Self-hosted LiveKit adds infra. Managed LiveKit Cloud exists
   as a fallback if self-hosting stalls.

## Open questions (answer before starting Phase 4)

- Inbound or outbound first? (Outbound is simpler; inbound needs number provisioning.)
- Which country for the number? Affects pricing/regulation.
- Latency budget for "acceptable conversation"? Measure Voicebox TTS p50/p95 per engine early.
- Trunk budget cap for the learning phase?
