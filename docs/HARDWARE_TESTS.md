# Hardware tests R1-R6: procedure

None of these tests has been run yet. They gate what LOCKED IN may do on the real Zeus Car
([SAFETY.md](SAFETY.md) §5).

**Since Milestone 3 they need the firmware patch first.** On stock firmware LOCKED IN sends no
motor command at all (SAFETY.md §7). Prerequisites: Z1-Z11 passed
([REAL_HARDWARE_TESTS.md](REAL_HARDWARE_TESTS.md)), patch v2 flashed with your approval and
reported by the robot, `LOCKEDIN_ZEUS_MOTOR_COMMANDS=bench` (only the `LOW_SPEED_TEST` and
`LINK_TEST` profiles, no autonomy) until R5 passes. R1's idle-link part is Z4. Run them in order, together, with the roles below. Write every
result into [PERFORMANCE.md](PERFORMANCE.md) using the template at the end, including
failures.

## Roles

| Role | Does | Must not |
|---|---|---|
| **Operator** | runs the dashboard, starts each step, presses **E-STOP** (top bar) on any doubt | look away from the robot during motion |
| **Spotter** | hand near the robot, ready to lift it or flip the shield's power switch | stand in the direction of travel |
| **Recorder** | films the wheels with the laptop screen in frame (phone slow-motion 240 fps if possible, else 60 fps), says the trial number out loud before each run | — |

With two people, the operator also records (phone on a stand).

## Equipment

- A box or stand that holds the chassis with all four wheels **at least 2 cm clear** of any
  surface. Check that the stand cannot tip when the wheels spin.
- Dummy payload with the weight of the phone (about 200 g), **non-magnetic**, fixed with the
  same mount as the phone. The real phone only goes on after R6.
- Tape measure, masking tape for floor marks, a flat box (for the ultrasonic check).
- Battery charged (charge over USB-C until the shield shows full), laptop on mains power.
- A clear floor area: 1.5 m × 1.5 m, nothing fragile, no table edges, no cables.

## Before every session

1. **Wiring:** each motor plug in the port the SunFounder manual assigns; ultrasonic, IR
   and grayscale connectors seated; ESP32-CAM seated in its adapter; no loose wires near the
   wheels.
2. **Wheels:** rollers form an **X** seen from above (manual). Wrong pairs make strafes go
   diagonal in R2.
3. **Shield switch** in the run position (not upload).
4. **Mount:** payload fixed with Velcro and two cable ties; tug test.
5. **Magnets:** nothing magnetic within 20 cm of the compass (speaker, MagSafe items,
   laptop edge).
6. **Firmware:** write down which Arduino sketch is flashed (stock 1.4.6, or the LOCKED IN
   patch v2) and the ESP32 version line from the serial monitor (`ESP32 firmware version …`).

## Safe startup order

1. Robot **on the stand**, power **off**.
2. Laptop: `make demo` (or `make dev`). Dashboard opens in Mode C (simulated).
3. Robot power **on**. LEDs go orange, then green when `setup()` is done.
4. Laptop joins the `Zeus_Car` Wi-Fi (password `12345678`).
5. Dashboard → Robot → **Zeus** → `192.168.4.1` → **Connect**.
6. Debug drawer → **Robot wire**: `{"F":true}` keepalives every 500 ms; replies are `null`
   (stock) or `{"li":2,"us":…,"wd":0,…}` (patch v2). No replies at all: shield switch.
7. Only now **Arm**, and only for the test being run. Disarm between tests.

Shutdown: Disarm → Disconnect → robot power off → laptop leaves the robot's Wi-Fi.

## Abort criteria (any test)

Press **E-STOP**, then lift the car or flip its power switch, if any of these happens:

- a wheel turns when nothing is commanded, or keeps turning after the dashboard says idle;
- the car rotates on its own (compass);
- "link degraded" repeats, or the dashboard disconnects mid-motion;
- the mount loosens, a wire touches a wheel, anything smells hot.

Write down what happened before continuing. Two aborts in one test: stop testing and
investigate.

---

## R1 · Link at idle (stand)

**Goal:** the link is stable and nothing moves while idle.

1. Connected and **disarmed**: watch for 2 minutes.
2. Robot panel: link `connected`, round trip (rtt) shown, frames sent and received both
   increasing.
3. With patch v2: the Robot wire shows `us`, `ir`, `md:0`, `wd:0`, `lk:0`, `fr`. Write down
   `fr` (free RAM, bytes).
4. With patch v2, ultrasonic check: hold the flat box squarely in front of the sensor at
   20, 50 and 100 cm (tape). Write down `us` for each, three readings per distance.

**Pass:** 2 min connected, no motion, no degraded events. Ultrasonic within ±3 cm at 20 and
50 cm (otherwise obstacle sensing is not trustworthy; tell the team).
**Unlocks:** R2.

## R2 · Directions (stand, wheels free)

**Goal:** every primitive turns the wheels the way the code expects.

1. Arm. Robot panel: enable the **low-speed test profile** (speed ≤ 45, ≤ 0.5 s).
2. Jog each primitive once at speed 40: forward, backward, strafe left, strafe right,
   rotate left, rotate right. Film each.
3. Note each wheel's direction (FL, FR, RL, RR: forward / backward / still).

Expected pattern (front of the car = the side the ultrasonic faces), derived from the
sketch: `car_control.h` numbers the motors [0] front-left, [1] front-right, [2] rear-right,
[3] rear-left, and `carMove()` gives the diagonal pairs 0/2 and 1/3 the same power for a
translation:

| Command | FL | FR | RL | RR |
|---|---|---|---|---|
| forward | fwd | fwd | fwd | fwd |
| backward | back | back | back | back |
| strafe right | fwd | back | back | fwd |
| strafe left | back | fwd | fwd | back |

Rotation goes through the compass loop, so on the stand it may turn briefly and settle:
note what happens, it is not a failure by itself.

**Pass:** the four translations match the table. **Unlocks:** floor tests (R3).

## R3 · Minimum moving speed (floor, dummy payload)

**Goal:** the lowest speed that reliably moves the loaded car, to set the profile speeds.

1. Car on the floor, payload on, 1 m clear ahead and behind. Arm, test profile.
2. Forward 0.5 s at speed 25. Five tries. Then 30, 35, 40, 45 until 5/5 tries move the car
   at least 2 cm.
3. Repeat for backward and one strafe at the speed found.

**Pass/record:** the lowest speed with 5/5 for each direction. Set the retreat and manual
speeds at least 10 above it (Settings → Motion). **Unlocks:** R4.

## R4 · Stop latency (floor)

**Goal:** how long the wheels keep turning after STOP.

1. Floor, payload, 1 m clear. Arm, normal profile.
2. Forward jog at speed 40 (the normal profile allows up to 1.0 s). About 0.4 s in, press
   **STOP**. Film the wheels and the screen.
3. Ten trials. From the video, count frames between the STOP button highlighting and the
   last wheel stopping. The timeline also shows `robot.stop_requested` and `robot.stop_sent`
   (host side only).

**Pass:** ≤ 250 ms in 10/10. **Unlocks:** operator jog with the phone mounted (after R6).

## R5 · Device-side stop on a silent link

**Goal:** prove the car stops **on its own** when the laptop goes silent but the connection
stays open (hung laptop, stalled Wi-Fi). This is the gate for autonomous motion on the real
car: until it passes, the safety layer refuses autonomous retreats (`device_watchdog_unverified`).

What to expect:

| Firmware | Expected | Why |
|---|---|---|
| Stock 1.4.6 | **fails**: wheels keep turning for the whole freeze (up to the ESP32's 3-4 s timeout) | the stock sketch has no command timeout ([HARDWARE_PROTOCOL.md](HARDWARE_PROTOCOL.md) §6) |
| LOCKED IN patch v2 | wheels stop **0.3-0.5 s** after the freeze starts, result row says the Arduino reported a watchdog trip | the watchdog counts 400 ms from the **last frame received**, which the 10 Hz refresh sent up to 0.1 s before the freeze, plus wheel spin-down |

### R5a · Link-freeze test, wheels off the ground (5 runs)

1. Car **on the stand**. Arm.
2. Debug drawer → **R5 link test** → setup **Wheels off the ground**, forward, speed 30,
   freeze 2 s.
3. Recorder films the wheels **and the big freeze timer** together. Say the run number.
4. **Run link test**. The robot drives 0.5 s, then the timer turns red: transmission is
   paused with the socket open. After 2 s the laptop sends stop.
5. From the video: the timer reading at the frame where the wheels stop. The result row
   must show `frames` = 0 (nothing was written during the freeze).

E-STOP during the freeze ends it at once.

### R5b · Wi-Fi off (2 runs, wheels off the ground)

The realistic version of the same failure.

1. Car on the stand, armed, normal profile. Start a 1.0 s forward jog at speed 30.
2. About 0.3 s in, turn the laptop's Wi-Fi off (menu bar). Film the wheels.
3. Measure from the Wi-Fi click to the wheels stopping.
4. Wi-Fi back on, rejoin `Zeus_Car`, reconnect in the dashboard (the robot comes back
   **disarmed**), check Robot wire is normal.

### R5c · On the floor (3 runs, only after R5a 5/5)

1. Floor, payload, 1.5 m clear ahead. Tape a start line at the front wheels.
2. R5 link test → setup **On the floor**, forward, speed 30, freeze 1.0 s.
3. Measure the distance travelled; compare with the R3/R4 numbers (the car should travel
   about 0.5 s worth of motion, not 1.5 s worth).
4. Put the car back and press **Set home** before any other motion: the link test does not
   enter the dead-reckoning log.

**Pass:** R5a 5/5, R5b 2/2 and R5c 3/3 with the wheels stopping ≤ 0.5 s after the freeze
(or the Wi-Fi click). Then, in `.env`:

```
LOCKEDIN_ZEUS_DEVICE_WATCHDOG_VERIFIED=true
LOCKEDIN_ZEUS_DEVICE_WATCHDOG_TIMEOUT_S=<worst measured, rounded up to 0.1 s>
```

Restart the backend. If R5 fails (stock firmware always will): autonomous retreat stays
locked on the real car. Manual jog, voice and the simulated robot still work. Flashing the
patch is a team decision ([firmware/README.md](../firmware/README.md)).

## R6 · Compass with the phone mounted (floor)

**Goal:** no rotation caused by the phone's magnets.

1. Mount the **real phone** (same mount). Calibrate the compass with the phone on: IR remote
   mute key, the car spins for about a minute on the floor with nothing nearby.
2. Lay a 1 m tape line on the floor and align the car with it.
3. Arm. Ten retreats (backward, the configured speed and duration) with a few seconds
   between them, then put the car back on the line each time.
4. Watch for rotation between motions (the car is parked in `MODE_NONE`, motors off, so
   there should be none) and measure the heading error after each retreat against the line.

**Pass:** no rotation while parked; heading error < 10° on every retreat.
**Unlocks:** autonomous retreat on the real car (with R5 passed): Robot panel → Autonomous
retreat, with the clear-path confirmation.

---

## Recording template

Copy one block per test into [PERFORMANCE.md](PERFORMANCE.md).

```markdown
### R? · <name> · <date> <time>

| Field | Value |
|---|---|
| People | operator / spotter / recorder |
| Arduino sketch | stock 1.4.6 / LOCKED IN patch v2 (ArduinoJson version) |
| ESP32 firmware line | |
| Battery | full / after N min of tests |
| Surface, payload | stand / floor (type), dummy 200 g / phone |
| Settings | speed, duration, freeze, profile |
| Video files | |

| Trial | Result (ms, cm, direction…) | Dashboard note (result row, events) |
|---|---|---|
| 1 | | |

Pass? yes / no · Notes:
```

## After the tests

1. PERFORMANCE.md: fill the hardware rows with the measured numbers (n, conditions).
2. `.env`: device watchdog settings from R5 (only if it passed).
3. Settings → Motion: speeds from R3; retreat duration from the R5c / R6 distances.
4. HARDWARE_PROTOCOL.md §11: tick H1-H6 with what was observed.
5. TASKS.md: move the hardware items from 🟡 to ✅ only for what passed.
