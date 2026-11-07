# Milestone 4 verification (2026-11-01)

What was checked, how, and what happened. Status keys: **PASS** verified with evidence ·
**SIMULATED** verified with a stand-in · **BLOCKED** a prerequisite is unavailable · **FAIL**
tested and failed · **NOT TESTED** not executed. Where a row says "real", it names the device.

Real devices used: MacBook Air microphone (input), MacBook Air speakers (played the stand-in
user voice), JLab GO Party Bluetooth speaker (the robot's voice), the Zeus Car on its own Wi-Fi
in one earlier session (link, ESP32 camera). Not available: Beats Pill (paired, not connected);
the robot's network during this work (joining it cuts this Mac's internet). **No motor command
was sent and no firmware was flashed.**

Evidence files: `data/voice_tests/*.json` (+ the 05:42 recording `.wav`),
`data/telemetry/run-20261101-044240.jsonl` (all git-ignored, on this Mac), the test suite.

## Acceptance matrix

### 1. Voice pipeline (mic → offline STT → intent → personality/state → speech → action)

| Requirement | Implementation location | Test performed | Actual result | Evidence | Status |
|---|---|---|---|---|---|
| Works with no internet | `scripts/offline_check.py` (`offline_speech`, `offline_start`), sandbox profile in `scripts/demo_check.py` | Whisper on a spoken command in a macOS sandbox that blocks every non-local connection; whole app in the same sandbox for 11 commands | heard "Lock me in for 25 minutes." (load 979 ms, transcribe 1180 ms); app ran 11/11 commands, 0 network attempts logged | `make offline-check` output; `demo-check-20261101-054250.json` | PASS |
| Real local speech recognition, verified offline | `app/voice/transcription.py` (faster-whisper base.en, separate process), `app/voice/service.py` | as above; plus real Whisper through `VoiceService` into the runtime | exact transcripts for all 11 demo commands; STT 615-1075 ms (p50 798) | `test_voice_pipeline.py::test_real_whisper_hears_a_spoken_command_and_the_robot_answers`; demo JSON | PASS |
| Push-to-talk and existing mic controls | `VoiceService.ptt_start/ptt_stop`, `/api/voice/ptt` (route bug fixed: it awaited a sync method), Space / Hold to talk | demo harness pressed PTT over HTTP like the dashboard button, spoke through the MacBook speakers into the MacBook mic | 11/11 utterances captured and transcribed | demo JSON | PASS (synthetic voice) |
| Natural phrases, not only scripts | `app/voice/intents.py` (start_session_natural, roast_me, about, hold on, time check, shush, …) | 25 phrase cases; real mic runs | all 25 classified right; real mic 9/10 with the speaker next to the mic ("How much longer?" heard as nothing); "help me focus" was the stuck-help intent: **bug found and fixed** | `test_intents.py`; `voice-loop-20261101-052030.json` | PASS |
| Live listening status, transcript, intent, response in the dashboard | `VoicePanel.tsx` (`VoiceLoop`), `runtime.voice_state()`, `voice_turns`, `VOICE_TURN` event | dashboard opened against a backend connected to the robot stand-in; typed and voice turns | card shows state, heard text + STT ms, intent + confidence, action, reply + source + latency | browser check (screenshots in this session); `test_voice_loop.py` | PASS (UI) |
| Stop / pause / mute / release / e-stop beat dialogue | speaker priorities (P0 safety lines), barge-in intents during playback, `STOP` cancels demo task + speech | e-stop while a long line plays; release over a joke; stop right after "Stopped." | e-stop latched mid-line; release ends session; user's "stop" obeyed | `test_voice_loop.py`, `test_voice_runtime.py` | PASS |
| AI text never authorizes movement | `_llm_reply` (text only, guarded); LLM off offline | LLM reply containing motion words | no motion request | `test_voice_runtime.py::test_llm_text_never_moves_the_robot_and_obeys_the_style` | PASS |
| Unclear / low-confidence / conflicting motion commands rejected | `_utterance_gate` (`low_confidence`, `motion_confidence`, `conflict`), `movement_conflict` | "come back, no, go left", "don't come back", low confidence | rejected, nothing moved, asked to repeat | `test_intents.py`, `test_voice_loop.py` | PASS |
| Does not transcribe/obey its own speech | `echo_split` (share + coverage), thresholds 0.6 during playback / 0.8 after | real JLab echo run, then the exact transcript as a regression test | **first real run FAILED** (score 0.56 on "I found one too. Emergency stop. I'm not moving."); fixed (score 1.0, ignored); re-run heard nothing | `voice-loop-20261101-051425.json`, `test_voice_loop.py` | FAIL → fixed (software PASS; physical re-test inconclusive) |
| Clear listening vs speaking state | `voice_state()` off/ready/hearing/transcribing/speaking | state during a busy transcription | "transcribing" while Whisper works | `test_voice_pipeline.py` | PASS |
| Hands-free fallback to dependable PTT | listen mode setting; hands-free never answers unrecognised speech | real mic in a room with people talking | hands-free 2/10 (room speech transcribed); PTT 11/11 | `voice-loop-20261101-051804.json`; demo JSON | PASS for PTT; hands-free FAIL in a talking room (documented) |
| No LLM / Fish API calls in the offline demo | offline mode; keys blanked in tests and the demo harness; cached clips play without a key (**bug fixed**) | sandboxed demo with no key | every reply from cache; `fish_key_configured: false` | demo JSON | PASS |

### 2. Personality

| Requirement | Implementation location | Test performed | Actual result | Evidence | Status |
|---|---|---|---|---|---|
| Starting a session | `session_start` lines | real app, mic | "Okay. Phone's mine now. Go." (Cached Fish Audio) | demo JSON | PASS |
| Staying focused | `_evaluate_focus_streak` (10 min focused, camera-confirmed) | forced focus state | praised once; no praise without a camera | `test_voice_loop.py` | SIMULATED |
| Reaching toward the phone | `_maybe_warn_reach` on `approaching`, reach lines on confirmation | events through `handle_vision_event` | one warning per 20 s; words only | `test_voice_pipeline.py`, `test_voice_loop.py`; real ESP32 detected 4 approaches but no session ran | SIMULATED (detection PASS on the robot camera) |
| Repeated distraction | `reach_repeat*` categories, policy cooldown | existing policy/personality tests | pass | `test_policy.py`, `test_personality.py` | SIMULATED |
| Questions | `about`, `status`, `time_left` (rendered ahead) | real app: "How much time do I have left?" | "25 minutes left." in 0.91 s | demo JSON | PASS |
| Pause / resume | `pause`, `resume` lines | real app | "Paused. Don't get comfortable." / "Oh, you're back. Good." | demo JSON | PASS |
| Ending the session | `end_session` phrases ("I'm done", "that is enough", "stop the timer") | tests | the session ends | `test_intents.py`, `test_voice_loop.py` | SIMULATED |
| Phone back | `release` | real app | "Fine. Robot privileges revoked."; session ended; nothing drove | demo JSON | PASS |
| Robot connection loss | `robot_lost` / `robot_back` | existing tests | lines only when it happens | `test_voice_runtime.py` | SIMULATED (the real loss at 04:42 had no session) |
| Emergency stop | `estop_ack` | real app | "Emergency stop. I'm not moving."; state EMERGENCY_STOP | demo JSON | PASS |
| Cached speech for immediate reactions | Fish cache (39 clips) + local voice prerendered (72 lines) + rendered ahead | 05:42 demo | every reply from cache, 0.84-1.34 s after speech end | demo JSON | PASS |
| Personality + profanity controls kept | unchanged settings path | existing suite | pass | `test_voice_runtime.py` | PASS |
| Prioritization, interruption, cooldowns | `Speaker`, reach warning cooldown, duplicate window | tests | pass | `test_speaker.py`, `test_voice_loop.py` | PASS |
| Measured voice latency | `voice_turns` (`reply_to_audio_ms`, `output_latency_ms`), `perf` | real app | see Measurements | demo JSON | PASS |
| Mac speech never labelled as the Fish voice | `SOURCE_LABELS`: "Cached Fish Audio" vs "Local voice (cached)" / "Local voice" | real app | both labels appear per reply | demo JSON | PASS |

### 3. Audio from the robot's speaker

| Requirement | Implementation location | Test performed | Actual result | Evidence | Status |
|---|---|---|---|---|---|
| Select MacBook speakers or Bluetooth output | settings `voice.output_device`, `make hardware SPEAKER=` (`LOCKEDIN_AUDIO_OUTPUT`) | JLab selected | replies played on the JLab | demo JSON (`output: JLab GO Party`) | PASS |
| Beats Pill | same | — | paired, not connected | — | BLOCKED |
| Show device + real output status | `audio_output_status()` (device list, macOS volume/mute of the default output, last source, device latency), `VoiceLoop` card, `offline_check` | macOS output was muted during the 05:42 run | dashboard and offline-check warn "muted in macOS" | browser check; offline-check output; `test_local_voice.py` | PASS |
| A2 playback through the Bluetooth speaker | — | cached "Nice try." on the JLab, MacBook mic listening | mic heard "Nice try." exactly, -41.8 dBFS, 346 ms acoustic delay | `voice-loop-20261101-051403.json` | PASS (observed by the mic; **you have not confirmed hearing it**) |
| A6 echo | see section 1 | | | | FAIL → fixed |
| A7 Bluetooth disconnect/recovery | `AudioDeviceError` → text only for 15 s, retry | software test | pass | `test_speaker.py` | NOT TESTED physically (needs someone to switch the speaker off) |

### 4. Voice → robot behaviour

| Requirement | Implementation location | Test performed | Actual result | Evidence | Status |
|---|---|---|---|---|---|
| One pipeline: intent → request → state machine → safety → transport → result | `_dispatch_intent` → `SafetyController.request/evaluate` → transport; action log (`_note_action`) | tests with the simulator and the Zeus stand-in | denials carry reason codes; log shows requested/denied/sent/ended | `test_voice_loop.py`, `test_link_test.py` | SIMULATED |
| "Lock me in": session + monitoring + speech | `_ensure_monitoring` (spoken start only) | real app + test | session started, spoke; camera start requested | demo JSON; `test_voice_loop.py` | PASS (session, speech) / SIMULATED (camera start) |
| "Roast me" | `roast_me` slot → personality roast + `roast_me` line | real app | "You asked for it. Roast mode is on." | demo JSON | PASS |
| "Stop": cancel, suppress, stop if a safe transport exists | `STOP_MOTION` dispatch, `suppress_motion(15 s)` | real app (no robot) + tests | stop recorded, 15 s suppression shown, later motion denied `motion_suppressed` | demo JSON; `test_voice_loop.py` | PASS (app) / SIMULATED (robot stop) |
| "Give me my phone back" never drives to the user | `release` stops and enters RELEASED; `return_home` refused on the real robot until calibrated | real app + stand-in | session ended, no motion; return refused `return_uncalibrated` | demo JSON; `test_link_test.py` | PASS / SIMULATED |
| Reach: words now, retreat only when all checks pass | policy R4/R5 (voice only when disarmed/autonomy off), safety gates | tests | voice only, no request while disarmed | `test_voice_pipeline.py`, `test_policy.py` | SIMULATED |
| LLM never produces motor commands | as section 1 | | | | PASS |

### 5. Firmware patch v2 and motion preparation

| Requirement | Implementation location | Test performed | Actual result | Evidence | Status |
|---|---|---|---|---|---|
| Device-side expiry (400 ms) / deadman | `firmware/optional/locked_in_patch.h` | native harness on the real header | runs at 10 Hz never trip; stop at 401 ms of silence, not 400; one trip per stall | `firmware/test/patch_harness.cpp`, `test_firmware_patch.py` | SIMULATED (logic) |
| No stale command replay | latch until a Stop (F) line | harness | delayed frames after a trip ignored; Stop clears; driving resumes after Stop | same | SIMULATED |
| Stop priority | stock F handling + latch release | harness + source | Stop always runs the stock stop path | same | SIMULATED |
| No movement after startup/reset | stock `currentMode = MODE_NONE` at boot (`cmd_code_config.hpp` line 36 at `492912f`); host never arms after (re)connect | source review; M3 tests | no frame moves the car until armed and commanded | `test_zeus_monitor.py`, `test_hardware_mode.py` | SIMULATED |
| No unintended motion after reconnect | supervisor never arms; motor power check cleared on link drop | tests | disarmed after reconnect; power check cleared | `test_link_test.py` | SIMULATED |
| E-stop and link loss | host latch; firmware expiry | tests + harness | — | as above | SIMULATED |
| Reject commands without the right marker | `SUPPORTED_PATCH_VERSIONS = {2}` | stand-in reporting `li: 3` | motion locked, arm refused, reason shown | `test_link_test.py` | SIMULATED |
| The harness can fail | — | three broken copies of the header (`>=`, no latch, Stop not clearing) | each caught (1, 7, 3 failed checks) | this session | PASS |
| Builds | — | arduino-cli compile (Milestone 3) | 21,744 B flash, 1,516 B RAM, no new warnings | `firmware/README.md` | PASS (compile only) |
| Not flashed; motor lock on by default | `LOCKEDIN_ZEUS_MOTOR_COMMANDS=disabled` default | — | nothing flashed, no motor command sent | git history; config | PASS |
| Battery / motor power is a separate prerequisite | operator motor-power check in `SafetyController` (arm + evaluate), dashboard checklist | tests; browser check with the stand-in | arm refused and motion denied `motor_power_unchecked` until confirmed; expires 30 min; cleared on link drop | `test_safety.py`, `test_link_test.py` | PASS (software); physical battery check BLOCKED (needs you) |

### 6. Physical reaction states

| Requirement | Implementation location | Test performed | Actual result | Evidence | Status |
|---|---|---|---|---|---|
| Idle / focus / possible reach / confirmed / hold / release / E-STOP | state machine + policy (unchanged), reach warning | state machine and policy suites | pass | `test_state_machine.py`, `test_policy.py` | SIMULATED |
| Retreat in small bounded steps | profiles + device expiry | existing safety tests | bounded duration/speed | `test_safety.py` | SIMULATED |
| Return only with a calibrated procedure | `zeus_return_calibrated` gate | stand-in | refused | `test_link_test.py` | SIMULATED |
| Fail closed on camera/tracking/network/marker loss | `_perception_check`, link-loss disarm | tests | each case denies | `test_voice_loop.py`, M3 suites | SIMULATED |
| No autonomous retreat until tracking is demonstrated | `zeus_tracking_verified` default false | test | denied with the reason | `test_voice_loop.py` | PASS (gate) |

### 7. Dashboard

| Requirement | Implementation location | Test performed | Actual result | Evidence | Status |
|---|---|---|---|---|---|
| Transcript, intent, speaking/listening, reaction played, output device | `VoicePanel.tsx` | browser, backend on the stand-in, muted voice | all shown; muted-speaker warning shown | browser check | PASS |
| Camera / robot connection, motor lock + firmware verification | `RobotPanel.tsx` gate card (+ motor power row, autonomy checks) | browser with the stand-in (patch v2 reported) | gate lists every lock; "Confirm motor power" flips bench motion | browser check | SIMULATED (stand-in robot) |
| Pending and executed actions, physical vs simulated, denial reasons | `ActionLog`, `motion_suppressed` note, jog-disabled reason | browser | shows "stop · typed · stop sent" with "not measured (no wheel feedback)"; **a "real robot" tag that the stand-in also got was removed**; jog note now names the real lock | browser check | PASS |
| Never claims a physical action without evidence | wording "sent" / "no wheel feedback" | review + tests | — | `test_voice_loop.py` | PASS |

### 8. Offline and robot

| Requirement | Implementation location | Test performed | Actual result | Evidence | Status |
|---|---|---|---|---|---|
| `make hardware` starts everything | `Makefile` (`VOICE_AUTOSTART`, `SPEAKER`) | `make -n`; the demo harness starts the backend with the same variables | voice, camera, Zeus link, offline | dry run; demo JSON | PASS on this Mac; NOT TESTED on the robot network since voice was added |
| Camera from the ESP32, not the webcam | `LOCKEDIN_CAMERA_SOURCE=zeus` | real session | `ZEUS ESP32-CAM` frames at +1.2 s | telemetry 04:42 | PASS (real) |
| Hardware vs simulation distinguished; real link reported | M3 | real session | `simulated: false`, `ws://192.168.4.1:8765` | telemetry | PASS (real) |
| Starts disarmed | M3 | real session + demo | `armed: false`, `motion_permitted: false` | telemetry; demo JSON | PASS |
| Network loss: no freeze, no false success | M3 supervisor | real Wi-Fi switch | camera and robot reported lost at +70-71 s, 31 retries, clean exit | telemetry | PASS (real) |
| No API key used or exposed in tests | conftest blanks keys; `.env` untracked | suite | — | `test_voice_runtime.py::test_tests_never_see_the_developers_real_key` | PASS |

### 9. Edge cases (automated, real entry points where possible)

| Case | Test | Status |
|---|---|---|
| Mic will not open | `test_a_microphone_that_will_not_open_is_reported_not_fatal` | PASS |
| No speech / Whisper hallucination | `test_silence_or_a_whisper_hallucination_is_not_a_command` | PASS |
| Ambiguous / unsupported | `test_hands_free_mic_does_not_answer_what_it_does_not_understand`, conflict tests | PASS |
| Duplicate command | `test_the_same_command_twice_does_one_thing` | PASS |
| Speaking over another line; e-stop while speaking | `test_e_stop_while_speaking_cuts_the_line`, `test_speaker.py` | PASS |
| Bluetooth disconnect | `test_speaker.py` (device error → text, retry) | PASS (software) |
| Camera / robot disconnect | M3 suites; real session | PASS |
| Missing cached line | `test_fish_audio.py` (local voice, labelled) | PASS |
| Offline startup | `test_offline.py`, offline-check | PASS |
| Repeated start/stop | `test_repeated_start_and_stop_stay_consistent` | PASS |
| Shutdown during transcription | `test_shutdown_while_whisper_is_busy_returns_promptly` | PASS |
| Long-running stability | `scripts/soak_demo.py` exists; not run this milestone | NOT TESTED |

### 10. The full demo sequence on the robot

| Step | Status |
|---|---|
| One command start on `Zeus_Car` Wi-Fi with voice | NOT TESTED (needs you on the robot network) |
| Robot camera connects | PASS (04:42 session) |
| Speak → recognised → dashboard → spoken reply on the speaker | PASS on this Mac, offline, robot not connected (05:42 app run; audible playback of that run unverified: speaker muted) |
| Camera sees a reach → verbal reaction | SIMULATED (detection PASS on the robot camera, without a session) |
| Robot stays still | PASS (monitor link: no motor frames exist on it) |

## Measurements (real, this Mac)

| Measure | Value | Source |
|---|---|---|
| Whisper base.en load | 979 ms (sandboxed), 1602 ms (earlier) | offline-check; Milestone 4 log |
| STT per command (PTT, real mic) | 615-1075 ms, p50 798 ms (n=11) | demo 05:42 |
| End of speech → reply starts playing (backend clock) | 838-1342 ms, p50 1010 ms (n=10), all from cache | demo 05:42 |
| Same, before the latency fix (local voice rendered on demand) | 3851 and 5676 ms | demo 05:32 |
| `say` render, idle / with one render in parallel | 1.4-2.1 s / ~2.5 s | this session |
| Bluetooth: playback start → sound at the mic | 346 ms (device reports 466 ms) | A2 05:14 |
| ⇒ end of speech → audible on the JLab | about 1.2-1.8 s (sum of the two rows above) | derived |
| Backend start / stop (sandboxed) | 3.3 s / 1.3 s | demo 05:42 |

## Tests

Backend: **457 passed** (`pytest -q`, 118 s), was 385 at the start of the milestone. Ruff clean.
Frontend: `tsc -b` and `vite build` pass. New suites: `test_voice_loop.py`, `test_voice_pipeline.py`,
`test_local_voice.py`, `test_firmware_patch.py`; extended: `test_intents.py`, `test_safety.py`,
`test_link_test.py`, `test_fish_audio.py`. Tests changed because of new safety requirements: two
real-robot tests in `test_safety.py` and one in `test_link_test.py` now confirm motor power first
(their original assertions are unchanged; new tests check the refusal without it).

## Defects found during verification (fixed)

1. Echo: the robot's own e-stop line, padded by Whisper, scored 0.56 and would have been obeyed (real JLab run).
2. Cached Fish clips did not play without an API key (offline demo).
3. HTTP push-to-talk route awaited a synchronous method (failed on use).
4. Local-voice replies took 3.9-5.7 s (rendered on demand behind the background prewarm).
5. "Help me focus" was taken as "I'm stuck" instead of starting a session.
6. A muted macOS output silently swallowed every reply (05:42 run); nothing warned.
7. Dashboard: actions to the robot stand-in were tagged "real robot"; the stop's origin was missing; the jog note gave the monitor-link reason for a different lock.

## Still open

- Physical: Beats Pill (not connected); A7 disconnect; a person confirming the JLab playback;
  the demo sequence on the robot's network with voice; reach reactions on the robot camera
  during a session; motor battery check; everything in R1-R6.
- Hands-free listening is unreliable in a room with conversation (2/10): use push-to-talk.
- 56 personality lines have no Fish clip and use the local voice (labelled). Generating them
  is a Fish Audio request: needs your approval.
- Focus-streak praise and confirmed-reach reactions are only tested with simulated events.
- Long-run soak not repeated this milestone.

## Firmware patch v2: ready for your approval?

The logic is reviewed against the 1.4.5 source, compiles (21,744 B / 1,516 B), and passes the
native harness on the real header (expiry, latch, stale frames, wraparound, telemetry); the
host trusts only version 2. What no test here can show: the stop latency on the ATmega328P with
the motors running, and behaviour with the ESP32 relaying (that is R5, wheels off the ground).
Before flashing: charged motor battery confirmed, a flash backup taken, the rollback written down
(`firmware/README.md` → "Backup, flash, rollback").

## Physical acceptance checklist (for you)

1. Unmute the JLab (or connect the Beats Pill), `make offline-check` → READY, no audio warning.
2. Join `Zeus_Car`, `make hardware SPEAKER="JLab GO Party"`, open http://127.0.0.1:8077.
3. Confirm: Vision `LIVE · ZEUS ESP32-CAM`; Robot `ZEUS · MOTION LOCKED`; Voice card `READY`, `out · JLab GO Party`.
4. Hold Space: "Lock me in for 25 minutes" → you **hear** "Okay. Phone's mine now. Go." from the speaker; the card shows it as Cached Fish Audio. Tell me if you heard it.
5. Reach toward the phone in the robot camera's view → spoken warning / roast; "Why it acted" says voice only; the car does not move.
6. "Stop", "Give me my phone back", "Emergency stop" → each answered; E-STOP needs Reset.
7. Switch the speaker off mid-line (A7) → text only for 15 s, then sound again after switching it back on.
8. Motor battery: charge it; with the power switch on, both battery LEDs light. Only then think about R2/R5 (after approving the flash).
