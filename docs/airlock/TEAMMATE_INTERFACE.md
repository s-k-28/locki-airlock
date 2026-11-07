# AIRLOCK publisher and managed-runner handoff

The database reducer contract is [`spacetime/CONTRACT.md`](../../spacetime/CONTRACT.md). The portable JSON observation contract is [SecurityEventV1.schema.json](SecurityEventV1.schema.json); TypeScript interfaces are [`scripts/airlock_contract.d.ts`](../../scripts/airlock_contract.d.ts). The helper [`scripts/airlock_publisher.mjs`](../../scripts/airlock_publisher.mjs) validates an envelope and maps its snake-case fields to the reducer's frozen positional HTTP argument order.

## Dhruv: Mac adapter

Create a dedicated identity and save its private token locally:

```bash
node scripts/airlock_publisher.mjs --new-identity data/airlock/mac-token.json
```

Give the printed public identity to the workstation owner. The owner enrolls it as role `mac` with a unique device ID. Mac publishers may emit `usb_added`, `usb_removed`, `app_launched`, and `heartbeat`. Enrollment binds identity, role, and workstation on the server; an envelope's `source` field grants no authority.

Use the existing read-only polling fallback to collect the actual current USB baseline:

```bash
python3 backend/app/security/mac_observer.py --snapshot
```

The owner arms a live session with the returned `baseline` string array. Start publication explicitly, with the **same enrolled Mac device ID**:

```bash
python3 backend/app/security/mac_observer.py \
  --workstation WORKSTATION_ID --session SESSION_ID --device DEVICE_ID \
  --publish --token-file data/airlock/mac-token.json \
  --state-file data/airlock/mac-sequence.json --duration 10
```

This adapter polls IORegistry, hashes observational device identifiers, reports USB changes/heartbeats, and retains monotonic sequence state. It reads no USB files, executes no device contents, provides no app-launch coverage, and performs no lock. It labels polling as a fallback. A USB identifier is observable metadata, not authenticated hardware identity. `--snapshot` alone does not prove an insertion or detection latency.

The native visible-app observer is separate and needs its own enrolled Mac device ID and sequence file:

```bash
swift scripts/airlock_app_observer.swift --snapshot
swift scripts/airlock_app_observer.swift --workstation WORKSTATION_ID \
  --session SESSION_ID --device APP_DEVICE_ID \
  --state-file data/airlock/app-seq.json --duration 30 | \
  AIRLOCK_DEVICE_TOKEN_FILE=data/airlock/app-token.json \
  node scripts/airlock_publish_stream.mjs --real-observation
```

It uses genuine `NSWorkspace.didLaunchApplicationNotification` notifications for visible apps, not all daemons. A snapshot includes bundle IDs, frontmost app, executable paths, and basic signature validation where available. Published metadata includes bundle ID, signature status, executable basename, and the native capture mode. A signature check is an observation at capture time, not a trust or malware verdict. The stream reports app coverage independently of USB health. It neither starts apps nor locks the screen.

An explicit bounded proof helper captures actual baseline metadata, creates its own live test session with auto-lock disabled, runs both adapters, and disarms afterward:

```bash
"$HOME/.local/share/locki-airlock/tools/node_modules/node/bin/node" \
  frontend/node_modules/tsx/dist/cli.mjs scripts/airlock_observer_smoke.ts
```

Add `--launch-calculator` only for the intentional harmless visible-app launch trial; Calculator must not already be running. The helper distinguishes real snapshot/heartbeat/app evidence from an unperformed physical USB insertion trial. Reports are under gitignored `data/airlock-observer-proof/`.

## Areeb: Jetson adapter

Create a separate identity using the same identity command; owner enrollment uses role `jetson`. Publish `person_in_zone`, `person_cleared`, `camera_online`, `camera_offline`, and `heartbeat`. Report detection confidence as a number in `[0,1]`; report no face identity or raw image/audio bytes. Include `captureMode` for the real capture path. A fallback webcam must identify itself accurately.

Send heartbeats at least every 3–5 seconds. A durable database timer runs every 5 seconds and marks health offline after more than 15 seconds without a heartbeat. Detection still needs a genuine camera adapter; the contract and fixtures do not implement inference.

## SecurityEventV1 example

```json
{
  "schema_version": 1,
  "workstation_id": "sid-desk",
  "session_id": "sid-session-1",
  "device_id": "sid-jetson",
  "event_id": "jetson-boot1-event42",
  "source": "jetson",
  "kind": "person_in_zone",
  "observed_at": 1794084000000,
  "seq": 42,
  "confidence": 0.91,
  "metadata": { "detail": "Zone occupied; no identity inferred", "captureMode": "Logitech live capture" },
  "simulated": false
}
```

Replace the example time with the actual capture time. Timestamps are UTC **epoch milliseconds**. IDs contain 1–128 letters, digits, `_`, `.`, `:`, or `-`. `seq` increases per enrolled device across reconnects and sessions; retain it on disk. Re-enrollment/sequence reset requires the owner workflow. Events older than 120 seconds, over 5 seconds in the future, outside the armed session, from the wrong source role, or with mismatched simulation provenance are denied. Metadata is at most 2048 serialized bytes and only bounded strings in `deviceKey`, `appId`, `label`, `detail`, `captureMode`, and `bootId`.

An exact duplicate event ID/payload is idempotent; reusing its ID for a different payload is denied. Retry the same queued event after an uncertain transport result. Do not manufacture a new event ID for the same observation. Use a durable producer queue; a callback success is not an end-to-end proof until the reducer commits.

From Node:

```js
import { publishSecurityEvent } from './scripts/airlock_publisher.mjs';
await publishSecurityEvent(event, {
  uri: 'http://127.0.0.1:3000', database: 'locki-airlock-sid', token: enrolledDeviceToken
});
```

From another runtime, POST a JSON positional array to `/v1/database/locki-airlock-sid/call/ingest_device_event?confirmed=true` with `Authorization: Bearer <device token>`:

```text
[workstation_id, session_id, device_id, event_id, kind, observed_at, seq,
 confidence, JSON.stringify(metadata), simulated, 1]
```

Clients should use generated SDK subscriptions to the authorized `my_*` views for incident/lock state. An enabled enrolled `mac` identity receives `my_incidents` and `my_device_events` for its own workstation, including correlated physical evidence; `my_lock_requests` remains restricted to requests targeting that Mac identity. Reuse the enrolled Mac token; do not grant it reviewer/service rights to enable speech. There is no unscoped incident feed. Raw private tables are intentionally inaccessible. Exact metadata and health values are listed in [the Mac integration contract](CONTRACTS.md).

## Synthetic fixtures

```bash
node scripts/airlock_publisher.mjs --fixture --workstation WORKSTATION_ID \
  --session SESSION_ID --kind usb_added --seq 1
```

This only prints a fixture. Add `--publish` with `AIRLOCK_DEVICE_TOKEN_FILE` pointing to the owner's private token to publish it through the real reducer. Owner fixtures require `device_id:"fixture"`, `source:"fixture"`, `simulated:true`, and a synthetic session. The helper never lets a fixture use `simulated:false`. Real JSON files require `--real-observation` to publish from the CLI; that flag is for actual sensor observations, not relabeling fixtures.

## New AIRLOCK managed runner

[`scripts/airlock_gateway.ts`](../../scripts/airlock_gateway.ts) is an explicit managed callback boundary. It is **not** a native NeMo Relay or Code Quest integration. It supports only `fixture_word_count`, `read_incident`, and `read_health`. Unknown tools (including shell commands) are rejected before any callback.

Create/enroll a separate identity as `service`, then propose a harmless action:

```bash
AIRLOCK_SERVICE_TOKEN_FILE=data/airlock/service-token.json \
  "$HOME/.local/share/locki-airlock/tools/node_modules/node/bin/node" \
  frontend/node_modules/tsx/dist/cli.mjs scripts/airlock_gateway.ts \
  --workstation WORKSTATION_ID --tool fixture_word_count \
  --args '{"text":"untrusted text stays data"}' --wait-seconds 120
```

The runner prints the pending action ID and exact arguments. A **different** owner or approval-capable reviewer approves it in the console. The service then awaits committed `consume_approval`, executes the allowlisted callback with immutable stored arguments, and reports the actual outcome. A pending action, model suggestion, expired grant, altered digest, reused grant, or mismatched service does not authorize execution. If execution succeeds but outcome reporting fails, do not retry the callback automatically.

```bash
"$HOME/.local/share/locki-airlock/tools/node_modules/node/bin/node" \
  frontend/node_modules/tsx/dist/cli.mjs scripts/airlock_gateway.ts --deny-demo
```

Lock requests are durable intent rows for a future explicitly enabled target-Mac adapter. No script supplied here executes a screen lock, motor command, real external notification, or arbitrary shell tool.
