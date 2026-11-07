# AIRLOCK immediate warning and preserved voice surface

The new warning path is real: an authenticated service subscribes to committed SpacetimeDB incidents, supplies bounded incident facts to an authenticated loopback endpoint, and that endpoint queues a fixed factual line at **P0** through the existing `Speaker`. No model inference is required. The existing voice engine/runtime files remain unchanged.

## Start safely, with audio disabled

From the repository root:

```bash
PYTHONPATH=backend backend/.venv/bin/python -m app.security.voice_service \
  --init-token data/airlock-voice/local-token.private.json

PYTHONPATH=backend backend/.venv/bin/python -m app.security.voice_service \
  --token-file data/airlock-voice/local-token.private.json \
  --data-dir data/airlock-voice/service
```

The first command creates a new private `0600` local capability and refuses to overwrite one. Run it only once for that path. The server binds to `127.0.0.1:8077`. By default it is **text only**: no audio output, microphone recording, ASR/model loading, camera start, robot connection, screen lock, external notification, or SpacetimeDB security control occurs.

The existing Vite `/api` and `/ws` proxy targets port 8077, so the AIRLOCK voice panel and existing Voice Studio can inspect genuine voice state/history. `GET /api/airlock/voice-status` explicitly distinguishes text-only versus cached-system playback. The displayed adapter is the macOS system voice; this is not a claim that Fish Audio, Kokoro, Parakeet or a model ran.

The service constructs the existing runtime with mock transport **disconnected**, automatic connection off, vision disabled, microphone/ASR disabled, `llm_provider="none"`, `enable_llm=False`, offline mode, judge-demo personality and profanity off. It starts with `NullTTS` to avoid the legacy full-library prewarm or neural voice/model loading.

## Connect the enrolled service

Create/enroll a dedicated SpacetimeDB service identity, or use a private integration service credential produced by `scripts/airlock_seed_case.ts`. Then:

```bash
AIRLOCK_SERVICE_TOKEN_FILE=data/airlock-integration/RUN/service.private.json \
AIRLOCK_VOICE_LOCAL_TOKEN_FILE=data/airlock-voice/local-token.private.json \
  "$HOME/.local/share/locki-airlock/tools/node_modules/node/bin/node" \
  frontend/node_modules/tsx/dist/cli.mjs scripts/airlock_voice_bridge.ts \
  --workstation WORKSTATION_ID
```

The bridge uses `AIRLOCK_SPACETIME_URI`/`AIRLOCK_DATABASE` or the service file's saved context. It reads only authorized SDK views, scopes itself to the specified workstation, and submits only fresh open incident updates. It reconnects with the same identity. Its destination must be an HTTP loopback URL. Private service and local token files must be `0600`; neither token is printed.

Synthetic incidents are **skipped by default**. Add `--include-synthetic` deliberately for a rehearsal. Every synthetic spoken line begins **“Rehearsal.”** and explicitly says that no real intrusion or screen lock is being claimed. A clearly labeled fixture committed by a real reducer is still synthetic evidence.

The endpoint accepts only typed IDs, a supported rule/severity, simulation provenance, and the committed update time. It accepts no arbitrary text, model-generated accusation, lock-success assertion, or notification-delivery claim. Warning text is selected from eight fixed templates in `backend/app/security/announcements.py`. Sensor lines describe observed USB/app/zone activity and request verification. The high-severity `owner_panic` line instead states only that the authenticated owner requested a manual panic response; it infers no USB/person evidence. Its synthetic variant begins “Rehearsal.” Neither variant claims a notification, lock, guilt, or compromise.

## Explicit audio opt-in

Only after choosing to use the local speaker, stop the text-only service and restart with **`--enable-audio`**. The existing voice extra must provide `sounddevice`/PortAudio, and macOS `say` must be available:

```bash
PYTHONPATH=backend backend/.venv/bin/python -m app.security.voice_service \
  --token-file data/airlock-voice/local-token.private.json \
  --data-dir data/airlock-voice/service --enable-audio
```

Startup silently renders only the eight fixed AIRLOCK lines with the existing `CachedSystemTTS` into local WAV cache, verifies that every clip is loaded, then enables cached playback. It does not download or run an ML model. Unavailable playback support/cache completion fails explicitly rather than claiming a ready cached warning.

P0 warning submission interrupts an existing lower-priority conversation and clears stale ambient lines using the existing queue. It respects mute. The response `queued:true` is **not proof of audible output**; inspect the actual `Speaker` history/source/outcome and cache-hit counters. Audio-driver completion is not an acoustic microphone measurement.

## Bounded replay, mute, and API surface

The service durably claims a deterministic warning ID in SQLite before queueing. The bridge also persists acknowledged IDs with a 1024-entry bound. The service retains at most 4096 dedup records, accepts only updates at most 30 seconds old with bounded future skew, admits at most ten warning attempts per minute, limits the queued warning backlog, and applies a per-workstation cooldown while allowing a medium→high upgrade. Retries remain bounded; failures are reported as degraded. A crash after a durable claim and before audio may leave an uncertain silent warning; the visible incident remains, and the code does not pretend external playback is exactly once.

`stop_talking`, the stop button, and mute only stop speech. They call no security reducer. The service does not mount `/api/command`, `/api/robot/*`, `/api/session/*`, camera routes, or microphone-test routes. `POST /api/voice/start` explicitly reports that this announcement adapter has no microphone/ASR. Existing Voice Studio configuration is readable; unsupported legacy configuration/microphone/cloud-generation actions are not exposed through this restricted adapter.

The announcement endpoint requires the local bearer capability, accepts loopback clients only, restricts browser origins to the local AIRLOCK UI, and rejects oversized announced bodies. Health/state/history/config and the existing WebSocket snapshot format are reused. Incoming WebSocket PTT cannot record because no voice-input service exists.

## Verified evidence

`backend/tests/test_airlock_announcements.py`: **21 tests passed**. These use no real audio and cover strict facts, factual/rehearsal text, bearer/origin controls, absent hardware/control routes, inactive mic/model/camera/transport, durable dedup, bounded queue/cooldown, mute, owner-panic severity/manual-intent policy, and a genuine existing-`Speaker` test in which a P0 incident interrupts a playing P2 request and removes a waiting P3 request.

`scripts/tests/airlock_voice_bridge.test.ts`: **2 tests passed** without network/audio. They verify that the owner-panic mapping carries only typed manual intent, excludes untrusted owner-summary/sensor/response claims, and rejects an unknown manual-sounding rule.

One explicitly authorized rehearsal exercised the actual path against local `locki-airlock-sid` before the two manual-panic templates were added. Its historical cache count is six; no additional audio was played for the panic change:

```text
Committed incident: incident:fixture-ba1d6398-cbee-4c08-bb4e-880188879a72
Workstation: integration-1794086155495-41c541
Real SDK -> authenticated local POST -> Speaker P0 -> cached macOS output
Evidence provenance: synthetic; spoken text begins Rehearsal
Source/outcome: system_cache / finished
Cache: 6 prewarmed, 1 hit, 0 misses
Speaker queue wait: 0.9 ms
Audio-driver startup: 76.3 ms
Local authenticated HTTP delivery: 85.68 ms
Clip duration: 6854.8 ms
Sample count: 1
```

The SDK bridge reconnected without replay. After an actual voice-service restart, the same warning returned `duplicate:true`, `queued:false`, `audio_enabled:false`; durable suppression survived restart. The service was returned to its **default audio-disabled mode** afterward. Sanitized evidence is `data/airlock-voice/proof/report.json`.

This proves one actual cached-system rehearsal and model-independent warning integration. It does not prove the PRD's physical-event-to-audible 1.5-second target, microphone→ASR→Nemotron live conversation, Bluetooth-speaker acoustic latency, or a two-Mac trial. Those remain separately measured acceptance work.
