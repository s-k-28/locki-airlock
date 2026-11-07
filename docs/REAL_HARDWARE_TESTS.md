# Real hardware tests on the Zeus_Car network (no motion)

Milestone 3 tests to run once the Mac is on the robot's `Zeus_Car` Wi-Fi. **None of these
moves the robot**: LOCKED IN runs a monitor link that sends no motor or stop frames
([SAFETY.md](SAFETY.md) section 7). Record what you see in the template at the end and in
[PERFORMANCE.md](PERFORMANCE.md). What has been seen so far is under
[Results](#results-from-the-first-session-2026-11-01-0442).

Motor tests (R1-R6, wheels off the ground first) live in [HARDWARE_TESTS.md](HARDWARE_TESTS.md)
and need the firmware patch flashed and `LOCKEDIN_ZEUS_MOTOR_COMMANDS=bench`
([HARDWARE_HANDOFF.md](HARDWARE_HANDOFF.md) section 6). They are not part of this milestone.

Setup for every test: car on the desk or a box, phone **not** mounted yet, SunFounder app
closed, `make offline-check` passed on home Wi-Fi, then `make hardware` on `Zeus_Car`.
Keep the debug drawer (bug icon) open on "Robot wire" to see every frame in and out.

| ID | Test | Steps | Pass |
|---|---|---|---|
| Z1 | Start on the robot network | Join `Zeus_Car`, `make hardware`, open http://127.0.0.1:8077 | readiness check READY with `robot WebSocket` and `robot camera` reachable; top bar `ZEUS · MOTION LOCKED` within 10 s; Robot panel shows the camera firmware family. Record the family: it answers HARDWARE_PROTOCOL H1 without a serial monitor |
| Z2 | ESP32 camera in the dashboard | Vision panel, 60 s | `LIVE` with `ZEUS ESP32-CAM` on the video; record resolution, camera FPS, camera-clock FPS, kbps (curl measured ~1.7 Mbit/s earlier), KB/frame, delay above best p50/p95, lost in transit; 0 reconnects in 60 s |
| Z3 | Camera interruption | Switch the car off for 10 s, then on | Vision shows `ZEUS ESP32-CAM DISCONNECTED` with the reason within ~3 s, never the Mac webcam; back to `LIVE` without touching anything; reconnects +1. Repeat once with `LOCKEDIN_MJPEG_BACKEND=opencv make hardware` and note the FPS of both readers |
| Z4 | Monitor link at idle (2 min) | Leave it connected | link stays `connected`; WS ping p50 recorded; Arduino replies count up (firmware ≤ 1.5.2: about one every 2 s; 1.5.3+: one shortly after connecting); Robot wire shows only `ping` going out (1.5.3+) or nothing (≤ 1.5.2), never `{"F"…}` or `{"K"…}`; wheels never turn |
| Z5 | Robot power cycle | Car off 10 s, on | `ZEUS DISCONNECTED` within ~3 s, attempt counter rising; reconnects on its own after the ESP32 boots; `Disarmed · locked` throughout; no simulation labels anywhere |
| Z6 | Network switch | Start `make hardware` on home Wi-Fi (shows `ZEUS DISCONNECTED`), then switch the Mac to `Zeus_Car` without restarting; later switch back | connects within ~5 s of joining; camera live; after switching back: disconnected and retrying, nothing crashes |
| Z7 | E-STOP is a software stop here | Press E-STOP, read the top bar and Robot panel, then Reset | top bar `Software stop · no motor command sent`; Robot panel says no stop frame was sent; Robot wire shows no frame; reset leaves the robot disarmed |
| Z8 | Voice offline | Personality + Voice → press the samples; then type "how am I doing" in the Voice panel | pack lines show **Cached Fish Audio** in the speech history; an uncached line shows **Local voice** with the reason `offline mode`; audible on the chosen output |
| Z9 | What the robot camera sees | Put the car where it will sit in the demo, the user seated. Note the ESP32-CAM tilt. Vision → Zone: draw the robot camera zone around where a reaching hand appears; 10 deliberate reaches, 10 other movements (typing, drinking, waving) | record hands detected (yes/no), face visible, reach confirmations for each run; confirmations trigger the voice only (`Motion blocked · hardware motion locked`). See [CAMERA_PLACEMENT.md](CAMERA_PLACEMENT.md) for what to conclude |
| Z10 | Mac webcam alternative | Pick **Mac webcam** in the Vision panel while still connected to the robot; print marker 7, stick it on the car facing the laptop; Vision → Phone zone → Follows robot | switches without restarting; ROBOT_TRACKING trial T1 (marker detected ≥ 95 % of frames); the robot link is unaffected |
| Z11 | Clean exit | Ctrl-C `make hardware` | exits within ~3 s; the ESP32 LED shows the client gone |

Optional, only with both of you there: the serial monitor shows the camera firmware version
at boot (H1), but opening the USB serial port resets the Arduino (DTR), and the ESP32 shares
those pins. Z1 gives the generation without it.

## Results from the first session (2026-11-01 04:42)

One `make hardware` run on the robot's network, from its telemetry
(`data/telemetry/run-20261101-044240.jsonl`, 78 events). Not a full Z1-Z11 pass: nobody
followed the steps or wrote down observations, so only what the log proves is listed.

| Test | Seen in the log | Status |
|---|---|---|
| Z1 start on the robot network | robot link `ws://192.168.4.1:8765` connected at +0.2 s; robot reports `Zeus_Car`; camera firmware generation **1.1-1.4.x (single-client)** (answers HARDWARE_PROTOCOL H1); mode `ZEUS · MOTION LOCKED` | PASS (from the log) |
| Z2 ESP32 camera | `ZEUS ESP32-CAM` started at +1.1 s, delivering frames at +1.2 s; frame rate and bitrate were not recorded | partial |
| Perception on the robot camera | 4 hands approaching, 2 reaches confirmed, 1 suppressed, 2 cleared; face present/absent 4 times each. No session was running, so nothing spoke | PASS for detection; reactions NOT TESTED on the robot |
| Z6 network switch | +70.4 s camera: "no data from the camera for 2s"; +71.0 s robot: "link lost: nothing from the robot for 2.5s"; 31 reconnect attempts while on another network; clean shutdown at +1180 s (you stopped it) | PASS for detection and no crash; reconnecting after rejoining NOT TESTED |
| Z3-Z5, Z7-Z11 | not run | NOT TESTED |

The car never moved: the link was a monitor link (no frame the Arduino acts on). Separately,
the SunFounder app's joysticks did not move the wheels either, with the motor battery LEDs off:
motor power is unverified and is now an explicit operator check before any motion
([SAFETY.md](SAFETY.md) 7.4).

## Recording template

```
### Z? · <name> · <date> <time>
Firmware family:            Mac on Zeus_Car: yes / no
Steps done:
Result:            pass / fail
Numbers:
Notes / surprises:
```
