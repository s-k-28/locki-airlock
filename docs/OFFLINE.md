# Offline setup

The robot's `Zeus_Car` Wi-Fi has no internet, and this MacBook has one Wi-Fi radio, so while
the demo runs nothing can be downloaded or looked up. Everything LOCKED IN needs is local;
this page says what that is and how to check it before leaving home Wi-Fi.

## Once, while online

```bash
make setup                 # Python venv + packages, npm install, MediaPipe + Whisper models
make build                 # dashboard build (later rebuilds work offline from node_modules)
```

Voice: Personality + Voice → **Offline cache** → **Plan: voice pack** → approve. The E-girl
pack (39 clips, $0 on `s2.1-pro-free`) is already cached on this Mac. Other lines are spoken
by the macOS voice, which is installed with macOS (a downloaded premium voice must be
downloaded before going offline).

## Before every hardware session

```bash
make offline-check
```

| Group | What it checks |
|---|---|
| python | the venv interpreter and every package the backend imports (FastAPI, OpenCV, MediaPipe, websockets, sounddevice, faster-whisper...) |
| models | `backend/models`: hand landmarker, face detector, Whisper `base.en` (otherwise speech recognition would try to download it) |
| dashboard | `frontend/dist` exists and is newer than the source; no font, script or style loads from the internet (fonts come from bundled @fontsource packages); node_modules can rebuild it |
| voice | the Fish Audio voice pack for the active voice/model/settings (39/39), how many other lines fall back to the local voice, the macOS voice |
| audio | speakers and microphones present; the chosen output connected |
| hardware | robot address, camera URL, motor commands (`disabled`), offline mode |
| network | port 8077 free; robot WebSocket and camera reachable (only on `Zeus_Car`) |
| offline | starts the backend in-process with every non-local connection blocked and counted; serves health, state and a session (0 attempts expected) |

`FAIL` lines must be fixed while online. `info` lines are expected (the robot is not
reachable from home Wi-Fi).

## What offline mode does

`make hardware` sets `LOCKEDIN_OFFLINE=true`:

- **Fish Audio is never contacted.** Cached clips play; live generation, cache jobs, voice
  checks and credit lookups are refused locally and counted (`offline.fish_refusals` in the
  state). Missing lines use the macOS voice, and the speech history says why.
- **The cloud LLM is not used** (`LOCKEDIN_LLM_PROVIDER=anthropic` is ignored offline).
  The **local** language model (default since Milestone 5) runs entirely on the laptop:
  `make offline-check` has it write a reply and Kokoro voice it inside a macOS sandbox that
  blocks every network except this machine.
- **Lines the local model writes never go to Fish Audio**, online or not: they can contain
  what the user said. Anything without a cached clip is spoken by Kokoro (local, labelled).
- **Model libraries stay offline** (`HF_HUB_OFFLINE=1`): every model loads from
  `backend/models`.
- The backend is started with the venv's Python, not `uv run`, so nothing tries to resolve
  packages.
- The dashboard is served by the backend from `frontend/dist` (no Vite dev server needed).

Everything else (state machine, sessions, safety, the simulated robot, camera, MediaPipe,
whisper.cpp / Whisper, Silero VAD, the language model, Kokoro, the timeline) never needed
the internet. `make models` downloads all of it once (about 3.6 GB, docs/MODELS.md).

The Fish Audio key stays in `.env` (git-ignored, mode 600) and never reaches the dashboard,
the logs or the readiness check output.
