# Zeus Car control protocol (verified from source)

Everything below was read from the upstream source code on 2026-10-31. Statements
marked **verified (source)** come from reading the code; nothing here has been
measured on the physical robot yet. Hardware verification items are listed in
section 11 and in [TEST_PLAN.md](TEST_PLAN.md).

## 1. Sources

| Repository | Commit read | Role |
|---|---|---|
| https://github.com/sunfounder/zeus-car | `82467e9` (2025-02-28), sketch `VERSION "1.4.6"` | Arduino UNO sketch `Zeus_Car/` |
| same, **robot's own version** (checked 2026-11-01) | tag `1.4.5` = `492912f` (2023-06-26); `cad68d5` and `7fec2f7` also say 1.4.5 | factory firmware on this robot. Against 1.4.6 the only functional change is camera-lamp control (regions C/D, `SET+LAMP`); the parser, region map, `stop()`, `loop()` and connection handling are identical (statement-level diff). Everything below holds for 1.4.5 |
| https://github.com/sunfounder/ai-camera-firmware | `d8fc8d3` (2026-06-02, `VERSION "1.5.4"`); also tags `1.3.0`, `1.4.1`, `1.5.3` | ESP32-CAM WebSocket bridge |
| https://github.com/sunfounder/SunFounder_AI_Camera | `c498867` (2026-10-16, lib 1.2.2) | Standalone Arduino library (Zeus Car bundles its own older copy) |
| https://docs.sunfounder.com/projects/zeus-car/en/latest/ | app-control page | Widget-to-region layout, Wi-Fi defaults |

Key files: `Zeus_Car/Zeus_Car.ino` (`onReceive()`, `loop()`, `modeHandler()`),
`Zeus_Car/ai_camera.cpp` (`loop()`, `readInto()`, `getStrOf()`, `getJoystick()`),
`Zeus_Car/car_control.cpp` (`carMove()`, `carMoveFieldCentric()`),
`Zeus_Car/cmd_code_config.hpp` (modes, `stop()`), `ai-camera-firmware/src/ws_server.cpp`
(`handleSunFounderController()`, ping/pong, timeouts), `ai-camera-firmware/src/main.cpp`
(`serialReceivedHandler()`, `start()`).

## 2. Topology

```
laptop ──Wi-Fi──▶ ESP32-CAM ──UART 115200──▶ Arduino UNO + Zeus shield ──▶ 4 DC motors (mecanum)
       ws://<ip>:8765        "WS+v0;v1;...;v25\n"      onReceive() → modeHandler() every loop()
       http://<ip>:9000/mjpg (camera)
```

- **Wi-Fi (verified, source + docs):** the sketch configures AP mode, SSID `Zeus_Car`,
  password `12345678` (`Zeus_Car.ino` lines 67-69). On camera firmware 1.5.x the settings
  are persisted in ESP32 flash and only applied from the sketch when flash is empty
  (`main.cpp` `handleSet`, "default settings will be ignored if settings in flash are not
  empty"). Default soft-AP IP is the ESP32 default `192.168.4.1` (not stated in the code;
  confirm on hardware: the camera prints it after `SET+START`).
- **WebSocket:** port 8765 (`Zeus_Car.ino` `#define PORT "8765"`, "Sunfounder Controller APP
  fixed using port 8765").
- **Camera:** MJPEG at `http://<ip>:9000/mjpg`, single JPEG at `/capture`
  (`camera_server.cpp`, `config.server_port = 9000`), VGA 640x480 by default (`defaults.h`).
- **Clients:** firmware 1.5.x accepts up to 5 WebSocket clients and forwards *all* of their
  frames to the Arduino (`ws_server.h` `MAX_CLIENTS 5`). **Close the SunFounder app before
  connecting LOCKED IN**, otherwise both stream commands.
- **Laptop networking:** joining the `Zeus_Car` AP removes internet on a single-radio laptop
  (this MacBook Air has one Wi-Fi interface, `en0`). LOCKED IN runs fully offline; only
  the optional LLM and live Fish Audio need internet (cached Fish Audio clips do not). Alternative: STA mode on a shared
  phone hotspot (requires editing `SSID`/`PASSWORD`/`WIFI_MODE` in the sketch and
  re-flashing, which needs the team's approval).

## 3. Handshake and camera firmware families (verified, source)

Two behaviours, told apart by what arrives on connect (`classify_camera_firmware()`):

| Family | Versions | On connect | Client sends no text | Client sends text `ping` |
|---|---|---|---|---|
| single-client | 1.1.0-1.5.2 (incl. 1.4.1-1.4.3 from 2025-26) | `pong <ms>` (sent on **every** WebSocket event, at most every 200 ms), then the check info: twice on ≤ 1.4.x; once with `StaIp`/`VideoTemplate` on 1.5.2 | socket stays open; every 2 s the ESP32 tells the Arduino `[DISCONNECTED] timeout` | ESP32 tells the Arduino `[APPSTOP]` (never send it) |
| multi-client | 1.5.3, 1.5.4 | check info once with `StaIp`/`VideoTemplate`, no pong | **closed after 3 s** (WebSocket protocol pings do not count); one `[APPSTOP] Data timeout from all clients` after 3 s without data | ESP32 answers `pong <ms>`; nothing reaches the Arduino |

The ESP32 also writes `[CONNECTED] …` to the Arduino, which sets `ws_connected = true`.
1.5.2 has the new check-info fields but the old timeouts; before Milestone 3 it was classed
with 1.5.3+. The dashboard shows the family (`robot.firmware.camera_firmware_family`).

## 4. Frames

### 4.1 JSON → serial conversion (verified, source: `handleSunFounderController`)

For each region letter `A`..`Z` in order the ESP32 takes the JSON value, joins arrays
with `,`, maps `true`→`1`, `false`→`0`, missing→empty, and writes one line:

```
{"K":[0,40]}   →   WS+;;;;;;;;;;0,40;;;;;;;;;;;;;;;
                       A B C D E F G H I J  K  L ... Z   (26 fields, 25 separators)
```

Frames that arrive within **20 ms** of the previously forwarded frame are **dropped, not
queued** (`if (millis() - last_send_time > SEND_INTERVAL)`, `SEND_INTERVAL = 20`).

### 4.2 Region map (verified, source `onReceive()` + docs app-control page)

| Region | Index | Arduino use | LOCKED IN |
|---|---|---|---|
| C, D | 2, 3 | camera lamp switch / level | never sent |
| **E** | 4 | compass calibration: **spins the car for about a minute** | **never sent** |
| **F** | 5 | Stop button → `currentMode = MODE_NONE; stop()` | **stop frame** |
| G | 6 | reset heading origin | never sent |
| **I** | 8 | speech text → `forward`/`turn left`/... at power 60 | **never sent** |
| J | 9 | drift switch | never sent (0) |
| **K** | 10 | move joystick `x,y` | **translation** |
| M, N, O, P | 12-15 | autonomous modes: line track (×2), obstacle follow, obstacle avoid | **never sent** (0 cancels them) |
| **Q** | 16 | heading joystick `x,y` | **rotation** |

### 4.3 What LOCKED IN sends (and nothing else)

| Purpose | JSON text | Arduino effect (verified, source) |
|---|---|---|
| Forward / backward | `{"K":[0,±s]}` | `angle = atan2(x,y)` → 0° / 180°, `power = sqrt(x²+y²)` → `MODE_APP_CONTROL` |
| Strafe left / right | `{"K":[∓s,0]}` | angle −90° / +90° |
| Rotate left / right | `{"Q":[∓100,0]}` | `remoteHeading = ∓90°`; the magnetometer PD loop turns toward that heading relative to the heading captured when app mode was entered |
| Stop / idle keepalive | `{"F":true}` | `currentMode = MODE_NONE; stop()` → `carStop()` + `carResetHeading()`; `modeHandler()` keeps calling `carStop()` every loop |

`s` is clamped to 0..100 (`app/robot/zeus.py`). Missing regions become empty fields,
which the Arduino parses as 0 (`String("").toInt() == 0`), so a minimal frame implicitly
sets every other control to off. Frame strings are ≤ 36 characters, which fits the Uno's
64-byte serial RX buffer even with one more line behind it.

LOCKED IN never sends `SET+...` (Wi-Fi reconfiguration) and never sets E, G, I, J, M-P, C or
D. The text `ping` goes only to multi-client firmware, which answers it itself (section 3).

### 4.4 Monitor link (default since Milestone 3)

The frames above are only sent on a **control** link, which the hardware gate does not allow
on stock firmware ([SAFETY.md](SAFETY.md) section 7). The default **monitor** link writes no
region frame at all, not even Stop:

| Family | What LOCKED IN sends | What the Arduino receives |
|---|---|---|
| single-client | WebSocket protocol pings, 1 per second (answered by the ESP32's WebSocket library) | only the ESP32's own notices (`[CONNECTED]`, `[DISCONNECTED] timeout` every 2 s) |
| multi-client | protocol pings + text `ping`, 1 per second (keeps the socket open) | only the ESP32's own notices (`[CONNECTED]`, one `[APPSTOP] Data timeout`) |

Latency is the protocol ping's round trip to the ESP32. The Arduino answers each notice with
`sendData()` (section 7), so its replies arrive without any frame from LOCKED IN: proof that
the Arduino runs and the serial link works. With patch v2 flashed, those replies carry `li`,
so the patch is recognised before any motor command could exist.

### 4.5 Motion semantics (verified, source `car_control.cpp`)

- In `MODE_APP_CONTROL` every loop runs `carMoveFieldCentric(angle, power, heading, drift)`:
  it reads the QMC6310 magnetometer (30-sample moving average), runs a PD controller
  (`KP 0.8`, `KD 20`) to hold `originHeading + heading`, and rotates the translation angle
  into the field frame. **Even at joystick (0,0) the car actively holds heading** and can
  rotate if the compass reading changes. The real "motors off" state is `MODE_NONE`, which
  is why LOCKED IN parks the car with `F` (Stop) rather than a zero joystick.
- Wheel power from joystick radius `r`: `power = r / √2 × 0.5`, wheel = `power·sin − power·cos`
  of `(angle + 90°)`, then `map(|p|, 0, 100, 50, 255)` PWM with a 200 µs kick to 100 if
  below 100. Joystick 100 → ~47 % PWM duty; joystick 40 → PWM ~78/255 (~31 %).
  **Minimum speed that moves the car with a phone on it is unknown until tested (R3).**
- `SoftPWMSetFadeTime(pin, 100, 100)`: PWM changes ramp over 100 ms, so a stop takes up
  to ~100 ms to reach zero after the Arduino processes it.
- Rotation speed cannot be set through the stock protocol (PD-controlled). LOCKED IN
  therefore excludes rotation from autonomous motion (`SHORT_RETREAT` profile).

## 5. Timing constraints and the chosen cadence

| Constraint (verified, source) | Value | LOCKED IN choice |
|---|---|---|
| ESP32 forward drop window | 20 ms | first stop frame ≥ 25 ms after the last frame; all other frames ≥ 45 ms apart |
| Arduino `readInto()` | `delay(1)` per character → ~1 ms/char; one line per `loop()` | frames ≤ 36 chars; motion refresh 10 Hz |
| Arduino echo `sendData()` | at most every 60 ms, only after it read a line | used as a link-level acknowledgement |
| `carResetHeading()` | ~93 ms (comment in source) on every mode entry and every Stop | motion start latency includes it |
| Legacy (≤1.4) silence timeout | 2 s without a text frame → `[DISCONNECTED] timeout` | idle keepalive every 500 ms |
| Modern (1.5) timeouts | 3 s no frames → disconnect; 3 s no data → `[APPSTOP]` (checked every 1 s) | idle keepalive every 500 ms |

Control-link cadence implemented in `app/robot/zeus.py` (the monitor link, section 4.4,
sends none of this): motion frames every 100 ms while moving,
`{"F":true}` every 500 ms while idle, a 3-frame stop burst on every stop, a
transport-level hard cap that stops on its own at `duration + 0.2 s` if nobody calls
`stop()`, and link loss declared after 2.5 s without any message from the robot.

## 6. What stops the motors on stock firmware (verified, source)

The Arduino stops when it receives `[DISCONNECTED]`, `[APPSTOP]` or `[Init]` (ESP32
reboot): `ws_connected = false` → `currentMode = MODE_NONE` → `carStop()`.
`#define WATCH_DOG 0` in `Zeus_Car.ino` is the AVR hardware reset watchdog, disabled;
it is not a motion timeout.

| Event | ESP32 | Arduino / motors | Expected stop latency |
|---|---|---|---|
| LOCKED IN sends Stop | forwards `F=1` | `MODE_NONE`, `carStop()` | serial + parse (~30-60 ms) + 100 ms PWM fade |
| Backend process exits / socket closed cleanly | `WStype_DISCONNECTED` → `[DISCONNECTED]` | stops | fast (TCP close) |
| Laptop Wi-Fi drops, laptop freezes, or socket half-open | 1.5.x: no data for 3 s → `[APPSTOP]`; no frames for 3 s → disconnect. ≤1.4: no frames for 2 s → `[DISCONNECTED] timeout` | stops | **2-4 s** depending on firmware |
| A motion frame is the last thing received and nothing else happens | — | **keeps driving with the last command** until one of the timeouts above | 2-4 s |

There is **no Arduino-side command timeout** in the stock sketch: `modeHandler()` replays
the last joystick values every loop. A test against the firmware model reproduces this
(`tests/test_zeus_protocol.py::test_stock_firmware_failsafe_when_laptop_goes_silent`).
Consequences: autonomous motion on real hardware is refused by the safety controller until
the device-side stop has been measured (`LOCKEDIN_ZEUS_DEVICE_WATCHDOG_VERIFIED=true`,
test R5), and an optional Arduino patch adds a 400 ms motion watchdog that latches until a
Stop frame ([firmware/README.md](../firmware/README.md)); **flashing it requires the team's
approval**. Test R5 uses the dashboard's link-freeze test (transmission paused with the
socket open; [HARDWARE_TESTS.md](HARDWARE_TESTS.md)).

## 7. What comes back

| Message | Meaning |
|---|---|
| check info JSON | ESP32 identity on connect |
| `pong <ms>` | ESP32 alive (single-client: on every event; multi-client: reply to text `ping`) |
| `null` or `{"I":0}` | the Arduino's `sendData()` reply after it reads **any** serial line (a relayed frame or one of the ESP32's notices), at most every 60 ms, relayed by the ESP32 (`WS+` stripped; to the last active client on single-client firmware, to all clients on 1.5.3+). Proof that the Arduino is running and parsing, **not** proof that wheels moved |
| anything else | counted and shown as text, never read as sensor values (only replies with the patch marker `li` are) |

The stock sketch fills `send_doc` only with the speech toggle `I`. **No ultrasonic, IR,
battery or odometry data reaches the laptop on stock firmware.** The dashboard shows
battery and obstacle sensing as unavailable. With the optional patch (v2) the echo carries
`{"li":2,"us":<cm>,"ir":<bits>,"md":<mode>,"wd":<trips>,"lk":<0|1>,"fr":<bytes>}` while a
client is connected (`us` = -1 no echo within ~2 m, -2 not measured in this mode);
`ZeusRobotTransport` parses those keys, only then reports obstacle sensing as available,
and ends its own motion and sends Stop when `wd` rises (the Arduino stopped the motors).

## 8. Known hazards found in the source

1. **Magnetometer.** All app-mode motion is closed-loop on the QMC6310 compass. A MagSafe
   phone, Bluetooth speaker magnets or the laptop next to the car can bias it, which makes
   the car rotate while "idle" in app mode. Mount magnets away from the compass and run the
   compass calibration (region E / IR mute key) with the phone mounted (hard-iron offsets
   that rotate with the car are calibrated out; external fields are not).
2. **Camera firmware / sketch mismatch.** The Zeus sketch's bundled `ai_camera.cpp` copies
   camera replies with an unbounded `strcpy` into a 10-byte buffer (`set()`). Camera
   firmware 1.5.x replies with longer strings such as `[OK] SSID is deprecating, use APSSID
   or STASSID`, which overflows it. The upstream library fixed exactly this in 1.2.1 ("on
   GalaxyRVR 2.0.1 it reset the board in a loop"). **Do not OTA-update the ESP32-CAM without
   also updating the sketch.** If the car boot-loops after a camera update, this is the
   likely cause.
3. **Blocking ultrasonic read.** `ultrasonicRead()` calls `pulseIn()` with the default 1 s
   timeout; with no echo it stalls the loop for up to a second. The optional patch uses a
   12 ms bound.
4. **Speech region.** Any text containing "forward", "left", etc. in region I drives the car
   at power 60. LOCKED IN never sends I.
5. **Upload switch.** The ESP32 shares the UNO's serial pins; the shield's small switch must
   be in the run position for the ESP32 to talk to the Arduino (docs). Uploading a sketch
   needs it in the upload position.

## 9. Answers to the questions in the brief

1. **How the laptop connects:** Wi-Fi to the ESP32-CAM, WebSocket `ws://192.168.4.1:8765`.
2. **WebSocket or serial:** WebSocket to the ESP32; the ESP32 relays to the Arduino over UART.
   USB serial to the Arduino is possible for bench work but conflicts with the ESP32 on the
   same pins.
3. **Framing:** one JSON object per WebSocket text message, keys `A`-`Z`; converted to one
   `WS+…;…\n` line (section 4.1).
4. **Motion commands:** `{"K":[x,y]}`, refreshed at 10 Hz (section 4.3).
5. **Stop immediately:** `{"F":true}` (Stop button) - motors off in `MODE_NONE`.
6. **Lateral mecanum movement:** `{"K":[±s,0]}`.
7. **Rotate:** `{"Q":[±100,0]}` (compass-closed-loop, speed not settable).
8. **Obstacle sensors:** read on the Arduino (`irObstacleRead()`, `ultrasonicRead()`), but
   not transmitted by stock firmware.
9. **Sensor data back:** no (only the echo); yes with the optional patch.
10. **Connection loss:** stop after the ESP32 reports it: immediate on clean socket close,
    2-4 s on a silent link (section 6).
11. **Heartbeat required:** to drive, yes: at least one frame every <2 s (≤ 1.5.2) / <3 s
    (1.5.3+) or the car is stopped; a control link sends Stop every 500 ms when idle. To stay
    connected without commanding anything, the monitor link uses protocol pings (and a text
    `ping` on 1.5.3+) instead (section 4.4).
12. **Firmware modification needed:** not to drive. Needed for a sub-second device-side stop
    and for sensor telemetry (section 6, firmware/README.md).

## 10. Simulator and protocol tests

`backend/tests/fake_esp32.py` reimplements `handleSunFounderController()`, the timeouts of
the single-client (≤ 1.4.x and 1.5.2) and multi-client (1.5.3+) firmware, the Arduino's reply to
every serial line, and the relevant parts of `getStrOf()`/`getJoystick()`/`onReceive()`.
`backend/tests/test_zeus_monitor.py` checks that a monitor link sends nothing the Arduino acts
on, for every family, past their silence timeouts.
`backend/tests/test_zeus_protocol.py` (18 tests) checks frame decoding, cadence (no frame
inside the 20 ms drop window, ~10 Hz refresh), legacy detection without `ping`, disconnect
handling, the transport's own hard cap, and the stock fail-safe gap. These prove conformance
with the published source, not physical behaviour.

## 11. Still to verify on the robot

| ID | Question | How |
|---|---|---|
| H1 | ESP32-CAM firmware version actually installed | Serial monitor at boot prints `ESP32 firmware version …`; LOCKED IN shows the generation |
| H2 | Soft-AP IP is 192.168.4.1 | connect, check `robot.connected` target |
| H3 | Direction mapping (K/Q signs) matches physical directions | test R2, wheels off the ground |
| H4 | Minimum joystick radius that moves the car with the phone mounted | test R3 |
| H5 | Stop latency (frame → wheels stopped) | test R4, video at 240 fps or a stopwatch app |
| H6 | Stop on silent link loss, stock firmware | test R5 |
| H7 | Compass behaviour with the phone mounted | test R6 |
| H8 | MJPEG stream usable as a LOCKED IN camera source | Z2/Z3 in [REAL_HARDWARE_TESTS.md](REAL_HARDWARE_TESTS.md) (reader tested against a stand-in writing the firmware's bytes) |

## 12. Camera stream (verified, source `camera_server.cpp`, 1.3.0 and 1.5.4 identical)

`http://<ip>:9000/mjpg`, HTTP/1.1 chunked (esp_http_server), `Content-Type:
multipart/x-mixed-replace;boundary=123456789000000000000987654321`; each frame
`\r\n--<boundary>\r\nContent-Type: image/jpeg\r\nContent-Length: <n>\r\nX-Timestamp: <sec>.<usec>\r\n\r\n<jpeg>`.
640×480 by default (`FRAMESIZE_VGA`). LOCKED IN reads it with its own multipart reader
(`app/vision/mjpeg.py`: socket timeouts, stop from any thread, the camera's X-Timestamp for
lost-frame and delay figures) and decodes with OpenCV; OpenCV's FFmpeg capture is the
alternative (`LOCKEDIN_MJPEG_BACKEND=opencv`).
