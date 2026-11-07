# AIRLOCK operator handoff

Run commands from the repository root. The team cloud deployment is [locki-w91bw](https://spacetimedb.com/@dhruvhydrox/locki-w91bw), served by `https://maincloud.spacetimedb.com`. The persistent local fallback is **`locki-airlock-sid`**; the older fixture database is preserved. Confirm the selected URI/database in the console before a demonstration. Browser identities still need owner enrollment; share public identities, never bearer tokens.

## Current services

At handoff, the integration owner reports:

| Service | Address | State |
|---|---|---|
| Local SpacetimeDB | `http://127.0.0.1:3000` | Running with persistent data |
| Dashboard | `http://127.0.0.1:5174` | Running |
| Restricted voice service | `http://127.0.0.1:8077` | Running, **audio OFF**, microphone/model disabled |
| Ollama | `http://127.0.0.1:11434` | Running, **no models loaded** |

A read-only Mac baseline bridge was enrolled through the owner console and started for one hour at approximately 21:47 UTC. It keeps fresh captures available for live arming; it never opens a camera/microphone, loads a model or invokes the lock adapter. The owner console was left **disarmed**, with automatic lock disabled.

The exact `nemotron-3.5-bolt:e4b` MLX model finished downloading at 22:03 UTC. Manifest verification confirmed the installed tag and no loaded models; inference remains blocked by the resource guard on this 16 GiB host. Do not start a duplicate download or bypass the guard. The intended 24GB Mac still needs to be connected.

## Start, inspect and stop

The AIRLOCK launcher requires SpacetimeDB 2.14.1 and Node 24.22.1+ within the Node 24 release line. It selects the installed AIRLOCK Node runtime when available; `AIRLOCK_NODE_BIN` overrides that selection.

```sh
bash scripts/airlock_dev.sh status   # read-only local DB reachability
make airlock-cloud                   # dashboard using the existing cloud module
```

For a local session after stopping the existing dashboard:

```sh
make airlock                         # reuse/start local DB, preserve data, publish/generate/build, run UI
```

`make airlock` starts no camera, microphone, model, real lock or motor. If only a stopped component needs restarting, use a separate terminal:

```sh
bash scripts/airlock_dev.sh server    # persistent local DB in foreground
bash scripts/airlock_dev.sh frontend  # dashboard only, default local database
```

Do not start a second listener on an occupied port. Stop adapters first, then use **Ctrl-C in the matching launcher terminal**. The combined launcher stops a DB process it started and leaves a reused server running. For a background service, identify its listener PID before sending `kill -TERM PID` to that specific process:

```sh
lsof -nP -iTCP:3000 -sTCP:LISTEN
lsof -nP -iTCP:5174 -sTCP:LISTEN
lsof -nP -iTCP:8077 -sTCP:LISTEN
```

Keep the persistent data directory intact when restarting SpacetimeDB. Do not use data deletion, broad process-kill commands or a forced database reset as a restart procedure. Stopping a browser or muting speech does not disarm a workstation; use the authenticated owner's **Disarm** action.

To restart the restricted voice endpoint in its current text-only mode, supply the existing local capability file through a generic private-file variable:

```sh
PYTHONPATH=backend backend/.venv/bin/python -m app.security.voice_service \
  --token-file "$VOICE_CAPABILITY_FILE" --data-dir "$VOICE_STATE_DIRECTORY"
```

Omit `--enable-audio` and `--allow-microphone`. Do not initialize a replacement capability over an existing one. The read-only status endpoint is `http://127.0.0.1:8077/api/airlock/voice-status`. `ollama ps` checks loaded models without requesting inference. See [VOICE.md](VOICE.md) and [NVIDIA.md](NVIDIA.md) for the separate opt-in paths.

## Verify safely

Start with a synthetic armed session and a second enrolled reviewer context. Send the labeled person/USB fixture, compare the committed incident ID, acknowledge as reviewer, resolve/disarm as owner, then reconnect. Synthetic evidence remains synthetic and cannot authorize a real screen lock.

The following software checks use no camera, model inference, audio or actual lock:

```sh
npm test --prefix spacetime
spacetime/node_modules/.bin/tsx --test scripts/tests/airlock_lock_adapter.test.ts
python3 -m unittest discover -s edge/jetson/tests -q
make airlock-verify
```

The deployed verifier creates unique fixture stations and real read-only managed callbacks; it preserves existing data. Its timing boundary is reducer publication to subscribed SDK cache, not physical capture to two laptops. Sanitized results and current acceptance status are summarized in [PRD_STATUS.md](PRD_STATUS.md).

For a real lock-request **preview**, use an enrolled Mac token and private preview-state filename:

```sh
frontend/node_modules/.bin/tsx scripts/airlock_lock_adapter.ts \
  --workstation WORKSTATION_ID --device MAC_DEVICE_ID \
  --token-file "$MAC_TOKEN_FILE" --state-file "$LOCK_PREVIEW_STATE_FILE" --once
```

Default dry run invokes no Swift helper or OS action. Real execution additionally requires explicit local consent, the fixed canonical ledger/mutex, a committed one-time database claim, matching live/frozen policy and existing Accessibility trust. No real lock or permission prompt was exercised during implementation. Read [LOCK_ADAPTER.md](LOCK_ADAPTER.md) before any owner-consented trial.

For read-only Mac baseline/USB capture after device enrollment:

```sh
frontend/node_modules/.bin/tsx scripts/airlock_mac_bridge.ts \
  --workstation WORKSTATION_ID --device MAC_DEVICE_ID \
  --token-file "$MAC_TOKEN_FILE" --duration 30
```

This captures/publishes real metadata; it does not execute peripheral files or lock the Mac. Live arming requires a fresh authenticated baseline. For model readiness without loading inference:

```sh
frontend/node_modules/.bin/tsx scripts/airlock_nemotron.ts --preflight
```

## Remaining acceptance

- **Physical:** Areeb must verify the exact Jetson, power supply, JetPack and camera; run a consented 10-minute stream/unplug/recovery trial, measured person/benign trials and a two-device proof. The camera adapter requires explicit `--capture`; motion-only fallback never claims person evidence. See [Jetson instructions](../../edge/jetson/README.md).
- **Mac:** Real Calculator app launch was verified again with nine committed app/heartbeat events. Test a harmless real USB insertion across two physical consoles, then measure capture-to-warning timing. Actual lock/unlock requires the local operator's explicit opt-in and an independent truthful lock observation; shortcut posting alone is unconfirmed.
- **Model/voice:** Exact E4B inference, three genuinely model-selected tools, model-mediated attack resistance, real microphone/ASR conversation and acoustic P0 timing remain distinct acceptance work. Native Relay/tool checks and synthetic cached playback do not establish those results.

Keep all wheels stationary. **No motor commands or firmware flashing.** Use the AIRLOCK targets for this handoff; legacy hardware/download/reset targets are outside this procedure. Credential files, tokens, private checkpoints and local captures stay outside public documentation and source control.
