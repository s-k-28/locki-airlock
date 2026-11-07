# Milestone 5 verification: the context-aware companion

Status on 2026-11-01, branch `milestone-2` (local commits only; nothing pushed, nothing
flashed, no motor command sent). Every number here was measured on the demo MacBook Air
(M4, 16 GB) and has a JSON file in `docs/benchmarks/`. The machine was also running the
owner's usual software, with 6-8 GB of swap in use during these measurements.

| Mark | Meaning |
|---|---|
| ✅ | verified by automated tests or measured on this Mac (software) |
| 🟡 | needs a person, the Bluetooth speaker, the real camera or the robot: **not done** |
| ⛔ | not allowed or not possible in this milestone |

**What software verification does not cover:** no person has talked to the companion yet;
no new voice has been heard on the Bluetooth speaker by a person in this milestone (macOS
output was muted, and audible tests need your OK); no session has run on the robot's Wi-Fi
with the robot camera since the companion exists. The checklist at the end is what remains.

## What was built

| Area | What | Where |
|---|---|---|
| Components | whisper.cpp (Metal) speech-to-text, Silero VAD, Qwen3-4B-Instruct-2507 on MLX, Kokoro int8; pinned downloads; measured against the alternatives | [MODELS.md](MODELS.md), `scripts/bench_local_ai.py`, `scripts/download_models.py` |
| Context | bounded memory (10 turns, 24 observations, 8 statements, goal, quiet mode, complaints); OBSERVED facts only from bus events, USER SAID kept apart; nothing on disk | `intelligence/context.py`, `companion._on_event` |
| Conversation | the local model writes one JSON reply per turn; the context is read while the user talks; replies stream; every sentence passes the guards before it is spoken; one retry | `intelligence/dialogue.py`, `local_llm.py`, `llm_worker.py`, `prompting.py` |
| Behaviour | speak / stay-silent decisions with rules: possible reach ≠ distraction, escalation over 10 min, quiet mode, complaints halve remarks, camera-loss hysteresis, never over the user | `intelligence/behavior.py` |
| Actions | a closed typed vocabulary; the safety controller decides anything physical; LEDs refused (firmware has none) | `intelligence/actions.py`, `companion._execute`, [SAFETY.md](SAFETY.md) §7.5 |
| Voice | Fish Audio cached clips kept; Kokoro (local, labelled) for everything else; generated lines never sent to a cloud voice | `voice/kokoro_tts.py`, `voice/tts.py` |
| Turn-taking | push-to-talk (default) with barge-in; Silero VAD for hands-free; "thinking" state; camera / mic voice controls | `voice/input.py`, `voice/service.py`, `voice/intents.py` |
| Dashboard | Companion panel: goal, bounds, observed vs said, decisions with rules, actions requested/resolved, replies written/prepared/fixed with timings; conversation switches | `frontend/src/components/CompanionPanel.tsx` |
| Evidence tools | 35-scenario evaluation, end-to-end latency, software soak, run report, listening samples | `scripts/conversation_eval.py`, `latency_check.py`, `soak_companion.py`, `run_report.py`, `voice_samples.py` |

## Requirement matrix

| # | Requirement (brief) | Evidence | Status |
|---|---|---|---|
| 1.1 | Evaluate the listed candidates on this Mac (memory, latency, contention, ESP32 camera pipeline) | MODELS.md; `m5-llm`, `m5-stt`, `m5-tts*`, `m5-vad`, `m5-contention` | ✅ (pipecat, speech-to-speech, openWakeWord: design review only, not installed; reasons in MODELS.md) |
| 1.2 | Smallest set that materially improves; download while online; offline at runtime | 4 components; `download_models.py` pinned + sha256; offline probe below | ✅ |
| 1.3 | Dependencies, licences, provenance, benchmarks documented | MODELS.md (incl. the GPL note for phonemizer / espeak-ng) | ✅ |
| 2.1 | A local model writes unscripted replies; personality becomes a style layer | `prompting.PERSONA` + STYLE line from the personality settings; 58/58 distinct replies per evaluation run | ✅ |
| 2.2 | Structured, bounded context; observations vs user statements; no raw audio/video kept | `context.py`; `test_the_context_keeps_what_was_seen_apart_from_what_was_said` | ✅ |
| 2.3 | Indirect, conversational statements understood (not a fixed list) | evaluation: the 8 brief examples 100% actions; held-out 75% actions (see below) | ✅ measured; 🟡 with real people |
| 2.4 | Deterministic parser for critical actions; the model never emits motor commands or overrides safety | `ALWAYS_REFLEX`, `IMPERATIVE_STOP`; malicious-output tests; **0 motion requests in every evaluation run** | ✅ |
| 3.1 | Event-driven behaviour from a timestamped history: intervene or not, urgency, already answered, style, physical request possible | `behavior.py`, `test_companion.py` (silence, quiet, complaints, escalation, blip vs loss); evaluation events 100% | ✅ |
| 3.2 | Hysteresis, thresholds, cooldowns, priorities; no invented scores | rules B1-B6 in `behavior.py`, P0-P3 speech queue | ✅ |
| 4.1 | Keep the Fish Audio pack; local dynamic TTS offline; the difference labelled | VoiceRouter order Fish cache → Kokoro → macOS; dashboard source labels | ✅ |
| 4.2 | Short, varied replies without gratuitous profanity | guards (style, length, repetition); mean 15 words, 0 identical pairs | ✅ |
| 4.3 | Total latency speech end → audible | pipeline to playback start measured (below); the speaker's own latency measured in Milestone 4 (JLab 346-466 ms) | ✅ pipeline; 🟡 audible end to end with the new voice |
| 4.4 | Bluetooth playback confirmed by a person | — | 🟡 H2 |
| 5.1 | Push-to-talk improved; Silero VAD and streaming investigated | context prefilled while the user talks; whisper.cpp 98 ms; Silero 0/20 false starts on clicks | ✅ |
| 5.2 | Hands-free not default without substantial improvement | push-to-talk default; Silero for hands-free; room test not repeated | ✅ / 🟡 H7 |
| 5.3 | Pauses, interruptions, corrections, follow-ups, self-transcription, background talk, stop/mute, mic/camera controls | barge-in (M4 tests), correction and follow-up scenarios, echo tests, hands-free room-talk scenario, voice commands for camera/mic off | ✅ |
| 6 | Typed action interface; only the safety controller approves; no movement | `actions.py`, `_execute`; every physical request refused today | ✅ (⛔ movement by design) |
| 7 | Dashboard shows goal, conversation, context, observations, why it spoke, generated vs cached, voice source, intervention state, robot link, motor safety, actions | Companion panel + existing robot/voice panels; browser check below | ✅ |
| 8.1 | ≥20 unseen natural scenarios; contextual correctness, action accuracy, interruptions, variation, latency, memory | 35 scenarios (8 held out), [MILESTONE5_EVALUATION.md](MILESTONE5_EVALUATION.md) | ✅ |
| 8.2 | Audio and video simultaneously; real offline inference; malformed / malicious output | contention benchmark, latency with camera load, sandboxed offline probe, malicious-output tests | ✅ |
| 8.3 | Real human evaluation | — | 🟡 H4, H10 |
| 8.4 | 20-minute live soak | two 20-minute software soaks (below), 0 errors; live soak with the robot | ✅ software / 🟡 H8 |
| 9 | The target demo, offline, robot stationary | [DEMO_SCRIPT.md](DEMO_SCRIPT.md) Milestone 5 section | ✅ script; 🟡 rehearsal with the robot |

## Response time, end to end (`scripts/latency_check.py`)

The production pieces in one process: a sentence spoken by a macOS voice (16 kHz), the
whisper.cpp worker, the runtime (parser or local model; the context prefilled while the
sentence is being "spoken"), the voice (cached Fish clip or Kokoro rendered now) and a stub
output that records when playback would start and plays nothing. Eight turns per run: three
for the parser, five for the model. Add the speaker's own latency on top (the JLab measured
346-466 ms in Milestone 4) for when it is actually heard.

| Run (`docs/benchmarks/m5-latency-*`) | Model turns: end of speech → voice starts | Parser turns | Whisper | First token | Prefill used |
|---|---|---|---|---|---|
| before the prefill fixes (`132205`) | p50 4485 ms (3411-5100) | 181, 1373 ms | p50 287 ms, max 1369 | p50 2208 ms | 0 of 6 |
| same, camera pipeline at 15 fps (`132353`) | p50 5876 ms (4000-6877) | 327, 1539 ms | p50 338 ms | p50 2390 ms | 0 of 6 |
| anchored prefill (`133503`) | **p50 1772 ms (1591-2856)** | 114, 174, 748 ms | p50 150 ms, max 746 | p50 277 ms | 4 of 5 |
| same, camera pipeline (`133620`) | p50 2431 ms (1899-3677) | 148, 196, 1461 ms | p50 149 ms, max 1458 | p50 332 ms | 5 of 5 |
| + Whisper GPU priority (`133909`) | p50 3270 ms (2643-4638) | 189, 261, 560 ms | p50 315 ms, max 554 | p50 666 ms | 5 of 5 |
| same, camera pipeline (`134033`) | p50 3309 ms (3157-3871) | 295, 375, 632 ms | p50 336 ms, max 663 | p50 529 ms | 5 of 5 |

**Final configuration** (prompt reuse, Whisper GPU priority off; commit `85c829b`), from
the interleaved A/B below, 2 runs × 5 model turns, swap at 7.7 GB:

| End of speech → the voice starts | p50 | p90 | range |
|---|---|---|---|
| Reply written by the model | **2.62 s** | 3.21 s | 1.56-3.25 s |
| Parser reply (time left, lock me in, give me my phone) | 0.19 s | | 0.14-0.81 s |
| Whisper (inside the above) | 0.17 s | | max 0.81 s |
| Model first token after the transcript | 0.27 s | | |

Add the Bluetooth speaker's own delay (JLab: 0.35-0.47 s, Milestone 4) for when it is heard:
roughly **3 s for a model-written reply, 0.5 s for a parser reply**, with this Mac swapping.

Whisper GPU priority A/B (same code, interleaved on/off/on/off, `m5-latency-silent-20261101-1413..1417`):

| | priority on | priority off |
|---|---|---|
| model turns p50 / p90 | 3.28 / 4.41 s | **2.62 / 3.21 s** |
| parser turns p50 / max | 229 / 932 ms | **194 / 811 ms** |
| Whisper p50 / max | 221 / 928 ms | **170 / 807 ms** |
| first token p50 | 419 ms | **272 ms** |

It is off by default (`LOCKEDIN_STT_GPU_PRIORITY=true` turns it back on). The slow short
command at the end of each run (~0.8 s) is Whisper sharing the GPU with the talk-time
prefill for a 1-second sentence; a next step is absolute clock times in the context, so the
prefill only reads the new lines (not done: it changes the prompt and needs a new evaluation).

What the earlier runs say:

- The fix that mattered: rendering each turn's context as of the moment the user started
  talking (and moving anything newer after the prefilled part). Before it, every turn after
  the first re-read the whole context after Whisper (first token ~2.2 s).
- With the anchor, a model-written reply starts **about 1.6-2.9 s after the user stops
  talking** (p50 1.8 s), 1.9-3.7 s with the camera pipeline running; add ~0.35-0.47 s of
  Bluetooth before it is heard. A parser reply (time left, lock me in, give me my phone)
  starts in 0.1-0.2 s typically, plus the speaker.
- Run-to-run variance on this Mac is large: swap grew from 7.4 to 7.9 GB across these
  runs, and Kokoro's render of the first sentence varied from 0.25 to 1.46 s.
- Whisper GPU priority (the model pauses its prefill while Whisper runs) looked useful for
  the worst Whisper time on short commands; the interleaved A/B above showed it slower in
  every measure, so it is off.
- The conversation evaluation measures the model part alone (transcript → first sentence
  handed to the voice): p50 1.21 s, p90 1.89 s over 58 turns.

## 20-minute software soak (`scripts/soak_companion.py`, `m5-soak-20.0min-20261101-140925.json`)

The real models for 20 minutes inside the real runtime: a sentence every 25-50 s (15
natural lines, three macOS voices, through whisper.cpp), a perception event every 40-70 s
(confirmed reach, possible reach, camera blip), silent output, memory sampled each minute.
Not the live soak (H8): no person, no robot camera, no real room.

| | Result |
|---|---|
| Turns | 27 (23 by the model, 4 by the parser), **0 unanswered, 0 errors**, 0 motion frames |
| Model turns, end of speech → voice | p50 3.59 s, p90 6.73 s, max 7.6 s (`run_report.py` on the run's telemetry) |
| Parser turns | p50 0.43 s |
| Whisper | p50 452 ms, max 787 ms |
| Model | 58 requests, 5 failed (4 were retries that ran out of time), prefill used 23 of 27 |
| Decisions | 9 confirmed reaches answered; possible reaches 2 spoken / 4 silent; 5 camera blips silent |
| Actions | extend ×2, goal, style, quiet: all executed |
| Memory | no growth (RSS fell 401 MB while the Mac swapped) |

**Second soak, final code** (`85c829b`; `m5-soak-20.0min-20261101-144133.json`, telemetry
`run-20261101-142124.jsonl` through `run_report.py`):

| | Soak 1 (`27ea1fa`+) | Soak 2 (final) |
|---|---|---|
| Turns, unanswered, errors, motion frames | 27, 0, 0, 0 | 27, 0, 0, 0 |
| Model turns, end of speech → voice (all 24, from telemetry) | p50 3.59 s, p90 6.73 s, max 7.6 s | **p50 3.62 s, p90 4.67 s, max 5.8 s** |
| Model request failures | 5 (4 retry timeouts) | **0** (3 retries, all in time) |
| Model time per reply | p50 2.29 s, p90 3.85 s | p50 1.82 s, p90 3.14 s |
| Whisper | p50 452 ms | p50 254 ms, p90 975 ms |
| Parser turns | p50 0.43 s | p50 0.83 s, max 1.14 s |
| Event-loop stalls | 27 (harness) | 0 |
| Decisions | 9 reaches answered; possible 2/4; blips silent | 9 of 10 reaches answered (1 in quiet mode); possible 2/4; blips silent |

Reading it honestly: late in a session the typical model reply still takes ~3.6 s to start
(the context is larger than in the 8-turn latency check: the last prefill read 462 of 501
tokens, because the clock and "ago" labels near the top change every turn, which is the
absolute-times next step above), but the slow tail and the failures are gone. The soak
script's own summary (p50 2.67 s, 15 turns) undercounted slow turns: it stopped waiting at
6 s; fixed (15 s), and the table uses the telemetry.

What the first soak found (this is why it ran): over a session the context grew to ~550 tokens, so the
prefill took up to 3.4 s, no longer finished while the user was talking, and a retry had to
read the whole context again. Fixed after the soak: the worker now keeps the last prompt and
reads only what changed (a retry or the next turn reuses the rest; verified to give the same
greedy reply), larger prefill chunks, a tighter context (8 observations, 3 older statements,
3 recent lines). The soak's "27 loop stalls" came from the soak harness itself (it called the
macOS voice on the event loop; fixed in the harness).

## Memory (16 GB Mac)

| Part | Measured | Source |
|---|---|---|
| Language model (worker process) | 2.44 GB GPU memory active, 2.95 GB peak; 50-300 MB RSS | evaluation runs, `m5-contention` |
| Kokoro int8 (in the backend) | +316 MB | `m5-tts` |
| whisper.cpp base.en (STT worker) | 148 MB weights + Metal buffers | file size; not measured separately |
| Qwen3-1.7B instead | 1.11 GB active, 1.66 GB peak | `m5-eval-Qwen3-1.7B` |

macOS RSS undercounts here: with 6-8 GB of swap in use, parts of every process were
compressed or swapped, which is also why render times varied. For the demo, quit other
apps so the ~3.5 GB LOCKED IN needs stays in memory.

## Offline (`scripts/offline_check.py`, 2026-11-01 ~14:20, home Wi-Fi)

READY: 0 missing, 1 warning (macOS output muted). Run in a macOS sandbox that blocks every
network connection except this machine:

| Probe | Result |
|---|---|
| backend | served health, state and a session; 0 outbound attempts |
| whisper.cpp | heard "Lock me in for 25 minutes." (load 471 ms, transcribe 91 ms) |
| language model + Kokoro | Qwen3-4B wrote "Again? You're already reaching for the phone like it's a calculator, not a distraction." (load 6.8 s, first token 1.48 s, reply 2.45 s from cold); Kokoro rendered it in 955 ms (4.8 s of speech) |
| models on disk | whisper.cpp, Qwen3-4B (2.26 GB), Kokoro int8, Silero VAD, MediaPipe, faster-whisper |
| Low Power Mode | off |

## Who speaks what (audio routing)

| Line | Voice | Where it comes from |
|---|---|---|
| Fixed lines with a cached Fish Audio clip (39) | Fish Audio E-girl | disk cache, ~10 ms to start |
| Other fixed lines (~50, e.g. "Camera's back.") | Kokoro `af_bella`, local | rendered at startup, kept on disk |
| "N minutes left." | Kokoro | rendered ahead every 10 s |
| Replies the model writes, reactions it prepared | Kokoro | rendered when needed (prepared lines ahead of time); kept in memory only |
| Kokoro unavailable | macOS voice | last resort |
| Any generated line | never Fish Audio live | they can contain what the user said; no external AI calls |

All of it goes through PortAudio to the selected output device (`make hardware
SPEAKER="JLab GO Party"`). The dashboard labels every line with the voice that actually
spoke it; Kokoro is **not** the Fish Audio voice.

## Dashboard check (browser, 2026-11-01 13:44)

A backend on port 8079 with a fresh data folder (camera and microphone off, speech text-only,
simulated robot, offline, the real local model): a typed "I have a calculus test tomorrow and
I keep checking my phone, give me like forty minutes" started a 40-minute session with the
goal "calculus test prep"; a simulated reach (labelled SIMULATED) got a reaction; "why do you
keep doing that, seriously" was answered and recorded as an objection; "can you be quiet for
a bit" set quiet mode; a second simulated reach was **silent** ("Quiet mode: noted on the
dashboard, not said aloud"). The Companion panel showed all of it: goal, bounds, observed vs
said, the four decisions with their rules, the two executed actions with their arguments,
and the replies with first-token and total times. Found and fixed: the reply list showed
"set_quiet undefined" (the resolved action was overwritten by the request).

## Known limitations (measured, not fixed)

- **Indirect requests are sometimes missed.** Held-out actions: 75% (a bathroom break did not
  pause; "let's call it here" did not end). An explicit word always works ("pause", "I'm done",
  "give me my phone").
- **Teasing can sound like refusing.** "You're not getting it back until…": the phone is
  always released on an explicit request (parser), but the words can say otherwise.
- **Numbers get invented** (a year, "100", "10 minutes"): the guards refuse them, which costs a
  retry (~1-2 s) or leaves a shorter reply.
- **A model-written reply takes ~2.6 s early in a session and ~3.6 s later** (p50, end of
  speech to the voice starting; latency check and soak 2), plus the Bluetooth speaker's
  ~0.4 s, more with the camera pipeline running and the Mac swapping. Parser replies start in
  ~0.2-0.8 s. Low Power Mode makes the model ~4x slower (`make offline-check` warns).
- **The local voice is not the Fish Audio voice**, and every line says which voice spoke it.
- **Hands-free still hears other people.** Silero VAD ignores clicks and hums, not
  conversation; push-to-talk stays the default.
- **No motion.** Every physical request is refused (motor power unverified, patch v2 not
  flashed, tracking unverified). LED requests are refused: the factory firmware has none.
- **Distribution:** the Kokoro path loads GPL-3.0-or-later components (MODELS.md).

## Reproduce

```bash
cd backend
.venv/bin/python -m pytest -q                                    # unit + integration tests (no 2.3 GB model)
LOCKEDIN_TEST_LOCAL_LLM=1 .venv/bin/python -m pytest -q tests/test_local_models.py   # the real models
.venv/bin/python scripts/conversation_eval.py --reps 2           # ~5 min, writes docs/benchmarks/m5-eval-*
.venv/bin/python scripts/latency_check.py [--vision-load]        # ~2 min each, silent
.venv/bin/python scripts/soak_companion.py --minutes 20          # software soak, silent
.venv/bin/python scripts/offline_check.py                        # incl. the sandboxed model + Kokoro probe
.venv/bin/python scripts/bench_local_ai.py llm|tts|stt|vad|contention
```

## Human and hardware checklist (not done by software; record results here)

Each line needs a person, the speaker, the camera or the robot. Mark it with the date, who
did it and what happened. Software results above do not count for any of these.

| # | Check | How | Pass when | Result |
|---|---|---|---|---|
| H1 | Machine ready | Low Power Mode **off** (`pmset -g \| grep lowpowermode` → 0); quit heavy apps (swap was at 6-8 GB during these measurements); macOS output **unmuted**; JLab connected; `make offline-check` on home Wi-Fi | READY; "conversation" probe OK; no muted-output warning | pending |
| H2 | Voice heard on the Bluetooth speaker | `make hardware SPEAKER="JLab GO Party"` (or `latency_check.py --audible`); say "Lock me in for 25 minutes" (cached Fish clip), then "I have a calculus test tomorrow" (Kokoro, generated) | **a person** confirms both are heard on the JLab and says which voice is which | pending |
| H3 | Kokoro voice choice | `scripts/voice_samples.py`, listen to `data/voice_samples/` | a person picks the voice; int8 judged not worse than fp32 (else set `LOCKEDIN_KOKORO_MODEL=kokoro-v1.0.onnx`) | pending |
| H4 | A person talks to it (push-to-talk) | the conversational demo (DEMO_SCRIPT.md) plus 10 sentences of their own, unscripted | per turn: heard right / right action / reply in context / felt fast enough (1-5); the person's own judgment | pending |
| H5 | Proactive behaviour with a real camera | camera live, zone calibrated; reach (1) → reaction about the goal; reach (2) → escalation; "why do you keep doing that?"; "be quiet for a bit"; reach (3) → silence; a hand passing near the phone without reaching → no accusation | each matches; the Companion panel shows the decision and its rules | pending |
| H6 | Interruptions and corrections | press push-to-talk while it talks; "actually make it thirty"; "no, the other assignment" | it stops at once; the correction is applied | pending |
| H7 | Hands-free in the room (optional) | wake-word mode, Silero VAD; 5 min of nearby conversation, then "Locked in, how long is left?" | no reply to the room; the addressed question answered; count false triggers | pending |
| H8 | 20-minute live soak on the robot's Wi-Fi | `make hardware`, robot camera, JLab, a person working and talking normally; afterwards `scripts/run_report.py` | no crash, no growth in memory, replies keep their speed, decisions sensible | pending |
| H9 | The robot never moves | during H5-H8 | every physical request shows refused; nothing moved | pending |
| H10 | Human evaluation | at least 2 people who did not build it use it for 5 minutes each and rate naturalness, helpfulness, annoyance (1-5) | recorded; not replaced by the automated evaluation | pending |

## Commits (branch `milestone-2`, local)

| Commit | What |
|---|---|
| `f12a6fc` | candidates measured on this Mac; pinned downloads; `local-ai` extra |
| `d44d7e3` | companion layer: context, local model worker, typed actions, guards, behaviour engine; whisper.cpp, Silero VAD, Kokoro |
| `a189d81` | fixes from the first evaluation round |
| `ad4be1e` | dashboard Companion panel and conversation settings |
| `27ea1fa` | end-to-end latency and honesty fixes; evaluation, latency and soak tools (the final evaluation round ran on this code) |
| `85c829b` | prompt reuse in the worker; Whisper GPU priority off after the A/B |
| (docs) | this file, MODELS.md, MILESTONE5_EVALUATION.md, README, SAFETY, OFFLINE, ARCHITECTURE, DEMO_SCRIPT, VOICE_PERSONALITY, TASKS |
