# Hardware assembly, setup and calibration

## 1. Inventory check (SunFounder Zeus Car, Micro Center SKU 917674)

Verify against the official manual (https://docs.sunfounder.com/projects/zeus-car/en/latest/)
as you unpack, and note anything missing here:

- [ ] Arduino UNO-compatible board + Zeus Car shield (with the small ESP32/upload switch)
- [ ] ESP32-CAM module and its adapter
- [ ] 4 DC gear motors + 4 mecanum wheels (two left-handed, two right-handed; check the roller
      orientation forms an X when viewed from above, per the manual)
- [ ] Ultrasonic module (single-pin trig/echo on D10 per the source), 2 IR obstacle sensors
      (read through a 74HC165 shift register), grayscale line module, QMC6310 compass
- [ ] RGB LED strip, IR remote + receiver
- [ ] Battery pack (charged over USB-C on the shield), screws, standoffs, cables

Purchased Bluetooth speaker model: **TBD, confirm** (the brief mentions the JLab GO Party).

## 2. Assembly notes that matter for LOCKED IN

1. **Wheel orientation** decides whether strafing works. If R2 shows strafe moving diagonally
   or rotating, two wheels are swapped.
2. **Phone mount:** low and centred over the chassis, as far as possible from the compass
   module. Industrial Velcro under the cradle **plus** two cable ties through the chassis
   holes. Keep the battery switch and the USB-C port reachable.
3. **Magnets:** a MagSafe phone, a MagSafe wallet or the speaker's magnets near the compass
   bias it. Run the compass calibration **with the phone mounted** (IR remote mute key, or
   app region E). The car spins in place for about a minute: do it on the floor, nothing nearby.
4. **ESP32-CAM:** mount it facing forward. If you want it to watch the user's hand, tilt it up
   and test it as a camera source (test V4) before relying on it.
5. Keep the robot's own Wi-Fi antenna (on the ESP32-CAM) unobstructed by the phone.
6. **Tracking markers:** print ArUco markers 7 and 8 (dashboard → Vision → Print marker), black
   square 50-60 mm, and mount them upright on the cradle facing the laptop camera, one each side
   of the phone. See [ROBOT_TRACKING.md](ROBOT_TRACKING.md).

## 3. Bring-up sequence (no LOCKED IN yet)

1. Charge the battery. Power on: LEDs orange then green (`setup()` finished).
2. Optional sanity check with the SunFounder Controller app (then **close the app**: the
   ESP32 forwards frames from every connected client).
3. Serial monitor at 115200 during boot: note the line `ESP32 firmware version …`. Record it
   in [HARDWARE_PROTOCOL.md](HARDWARE_PROTOCOL.md) §11 (H1). Do not OTA-update the camera.

## 4. Connecting LOCKED IN

1. Join the `Zeus_Car` Wi-Fi (password `12345678`) on the laptop. The laptop has no internet
   while joined; LOCKED IN does not need it.
2. Dashboard → Robot → **Zeus** → address `192.168.4.1` → Connect.
3. Debug drawer → **Robot wire**: you should see `{"F":true}` keepalives every 500 ms and
   `null` echoes from the Arduino. If there are no echoes, the shield switch is probably in the
   upload position.
4. The car's RGB LEDs: white = `MODE_NONE` (parked), magenta = app control (moving or holding
   heading).

Alternative network (needs a sketch edit and re-flash, so team approval first): set
`WIFI_MODE WIFI_MODE_STA` with a phone hotspot's SSID/password in `Zeus_Car.ino`, join the
laptop to the same hotspot, and use the IP the serial monitor prints.

## 5. Calibration

| Step | How | Result to write down |
|---|---|---|
| Directions | R2: car on a box, jog each arrow at speed 40 | which wheels spin which way |
| Minimum speed | R3: on the floor with a dummy weight, raise speed until 5/5 moves | e.g. "moves reliably from 45" → set `manual_speed`/`retreat_speed` |
| Retreat distance | 10 retreats at the chosen speed/duration; measure with tape | mean ± spread |
| Stop latency | R4 | ms |
| Device stop | R5 | seconds → `.env` |
| Phone zone | Vision → Zone: drag the corners around the phone as the camera sees it | saved automatically |
| Reach sensitivity | V1/V2 trials; adjust dwell/margin in Settings | precision / recall |

## 6. Camera placement

The MacBook camera faces the user. Place the robot so the phone sits inside the camera's
view, low in the frame (bottom-right by default), not hidden behind the laptop lid. If that is
not possible, try the ESP32-CAM stream as the source (Settings → Camera) and compare with
test V4.

## 7. Bluetooth speaker

macOS switches a speakerphone to a low-quality headset profile when its microphone is used.
Use the **laptop microphone** for input and the speaker for output only (Settings → Voice →
Speaker). Measure the added latency (A3).
