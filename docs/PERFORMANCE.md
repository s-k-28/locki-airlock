# Performance and validation results

Only measured numbers appear here. Each row says what produced it. Anything that needs the
assembled robot, a live camera or a person speaking is listed under "Not yet measured";
nothing has been extrapolated.

Machine for every measurement below: MacBook Air (Apple M4, 10 cores, 16 GB RAM), macOS 26.4,
Python 3.12.13, MediaPipe 1.2.0, faster-whisper 1.2.1 / CTranslate2 4.8.4, CPU only (no GPU
acceleration used). Date: 2026-11-01. Raw JSON: `docs/benchmarks/results-*.json`.
Reproduce: `cd backend && uv run python scripts/bench.py all --image <photo with hands>`.

## 1. Measured

### Backend and realtime path

| Metric | n | p50 | p95 | max | Conditions |
|---|---|---|---|---|---|
| Cold start: spawn → first 200 from `/api/health` | 3 | 428 ms | 519 ms | 519 ms | mock robot, no camera/mic |
| WebSocket ping → pong | 200 | 0.14 ms | 0.20 ms | 0.61 ms | localhost, 1 client |
| Event published → received by WebSocket client | 30 | 0.29 ms | 0.51 ms | 0.52 ms | localhost, same clock |
| Snapshot build + send | 200 | 1.04 ms | 2.09 ms | 35.5 ms | live backend, 1 dashboard tab |
| Deterministic intent classification | 2000 | 0.014 ms mean | | | |
| Idle process | | 43 MB RSS, 6 threads | | | mock robot, mic and camera off |

### Vision (MediaPipe Tasks on CPU)

Input: MediaPipe's published sample photo `woman_hands.jpg` letterboxed into a 640x480
frame, two hands visible, 300 frames in VIDEO running mode (tracking on). This measures model
cost per frame, not live-camera accuracy.

| Stage | n | p50 | p95 | max |
|---|---|---|---|---|
| Hand landmarks, 2 hands in frame | 290 | 9.43 ms | 9.75 ms | 10.6 ms |
| Hand landmarks, empty frame | 290 | 9.84 ms | 10.09 ms | 13.4 ms |
| Face detection (runs every 3rd frame) | 97 | 1.30 ms | 1.37 ms | 1.86 ms |
| Overlay render + JPEG encode | 100 | 1.19 ms | 1.26 ms | 41.4 ms (first frame) |

Per-frame processing at p50 ≈ 11.1 ms, a ceiling of ≈ 90 FPS before the camera's own rate
(typically 30 FPS) becomes the limit. The camera thread keeps only the newest frame, so a slow
frame drops stale frames instead of building a backlog.

### Vision on the live webcam

MacBook Air built-in camera, 640x480 mirrored, one person in view and no hands in frame for
most of the run, the dashboard streaming the annotated MJPEG in Chrome on the same laptop.
About 45 s of processing, two snapshots of the pipeline's rolling statistics:

| Metric | Snapshot 1 | Snapshot 2 |
|---|---|---|
| Camera capture rate | 30.9 FPS | 30.1 FPS |
| Processing rate | 25.8 FPS | 30.1 FPS |
| Hand landmarks p50 / p95 | 24.5 / 41.2 ms | 27.4 / 54.4 ms |
| Face detection p50 | 3.9 ms | 4.3 ms |
| Whole frame p50 / p95 | 25.7 / 45.5 ms | 29.5 / 63.8 ms |
| Capture → result p50 / p95 | 27.4 / 57.3 ms | 37.7 / 74.3 ms |
| Frames skipped (newest-frame policy) | 340 of ~1035 | 358 of ~1156 |
| Backend process with camera running | 227 MB RSS, 56 threads | |

Live hand-model time is ~2.6x the still-image benchmark. Profiled on 2026-11-01 (next
section): the web server and the stream are not the cause. At 30 FPS the reach detector's
180 ms confirmation dwell spans ~5 frames.

### Vision profile (2026-11-01, `scripts/profile_vision.py`)

**Replay.** The same camera frames, captured into memory (never written to disk), replayed
back to back through `VisionService.process_frame` in each configuration with fresh models,
3 runs per configuration in rotating order (median of the per-run percentiles; range of
the per-run p50 in brackets). Run A: 240 frames, a hand visible in 17. Run B: 300 frames, no
hand visible. An earlier single-run pass with a hand in 110 of 240 frames gave a 19.6 ms
p50 / 47.4 ms p95 total and 16.1 / 36.0 ms for the hand stage: hands in view cost more,
because palm detection re-runs whenever tracking drops.

| Configuration | Total p50 (run A) | Total p95 (A) | Total p50 (run B) | Hands p50/p95 (A) | Same hands as baseline |
|---|---|---|---|---|---|
| baseline: stream viewer, overlay every frame | 13.2 (13.1-14.7) | 17.7 | 13.1 | 10.1 / 15.2 | 100 % |
| no stream viewer (overlay every 15th frame) | 11.8 | 17.5 | 11.6 | 11.5 / 15.3 | 100 % |
| face detection off | 13.1 | 18.4 | 13.1 | 10.8 / 15.2 | 100 % |
| marker tracking on (zone follows the robot) | 15.3 | 19.3 | 15.3 | 11.6 / 15.1 | 100 % |
| MediaPipe GPU (Metal) delegate for hands | 9.3 | 19.5 | 9.0 | 6.6 / 16.1 | **92.9 %** (17 frames differ) |

Stage costs (run A, p50): markers 2.2-2.5 ms (every 5th frame in fixed-zone mode, every
frame when tracking), RGB conversion 0.1 ms, reach logic 0.01 ms, face 1.4 ms (every 3rd
frame), overlay render + JPEG 1.3 ms.

**Live, threaded.** Camera and processing threads as in the backend, 15-20 s each:

| Setting | Capture / processing FPS | Hands p50/p95 | Whole frame p50/p95 | Capture → result p50/p95 |
|---|---|---|---|---|
| backend, 3 dashboard stream viewers connected | 30.7 / 31.4 | 21.6 / 36.9 | 22.0 / 40.0 | 22.9 / 57.5 |
| standalone (no web server), no viewer | 30.3 / 30.6 | 22.1 / 33.8 | 23.9 / 36.6 | 24.2 / 41.9 |
| standalone, one simulated viewer (render every frame) | 30.5 / 31.3 | 15.3 / 26.0 | 17.2 / 28.9 | 17.4 / 41.1 |

Findings:

- The pipeline keeps up with the camera (processing ≈ capture FPS); p50 is well inside the
  33 ms frame interval, and the newest-frame policy bounds latency when a slow frame happens.
- The web server, WebSocket and MJPEG streaming add nothing measurable: the backend and the
  standalone pipeline are within run-to-run noise (the two standalone rows differ by more
  than the viewer could explain; they ran one after the other).
- Live hand inference is 1.5-2x the back-to-back replay of the same kind of frames. The thread
  QoS class makes no consistent difference (probe: 9.7-13.7 ms p50 across all five macOS QoS
  classes, no ordering). The remaining explanation, not proven: with frames every 33 ms the
  CPU idles between inferences and runs them at lower clocks.

Decisions (nothing optimised):

- GPU delegate: **rejected**. Faster median, no better p95, and it disagreed with the CPU
  results on 17 frames in run A (7.1 %). It stays available only as an experiment
  (`HandTracker(delegate="gpu")`, feeding 4-channel frames as the Metal path requires).
- Face off: saves < 1 ms on average and would lose the focus estimate. Kept on.
- Overlay rendering already runs only every 15th frame without a viewer.
- Marker tracking costs ~2 ms per frame when enabled; acceptable.
- The response time that matters is dominated by the 180 ms dwell and the robot, not by
  these 10-20 ms.

**Event-loop stalls found and fixed.** Starting the camera the first time stalled the asyncio
loop for 434 ms and 288 ms while MediaPipe loaded its graphs (no stalls during the 45 s of
processing). The watchdog stops any motion during a stall > 250 ms, so a stall during a retreat
would have aborted it. Models are now loaded at backend startup (629 ms, before anything can
move); after the change, a restart plus camera start/stop recorded no stalls (one run).

### Decision and command path (software)

From the dashboard run of the demo script (simulated reach input, mock robot with 30 ms
configured latency), times measured from the frame-capture timestamp of the reach:

| Stage | Time |
|---|---|
| Reach event → policy decision | +0.5 ms |
| Safety approval + motion frame sent | +1.5 ms |
| Acknowledgement (simulated robot) | +31.6 ms |
| Commanded window complete, stop sent | +602 ms (0.6 s retreat) |

Zeus driver against the firmware model (`tests/fake_esp32.py`) over localhost. This measures
our driver and WebSocket path only, not Wi-Fi, the ESP32 or the Arduino:

| Metric | n | p50 | p95 | max |
|---|---|---|---|---|
| `move()` → model in app-control mode | 40 | 1.38 ms | 1.56 ms | 1.59 ms |
| `stop()` → model in `MODE_NONE` (after adding the 25 ms first-stop gap) | 40 | 1.38 ms | 2.02 ms | 3.11 ms |
| `stop()` → model in `MODE_NONE` (before: every frame 45 ms apart) | 40 | 23.45 ms | 23.63 ms | 23.73 ms |

### Voice

Speech-to-text: faster-whisper `base.en`, int8, in a separate worker process (keeps PyAV out
of the OpenCV process). Model load from the local copy: 600 ms.

Command accuracy on **synthetic speech**: 18 commands from the brief + 3 non-commands, each
rendered by three macOS voices (Samantha, Daniel, Karen) with `say`, resampled to 16 kHz and
fed straight to Whisper (no microphone, no room):

| Metric | Result |
|---|---|
| Correct intent (or correctly ignored) | **63 / 63 (100 %)** |
| Non-commands acted on | 0 / 9 |
| Whisper round trip, 1-3 s clips | p50 498 ms, p95 565 ms, max 604 ms (n = 63) |

Clean synthetic audio is far easier than a person in a hackathon hall. One live trial
(laptop speaker → laptop microphone, push-to-talk) was misrecognised ("It's going to be a
shoddy spot.", confidence 0.39) and was **correctly rejected, with no action taken**.
Real speech accuracy is test A1, not yet run.

Text-to-speech (macOS `say`):

| Metric | Result |
|---|---|
| `say` fixed start-up cost, 3-character line, by voice | Daniel 755 ms · Samantha 1013 ms · default 1447 ms |
| Cached line, in-process playback: call → audio stream started | 8-10 ms (n = 4 after warm-up; first call 52 ms) |
| Fixed lines pre-rendered in the background at startup | 47 |

The fixed start-up cost of `say` would add ~1 s before every remark. Every template line
without live numbers ("Bro. We literally just started.") is therefore rendered once and played
from memory. Lines that quote session numbers still pay the `say` cost.

Through the milestone 2.5 voice router (2026-11-01, backend running, volume 0 so nothing was
audible, local voice because no Fish Audio key is configured):

| Line | Source | Queue → start | Total (render + audio) |
|---|---|---|---|
| "Nice try." | local voice, pre-rendered | 0.3 ms | 748 ms (= audio length) |
| "Bro, we literally just started." | local voice, pre-rendered | 0.3 ms | 1,837 ms (= audio length) |
| "2 problems of 5 down. 3 problems to go." | local voice, rendered live | render 2,018 ms | — |
| `say` render to WAV, 3 lines, this laptop | | | 1,587-2,022 ms per line |

Rendering a line that contains a number takes ~2 s before the first sound, which kills a
joke's timing, so reactions use pre-rendered fixed lines 80 % of the time
(`STATIC_JOKE_SHARE`). Pre-rendering ~100-150 fixed lines at startup takes ~2.5 minutes in the
background.

Fish Audio, measured for real (2026-11-01, home Wi-Fi, `s2.1-pro-free`, E-girl voice):
first audio bytes 656 ms, whole 3.90 s clip 2,047 ms, playback start +68 ms (+28 ms device
latency); 39-clip voice pack: median 1.50 s per clip, p95 2.07 s. Local Whisper transcript of
the test clip matched the text 100 %. Details: [VOICE_PERSONALITY.md](VOICE_PERSONALITY.md).

### Firmware patch (compile only, never run)

Patch v2, clean builds with all warnings, fresh upstream clone at `82467e9`: with
ArduinoJson 6.21.5 (recommended) upstream 21,600 B flash / 1,528 B RAM, patched 22,314 B /
1,561 B; with 7.4.3 upstream 23,208 / 1,360, patched 24,212 / 1,393 (v7 keeps the reply
document on the heap, outside these RAM figures). No new warnings. See
[firmware/README.md](../firmware/README.md).

Patch v2 on the robot's own 1.4.5 sketch (Milestone 3, same flags, ArduinoJson 6.21.5): stock
21,046 B flash / 1,483 B RAM, patched 21,744 B / 1,516 B (532 B left for stack and heap); no new
warnings.

### Hardware mode without the robot (Milestone 3, 2026-11-01, home Wi-Fi)

| Measure | Value | Setup |
|---|---|---|
| `make offline-check` | READY, 0 missing, 0 warnings; voice pack 39/39; offline start: 0 non-local connection attempts | this Mac |
| `make hardware` startup to "ready" | 1,349 ms | robot not reachable |
| Robot unreachable | `ZEUS DISCONNECTED`, retrying (1, 2, 3, 5 s backoff); camera `ZEUS ESP32-CAM DISCONNECTED` ("no answer from 192.168.4.1:9000 within 3s"); never the webcam, never the simulator | 192.168.4.1 not on this network |
| Ctrl-C to exit | 2.3 s (MediaPipe graphs closed at shutdown; before this fix the process could hang at exit) | — |
| Against local stand-ins (ESP32 WebSocket model, single-client firmware; MJPEG stand-in at 15 FPS VGA) | connected in < 1 s; WebSocket ping ~0 ms; camera 13.7 FPS arriving vs 13.6 FPS on the stand-in's clock, 0 lost, ~810 kbps; 0 region frames reached the modelled Arduino | localhost: says nothing about Wi-Fi |

### Soak (simulated robot, `make soak`, 2026-11-01)

10 scripted demos back to back plus 12 mixed sessions (reach → retreat → reach while holding
→ return → pause → progress, robot link dropped and reconnected every third session): 1,004
events, 34 motions sent, 0 blocked, 0 invariant failures (unique event ids, increasing
sequence numbers, every sent motion has an ending, per-session counters, no watchdog fires),
event-loop max lag 10.5 ms, 30 s wall time. Software only.

## 2. Not yet measured (needs hardware or people)

| ID | What | Status |
|---|---|---|
| V1-V4 | Reach precision / recall / false positive rate / reach-to-confirmation latency on the live webcam and the ESP32-CAM | live webcam works now; trials need the team at the demo table |
| R3 | Minimum moving speed with phone payload | robot not assembled |
| R4 | Stop latency, frame → wheels stopped | robot not assembled |
| R5 | Stop on silent link loss (stock firmware expected 2-4 s from source; dashboard link-freeze test ready) | robot not assembled |
| R6 | Heading drift with phone mounted | robot not assembled |
| — | Wi-Fi round trip to the ESP32 (WebSocket ping) and Arduino reply rate on a monitor link | Z4 on the `Zeus_Car` network |
| Z2 | ESP32-CAM stream: FPS, kbps, delay above best, lost frames, with both readers | Z2/Z3 on the `Zeus_Car` network |
| — | Retreat distance and consistency (10 runs) | robot not assembled |
| A1-A3 | Real speech accuracy, false activations in noise, Bluetooth speaker latency ([VOICE_TESTS.md](VOICE_TESTS.md), dashboard voice trials ready) | needs teammates at the demo table |
| — | Time-to-first-audio measured acoustically | needs a microphone loopback test |

## 3. Limitations of the numbers above

- One machine, one session, small n for some rows (startup n = 3).
- Vision timing uses a still photo; motion blur, lighting and real hand poses change both cost
  and accuracy.
- Software latency rows exclude Wi-Fi, the ESP32's serial forwarding, the Arduino's ~1 ms per
  character parsing, `carResetHeading()` (~93 ms) and the 100 ms PWM fade; on the real robot
  those dominate.
- STT accuracy is on synthetic voices.
