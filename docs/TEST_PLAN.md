# Test plan

## 1. Automated (software)

```bash
cd backend && uv run pytest -q          # 385 tests, ~100 s on an M4 MacBook Air
cd frontend && npm run build            # type-check + production build
```

| File | Tests | Covers |
|---|---|---|
| `test_state_machine.py` | 36 | every state accepts E-STOP, RELEASE overrides, valid demo path, invalid transitions rejected without side effects, guards (return needs displacement + link), reconnect restores context, explicit IGNORE entries |
| `test_sessions.py` | 11 | timer, pause/resume excluded from active time, auto-complete, progress bounds, focus integration only while counting, unavailable metrics, event ordering, slow-subscriber backpressure |
| `test_safety.py` | 18 | disarmed default, link required, state gating, clamping, zero/NaN values, profile restrictions, autonomy + cooldown, real-hardware gates, direction-specific obstacle blocking, ordered lifecycle events, stop preemption, e-stop latch, device watchdog on link loss, orphan + stall watchdogs, the real-robot motion gate (no arming or motion while locked, bench profiles only, no bench autonomy) |
| `test_robot_mock.py` | 10 | simulator kinematics per direction, rotation, own deadline, stop, frames lost while link down |
| `test_link_test.py` | 14 | patch v2 telemetry parsed; freeze writes nothing and keeps the socket open; patched-firmware model stops 0.39-0.48 s after the last frame, latches, Stop clears it; stock model keeps driving; stop during a freeze goes out at once; freeze cap ends the motion and never resumes it; Wi-Fi stall trips the watchdog and stale frames do not restart the wheels; R5 orchestration: preconditions, simulator with/without device watchdog, e-stop interrupts, against both firmware models |
| `test_demo_reliability.py` | 7 | reach during a retreat recorded but not counted/acted on; offline reach counts with voice fallback; no session / paused does nothing; demo twice in a row retreats both times; demo ends a running session; refuses during e-stop; 5-session soak (ids, seq, motion endings, counters, reconnect) |
| `test_personality.py` | 26 | every line's profanity tag matches the lexicon, no slurs/threats/humiliation in the library, insults tagged and unhinged-only, judge demo clean, the brief's example lines, mode caps on profanity, unhinged opt-in, legacy names never unhinged, insult pacing, help/gentle drop insults, serious tone, ordinals only for real counts, no invented numbers in any mode, cacheable lines, guard cases, LLM prompt follows the style, reactions mostly instant lines, no line repeats in a session until its pool is used up, cached lines preferred, voice pack small, grounded and covering every category at every profanity level, no generic assistant phrases |
| `test_speaker.py` | 13 | priority order, P0 cuts and clears, no joke after an interrupt, burst of reach jokes → one, answer cuts a joke, mute, duplicates, stale lines, device failure → text → recovery, push-to-talk barge-in keeps status, history sources, echo scoring, a comedic beat waits but a safety line does not |
| `test_fish_audio.py` | 18 | request matches docs.fish.audio, WAV parsing (streamed, float), error mapping without the key, unknown models refused locally, no-key / text-only fallback, cached clip with no network, cache-only never calls the API, live then cached, session details never on disk, network loss + backoff, safety lines never wait, budget, cache key covers voice/model/settings/text, S2-only cues, free voice lookups, cache job, key never in logs, API credit is a free GET |
| `test_voice_runtime.py` | 15 | voice commands for personality/profanity/tone, help drops insults, target reached celebrated, e-stop cancels jokes and never jokes, "give me my phone" cuts speech and stops, LLM text never moves the robot and obeys the style, robot ignores its own voice but takes barge-in, voice API config/presets/validation, personality caps and opt-in, paid calls need key + plan + confirmation, approved generation against the mock, the key never appears in any API response, a blocked retreat or a dropped robot/camera gets its own line only when it really happens, tests never see the developer's real key |
| `test_voice_trials.py` | 7 | prompt scoring (correct / wrong slots / wrong intent / rejected / nothing heard), dry run never acts but stop words still halt motion, execute mode, near-miss and quiet-window false activations, wake-word mode scored as configured, JSONL text only, REST API |
| `test_zeus_protocol.py` | 18 | frame encoding vs a firmware model written from source, cadence, legacy firmware never pinged, disconnect, hard cap, stock fail-safe gap, honest capabilities |
| `test_vision.py` | 16 | zone geometry, deliberate reach confirmed once, resting hand never repeats, pass-through ignored, hovering cleared, low confidence ignored, cooldown suppression then re-arm, static dwell, missing frames, two hands, tuning effect, focus glance vs sustained turn, pipeline plumbing on a generated video |
| `test_intents.py` | 42 | every command in the brief, slot extraction (duration, task, target units), progress units, stop-word flag, unknown/low confidence |
| `test_conversation.py` | 10 | grounded status/time/next-step answers, camera-off honesty, grounding guard, templates never render missing slots |
| `test_policy.py` | 13 | retreat request through safety, no session/paused, cooldown, intensity, voice-only fallbacks, holding never moves, line categories, mute, focus rules, adaptation rules |
| `test_voice.py` | 13 | VAD on synthetic audio (clicks, loud room), push-to-talk, echo suppression, silent-input permission warning, real Whisper round trip on synthesised speech, TTS cache, a microphone that goes quiet at the device is reported and recovers |
| `test_llm.py` | 4 | grounded LLM reply used, ungrounded reply discarded, safety commands never reach the LLM |
| `test_integration.py` | 16 | full loop event order, voice-only when disarmed, cooldown, release during retreat, low-confidence voice, stop word, unknown command, grounded status, disconnect mid-session + reconnect, e-stop, return home, timer completion, REST + WebSocket delivery |
| `test_robot_tracker.py` | 22 | marker frame round trip, detection on mirrored frames, ID/size/squareness filters, SEARCHING→TRACKING after 3 frames, HELD while stationary, LOST when moved unseen or hold expired, jump persistence, max step, two-marker fusion and disagreement, zone anchor attach/project |
| `test_tracking_pipeline.py` | 8 | rendered video with a moving marker: zone follows, reach paused while unknown, hand-speed re-basing (zone sliding toward a resting hand is not an approach), static mode unchanged, attach from the last frame, anchor persistence |
| `test_api.py` | 11 | health/state, session lifecycle over REST, typed commands, arming, validation, e-stop/reset, honest vision offline, simulation labels, printable marker PNG decodes, attach/detach |
| `test_zeus_monitor.py` | 13 | camera firmware families from the handshake (1.1-1.4.x, 1.5.2, 1.5.3+); the monitor link writes no region frame and never a text "ping" to single-client firmware, past every silence timeout, and stays open on 1.5.3+; move/stop write nothing; unexpected packets counted, never read as telemetry; a frozen link is declared lost without a TCP close; patch v2 recognised without sending a frame; the supervisor: robot unavailable shows ZEUS DISCONNECTED (never the simulator), a network switch connects on its own, drops are survived, reconnects never arm, switching to sim stops it; e-stop reported as a software stop |
| `test_camera_sources.py` | 12 | MJPEG parsing of the ESP32's chunked multipart stream (stand-in writing camera_server.cpp's bytes), decoded frames with measured FPS and the camera's clock, a stalled stream reconnects, a dropped one too, an unreachable camera retries with the reason and stops in < 1 s, wrong content type / 404 explained, corrupt frames skipped, frames lost in transit counted, OpenCV's FFmpeg backend reads the same stream, flip |
| `test_hardware_mode.py` | 7 | the `make hardware` profile against both stand-ins (robot link + ESP32 camera, motion locked, nothing sent), an unreachable Zeus camera is reported and never replaced by the webcam, switching cameras without a restart releases the old stream, the robot camera's own zone and no marker tracking, the zone API saves the zone of the camera in use, reach pauses while a robot-mounted camera moves, no face on the robot camera is unknown, not away |
| `test_offline.py` | 5 | the network guard blocks the internet and allows this machine; the backend serves health, state, sessions, e-stop and voice endpoints with every non-local connection blocked (0 attempts, key never in the state); offline voice plays cached clips and falls back to the local voice without a request; the readiness check finds missing models, internet assets in the build and an uninstalled voice |

Everything above runs without a robot, camera or microphone. Passing it says nothing
about physical behaviour.

## 2. Hardware tests (to run during assembly)

Record every result in [PERFORMANCE.md](PERFORMANCE.md) with date, conditions and n.

### Robot (gates autonomy; details in [SAFETY.md](SAFETY.md) §5)

R1 link idle 2 min · R2 directions with wheels off the ground · R3 minimum moving speed with
payload · R4 stop latency (10 trials) · R5 device stop on silent link loss · R6 compass with
phone mounted.

### Zeus network, no motion (Milestone 3)

Z1-Z11 in [REAL_HARDWARE_TESTS.md](REAL_HARDWARE_TESTS.md): start on the robot network, ESP32
camera, camera interruption, monitor link at idle, robot power cycle, network switch, E-STOP
as a software stop, offline voice, what the robot camera sees, Mac webcam alternative, clean
exit. The R tests above need the firmware patch first ([HARDWARE_HANDOFF.md](HARDWARE_HANDOFF.md)).

### Vision (precision / recall)

Use the dashboard's debug drawer → Experiments.

| ID | Procedure | n |
|---|---|---|
| V1 | Deliberate reach toward the phone from the right, left and above (start trial "reach", reach, end trial) | ≥ 15 |
| V2 | Non-reach movements near the phone: typing, picking up a pen next to it, waving, scratching the face, moving a book across | ≥ 10 |
| V3 | Repeat V1/V2 under a second lighting condition (lamp off / window light) | ≥ 10 |
| V4 | Laptop webcam vs ESP32-CAM stream as the source: FPS, latency, V1 recall | 10 each |

Report precision = TP/(TP+FP), recall = TP/(TP+FN), false-positive rate = FP/(FP+TN), and
time from reach start to confirmation (video timestamp of the hand starting to move vs the
`vision.phone_reach_confirmed` event).

### Robot tracking

T1-T6 in [ROBOT_TRACKING.md](ROBOT_TRACKING.md): detection rate at demo distance, zone stays on
the phone after a retreat, hand covering the marker (HELD), marker covered while the robot
moves (LOST), a decoy printout, lighting.

### Voice

Real-speech protocol, metrics, targets and the dashboard tool: [VOICE_TESTS.md](VOICE_TESTS.md).

| ID | Procedure | n |
|---|---|---|
| A1 | Voice trials (debug drawer → Experiments): full prompt list per speaker and condition, push-to-talk, then wake word | ≥ 2 speakers × 2 conditions × 22 |
| A2 | Quiet windows with normal conversation near the mic: false activations per 10 min | ≥ 4 min per condition |
| A3 | Bluetooth speaker as output: does the laptop mic still work, added latency | 5 |

### Session accounting

S1: 10-minute session with 2 pauses and 3 progress reports; compare the dashboard's active
time with a phone stopwatch (expect < 1 s difference) and the event counts with what
happened.
