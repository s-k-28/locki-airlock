# LOCKI AIRLOCK

AIRLOCK is the current product mode: an authenticated workstation security control room powered by SpacetimeDB. It correlates independent physical observations with Mac USB/application events, coordinates human review, and authorizes exact, short-lived protected actions before their callbacks run.

The team deployment is [locki-w91bw](https://spacetimedb.com/@dhruvhydrox/locki-w91bw). Sensitive state stays in private tables and caller-scoped views. The browser starts with a distinct identity and receives workstation access only through owner enrollment.

```bash
npm ci --prefix frontend
npm ci --prefix spacetime
make airlock-cloud    # security dashboard at http://127.0.0.1:5174
# Local persistent/offline alternative after installing the official SpacetimeDB CLI:
make airlock          # database locki-airlock-sid; preserves existing data
```

Use Node 24.22.1 or newer within the Node 24 release line and SpacetimeDB 2.14.1. `make airlock` does not start a camera, microphone, model, screen lock or motor. The retained focus interface is available at `?product=legacy`.

Start with **Create my workstation → Arm workstation → Synthetic → Demo fixtures**. Fixtures pass through real reducers and remain visibly labeled. Open `?client=reviewer`, copy its public identity, and enroll it from the owner console to review the same incident with a separate identity. Live arming requires a fresh authenticated capture from the enrolled Mac, and physical observations alone do not trigger a screen lock.

Implemented paths include enrollment/roles, sessions, fresh and frozen baselines, deterministic correlation, private realtime subscriptions, reviewer recovery, one-time digest-bound approvals, native macOS observers, native NeMo Relay guardrails/traces, a bounded Nemotron adapter, a consent-gated lock adapter and the existing priority speech queue. Native Relay runs and cached rehearsal playback have been verified. **Actual Nemotron inference, Jetson/camera trials, genuine USB insertion and a consented real screen-lock/unlock trial remain separate acceptance evidence.** The model resource guard refuses to load the large model under this machine's current memory pressure.

Run instructions and evidence are in [docs/airlock/README.md](docs/airlock/README.md), the [requirements status](docs/airlock/PRD_STATUS.md), [Mac capture guide](docs/airlock/MAC_CAPTURE.md), [NVIDIA integration](docs/airlock/NVIDIA.md), [voice integration](docs/airlock/VOICE.md), and [lock adapter](docs/airlock/LOCK_ADAPTER.md). Measurements distinguish real observations, synthetic evidence, model inference, and tool execution. No detector-accuracy or complete sponsor-compliance claim follows from passing software tests.

The [operator handoff](docs/airlock/HANDOFF.md) covers safe service operation and remaining trials. [Source provenance and licensing](SOURCE_NOTICE.md) describes the sanitized competition snapshot and retained upstream notices.

The original implementation and its historical measurements follow. Its older firmware/motion recommendations are superseded for AIRLOCK: **all motor commands and firmware flashes remain prohibited**.

## Preserved LOCKED IN implementation

A small robot that holds your phone while you study. It watches for your hand reaching
toward the phone, decides whether to intervene, backs away through a safety layer, says
something cheeky, answers questions about your session from what it actually measured, and
gives the phone back the moment you ask. It also *talks*: a language model running on the
laptop understands what you say in your own words ("I have a calculus test tomorrow"),
remembers it, speaks up on its own when it sees a real reach, and knows when to stay quiet.
Everything runs offline; the model can only *ask* the robot to do things.

Built by Dhruv Jain, Siddartha Kodithyala, Areeb Hirani, and Mustafa Hassan for HackUTD 2026 (Nov 7-8, UT Dallas).

## Hardware

| Part | Role |
|---|---|
| SunFounder Zeus Car: Arduino UNO with the Zeus shield, mecanum wheels | carries the phone; runs SunFounder's sketch (1.4.5 on this car) |
| ESP32-CAM on the car (SunFounder AI-camera firmware) | the car's Wi-Fi access point (`Zeus_Car`, `192.168.4.1`), WebSocket link to the sketch (port 8765), MJPEG camera (port 9000) |
| MacBook with Apple silicon (developed on a MacBook Air M4, 16 GB) | runs everything: vision, speech recognition, the language model, the voice, the dashboard |
| Mac microphone; Bluetooth speaker or the Mac's speakers | push-to-talk or hands-free input; the robot's voice |
| Mac webcam (optional) | an alternative to the robot's camera ([docs/CAMERA_PLACEMENT.md](docs/CAMERA_PLACEMENT.md)) |

## Architecture

```
camera (Mac webcam | Zeus ESP32-CAM over MJPEG) → ArUco markers on the robot → phone zone
       → MediaPipe hand landmarks → reach detector (zone + approach + dwell + cooldown)
       → state machine (11 explicit states) → policy (rule trace) → safety controller
       → robot transport (mock | Zeus over WebSocket) → events → dashboard / JSONL log
mic → push-to-talk (or Silero VAD) → whisper.cpp (separate process) → deterministic intents
       → short commands: same runtime, cached voice
       → anything else: local language model (separate process, context read while you talk)
         → typed action request → runtime handlers / safety controller → reply → Kokoro voice
camera / timer / link events → behaviour engine (speak or stay silent, with the rule) → voice
```

| Path | Contents |
|---|---|
| `backend/app/runtime.py` | the runtime that wires everything together |
| `backend/app/vision/` | webcam and Zeus MJPEG readers, MediaPipe hands, phone zone, ArUco robot tracking, reach detector |
| `backend/app/voice/` | microphone input and speech detection, whisper.cpp worker, intents, personality and lines, Fish Audio cache, Kokoro, speech queue |
| `backend/app/intelligence/` | state machine, policy, adaptation, behaviour engine, the companion (context, prompt, typed actions, local model worker) |
| `backend/app/safety/` | safety controller (arming, motion profiles, clamps, E-STOP latch) and watchdog |
| `backend/app/robot/` | robot transports: `mock` (simulator) and `zeus` (WebSocket to the car) |
| `backend/app/sessions/`, `backend/app/telemetry/` | sessions and metrics; the event bus and JSONL telemetry |
| `backend/app/api/` | REST and WebSocket API for the dashboard |
| `frontend/` | the dashboard |
| `firmware/` | watchdog patch v2 for the car's sketch (optional, not flashed), its native test harness, flash and rollback notes |

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (components, threads, event model,
privacy) and [docs/HARDWARE_PROTOCOL.md](docs/HARDWARE_PROTOCOL.md) (the Zeus Car protocol,
verified against the SunFounder source).

## Main technologies

- **Backend:** Python 3.12, FastAPI and uvicorn, pydantic, asyncio, WebSockets, NumPy
- **Vision:** MediaPipe Tasks (hand landmarker, BlazeFace), OpenCV with ArUco markers
- **Speech in:** whisper.cpp through pywhispercpp (Metal GPU); faster-whisper (CTranslate2) as the
  fallback; Silero VAD on ONNX Runtime for hands-free mode; sounddevice (PortAudio)
- **Conversation:** Qwen3-4B-Instruct-2507 (4-bit) on MLX through mlx-lm, in a worker process,
  limited to typed actions validated with pydantic
- **Speech out:** Kokoro-82M through kokoro-onnx; cached Fish Audio clips for fixed lines;
  the macOS voice as the last fallback
- **Dashboard:** React 18, TypeScript, Vite 6, Tailwind CSS 4, Recharts
- **Firmware:** Arduino C++ (ArduinoJson 6.21.5) with a native g++ test harness
- **Tooling:** uv, npm, pytest, Ruff

## Status (2026-11-01)

| | State |
|---|---|
| Backend: state machine, sessions, safety, policy, adaptation, events, REST + WebSocket | working, 496 automated tests passing (+2 real-model tests run on demand); `make soak`: 10 demos + 12 sessions, 0 invariant failures |
| **Companion (Milestone 5)** | local Qwen3-4B on MLX: understands natural speech, sets goals, remembers, speaks up on confirmed reaches and stays silent by recorded rules; typed actions only, the safety controller decides. 35-scenario evaluation and end-to-end latency in [docs/MILESTONE5_VERIFICATION.md](docs/MILESTONE5_VERIFICATION.md); **not yet tried by a person or on the robot's network** |
| Speech / voice (Milestone 5) | whisper.cpp (Metal) 98 ms p50, 0.3% word errors on 104 utterances incl. 11 real-mic; Silero VAD for hands-free; Kokoro (local, labelled, **not** the Fish Audio voice) for anything without a cached clip ([docs/MODELS.md](docs/MODELS.md)) |
| Simulated robot (Mode C / B) | working end to end in the dashboard |
| Real Zeus Car (Milestone 3) | `make hardware`: ESP32-CAM camera + monitor link (receives and measures, sends no motor or stop frames), offline, reconnects on its own, never arms; checked against the robot's own sketch (1.4.5) and every ESP32 camera firmware generation; dry-run on home Wi-Fi and against stand-ins; **not yet run on the `Zeus_Car` network** ([docs/HARDWARE_HANDOFF.md](docs/HARDWARE_HANDOFF.md)) |
| Hardware motion gate | stock firmware has no device-side command expiry, so LOCKED IN sends **no motor command at all** until firmware patch v2 is flashed and reported ([docs/SAFETY.md](docs/SAFETY.md) §7) |
| Offline | `make offline-check`: READY, 0 missing; the backend serves everything with every non-local connection blocked (0 attempts) ([docs/OFFLINE.md](docs/OFFLINE.md)) |
| Phone zone follows the robot (ArUco markers) | implemented and tested on rendered marker frames; **not yet tried with a printed marker on the car** |
| Hand-reach detection (MediaPipe) | runs on the live webcam (30 FPS capture, 27-38 ms p50 capture → result); reach logic tested on synthetic tracks; **reach precision/recall trials not yet run** |
| Voice | microphone, push-to-talk and Whisper working on this laptop; real-speech accuracy with people not yet measured |
| Personality + voice | four modes (chill / roast / opt-in unhinged / judge demo), profanity off/mild/strong, P0-P3 speech queue with interrupts; Fish Audio voice (E-girl preset) **working with the real API** on the free `s2.1-pro-free` model; 39-line offline voice pack cached and verified with Fish Audio unreachable ([docs/VOICE_PERSONALITY.md](docs/VOICE_PERSONALITY.md)) |
| Dashboard | working (React + Vite + Tailwind) |
| Firmware watchdog patch (v2: 400 ms stop, latch, telemetry) | **required** before any motor command; prepared for the robot's 1.4.5 sketch and 1.4.6, compile-checked (ArduinoJson 6.21.5: 21,744 B flash, 1,516 B RAM); **not flashed, never run** |
| R5 link-freeze test, R1-R6 procedure | dashboard tool ready, tested against firmware models only ([docs/HARDWARE_TESTS.md](docs/HARDWARE_TESTS.md)) |
| Voice trials with real people | protocol and dashboard tool ready ([docs/VOICE_TESTS.md](docs/VOICE_TESTS.md)); **not run** |

Measured numbers only: [docs/PERFORMANCE.md](docs/PERFORMANCE.md). Open work: [TASKS.md](TASKS.md).

## Setup

Requirements: a Mac with Apple silicon (the language model needs MLX; other platforms are
untested), [uv](https://docs.astral.sh/uv/), Node 20+, git, and about 4 GB of free disk for
the models. uv installs Python 3.12 (MediaPipe has wheels for 3.11 and 3.12 only). ffmpeg is
needed for the voice benchmark only.

```bash
git clone https://github.com/Dhruvjain35/locked-in.git && cd locked-in
make setup            # Python 3.12 venv with all extras from uv.lock, npm install, make models
make verify-models    # offline: every model file against backend/models.sha256
cp .env.example .env  # optional: every setting has a safe default
```

`.env` is git-ignored. It holds the optional `FISH_AUDIO_API_KEY` and settings overrides;
keep it private (`chmod 600 .env`).

### Models

The weights are not in the repository. `make models` downloads them once (3.0 GB; 4.0 GB
with the optional Qwen3-1.7B) into `backend/models/` (git-ignored), each from a pinned
source, and checks every file against `backend/models.sha256`. A file that does not match
fails the command; `make verify-models` repeats the check offline.

| Model | Pinned source | Size | Licence |
|---|---|---|---|
| Qwen3-4B-Instruct-2507, 4-bit MLX (conversation) | `mlx-community/Qwen3-4B-Instruct-2507-4bit` @ `50d42775` | 2.28 GB | Apache-2.0 |
| whisper.cpp `ggml-base.en.bin` (speech-to-text) | `ggerganov/whisper.cpp` @ `5359861c` | 148 MB | MIT |
| faster-whisper `base.en` (speech-to-text fallback) | `Systran/faster-whisper-base.en` @ `3d3d5dee` | 148 MB | MIT |
| Silero VAD v6 (hands-free speech detection) | copied from the faster-whisper 1.2.1 package (uv.lock) | 1.2 MB | MIT |
| Kokoro-82M v1.0: int8, fp32 and voices (local voice) | `thewh1teagle/kokoro-onnx` release `model-files-v1.0` | 446 MB | Apache-2.0 |
| MediaPipe hand landmarker and BlazeFace short range (vision) | `storage.googleapis.com/mediapipe-models/…/float16/1/` | 8 MB | Apache-2.0 |
| Qwen3-1.7B, 4-bit MLX (optional: `download_models.py --llm all`) | `mlx-community/Qwen3-1.7B-4bit` @ `3b1b1768` | 0.98 GB | Apache-2.0 |

Options: `cd backend && uv run python scripts/download_models.py --help`. Why each model was
chosen, and every measurement: [docs/MODELS.md](docs/MODELS.md).

### Fish Audio voice clips (optional)

The robot's fixed lines play cached Fish Audio clips (the E-girl voice) when they exist.
The clips are generated output under Fish Audio's terms, so they are not in the
repository; they live in `data/tts_cache/fish_audio/` (git-ignored). On another Mac, copy
that folder from a machine that has it, or generate the clips with a Fish Audio key in `.env`:

```bash
cd backend && uv run python scripts/generate_voice_cache.py   # shows the plan, sends nothing
# then add --yes --max-bytes N, where N covers the planned bytes (the free s2.1-pro-free model costs nothing)
```

Regenerated clips sound alike but are not byte-identical. Without clips, every line is
spoken by the local voice, and the dashboard labels which voice spoke.

## Running

### Simulation (no hardware)

```bash
make dev    # backend on :8077 with reload + dashboard on http://127.0.0.1:5174
make demo   # or: build the dashboard and serve everything from http://127.0.0.1:8077
```

The app starts in **Mode C (full simulation)**: simulated robot, camera off, everything
labelled SIMULATED. **Start camera** switches to Mode B (real webcam, simulated robot);
connecting the Zeus Car is Mode A.

Try it in 30 seconds:

1. Open the dashboard, type a task, press **Lock in**.
2. Robot panel: toggle **Armed**, then **Autonomous retreat**.
3. Closed loop panel: press **Reach**. Watch the state rail, the banner, the loop trace, the
   simulated robot backing away on the map, and the "Why it acted" rules.
4. Voice panel: type "How am I doing?", then "Give me my phone".

Without `make`: `cd backend && uv sync --all-extras && uv run uvicorn app.main:app --port 8077`
and `cd frontend && npm install && npm run dev`.

### Offline hardware mode (real Zeus Car)

```bash
make offline-check   # on home Wi-Fi first: everything the demo needs is local (READY, 0 missing)
# join the car's Zeus_Car Wi-Fi (it has no internet), then:
make hardware        # ESP32 camera + robot link, offline, disarmed, no motor commands
make hardware SPEAKER="JLab GO Party"   # the same, with the robot's voice on that output device
```

`make hardware` runs the venv's Python directly (nothing is resolved over the network),
turns on offline mode (Fish Audio and model hubs are never contacted), connects to
`192.168.4.1`, and never arms or moves the robot. Open http://127.0.0.1:8077. What you
should see and what to test (no motion): [docs/HARDWARE_HANDOFF.md](docs/HARDWARE_HANDOFF.md),
[docs/REAL_HARDWARE_TESTS.md](docs/REAL_HARDWARE_TESTS.md).

## Tests

```bash
make test            # backend tests (pytest) + dashboard type-check and build
make lint            # Ruff on the backend
make verify-models   # model files against backend/models.sha256
make soak            # scripted demo + mixed sessions back to back on the simulated robot, invariant checks
```

The backend suite needs no robot, camera or microphone. Two tests load the real language
model (2.3 GB) and run only on request:
`cd backend && LOCKEDIN_TEST_LOCAL_LLM=1 uv run pytest tests/test_local_models.py`.
The companion's evaluation, latency and soak tools, and what each has shown:
[docs/MILESTONE5_VERIFICATION.md](docs/MILESTONE5_VERIFICATION.md).

## Safety

- Disarmed at start and after any disconnect. STOP always works and E-STOP latches.
- Every motion is a typed request clamped to a profile (autonomous retreat: ≤ 60/100 speed,
  ≤ 0.8 s, translation only), and the transport stops itself at a hard cap even if nothing
  else does.
- **On the real car LOCKED IN sends no motor command at all.** The default
  (`LOCKEDIN_ZEUS_MOTOR_COMMANDS=disabled`) is a monitor link that receives and measures and
  sends no motion or stop frames. The stock firmware keeps driving for 2-4 s on a silent
  link, so autonomous motion stays locked until firmware patch v2 is flashed and test R5 has
  measured the car stopping on its own when the laptop's link drops. The patch is
  compile-checked but has not been flashed or run.
- The language model only produces text and requests from a closed list of typed actions.
  The safety controller decides, and every physical request from the model is refused
  today. "Give me my phone" always stops the robot and ends the session.
- What software cannot protect against, and the hardware tests that gate autonomy:
  [docs/SAFETY.md](docs/SAFETY.md).

## Known limitations

- No odometry or localisation: "Return" reverses recorded commands and is approximate.
- Stock firmware sends no sensor data: obstacle and battery readings are unavailable unless
  the optional patch is flashed.
- Rotation on the Zeus Car is magnetometer-controlled and its speed cannot be set; autonomous
  motion never rotates.
- Reach detection sees a hand entering a calibrated region; it does not know intent.
- After a retreat the zone is only right if a marker on the robot is visible; with no marker it
  says the zone is unknown and pauses reach confirmations instead of guessing.
- Focus is a presence/head-turn proxy, not attention measurement; looking down at paper is not
  penalised.
- The language model makes mistakes: it sometimes misses an indirect request (a bathroom
  break did not pause the session in the held-out evaluation) and invents numbers, which the
  reply guards refuse. It never moves the robot. Low Power Mode makes it ~4x slower.
- Python 3.11/3.12 only (MediaPipe wheels).

## Troubleshooting

| Symptom | Fix |
|---|---|
| CAMERA OFFLINE "could not open camera … grant Camera access" | System Settings → Privacy & Security → Camera → enable the app that runs the backend (Terminal, iTerm, VS Code…), restart it |
| Voice warning "Microphone delivers exact silence" | same, under Microphone |
| Dashboard says BACKEND OFFLINE | `make backend`; check port 8077 is free (`lsof -i :8077`), or `make backend PORT=…` plus `LOCKEDIN_BACKEND=http://127.0.0.1:… npm run dev` |
| Robot won't connect | join the `Zeus_Car` Wi-Fi; close the SunFounder app; address `192.168.4.1` |
| Connected but no `null` echoes in Robot wire | the shield's small switch is in the upload position |
| Car rotates by itself | compass disturbed: move magnets (phone, speaker) away, recalibrate with the phone mounted |
| No internet while on the robot's Wi-Fi | expected (one Wi-Fi radio); generate the Fish Audio cache before joining it (docs/VOICE_PERSONALITY.md). Cached clips, the local voice and the local language model work offline; live Fish Audio and the optional cloud replier do not |
| Robot speaks with the local voice instead of Fish Audio | the line has no cached clip (see [Fish Audio voice clips](#fish-audio-voice-clips-optional)); generated replies always use the local voice; the speech history says which voice spoke |
| "Whisper model … not found", "model not downloaded" | `make models` while online |
| Car boot-loops after a camera firmware update | camera 1.5.x vs sketch 1.4.6 buffer overflow; see HARDWARE_PROTOCOL §8.2 |

## Documentation

- [docs/MODELS.md](docs/MODELS.md): the local AI components, why each was chosen, licences,
  every measurement. [docs/MILESTONE5_VERIFICATION.md](docs/MILESTONE5_VERIFICATION.md) and
  [docs/MILESTONE5_EVALUATION.md](docs/MILESTONE5_EVALUATION.md): what was verified and how.
  [docs/MILESTONE4_VERIFICATION.md](docs/MILESTONE4_VERIFICATION.md): the Milestone 4
  verification matrix, audio and robot results.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, threads, event model, privacy.
- [docs/HARDWARE_PROTOCOL.md](docs/HARDWARE_PROTOCOL.md): the Zeus Car protocol, verified
  line by line from the SunFounder source, including fail-safe gaps in the stock firmware.
- [docs/SAFETY.md](docs/SAFETY.md): rules, profiles, what software cannot protect, hardware
  tests that gate autonomy.
- [docs/HARDWARE_ASSEMBLY.md](docs/HARDWARE_ASSEMBLY.md): assembly, phone mount, network,
  calibration.
- [docs/ROBOT_TRACKING.md](docs/ROBOT_TRACKING.md): how the phone zone follows the robot,
  marker placement, failure handling, physical trials T1-T6.
- [docs/HARDWARE_TESTS.md](docs/HARDWARE_TESTS.md): R1-R6 step by step (roles, startup order,
  abort criteria, recording template), including the R5 link-freeze test. Need the firmware
  patch first.
- [docs/HARDWARE_HANDOFF.md](docs/HARDWARE_HANDOFF.md): `make hardware`, what the dashboard
  shows, what is verified, the one-page checklist before driving the real robot.
- [docs/REAL_HARDWARE_TESTS.md](docs/REAL_HARDWARE_TESTS.md): Z1-Z11 on the robot's network,
  no motion.
- [docs/CAMERA_PLACEMENT.md](docs/CAMERA_PLACEMENT.md): Mac webcam vs. the robot-mounted
  ESP32-CAM: who sees the user, the phone, the robot; calibration.
- [docs/OFFLINE.md](docs/OFFLINE.md): what runs without internet and how it is checked.
- [docs/VOICE_TESTS.md](docs/VOICE_TESTS.md): real-speech trials, metrics and targets.
- [docs/VOICE_PERSONALITY.md](docs/VOICE_PERSONALITY.md): personality modes, profanity, the
  Fish Audio key, choosing a voice, generating the offline cache, speech priorities.
- [docs/AUDIO_TESTS.md](docs/AUDIO_TESTS.md): real speaker / microphone tests.
- [docs/TEST_PLAN.md](docs/TEST_PLAN.md), [docs/PERFORMANCE.md](docs/PERFORMANCE.md),
  [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md), [docs/HACKUTD_STRATEGY.md](docs/HACKUTD_STRATEGY.md),
  [firmware/README.md](firmware/README.md).

## Repository layout

```
backend/   FastAPI app (app/), tests/, scripts/ (benchmarks, model download, checks),
           models.sha256 (pinned model hashes), models/ (git-ignored)
frontend/  React + TypeScript + Vite + Tailwind dashboard
firmware/  notes, the optional unflashed Arduino patch, its native test harness
docs/      design, protocol, safety, tests, performance, models, demo, strategy;
           benchmarks/ holds the raw measurement files
data/      runtime telemetry, sessions, settings, voice caches (git-ignored)
```

## Credits and licences

SunFounder Zeus Car sketch and AI camera firmware (GPL-3.0); this repository does not vendor
their code. The optional patch in `firmware/optional/` extends their sketch and is
GPL-3.0-or-later. MediaPipe models (Apache-2.0), faster-whisper / CTranslate2 (MIT), OpenAI
Whisper weights (MIT), fonts Chakra Petch, Archivo and Martian Mono (SIL OFL). Milestone 5:
whisper.cpp / pywhispercpp (MIT), Silero VAD (MIT), MLX and mlx-lm (MIT), Qwen3-4B-Instruct-2507
weights (Apache-2.0), Kokoro-82M weights (Apache-2.0) via kokoro-onnx (MIT), which loads
phonemizer and espeak-ng (**GPL-3.0-or-later**); see docs/MODELS.md before redistributing.
Fish Audio clips are generated under Fish Audio's terms and are not included.
