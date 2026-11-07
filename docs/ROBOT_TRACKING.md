# Robot tracking: the phone zone follows the robot

When the robot retreats, the phone goes with it. A zone fixed in camera coordinates would
then point at an empty patch of desk: reaching there would "count", and reaching for the
phone's real new position would not. LOCKED IN therefore tracks printed ArUco markers on the
robot and moves the zone with them.

Status: implemented and tested on rendered frames containing real ArUco codes (32 automated
tests). **Not yet tested with a printed marker on the physical robot** (trial T1-T6 below).

## How it works

1. Print marker 7 (and ideally 8): dashboard → Vision → "Print marker 7", or
   `GET /api/vision/marker.png?marker_id=7`. Print at 100 % so the black square is
   50-60 mm. Matte paper; keep the white border.
2. Stick it flat on the robot or the phone cradle, **facing the laptop camera**. Two markers
   (left and right of the phone) keep tracking alive when a hand covers one.
3. With the robot at home and the marker in view, calibrate the zone around the phone
   (Vision → Zone), then switch **Phone zone → Follows robot**. The zone is stored in each
   marker's own frame (`phone_zone.anchor` in `data/runtime_settings.json`).
4. Every frame: detect markers (on the unmirrored image: mirrored ArUco codes do not decode),
   project the zone from each visible marker, reject outliers, fuse, smooth, and hand the
   zone to the reach detector.

Geometry is a similarity transform (translation, rotation, uniform scale), not a full
homography: four parameters from a small marker are far steadier than eight, and the robot
moves on a plane.

## Failure handling

| Situation | Behaviour |
|---|---|
| Marker never seen since startup / attach | SEARCHING: zone unknown, reach confirmations paused, dashboard "Searching for robot" |
| Marker seen in 3 consistent frames | TRACKING |
| Marker hidden (e.g. the reaching hand covers it) and the robot has **not** been commanded to move | HELD: last pose kept for up to `marker_hold_s` (default 8 s); reaches still detected |
| Marker hidden and the robot moved (or the hold expired) | LOST: zone unknown, reach confirmations paused, timeline + dashboard say so; the status answer says "I can't see the robot's marker" |
| One of two markers covered | still TRACKING from the other |
| Two markers disagree (one is a false detection) | the one consistent with the current track wins |
| Marker with an unconfigured ID, too small, too skewed, or 0.4x/2.5x the calibrated size | ignored |
| Sudden jump > 8 % of the frame while the robot is told to stand still | needs 3 consistent frames (glitch / reflection / robot moved by hand) |
| Jump > 35 % of the frame in one frame | rejected even while moving (not physically possible) |
| Zone moves because the robot moved | hand speeds are re-based so the zone sliding toward a resting hand is not an "approach"; dwell restarts |
| Fixed mode | unchanged behaviour; markers are still scanned every 5th frame so the UI can say "marker 7 in view" |

The robot-moving signal comes from the safety layer's own events (`robot.motion_sent` →
moving; `robot.motion_completed` / `robot.motion_aborted` → stopped).

## Physical trials (to run when the robot is assembled)

| ID | Procedure | Pass |
|---|---|---|
| T1 | Robot at home, marker in view: does the dashboard show "marker 7 in view" at the demo distance? | detected in ≥ 95 % of frames over 10 s (`tracking.stats.fixes`) |
| T2 | Attach the zone, retreat the robot (sim command or manual jog), compare the overlay zone with the phone | zone outline stays on the phone within ~1 cm |
| T3 | Reach while your hand covers the marker | HELD, reach confirmed |
| T4 | Cover the marker, then jog the robot | LOST, no confirmation until the marker reappears |
| T5 | Put a second printout of marker 7 elsewhere in view for 1 s | zone does not jump |
| T6 | Lighting: lamp on/off, screen glare on the marker | note detection rate |

## Limitations

- Needs the marker in the laptop camera's view; at a steep angle (camera high, marker flat
  on top of the robot) detection fails. Mount it upright facing the camera.
- No depth: if the robot moves toward or away from the camera, the zone scales with the
  marker, which is right only if the zone is roughly in the marker's plane.
- Marker detection costs a few milliseconds per frame (measured: docs/PERFORMANCE.md).
