# Tasks

Status as of 2026-11-01 (branch `milestone-2`). ✅ done and verified in software · 🟡 built, needs hardware/people to
verify · ⛔ blocked · ⬜ not started.

## P0

- ✅ Local backend + dashboard, live WebSocket updates
- ✅ Simulator (kinematics, latency, device watchdog, link loss) clearly labelled
- ✅ Explicit state machine (11 states, 18 triggers, guards, rejections reported)
- ✅ Safety-controlled motion: arming, e-stop latch, profiles, autonomy gates, watchdogs
- ✅ Focus timer with pause accounting, progress, auto-complete
- ✅ Manual E-STOP / STOP / RELEASE always available
- 🟡 Webcam hand detection: running on the live webcam at ~30 FPS (docs/PERFORMANCE.md);
  reach accuracy trials V1-V3 still to run
- 🟡 Phone-zone calibration: UI + persistence work; calibrate once the phone sits in the camera view
- 🟡 Phone zone follows the robot (ArUco markers 7/8): implemented, 32 tests on rendered marker
  frames; physical trials T1-T6 (docs/ROBOT_TRACKING.md) pending
- 🟡 Confirmed reach detection: logic tested on synthetic trajectories; needs trials V1-V3
- 🟡 Real Zeus Car integration (Milestone 3): `make hardware` (offline, ESP32 camera, monitor
  link, auto-reconnect, never arms); robot sketch 1.4.5 verified against source; dry-run on home
  Wi-Fi and against stand-ins; ⛔ not yet run on the Zeus_Car network (Z1-Z11,
  docs/REAL_HARDWARE_TESTS.md)
- ✅ Hardware motion gate: no motor command (not even Stop) on stock firmware; three locks, tested
- 🟡 ESP32-CAM as a camera source: reader with timeouts/reconnect/frame timing, robot-mounted
  perception (own zone, reach pause while moving); tested against a stand-in; Z2/Z3/Z9 pending
- ✅ Offline: readiness check (`make offline-check`: READY, 0 missing), offline mode verified with
  every non-local connection blocked (0 attempts)
- 🟡 Real stop + short movement: ⛔ needs patch v2 flashed (your approval), then bench mode, R2/R5
- 🟡 Working live demo: works in Modes B/C; on the real car after Z1-Z11 (no motion) and R1-R6

## P1

- ✅ Voice commands (deterministic intents, push-to-talk / wake word / always-on, echo suppression)
- 🟡 Speech recognition: Whisper working on this laptop (100 % on 63 synthetic utterances);
  real-speech accuracy test A1 pending
- ✅ TTS personality (chill / coach / chaos), cached lines for fast replies, mute
- 🟡 Focus-state observations (face presence + head-turn proxy): reports FOCUSED on the live
  webcam; distraction/away thresholds not yet validated with people
- ✅ Session history (JSONL telemetry + per-session summaries)
- ✅ Adaptive intervention rules with visible adaptation events
- 🟡 Firmware patch v2 (400 ms Arduino-side expiry + latch + telemetry): now **required** before any
  motor command; prepared for the robot's 1.4.5 sketch and 1.4.6, compile-checked (ArduinoJson
  6.21.5: 21,744 B flash, 1,516 B RAM); recognised on a monitor link (⛔ not flashed, never run)
- 🟡 R5 link-freeze test in the dashboard (debug drawer → R5 link test) and the R1-R6 procedure
  (docs/HARDWARE_TESTS.md); tested against firmware models only
- 🟡 Voice trials with real people: protocol (docs/VOICE_TESTS.md) and dashboard tool ready; not run
- ✅ Demo script repeatable (prepares state, waits out cooldowns); `make soak` passes (simulated)
- ✅ Vision profiled (docs/PERFORMANCE.md); no optimisation adopted, GPU delegate rejected
- ✅ Personality engine (chill / roast / opt-in unhinged / judge demo, profanity off/mild/strong,
  tones, help window), P0-P3 speech queue with interrupts, barge-in with echo filtering
- ✅ Fish Audio voice (E-girl, `s2.1-pro-free`) verified with the real API; 39-line offline voice
  pack cached; cached playback verified with Fish Audio unreachable (docs/VOICE_PERSONALITY.md)
- 🟡 Real speakers / microphones (docs/AUDIO_TESTS.md): only the laptop mic and speakers are
  connected; no Bluetooth speaker paired yet
- 🟡 Paid Fish Audio models (s2.1-pro, s2-pro, drama-3): not tested, account credit is 0
- ✅ Dashboard polish: loop trace, reach banner, state rail, "why it acted"
- ✅ Benchmarks for the software path (docs/PERFORMANCE.md); hardware rows pending

## Milestone 5: the companion (2026-11-01)

- ✅ Local AI components measured on this Mac and chosen (docs/MODELS.md): Qwen3-4B (MLX),
  whisper.cpp (Metal), Silero VAD, Kokoro int8; pinned downloads; everything offline
- ✅ Context engine: bounded memory, observations from events only, user statements kept apart
- ✅ Behaviour engine: speak / stay silent with recorded rules; possible reach ≠ distraction;
  escalation, quiet mode, complaints, camera-loss hysteresis
- ✅ Typed action requests; the safety controller decides; malicious model output moves nothing
- ✅ Dashboard: Companion panel (goal, observed vs said, decisions, actions, replies)
- ✅ Automated evaluation (35 scenarios, 8 held out) and end-to-end latency, silent output
  (docs/MILESTONE5_VERIFICATION.md)
- 🟡 Bluetooth playback of the new voice confirmed by a person (H2), voice choice by ear (H3)
- 🟡 A person talking to it; human evaluation (H4, H10)
- 🟡 Proactive behaviour with a real camera; 20-minute live soak on the robot's Wi-Fi (H5, H8)
- ⬜ Hands-free in a real room with Silero VAD (H7, optional; push-to-talk stays the default)

## Needs the team (in order)

1. Put the phone where the webcam can see it, calibrate the zone (Vision → Zone), and run V1-V3
   trials (dashboard → debug → Experiments).
2. Print markers 7 and 8 (Vision → Print marker), mount them on the cradle facing the camera,
   switch the zone to **Follows robot**, run T1-T6.
3. `make offline-check` on home Wi-Fi, join `Zeus_Car`, `make hardware`, run Z1-Z11
   (docs/REAL_HARDWARE_TESTS.md, no motion). Z1 records the camera firmware family (H1).
4. Decide whether to flash patch v2 on the 1.4.5 base (firmware/README.md, ArduinoJson 6.21.5).
   Without it LOCKED IN sends no motor command at all.
5. After flashing: gate shows the patch; `LOCKEDIN_ZEUS_MOTOR_COMMANDS=bench`; R2 and R5 on a
   stand (docs/HARDWARE_TESTS.md); `..._WATCHDOG_VERIFIED=true` and `..._MOTOR_COMMANDS=enabled`
   only if the car stops on its own every time; then R3, R4 on the floor.
6. R6 with the phone mounted; then enable autonomous retreat on the real car.
7. Voice trials (docs/VOICE_TESTS.md) at the demo table; A3 Bluetooth latency; confirm which
   Bluetooth speaker was bought.
8. Voice: done (key in `.env`, E-girl verified, voice pack cached). Listen to
   `data/voice_tests/*.wav` to confirm it is the voice you wanted; run the audio tests
   (docs/AUDIO_TESTS.md) once a Bluetooth speaker is connected.
   Start from docs/HARDWARE_HANDOFF.md when the car is assembled.
9. Re-check https://zeroday.hackutd.co/ for tracks; pick two using docs/HACKUTD_STRATEGY.md.
10. Rehearse docs/DEMO_SCRIPT.md in all three modes.
11. ✅ Git: backed up to the private GitHub repository `Dhruvjain35/locked-in` (`origin`,
    2026-11-01): `milestone-2` (default branch, full history) and `main` (Milestone 1). Model
    weights and `data/` (Fish Audio clips, telemetry, sessions) stay local; README → Setup.

## P2 (only after P0 is verified on hardware)

- ⬜ Hand-motion prediction (trigger on trajectory before the hand enters the zone)
- ⬜ Retreat direction chosen from the hand's approach side
- ⬜ Localisation for a real return-to-home (the robot markers already give an image-space pose;
  closing the loop on it is not built)
- ⬜ Accelerated inference (only if hardware is available; no CUDA on this laptop)
- ⬜ Contextual-bandit line selection once enough sessions are logged
