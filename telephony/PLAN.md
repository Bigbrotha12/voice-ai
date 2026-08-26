# Telephony implementation plan (Phase 6)

Status: planned. Prereqs done: agent core runs on LiveKit transport
(`telephony/pipecat-bot/`), every service leg realtime-grade
(`RESEARCH.md` latency tables). Nothing SIP-related exists yet.

## What is actually left

| item | state |
|---|---|
| Agent core (STT → LLM → Voicebox TTS, tools/MCP bridge) | done |
| Conversation dynamics (smart-turn EOU, backchannels) | done |
| livekit-sip container + dispatch config | not started |
| Telnyx account, number, trunk | not started |
| Outbound trigger (MCP dial tool) + task-card ("skill") loader | not started |
| Barge-in tuning for PSTN acoustics | Phase 5 leftover — matters more once on SIP |

## Rejected alternatives (decided 2026-08-26: drop WhatsApp/Telegram)

Messaging-app voice notes exercise the brain (STT/LLM/TTS) but none of the
call path: no turn-taking, no barge-in, no narrowband audio, no SIP media.
Real-time calls there would mean Telegram TDLib userbot hacks (Bot API has
no calls) or WhatsApp Business Calling (Meta approval + custom WebRTC
bridge, no pipecat transport). Voice-note bots add webhook/tunnel friction
for zero coverage of the remaining risk. **The cheapest faithful pre-Telnyx
test is a LAN softphone through livekit-sip** - same container, same codec
negotiation (G.711), same dispatch rules the carrier will use. Cost: $0.

## Track A — LAN SIP dry-run (do first)

Goal: place calls between a softphone and the agent with no carrier.

1. **Add `docker/livekit-sip.yml`** (pinned image matching server v1.13.x
   family), sibling of `docker/livekit.yml`, host-networked like the server
   (same ICE reasoning documented there).
   - livekit-sip requires Redis even single-node - include a tiny redis
     service in the same file.
   - Narrow RTP range (e.g. `10000-10100/udp`) now; makes later NAT
     forwarding tractable.
   - Env: `LIVEKIT_SIP_DOMAIN`-style vars into `.env.example`; reuse
     `LIVEKIT_API_KEY/SECRET`.
2. **Dispatch rule**: inbound SIP → room `voicebot-room` (or per-DID room
   naming from the start: `<did>-room`; cheap now, painful retrofitted).
3. **Softphone test** (MicroSIP/Linphone/Zoiper on phone or desktop, LAN):
   - outbound leg: create a SIP participant in the room dialing
     `sip:test@<host-lan-ip>` (direct IP call - no registration needed);
   - inbound leg: softphone registers against livekit-sip, dials the
     configured username, lands in the agent's room.
4. **Verify** (`telephony/pipecat-bot/README.md` web client still works as
   the control path):
   - two-way audio, agent hears/responds;
   - LATENCY observer lines under narrowband (expect STT slightly worse at
     8 kHz; kokoro TTS resampled 24k→8k - quality check by ear);
   - barge-in over speakerphone - first data point for acoustic echo
     mitigation (RESEARCH.md §3: WebRTC AEC does not help PSTN callees).
5. Update `docs/setup.md` + this file with results.

Exit criteria: a LAN call completes end-to-end with acceptable turn-taking.

## Track B — Telnyx trunk (real PSTN)

Prerequisites - decisions RESEARCH.md left open, answer before buying:

- [ ] Country + number type (affects KYC/regulation; US numbers need E911 address)
- [ ] Inbound-first or outbound-first? (outbound simpler; inbound proves dispatch)
- [ ] Budget: number ≈ $1/mo; **channel billing $12/mo for the first 10
      concurrent channels** (zone-dependent: up to $25/mo; verified against
      telnyx.com/pricing.md 2026-08-26 - this was missing from earlier
      estimates); outbound from $0.005/min, inbound local from $0.0032/min;
      optional E911 $1.50/mo. Realistic learning floor ≈ $13-15/mo fixed.
- [ ] Read upstream RESPONSIBLE_USE.md; test numbers = our own only (TCPA)

Network reality check (home deployment):

- Port-forward UDP 5060 + the narrowed RTP range to the LiveKit host.
- Dynamic IP → use Telnyx FQDN connection mode against DDNS name, or IP-auth
  if static.
- Firewall scope: allow only Telnyx signaling/media CIDRs (livekit.yml
  already warns about all-interfaces exposure).

Steps:

1. Telnyx: account + KYC → buy number → create SIP Connection (FQDN mode,
   G.711 ULAW) → note credentials. STIR/SHAKEN attestation is handled
   carrier-side (Telnyx is a certified SHAKEN provider, full attestation) -
   no extra registration for standard outbound caller ID.
2. livekit-sip config: inbound trunk JSON (DID list, allowed addresses =
   Telnyx CIDRs) + dispatch rule → room; outbound trunk JSON with Telnyx
   auth.
3. Test inbound: mobile → number → agent answers (dispatch verified).
4. Test outbound: agent dials own mobile (SIP participant with
   `sip:<num>@<telnyx-sip-domain>`).
5. Outbound trigger plumbing: small MCP tool (`dial(number)`, `hangup()`)
   wrapping LiveKit's `CreateSIPParticipant` - same wrapper shape as
   `services/voice-mcp`. Deliberately NOT registered into opencode for now
   (capability ships, exposure deferred - decided 2026-08-26).
6. Task cards ("skills") for scenario calls: replace the hardcoded
   SYSTEM_INSTRUCTION with a loader -
   `telephony/pipecat-bot/tasks/<name>.md`: frontmatter (voice profile,
   engine, greeting) + body (goal, info slots to gather, what to ask, end
   condition). Selected per call via `VOICEBOT_TASK`. Slot-filling enforced
   by a native `submit_*` tool (JSON args -> validate -> re-ask on gaps),
   not prompt-hoping. Constraints: cards stay <=~1k tokens (llama.cpp is
   CPU-resident; prefill lands on first-turn latency), voice-behavior rules
   ("one question at a time", no lists) stay as the base prompt layer under
   every card.
7. Post-trunk hardening round:
   - barge-in tuning for PSTN echo (agent-side noise suppression/VAD gates);
   - pipecat `FilterIncompleteUserTurnStrategies`;
   - re-run `scripts/warmup.sh` coverage check - cold-start on an answered
     call is user-facing;
   - remeasure latency table over narrowband; update `RESEARCH.md`.

## Cross-check vs Telnyx docs (2026-08-26)

Sources: `telnyx.com/llms-full.txt` + `telnyx.com/pricing.md`.

- **No architectural contradiction.** Telnyx fully supports standard SIP
  (RFC 3261) + RTP; their native guides target Asterisk/FreeSWITCH/Kamailio
  - livekit-sip speaks the same standard SIP, so the FQDN-connection +
  G.711 plan holds.
- LiveKit is absent from their integration matrix - expected: Telnyx sells
  its own managed Conversational AI stack ($0.05/min all-inclusive) and
  flags LiveKit as the "better alternative" for WebRTC-first apps. Our
  marginal cost stays ~$0.005/min trunk + owned GPU.
- Their Voice API / Call Control path requires public webhooks - one more
  reason we chose a plain SIP Connection instead (fits loopback-only home
  posture; only UDP 5060 + RTP range get exposed).
- STIR/SHAKEN: carrier-side, full attestation level A. No customer-side
  registration needed for standard outbound CLI.
- **Correction adopted**: channel billing ($12/mo first 10 concurrent
  channels) was missing from earlier budget estimates; folded into Track B
  prerequisites.

## Sequencing

A → B is the critical path. A de-risks ~all of B except carrier-side
config. If A stalls, managed LiveKit Cloud SIP remains the fallback
(RESEARCH.md risk 3).
