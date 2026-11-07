# Firmware

**AIRLOCK: ARCHIVED / NO FLASH / NO MOTOR COMMANDS.** The AIRLOCK PRD supersedes the historical instructions below. Patch v2 must not be flashed: the reported firmware audit found simulated serial-buffer corruption could prevent Stop commands. Zeus is a stationary enclosure for this product. These files remain for history and review only.

LOCKED IN drives the **stock** SunFounder firmware. Nothing in this repository has been
flashed to any device. **Do not flash anything without both team members agreeing.**

| Device | Firmware | Source |
|---|---|---|
| Arduino UNO (Zeus shield) | **this robot: `Zeus_Car.ino` 1.4.5** (factory); upstream is 1.4.6 | https://github.com/sunfounder/zeus-car (tag `1.4.5` = `492912f`) |
| ESP32-CAM | ai-camera-firmware (1.4.x or 1.5.x; check, see below) | https://github.com/sunfounder/ai-camera-firmware |

Check what the ESP32-CAM runs: open the Arduino serial monitor at 115200 while the car boots
and look for `ESP32 firmware version …`, or connect LOCKED IN and read the firmware line in
the robot panel. Do **not** OTA-update the camera on its own: camera firmware 1.5.x can crash
the stock 1.4.6 sketch ([HARDWARE_PROTOCOL.md](../docs/HARDWARE_PROTOCOL.md) §8.2).

## Optional patch: device-side motion watchdog + telemetry

`optional/locked_in_patch.h` (patch version 2) + 3 added lines in `Zeus_Car.ino`:
`optional/zeus_car_locked_in_1.4.5.patch` for the robot's own 1.4.5 sketch (recommended: one
change at a time) or `optional/zeus_car_locked_in.patch` for upstream 1.4.6. The added lines
are the same; only the surrounding formatting differs (1.4.6 reformatted the sketch).
Licensed GPL-3.0-or-later like the upstream sketch; the license text is in [COPYING](COPYING).

**Why it is required (Milestone 3):** stock 1.4.5 has no Arduino-side command expiry, so a
lost or stalled link leaves the last motion running for 2-4 s, longer if the ESP32 itself
hangs ([../docs/SAFETY.md](../docs/SAFETY.md) section 7). LOCKED IN sends no motor command at all
until the robot reports this patch.

What it adds:

1. **Motion watchdog:** in app-control mode, if no WebSocket data reached the Arduino for
   400 ms, call the sketch's own `stop()` (the same code the app's Stop button runs:
   `MODE_NONE`, remote values zeroed, motors off). Stock firmware relies on the ESP32's
   3-4 s timeouts. After a trip it **latches**: lines are ignored until one carries Stop
   (`F`), so stale motion frames arriving after a Wi-Fi stall cannot restart the wheels.
   LOCKED IN sends Stop after every motion and every 500 ms when idle.
2. **Telemetry** (only while a WebSocket client is connected): ultrasonic distance (bounded
   12 ms read instead of the stock 1 s `pulseIn` timeout; measured only in `MODE_NONE` /
   app control so the stock autonomous modes are untouched), IR obstacle bits, mode,
   watchdog-trip count, latch state and free RAM, appended to the JSON the sketch already
   sends back.
   LOCKED IN then reports obstacle sensing as available, blocks forward motion toward
   obstacles, and shows watchdog trips.

Side effects: a controller that stops sending frames while its joystick is held still (if
the SunFounder app does that) will see the car stop after 400 ms and must press Stop before
driving again. LOCKED IN refreshes motion at 10 Hz and is unaffected unless the link stalls,
which is the point.

### Review (2026-11-01): compile only, never run

Checked against a fresh clone of `sunfounder/zeus-car` at `82467e9` (`git apply --check`
clean), arduino-cli 1.5.1, `arduino:avr` 1.8.8, board `arduino:avr:uno`, IRLremote 2.0.2,
SoftPWM 1.0.1, `--clean --warnings all` (an earlier warning comparison without `--clean`
was empty because of the build cache, so it is not counted):

| Build | ArduinoJson | Flash | Static RAM | Left for stack + heap | New warnings |
|---|---|---|---|---|---|
| upstream | 6.21.5 | 21,600 B (66 %) | 1,528 B | 520 B | — |
| patched v2 | 6.21.5 | 22,314 B (69 %) | 1,561 B | 487 B | none |
| upstream | 7.4.3 | 23,208 B (71 %) | 1,360 B* | 688 B* | — |
| patched v2 | 7.4.3 | 24,212 B (75 %) | 1,393 B* | 655 B* | none |

\* With ArduinoJson 7 the sketch's `StaticJsonDocument<200>` is deprecated and its memory
pool moves to the **heap at run time**, so these RAM figures leave it out. With 6.21.x the
200-byte document is static and counted. The upstream code was written against the
ArduinoJson 6 API (`StaticJsonDocument`).

Findings and what changed from patch v1:

| # | Finding | Action |
|---|---|---|
| 1 | v1 zeroed `remotePower`/`remoteHeading` by hand; the sketch already has `stop()` that also clears angle, drift and heading reference and calls `carStop()` immediately | v2 calls `stop()` |
| 2 | v1 pinged the ultrasonic every 120 ms in every mode, adding up to 12 ms of blocking and extra pings to the stock line/obstacle modes, even with no client connected | v2 only measures with a client connected and in `MODE_NONE` / app control (`us` = -2 otherwise) |
| 3 | `pulseIn()` counts CPU cycles; SoftPWM's timer interrupt drives the motors, so readings come out short while interrupts fire | v2 uses `pulseInLong()` (micros-based, the documented choice when interrupts are on) |
| 4 | ArduinoJson 7 moves the reply document to the heap on a 2 KB MCU; compile output no longer shows real RAM use | **flash with ArduinoJson 6.21.5** (static, counted); v2 reports `fr` (free RAM) so R5 can check the margin on the device either way |
| 5 | After a trip, the host's next refresh frame (or frames Wi-Fi buffered during a stall) would put the car straight back into app control and restart the wheels | v2 latches until a Stop frame; the host also ends its motion when it sees the trip count rise |
| 6 | String-literal keys stay in RAM on AVR (18 B for six keys) | accepted; total patch cost 33 B static RAM |
| 7 | No new compiler warnings; all 26/28 warnings are upstream's own | none |

Verified by reading the sources (not by running them): watchdog timing only resets on
`WS+` lines, i.e. real WebSocket text frames (the ESP32 does not repeat old frames:
`ws_server.cpp` `handleSunFounderController`); the trip runs before `modeHandler()` in the
same loop pass; `MODE_NONE` calls `carStop()` every pass; IR bit meaning matches the
sketch's own obstacle code; replies are rate-limited by the sketch to one per 60 ms after a
received line, so telemetry arrives at the host's frame rate (10 Hz moving, 2 Hz idle).

Not verifiable without the car: real stop latency, ultrasonic accuracy with motors running,
free RAM with ArduinoJson 7, behaviour with the SunFounder app.

### Review (2026-11-01, Milestone 3): the 1.4.5 base, compile only

The 1.4.5 patch applies cleanly to all three commits that call themselves 1.4.5 (`492912f`,
`cad68d5`, `7fec2f7`). Compiled with arduino-cli 1.5.1, `arduino:avr` 1.8.8, `--clean
--warnings all`, IRLremote 2.0.2, SoftPWM 1.0.1:

| Build | ArduinoJson | Flash | Static RAM | Left for stack + heap | New warnings |
|---|---|---|---|---|---|
| 1.4.5 (`492912f`) stock | 6.21.5 | 21,046 B (65 %) | 1,483 B | 565 B | — |
| 1.4.5 (`492912f`) + patch v2 | 6.21.5 | 21,744 B (67 %) | 1,516 B | 532 B | none |
| 1.4.5 (`7fec2f7`) + patch v2 | 6.21.5 | 21,766 B (67 %) | 1,516 B | 532 B | none |
| 1.4.5 (`492912f`) + patch v2 | 7.4.3 | 23,642 B (73 %) | 1,348 B* | 700 B* | none |

\* ArduinoJson 7 puts the reply document on the heap, so these RAM figures leave it out
(see above). Flash with 6.21.5. Never uploaded, never run.

### Applying it (only after agreeing to flash)

```bash
git clone https://github.com/sunfounder/zeus-car && cd zeus-car
git checkout 1.4.5     # the version on this robot (or 82467e9 with zeus_car_locked_in.patch)
cp /path/to/locked-in/firmware/optional/locked_in_patch.h Zeus_Car/
git apply /path/to/locked-in/firmware/optional/zeus_car_locked_in_1.4.5.patch
arduino-cli lib install ArduinoJson@6.21.5 IRLremote@2.0.2 SoftPWM@1.0.1
arduino-cli compile --fqbn arduino:avr:uno Zeus_Car   # 1.4.5: expect 21,744 B flash, 1,516 B RAM
# Flip the shield's small switch to the upload position, then:
arduino-cli upload --fqbn arduino:avr:uno -p /dev/cu.usbserial-XXXX Zeus_Car
# Flip the switch back to run.
```

If ArduinoJson 7 is already installed globally and you do not want to downgrade it, build
with a separate library folder: `ARDUINO_DIRECTORIES_USER=$PWD/.arduino arduino-cli lib
install …` and the same variable on `compile`/`upload`.

Then, still with motor commands disabled, `make hardware`: the Robot panel's gate must show
"Device-side command expiry ✓ (LOCKED IN firmware patch v2 reported by the robot)". Only then
set `LOCKEDIN_ZEUS_MOTOR_COMMANDS=bench` in `.env` (wheels-off-ground profiles only, no
autonomy) and follow the R5 procedure in [HARDWARE_TESTS.md](../docs/HARDWARE_TESTS.md)
(dashboard link-freeze test, then the Wi-Fi-off check), car on a stand. Only if the car stops
within ~0.5 s every time, set `LOCKEDIN_ZEUS_DEVICE_WATCHDOG_VERIFIED=true`,
`LOCKEDIN_ZEUS_DEVICE_WATCHDOG_TIMEOUT_S` to the worst measured value, and
`LOCKEDIN_ZEUS_MOTOR_COMMANDS=enabled`.

### Backup, flash, rollback (with the team, after approval; none of this has been run)

Opening the USB serial port resets the Arduino (DTR), and both reading and writing need the
shield's small switch in **upload** (it cuts the ESP32-CAM off the serial line). Car on a stand,
wheels off the ground.

```bash
AVRDUDE=~/Library/Arduino15/packages/arduino/tools/avrdude/8.0.0-arduino1   # bundled with arduino-cli
PORT=/dev/cu.usbserial-1130          # `arduino-cli board list` shows the current one
AVR="$AVRDUDE/bin/avrdude -C $AVRDUDE/etc/avrdude.conf -p atmega328p -c arduino -P $PORT -b 115200"

# 1. Back up exactly what runs on the robot now (read only).
$AVR -U flash:r:zeus-backup-$(date +%Y%m%d-%H%M).hex:i
shasum -a 256 zeus-backup-*.hex      # write the hash down next to the file

# 2. Build the patched image (above), then upload; avrdude reads the flash back to verify.
arduino-cli upload --fqbn arduino:avr:uno -p $PORT Zeus_Car

# 3. Switch back to run, power-cycle, `make hardware`: the gate must show patch v2.
```

**Rollback** (switch in upload again): write the backup back, verified the same way:

```bash
$AVR -U flash:w:zeus-backup-<stamp>.hex:i
```

The backup is the rollback image because it is byte for byte what ran before; it also holds
the bootloader area, which the bootloader cannot overwrite and which is identical anyway. Without
a backup, rebuild the stock sketch from source (`git checkout -- Zeus_Car`, delete
`Zeus_Car/locked_in_patch.h`, compile, upload). On this Mac (arduino-cli 1.5.1, `arduino:avr`
1.8.8, ArduinoJson 6.21.5, IRLremote 2.0.2, SoftPWM 1.0.1) the two 1.4.5 images are:

| Image (`492912f`) | `Zeus_Car.ino.hex` sha256 | Flash |
|---|---|---|
| stock | `591bb1aa5ebbf851433e87e4b3423c9cc80eeb17518850a49c167ac8fa0415c9` | 21,046 B |
| + patch v2 | `467ce186324865a3948dcea94b873654677f5035d461b14d2a3b3571807f4d97` | 21,744 B |

Another toolchain gives other bytes from the same source, and the robot may run one of the
other 1.4.5 commits, so a rebuild is a fallback, not a copy. After a rollback `make hardware`
must report "no device-side command expiry ... stock firmware" again, and motion stays locked.

### Native test of the patch logic

`firmware/test/patch_harness.cpp` compiles the real `locked_in_patch.h` for the Mac against
stubbed Arduino APIs and a fake `millis()`, and checks: no trip with frames at 10 Hz; stop at
401 ms of silence, not at 400; one trip per stall; stale frames after a trip are ignored until
Stop (F); stock modes are never stopped; `millis()` wraparound is not a stall; the telemetry keys.
It runs with the backend tests (`tests/test_firmware_patch.py`). Three deliberately broken
copies of the header (`>=` for `>`, no latch, Stop not clearing the latch) each fail it. It
proves the logic, not the timing on the ATmega328P or the motor driver: that is R5.
