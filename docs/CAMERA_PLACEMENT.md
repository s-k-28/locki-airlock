# Camera placement: fixed laptop camera or the robot-mounted ESP32-CAM

Milestone 3 audit of what each camera can see, now that the ESP32-CAM on the Zeus Car is a
camera source. Nothing here has been checked with the real car yet (test Z9 and Z10 in
[REAL_HARDWARE_TESTS.md](REAL_HARDWARE_TESTS.md)).

## The two geometries

| | Mac webcam ("0") | Zeus ESP32-CAM ("zeus") |
|---|---|---|
| Where | top of the laptop screen, facing the user | front of the car, facing forward, a few cm above the desk (check the bracket angle) |
| Moves | no | with every robot motion, including rotation |
| Image | 640×480 requested, ~30 FPS measured, mirrored self-view | 640×480 by default (`FRAMESIZE_VGA` in the camera firmware), FPS to be measured (Z2), not mirrored; flip setting if the module is rotated |
| Sees the user's face | yes, frontal | from below at desk height, often cut off |
| Sees the phone | yes, if the robot is in view | **never**: the phone rides on the robot behind/above the lens |
| Sees the robot | yes, if placed in view; position from ArUco markers 7/8 | never (its own markers are not in its view) |
| Sees a hand reaching for the phone | yes, the hand entering the phone zone around the robot | yes, the hand coming toward the robot, large and close in the lower part of the image |

## Answers to the brief

1. **Which camera must observe the user?** For focus (face present / turned away) the Mac
   webcam: frontal and stable. The robot camera may not see a face at all, so with it a
   missing face counts as "not in view" (focus unknown), never as "away".
2. **Which camera must observe the phone?** Only an external camera can: the phone is on the
   robot, behind the ESP32-CAM. With the robot camera, the phone's position does not need to
   be known: a hand entering the space in front of the robot *is* a hand approaching the
   phone. The zone is fixed in that camera's image, so it is robot-relative by construction.
3. **How is the robot's position established?** There is no odometry or localisation on
   stock firmware. In the Mac webcam's view, ArUco markers on the cradle give it
   ([ROBOT_TRACKING.md](ROBOT_TRACKING.md)). With the robot camera it is not needed for reach
   detection, and LOCKED IN does not pretend to know it.
4. **Markers?** Only for Mac-webcam mode: upright on the cradle, facing the laptop. The robot
   camera never uses them; LOCKED IN refuses "Follows robot" with that camera.
5. **Can the hardware see the required scene?** Not known until Z9. The risk is the angle:
   a forward-facing camera at desk level sees the desk surface and the user's torso; a hand
   reaching for the robot should be large and close (good for MediaPipe), a face may be
   missing. If hands are not detected reliably at the demo distance, use the Mac webcam.
6. **Physical calibration:** ESP32-CAM tilt and orientation (flip setting in Settings →
   Camera); the robot camera's zone (Vision → Zone with the ESP32 selected; it is stored
   separately from the webcam's zone); for Mac-webcam mode the phone zone and marker attach
   (trials T1-T6); compass calibration with the phone mounted (unchanged).

## What the software does for a moving camera

- Camera position: Auto (the ESP32-CAM is "on robot", everything else "fixed"), or set it in
  Settings → Camera.
- The robot camera has its own reach zone (`robot_camera_zone`), default: the lower middle
  of the image (x 0.2-0.8, y 0.45-1.0). A starting point only.
- While the robot moves, and for 0.6 s after, the whole image moves, so hand speed means
  nothing: reach confirmation pauses ("Reach paused · camera moving with the robot") and
  restarts from the new view. (In this milestone the robot never moves.)
- ArUco tracking and the periodic marker scan are off for the robot camera.
- The frame itself carries the source label; the dashboard names the source everywhere.

## Limits (not hidden)

- **No depth.** A hand far behind, but inside the zone's region of the image, can look like a
  reach. Dwell and approach speed reduce this; precision on the real car is unmeasured.
- **The phone is never seen** by the robot camera, so "hand on the phone" cannot be
  confirmed visually; only "hand at the robot".
- **During a retreat** reach detection is paused (a reach then was already ignored by the
  state machine).
- **Bandwidth and latency:** the stream (~1.7 Mbit/s measured with curl) shares the ESP32's
  Wi-Fi with the robot link; the dashboard shows its delay above the best frame.
- **One camera at a time.** Using the webcam for focus and the ESP32 for reach together would
  need two vision pipelines; not built.

## Recommendation

`make hardware` starts on the ESP32-CAM, as requested. Keep it if Z9 shows hands detected
reliably at the demo distance. Otherwise switch to the Mac webcam in the Vision panel (the
robot link is unaffected) and run the marker trials; that mode was tested on rendered frames
in Milestone 2 but not yet on the car.
