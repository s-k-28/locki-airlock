# Safety design

A small robot that drives near people and desks, carrying someone's phone. The design
goal is not "never move", it is: **every motion is bounded, explainable, stoppable, and
refused whenever an input it depends on is unknown.**

## 1. Rules (enforced in code)

| Rule | Where |
|---|---|
| **Real robot: no motor command at all (motion or Stop) until the hardware gate opens** (section 7) | `ZeusRobotTransport` monitor link, `SafetyController.arm()` / `evaluate()` (`hardware_motion_locked`) |
| Disarmed at startup and after every disconnect, e-stop or link loss; a reconnect never arms | `SafetyController.armed`, `runtime._on_robot_disconnected`, Zeus link supervisor |
| STOP is always accepted and preempts any motion | `SafetyController.stop()` |
| Emergency stop latches; reset never re-arms | `emergency_stop()`, `reset_estop()` |
| The language model never reaches motion: it returns text only, and intents are matched by fixed rules | `voice/llm.py`, `voice/intents.py` |
| Any utterance containing *stop / freeze / halt / emergency* halts motion before interpretation, at any confidence | `runtime.handle_utterance` |
| Voice commands that would move the robot need STT confidence ≥ 0.80; anything below 0.45 is ignored | `VoiceTuning` |
| Every motion goes through one function with a typed `MotionRequest` | `SafetyController.request()` |
| Speed and duration are clamped to a named profile | `DEFAULT_PROFILES` |
| Autonomous retreat uses translation only (no rotation) | `SHORT_RETREAT.allowed` |
| Autonomous motion needs autonomy enabled + cooldown (default 10 s) | `evaluate()` |
| On real hardware, autonomous motion also needs a **verified** device-side stop and either fresh sensor coverage in the direction of travel or an operator's clear-path confirmation (valid 15 min) | `evaluate()`, checks `device_watchdog_unverified`, `path_unverified` |
| Fresh sensor data reporting an obstacle in the direction of travel blocks any motion; stale data blocks autonomous motion | `SENSOR_COVERAGE` |
| The transport stops on its own at `duration + 0.2 s` even if nobody calls stop | `ZeusRobotTransport._tx_loop`, mock deadline |
| A watchdog stops motion the safety layer is not tracking, motion that overran its deadline, and any motion during an event-loop stall > 250 ms | `safety/watchdog.py` |
| The phone is always retrievable: "give me my phone" stops the robot and ends the session; the robot never moves toward a person and never "chases" | `runtime.release()`, policy |

Every refusal is published as `robot.motion_blocked` with a reason code and the full
list of checks, so the dashboard shows exactly why the robot did not move.

## 2. Motion profiles (joystick units 0-100)

| Profile | Max speed | Max duration | Allowed | Used by |
|---|---|---|---|---|
| `LOW_SPEED_TEST` | 45 | 0.5 s | all | bench tests (dashboard "low-speed test profile") |
| `NORMAL_CONTROL` | 70 | 1.0 s | all | operator jog, return-to-home |
| `SHORT_RETREAT` | 60 | 0.8 s | translations only | autonomous retreat |
| `LINK_TEST` | 40 | 7.0 s | translations only | the R5 link-freeze test only (origin `test`; the safety layer refuses it for anything else) |

These numbers are placeholders until tests R3 and R4. Default retreat: backward, speed 55,
0.6 s.

## 3. What the software cannot protect against

- **A frozen laptop with an open socket.** Host watchdogs live in the same process. On
  stock firmware the car keeps the last command until the ESP32's 2-4 s timeout
  ([HARDWARE_PROTOCOL.md](HARDWARE_PROTOCOL.md) section 6). Mitigations: short motions, low
  speed, floor operation, the transport's own hard cap, and the optional 400 ms Arduino
  watchdog patch.
- **Falling off an edge.** The ultrasonic sensor faces forward and is not cliff detection;
  IR sensors look sideways. No sensor covers backward motion.
- **Position.** There is no odometry or localisation. "Return" reverses the recorded
  commands (dead reckoning) and is labelled approximate.
- **Compass disturbance.** App-mode motion holds heading with the magnetometer; strong
  magnets can make the car rotate while "idle" in app mode. LOCKED IN parks the car in
  `MODE_NONE` (Stop) between motions, where the motors are off.

## 4. Physical rules for testing and demos

1. First tests with the **wheels off the ground** (car on a box), dummy weight instead of
   the phone.
2. Then on a **clear floor area** with nothing within 60 cm in the retreat direction.
3. **Never** run autonomous motion on a desk or near a table edge. If the demo must be on a
   table: a large table, the car ≥ 60 cm from every edge in the retreat direction, a
   physical lip or barrier at the edge, retreat ≤ 0.6 s at low speed, and an operator with
   a hand near the robot and the E-STOP.
4. Secure the phone with Velcro **and** cable ties; check the mount after every run.
5. Someone always has the E-STOP in reach (dashboard top-right, or close the backend: a
   clean socket close stops the car on stock firmware). The physical power switch on the
   shield is the final stop.

## 5. Hardware tests that gate autonomy

| ID | Test | Pass criterion | Unlocks |
|---|---|---|---|
| R1 | Connect, observe idle keepalives and echoes (debug drawer, "robot wire") | link stays `connected` for 2 min, no motion | manual jog on a box |
| R2 | Wheels off ground: jog each primitive once at speed 40 | each wheel pattern matches the commanded direction | floor tests |
| R3 | Floor, dummy payload: find the lowest speed that moves the car | reliable motion in 5/5 tries | set profile speeds |
| R4 | Stop latency: start a 1 s jog, press STOP at ~0.4 s, film at high frame rate | wheels stop within 250 ms of the click, 10/10 | operator jog with phone |
| R5 | **Device-side stop on a silent link**: dashboard link-freeze test (transmit paused, socket open) with the wheels off the ground, Wi-Fi-off check, then on the floor | car stops on its own within 0.5 s every run (stock firmware fails by design; patch v2 expected 0.3-0.5 s) | set `LOCKEDIN_ZEUS_DEVICE_WATCHDOG_VERIFIED=true` and `..._TIMEOUT_S` to the worst measured value |
| R6 | Compass with phone mounted: park 2 min in app mode, then 10 retreats | no unexplained rotation; heading drift < 10° | autonomous retreat |

Since Milestone 3 every one of these needs motor commands, so none can run until the firmware
patch is flashed (with your approval) and `LOCKEDIN_ZEUS_MOTOR_COMMANDS=bench` is set
(section 7.3). R1's idle-link check is covered without motion by Z4 in
[REAL_HARDWARE_TESTS.md](REAL_HARDWARE_TESTS.md). Autonomy on the real robot stays locked
until R5 passes. Step-by-step procedure, roles,
abort criteria and recording template: [HARDWARE_TESTS.md](HARDWARE_TESTS.md). Record results
in [PERFORMANCE.md](PERFORMANCE.md).

## 6. Software safety tests (automated)

`tests/test_safety.py` (16), `tests/test_state_machine.py` (36 including parametrised
cases), `tests/test_integration.py` (16) and `tests/test_zeus_protocol.py` (18) cover:
disarmed by default, e-stop latch and reset, clamping, profile restrictions, autonomy gates
for real hardware, obstacle blocking by direction, ordered lifecycle events, stop preempting
motion, simulated device watchdog on link loss, the orphan and loop-stall watchdogs, release
during a retreat, low-confidence voice never moving the robot, and stop words inside other
intents. `tests/test_link_test.py` (14) adds the link freeze (nothing written, socket open,
stop ends it at once, never resumed), the patched-firmware model's watchdog and latch, the
host ending its motion when the robot reports a trip, and the LINK_TEST profile being
refused outside the R5 test. These run against models written from the firmware sources;
they do not show that the physical car stops.

## 7. Hardware safety contract (Milestone 3)

### 7.1 What the factory firmware does (verified from source)

The robot runs sketch 1.4.5 (zeus-car tag `1.4.5`, `492912f`, or one of the two later 1.4.5
commits; identical where it matters). Its frame parser and region map are the ones in
[HARDWARE_PROTOCOL.md](HARDWARE_PROTOCOL.md); 1.4.6 only added camera-lamp regions C/D.
Camera firmware: one of two families, told apart on connect (section 3 there).

| Situation | ESP32 | Arduino 1.4.5 | Motors |
|---|---|---|---|
| Stop frame `{"F":true}` | forwarded (frames < 20 ms after the previous are dropped) | `MODE_NONE`, `stop()`: `carStop()`, heading reset | off; latency unmeasured (serial + parse + 100 ms PWM fade, R4) |
| Backend exits or crashes (process gone, OS closes the socket) | `[DISCONNECTED] <ip>` | `ws_connected = false` → `MODE_NONE` if the app was in control | off, quickly |
| Silent link (Wi-Fi drops, Mac sleeps, backend hangs with the socket open) | ≤ 1.5.2: `[DISCONNECTED] timeout` after 2 s without text, socket left open; 1.5.3+: `[APPSTOP]` after 3 s without data, client closed after 3 s without text | as above | **keep the last command for 2-4 s** |
| A motion frame, then nothing | — | `modeHandler()` replays the last joystick every loop; **no Arduino-side expiry** (`WATCH_DOG` is the disabled AVR reset watchdog, not a motion timeout) | keep turning until the silence notice |
| Stale frames (held during a Wi-Fi stall, delivered late) | forwarded | executed | **a stale motion frame restarts the wheels** |
| Arduino reset / brown-out | — | reboots in `MODE_NONE` | off |
| ESP32 reboot or hang | sends `[Init]` once booted | `carStop()` on `[Init]`; **until then (or forever, if it never boots) the last command stays** | may keep turning |
| Emergency stop | (LOCKED IN's E-STOP is a Stop burst on a control link) | as Stop | as Stop |
| Control ownership | accepts several WebSocket clients and forwards all their frames | the IR remote works at any time and can start the autonomous modes | LOCKED IN cannot tell that another controller is active |

**Conclusion:** stock firmware has no device-side command expiry. A client-side timer
cannot help when the client is the thing that failed, and a WebSocket close only arrives
when the socket really closes. **A firmware change is required before LOCKED IN sends any
motor command.**

### 7.2 The firmware patch (prepared, not flashed)

Patch v2 (`firmware/optional/`): in app-control mode, no WebSocket data for 400 ms → the
sketch's own `stop()`, then **latched** (lines ignored until one carries Stop), so stale
frames cannot restart the wheels; plus telemetry (`li` marker, ultrasonic, IR, mode, trips,
free RAM). Now also prepared for the robot's 1.4.5 sketch (`zeus_car_locked_in_1.4.5.patch`),
compile-checked with arduino-cli (no new warnings; 1,516 B static RAM with ArduinoJson
6.21.5). It also covers an ESP32 that hangs or reboots: no line reaches the Arduino, so the
400 ms expiry stops the car. **Flashing needs your approval.**

### 7.3 The gate (code)

Real-robot motion needs all of these; each is shown in the dashboard's Robot panel:

| Lock | Where | Today (stock firmware) |
|---|---|---|
| `LOCKEDIN_ZEUS_MOTOR_COMMANDS` = `bench` or `enabled` | settings → link mode | `disabled` → monitor link |
| Control link (a monitor link writes no region frame, not even Stop) | `ZeusRobotTransport` | monitor |
| The robot reports patch v2 (`li` ≥ 2) on this connection | transport `motion_permitted` | not reported |
| R5 verified (`enabled`); `bench` allows only `LOW_SPEED_TEST` / `LINK_TEST`, never autonomy | `SafetyConfig.real_profiles`, `real_autonomy_allowed` | not verified |
| Operator checked motor power (battery, switch, wheels clear), within 30 min, link not dropped since | `SafetyController.motor_power_valid` | not checked |
| Armed, link fresh, e-stop clear, profile limits (unchanged) | `SafetyController` | disarmed |

Enforced three times: `arm()` refuses, `evaluate()` denies (`hardware_motion_locked`,
`real_profile`, `real_autonomy_off`, and since Milestone 4 `motor_power_unchecked`), and the transport's `move()`/`stop()` write nothing
on a monitor link. The link supervisor reconnects on its own but never arms. Tests:
`test_zeus_monitor.py`, `test_safety.py`, `test_link_test.py`, `test_hardware_mode.py`.

### 7.4 Milestone 4 additions (all fail closed)

| Gate | Why | Code | Test |
|---|---|---|---|
| **Motor power checked by the operator** before a real robot may arm or move; valid 30 min, cleared whenever the link drops | the firmware cannot measure the battery: with the motor battery flat (LEDs off) USB still powers the Arduino, the link and the camera work, and the wheels do nothing. A missing motor response must never be read as a software fault | `SafetyController.confirm_motor_power` / `motor_power_valid`, `arm()`, `evaluate()` → `motor_power_unchecked`; dashboard gate row + checklist | `test_safety.py`, `test_link_test.py` |
| **"Stop" holds still**: after a spoken or typed stop, every new motion request is refused for 15 s | a reach detected right after "stop" must not start a retreat | `STOP_SUPPRESS_S`, `SafetyController.suppress_motion` → `motion_suppressed` | `test_voice_loop.py` |
| **Return refused on the real robot** until a calibrated return exists (`LOCKEDIN_ZEUS_RETURN_CALIBRATED`) | the return is dead reckoning with no wheel feedback | `return_home` → `return_uncalibrated` | `test_link_test.py` |
| **Autonomous real motion needs perception**: tracking demonstrated (`LOCKEDIN_ZEUS_TRACKING_VERIFIED`), camera live, frames < 1 s old, marker tracked in marker mode | never move on a guessed robot position | `_perception_check` → `perception_unreliable` | `test_voice_loop.py` |
| **Only patch version 2 is trusted**; a robot reporting another `li` stays locked | a later, unreviewed patch must not inherit the trust | `SUPPORTED_PATCH_VERSIONS` in `zeus.py` | `test_link_test.py` |
| **Contradictory movement speech moves nothing** ("come back, no, go left", "don't come back") | ambiguity is not guessed at | `movement_conflict`, `_utterance_gate` → `conflict` | `test_intents.py`, `test_voice_loop.py` |
| **Action log never claims wheel motion**: "sent" means frames went over the robot link | there is no wheel feedback | `_note_action`, Robot panel "Robot actions" | `test_voice_loop.py` |

The patch logic itself (400 ms expiry, latch until Stop, no trips in stock modes, `millis()`
wraparound) is now tested natively on the real header (`firmware/test/patch_harness.cpp`,
`test_firmware_patch.py`); its timing on the robot is still R5.

### 7.5 Milestone 5: the language model can only ask

The local language model (Qwen3-4B on the laptop) writes replies and may *request* one action
per turn. It never touches the robot link, and nothing it writes is executed directly.

| Rule | Code | Test |
|---|---|---|
| A closed list of typed actions (start/pause/resume/end session, goal, extend, quiet, style, release the phone, request a retreat or return, stop, LEDs). Unknown names, extra fields and out-of-range values are refused and logged; there is no speed, direction, duration or motor field anywhere | `intelligence/actions.py` (`validate`, pydantic models with `extra="forbid"`) | `test_companion.py::test_a_malicious_or_malformed_reply_moves_nothing`, `test_parse_reply_refuses_anything_outside_the_closed_list` |
| Only actions the current state allows are offered and accepted | `dialogue.allowed_actions`, `parse_reply(allowed=...)` | same |
| A physical request (`request_retreat`, `request_return`) goes to the safety controller exactly like the motion policy's own requests: a retreat is allowed only in `PHONE_REACH_CONFIRMED`, and every gate in 7.3-7.4 applies. Today every such request is refused | `companion._execute` -> `SafetyController.request` / `return_home` | `test_a_retreat_request_goes_to_the_safety_controller_and_is_refused` |
| LED requests are refused: the factory firmware (1.4.5) has no LED control over the link | `companion.LED_REFUSAL` | `test_companion.py` |
| The model never decides a stop: stop words halt motion before any interpretation, and "stop", "emergency stop", "give me my phone" and camera/microphone off are settled by the deterministic parser however the sentence goes on | `companion.ALWAYS_REFLEX`, `IMPERATIVE_STOP`, `runtime._handle_utterance` (fail-safe stop first) | `test_short_commands_stay_on_the_fast_path_and_stop_is_never_negotiated`, `test_a_stop_word_inside_a_sentence_is_conversation_after_the_failsafe` |
| Words never claim what did not happen: replies that say the robot is moving, that describe an unrecorded sighting or reach, that state numbers not in the session record, or that accompanied a refused action are not spoken | `dialogue.check_reply`, `_attempt` | `test_speech_never_claims_motion_or_unseen_events`, `test_no_reach_accusation_without_a_recorded_reach` |
| A model failure (timeout, crash, out of memory) cannot change state: the worker is a separate process; the turn falls back to the parser's reading (if it passed the parser's gates) or a fixed line | `local_llm.py`, `companion.converse` | `test_when_the_model_fails_the_parser_still_acts` |

### 7.6 Stop and E-STOP wording

- Monitor link: E-STOP latches a **software** stop and disarms; no frame is sent, and the
  dashboard says so ("Software stop · no motor command sent"). LOCKED IN never commanded the
  motors on this link; if the car moves, something else drives it (IR remote, another app).
  The power switch is the hardware stop.
- Control link: "stop frames sent", never "wheels stopped": there is no wheel feedback.
  With patch v2 the Arduino reports its mode (`md` = 0), which is the mode, not the wheels.

