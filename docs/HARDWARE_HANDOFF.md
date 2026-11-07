# Hardware handoff: running LOCKED IN on the real Zeus Car

Milestones 3-4 (2026-11-01). Built and tested on home Wi-Fi against stand-ins written from the
firmware source, plus one real session on the robot's network (link, camera and hand detection
worked: [REAL_HARDWARE_TESTS.md](REAL_HARDWARE_TESTS.md#results-from-the-first-session-2026-11-01-0442)).
Tick boxes only for what you actually saw.

## 1. Before you leave home Wi-Fi (2 min)

```bash
cd ~/locked-in
make offline-check      # expect: READY for offline use: 0 missing
```

It checks packages, models, the built dashboard (no internet assets), the 39-clip Fish Audio
voice pack, the macOS fallback voice, audio devices, the hardware settings, and starts the
backend once with every non-local connection blocked. Fix any `FAIL` line now: nothing can
be downloaded on the robot's Wi-Fi. Details: [OFFLINE.md](OFFLINE.md).

Stop any LOCKED IN backend that is already running (port 8077), e.g. `make demo` in another
terminal: `make hardware` needs the port.

## 2. Switch networks and start

1. Turn the car on (shield switch in **run**), wait for the ESP32-CAM LED.
2. **Close the SunFounder app** on every phone (the ESP32 forwards every client's frames).
3. On the Mac: Wi-Fi → `Zeus_Car` (password `12345678` unless changed). No internet: fine.
4. Run:

```bash
make hardware SPEAKER="JLab GO Party"     # or "Beats Pill", or leave SPEAKER out for the saved output
```

then open **http://127.0.0.1:8077**. Ctrl-C stops it (about 2 s).

Before switching networks, check the speaker is **not muted in macOS** (volume keys): a muted
Bluetooth speaker accepts the audio and plays nothing. `make offline-check` and the Voice panel
both warn about it.

`make hardware` rebuilds the dashboard only if its source changed (works offline), re-runs
the readiness check, and starts the backend with: Zeus profile, `ws://192.168.4.1:8765`,
camera `http://192.168.4.1:9000/mjpg`, the microphone on (push-to-talk unless you changed it),
the robot's voice on `SPEAKER`, offline mode, automatic (re)connection. It never arms the
robot and sends no motor commands. Another address: `make hardware ZEUS_HOST=192.168.x.y`.

### The voice demo (Milestone 4)

1. Robot panel: `ZEUS · MOTION LOCKED`; Vision: `LIVE` with `ZEUS ESP32-CAM` on the video.
2. Voice panel: the loop card shows `READY` and `out · JLab GO Party` (no red speaker line).
3. Hold **Space** (or the Hold to talk button) and say "Lock me in for 25 minutes". Release.
   The card shows `TRANSCRIBING`, then: heard "Lock me in for 25 minutes." · STT ~0.8-1.2 s →
   intent start session → action "session started" → reply "Okay. Phone's mine now. Go." ·
   Cached Fish Audio · playing ~1 s after you stopped talking. The timer starts.
4. Reach toward the phone in view of the robot camera: the state goes to
   `POSSIBLE_PHONE_REACH` and it warns you out loud; a confirmed reach gets a roast. "Why it
   acted" shows the decision **voice only** with the rule "robot disarmed or autonomy off":
   no motion is even requested, and nothing moves.
5. Try "How much time do I have left?", "Roast me", "Pause my session", "Resume", "Stop
   talking", "You can talk again", "Stop", "Give me my phone back", "Emergency stop".
   "Stop" also refuses any motion for 15 s; E-STOP needs Reset.

What was verified on this Mac (not on the robot's network): steps 2-3 and 5 through the real
app, offline, with a macOS voice standing in for you ([AUDIO_TESTS.md](AUDIO_TESTS.md#results-2026-11-01-milestone-4)).
Step 4's spoken warning is tested with simulated camera events; hand detection itself worked
on the robot camera.

## 3. What the dashboard should show

| Where | Robot reachable | Robot not reachable (still on home Wi-Fi, car off) |
|---|---|---|
| Top bar | `ZEUS CONNECTING` then `ZEUS · MOTION LOCKED` within a few seconds; `OFFLINE`; Robot `<n>ms`; Camera `<n>fps` | `ZEUS DISCONNECTED` (red); Robot `off`; Camera `retrying` |
| Vision | `LIVE`, picker `Zeus ESP32-CAM`, video with `ZEUS ESP32-CAM · 640×480 · <fps>`; stream row: camera clock, kbps, delay, lost frames, reconnects | `ZEUS ESP32-CAM DISCONNECTED`, the reason (`no answer from 192.168.4.1:9000 within 3s`) and a retry countdown. It never switches to the Mac webcam |
| Robot | `Live · Zeus Car · monitor`; camera firmware family (1.1-1.4.x, 1.5.2 or 1.5.3+); WS ping; last message from the robot; Arduino replies counting up | `ZEUS DISCONNECTED`, attempt counter, retry countdown, `Stop trying` |
| Robot · gate | `PHYSICAL MOTION LOCKED`: ✗ motor commands disabled, ✗ monitor link, ✗ no device-side command expiry (stock firmware), ✗ R5 not demonstrated, ✓ link alive, ✓ e-stop clear, ? exclusive control | same, link ✗ |
| Robot · controls | `Disarmed · locked`; autonomy and jog disabled with the reason; **Stop** always enabled | same |
| E-STOP | latches a **software** stop; the top bar then says `Software stop · no motor command sent` (the robot's power switch is the hardware stop) | same |

Pick **Mac webcam** in the Vision panel to perceive with the laptop camera instead
([CAMERA_PLACEMENT.md](CAMERA_PLACEMENT.md) explains when that is the better choice). The
choice switches at once and is kept.

Then run the no-motion tests Z1-Z11 in [REAL_HARDWARE_TESTS.md](REAL_HARDWARE_TESTS.md).

## 4. What is verified, and how

| | Verified by | On the robot |
|---|---|---|
| Robot sketch 1.4.5 parses frames like 1.4.6 (only the camera-lamp regions differ) | source diff of zeus-car tag `1.4.5` and the two later 1.4.5 commits | — |
| Monitor link sends no frame the Arduino acts on, for every camera firmware generation | tests against firmware models (1.3.0/1.4.3 single-client, 1.5.2, 1.5.4) | Z4 |
| Link and camera reconnect after a network switch or power cycle, never arm, never fall back | tests with stand-ins; dry run of `make hardware` | Z5, Z6 |
| MJPEG stream read, decoded, timed | stand-in writing camera_server.cpp's bytes; OpenCV FFmpeg reads it too | Z2, Z3 |
| Offline: no internet use, cached voice, local fallback | in-process start with the network blocked (0 attempts) | Z8 |
| Motor commands impossible on stock firmware | three independent locks, tested | — (no motion in this milestone) |

## 5. Safety contract (summary of [SAFETY.md](SAFETY.md) section 7)

Stock firmware 1.4.5 has **no Arduino-side command expiry**: a motion command keeps the
wheels turning until the ESP32 reports silence (2 s on camera firmware ≤ 1.5.2, 3-4 s on
1.5.3+), and not at all while the ESP32 itself is rebooting or hung. A client-side timer or
a WebSocket close is not enough. Therefore:

- **A firmware patch is required** before any motor command: patch v2 (400 ms Arduino-side
  expiry + latch), now prepared for the robot's own 1.4.5 sketch and compile-checked
  (`firmware/README.md`). **Not flashed; flashing needs your approval.**
- Until the patch is flashed **and** the robot reports it, LOCKED IN cannot send motor
  commands, whatever the configuration says.

## 6. What remains before the first wheel-off-ground motor test

1. **Motor power, by hand.** Charge the motor battery; with the shield's power switch on, its
   LEDs must light (USB alone powers only the Arduino, which is why the SunFounder app's
   joysticks did nothing). Optionally confirm with the SunFounder app, car on a stand, before
   anything of ours is involved. No software can check this: the dashboard asks you to confirm
   it (Robot → gate → "Confirm motor power", valid 30 min, cleared when the link drops).
2. Z1-Z11 passed on the robot's network with the stock firmware first, so the link and
   camera are known good before anything changes.
3. Your decision to flash patch v2 on the 1.4.5 base, then the flash: **back up the robot's
   current flash first**, then upload, with the rollback written down
   (`firmware/README.md` → "Backup, flash, rollback"). About 21.7 KB flash, 1,516 B static RAM
   with ArduinoJson 6.21.5; the patch logic passes the native test on the real header.
4. After flashing: `make hardware` shows `LOCKED IN firmware patch v2` reported (gate:
   device-side command expiry ✓) while still in monitor mode.
5. Set `LOCKEDIN_ZEUS_MOTOR_COMMANDS=bench` in `.env` (wheels-off-ground profiles only, no
   autonomy), car on a stand, spotter at the power switch, confirm motor power, then R2 and
   R5a from [HARDWARE_TESTS.md](HARDWARE_TESTS.md). Only after R5 passes:
   `LOCKEDIN_ZEUS_DEVICE_WATCHDOG_VERIFIED=true` and `..._MOTOR_COMMANDS=enabled`.
6. Before any autonomous retreat: robot position tracking demonstrated on the real car
   (`LOCKEDIN_ZEUS_TRACKING_VERIFIED`), a clear-path confirmation, and still no automatic
   return until a calibrated return exists (`LOCKEDIN_ZEUS_RETURN_CALIBRATED`).
7. Physical setup from [CAMERA_PLACEMENT.md](CAMERA_PLACEMENT.md): which camera watches the
   user, ESP32-CAM angle, robot camera zone calibration (Z9).

The assembly checklist (mecanum X pattern, motor ports, phone mount, compass away from
magnets, markers) is unchanged: [HARDWARE_ASSEMBLY.md](HARDWARE_ASSEMBLY.md).
