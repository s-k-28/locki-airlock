# Fresh Mac capture and USB monitoring

The read-only Mac bridge publishes actual IORegistry USB snapshots and detects changes through a labeled one-second polling fallback. Device identifiers are SHA256 digests; no raw serial number or peripheral file is sent. A separately compiled AppKit helper supplies the active application bundle ID. A missing app helper yields `unknown`, not an invented application or a claim that app-launch monitoring is healthy.

```bash
mkdir -p data/airlock/bin
swiftc scripts/airlock_app_observer.swift -o data/airlock/bin/app-observer
node scripts/airlock_publisher.mjs --new-identity data/airlock/mac.private.json
```

Enroll the printed public identity from the owner console as a Mac device. Keep the token file private (0600); never paste it into a URL or source file. Then run:

```bash
AIRLOCK_SPACETIME_URI=https://maincloud.spacetimedb.com \
AIRLOCK_DATABASE=locki-w91bw \
frontend/node_modules/.bin/tsx scripts/airlock_mac_bridge.ts \
  --workstation WORKSTATION_ID --device ENROLLED_MAC_ID \
  --token-file data/airlock/mac.private.json
```

The bridge records a fresh baseline every five seconds. Live arming requires a capture no older than 30 seconds plus fresh USB health from the same enrolled Mac. The server freezes the USB set, active app and its own current camera/health state. Camera unavailability does not block healthy USB protection. The owner form displays the capture and requires explicit policy confirmation; a fixture session cannot masquerade as live protection.

After live arming, added/removed USB observations and a three-second watcher heartbeat go through the actual reducers. Per-device sequence and pending observation state persist with owner-only file permissions. One process owns the device's sequence. Reconnects use the saved identity; bounded retry queues stop rather than silently claiming full coverage. An insertion during a disconnect can be observed after reconnect, but this is not proof of its original physical insertion time.

The bridge never arms/disarms, opens a camera or microphone, loads a model, locks the screen, executes device files, or sends motor commands. Actual application launches are a separate native observer and should use their separately enrolled publisher/sequence, described in [TEAMMATE_INTERFACE.md](TEAMMATE_INTERFACE.md).

## Verified on this workstation

`scripts/airlock_mac_capture_smoke.ts` exercised actual capture → authenticated cloud publication → live arm → frozen baseline → actual monitoring heartbeat → owner disarm. The report is `data/airlock-capture-proof/capture-1794086449732-62ddd0/report.json` (local, ignored from source).

The machine enumerated zero USB peripherals during this trial, captured active app `com.openai.codex`, recorded camera `unavailable`, and froze the exact captured baseline. Auto-lock was off. No camera/microphone/OS lock ran. This proves the capture path and state handling; it does not prove a genuine USB insertion or person detection.

The bridge's unit checks reject unredacted or simulated inventories, avoid false changes on reordered device sets, and preserve added/removed event meaning. Run with the selected supported Node:

```bash
frontend/node_modules/.bin/tsx --test scripts/airlock_mac_bridge.test.ts
```
