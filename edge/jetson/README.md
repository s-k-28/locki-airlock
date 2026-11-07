# AIRLOCK stationary Jetson camera adapter

This adapter supplies camera observations to the same authoritative SpacetimeDB module as the Mac publisher. It has no motor, GPIO, firmware, lock or remote-shell controls. Camera capture and database publication each require an explicit CLI flag. Nothing in this directory has been validated on a physical Jetson or volunteer video during implementation.

The default detector uses OpenCV's **built-in HOG pedestrian classifier**. It performs real local classification without downloading a model. It is a CPU fallback, not evidence of GPU inference, Nemotron use or a new trained model. Full-body HOG often misses seated or partially occluded people; the marked desk region and camera placement require actual human trials.

`--detector motion` uses background subtraction within the selected ROI. Motion-only operation reports **degraded person coverage**, exposes motion fraction in metrics, and never emits `person_in_zone`. It cannot upgrade a USB incident as person evidence. USB protection remains active independently of that limitation.

## Inventory and dependencies

Run the read-only inventory before selecting a camera or changing hardware:

```sh
python3 edge/jetson/inventory.py
```

It records the device-tree model when present, L4T release, architecture, RAM, camera device names and available utilities. It does not open a camera, collect raw USB serials or identify a power adapter from software. The exact Jetson model, JetPack version, Logitech camera and physical power label remain unverified until Areeb records them on the actual device.

Use the OpenCV provided by the JetPack installation where available. Check it without opening a camera:

```sh
python3 -c 'import cv2; print(cv2.__version__)'
```

For an isolated desktop fallback environment, `requirements.txt` lists the optional CPU OpenCV package. Do not overwrite JetPack's existing OpenCV/CUDA packages to run this prototype. No model weight download is required. The stdlib tests and inventory do not require OpenCV.

## Enroll and run

Create a separate device identity using the repository's existing publisher utility, then enroll its printed public identity in the owner dashboard with role **Jetson**. Keep the token file private and mode 0600. Copy only that device's token to the Jetson; an owner token is not a device credential.

```sh
node scripts/airlock_publisher.mjs --new-identity data/airlock/jetson-token.private.json
```

First inspect local metadata with a consented camera and volunteer. This opens the selected camera but does not publish to the database or store images:

```sh
python3 edge/jetson/airlock_camera.py \
  --capture --camera 0 --detector hog \
  --workstation YOUR_WORKSTATION --device YOUR_ENROLLED_JETSON_ID \
  --state-file data/airlock/jetson-state.private.json \
  --roi 0.1,0.05,0.8,0.9 --fps 3 --duration 600
```

The ROI is `x,y,width,height`, normalized to the frame and validated within `[0,1]`. A detected bounding-box center must lie inside it. Defaults debounce 3 positive frames before entry and 5 negative frames before clearing. Continuous confirmed presence emits fresh evidence every 5 seconds so a USB event can correlate while a volunteer stays in the zone. These defaults are engineering settings, not measured sensitivity/specificity.

For the primary cloud database, configure:

```sh
export AIRLOCK_SPACETIME_URI=https://maincloud.spacetimedb.com
export AIRLOCK_DATABASE=locki-w91bw
```

For the preserved local fallback, use the database host's actual URI and `AIRLOCK_DATABASE=locki-airlock-sid`. `127.0.0.1` on a Jetson refers to the Jetson itself. A database running on another Mac needs an explicitly configured reachable LAN address.

After the owner enrolls the device and arms a **live** session with auto-lock disabled for initial trials, enable publication:

```sh
python3 edge/jetson/airlock_camera.py \
  --capture --publish --camera 0 --detector hog \
  --workstation YOUR_WORKSTATION --device YOUR_ENROLLED_JETSON_ID \
  --token-file data/airlock/jetson-token.private.json \
  --state-file data/airlock/jetson-state.private.json \
  --roi 0.1,0.05,0.8,0.9 --duration 600
```

The adapter discovers its owner's current session through authorized views every 3 seconds. It publishes `camera_online`, `camera_offline`, `person_in_zone`, `person_cleared` and heartbeats using `ingest_device_event`. It reports camera and process health through `report_health`, including while disarmed. Real frames are never relabeled synthetic to fit a fixture session: synthetic sessions are skipped. The persisted sequence and metadata queue survive restarts; an exclusive process lock prevents two processes from sharing sequence state.

Retries retain the same event ID. The queue is bounded to 128 redacted metadata events, drops evidence older than 120 seconds or from a closed session, and exposes delivery errors/drops in metrics. Policy/authentication 4xx rejections are counted and dropped rather than retried forever. A lost state file requires the owner to disarm and re-enroll the device before restarting at sequence 1. Driver-level camera reads can still block despite the requested read timeout; independent health freshness and the server's 15-second timeout show degraded coverage in that case.

## Health and measurements

The process starts a read-only HTTP server on `127.0.0.1:8787`:

```sh
curl -i http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/metrics
```

`/health` returns 200 only when the HOG camera feed and configured database publication are healthy; it returns 503 for unavailable/stale capture, motion-only degraded coverage, or an unavailable database. `/metrics` exposes frame count/age, average FPS, the last 120 processing durations' p95, process CPU seconds, peak RSS, retries, drops, pending count and motion fraction. It returns no frame, video, credential or USB identifier. GPU utilization is explicitly **not measured** by the CPU adapter; use the Jetson's installed `tegrastats` separately when available and record actual samples.

A non-loopback `--bind` requires `--api-token-file`, a mode-0600 file containing a random bearer token of at least 16 characters. Supplying that option also protects loopback routes. There are no camera-image, mutating, notification or local-test-alert endpoints.

No frame is written to disk or sent to SpacetimeDB. The only saved data is private sequence state and bounded redacted metadata awaiting delivery. HOG margins are bounded into the v1 confidence field for transport and explicitly labeled **uncalibrated**; they are not probabilities, identity recognition or a hardware accuracy result.

## Verification completed and remaining

```sh
python3 -m py_compile edge/jetson/*.py
python3 -m unittest discover -s edge/jetson/tests -v
```

The 22 tests use fake classifier outputs and stdlib data structures, not a camera. They verify ROI bounds, debounce/reset/presence refresh, persisted dedup/retry sequence, queue limits and expiry, exclusive publisher locking, credential permissions, exact reducer argument order, actual SQL response decoding, stale-feed health, motion-only degradation, loopback/LAN authentication and explicit capture opt-in. Passing them does not establish camera accuracy or Jetson throughput.

Areeb still needs to record the actual inventory, 10-minute stream/recovery run, camera-unplug event on a second subscribed client, 20–40 consented positive/benign trials, measured frame-to-database timing, CPU/GPU/RAM and a proof clip. Report misses and false positives. Keep all robot wheels stationary.

Official OpenCV references checked on 2026-11-07: [HOG descriptor and built-in detector](https://docs.opencv.org/4.13.0/d5/d33/structcv_1_1HOGDescriptor.html), [background subtraction and camera reads](https://docs.opencv.org/4.13.0/d1/dc5/tutorial_background_subtraction.html).
