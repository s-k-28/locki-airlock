# Mac integration contract — Sid's server answers

These answers describe the current Sid module in `spacetime/src/index.ts`.
Dhruv reported local commit `4277d99` on `feature/airlock-dhruv`; that unpushed
branch and its original questions were not available in this checkout. This
document does not claim to review or merge that commit. The complete reducer
and row contract is [spacetime/CONTRACT.md](../../spacetime/CONTRACT.md).

## Enrolled Mac subscriptions

Use the same token whose identity the owner enrolled as role `mac`:

```sql
SELECT * FROM my_sessions;
SELECT * FROM my_incidents;
SELECT * FROM my_device_events;
SELECT * FROM my_lock_requests;
```

SDK accessors are `mySessions`, `myIncidents`, `myDeviceEvents`, and
`myLockRequests`. The two observation views now include enabled enrolled Macs,
scoped to their workstation memberships. They include the workstation's
correlated Jetson evidence, not only events published by the Mac itself.
Backing tables stay private. No event/incident row schema changed, so this fix
does not require regenerating client bindings.

Lock requests were already visible to the enrolled **target Mac identity**.
Check `targetDeviceId`: being another Mac enrolled at the same workstation
does not grant that request. A live session freezes the Mac identity/device
that supplied its baseline. No request means no authorized lock intent; lack
of a request must not be treated as a successful lock.

Revoking the identity or removing/reassigning its last enabled matching Mac
device removes its observation access. An unrelated workstation and Jetson
identities cannot use these incident/event views. This change grants no
acknowledgement, resolution, disarm, protected-action, approval, agent-trace or
audit access to a Mac. Do not promote the voice adapter to reviewer to work
around missing observations.

## Exact event metadata

`metadataJson` is a serialized JSON **object**, with only these optional keys:

| Key | Requirement |
|---|---|
| `deviceKey` | Required and nonempty for `usb_added` and `usb_removed`; use a redacted local device digest. |
| `appId` | Required and nonempty for `app_launched`; normally the bundle identifier. |
| `label` | Optional short redacted display label. |
| `detail` | Optional factual redacted observation detail. |
| `captureMode` | Optional description of the actual capture method; no enum. |
| `bootId` | Optional producer boot marker; no enum. |

Every present value must be a string of at most **256 JavaScript UTF-16 code
units**. Numbers, booleans, nulls, arrays and nested objects are rejected. Both
the submitted JSON and its canonical form must fit **2048 UTF-8 bytes**. No
additional keys are accepted; `vendorId`, `productId`, `serial`, `path`,
`signed`, `timestamp`, and `status` are not metadata keys. The adapter must
redact permitted strings too; a permitted key does not make its contents safe.
Do not send recordings, device files or raw serials.

The portable JSON Schema is slightly stricter: if `deviceKey` or `appId` is
present, it must be nonempty even for another event kind. Omit irrelevant keys
instead of sending empty values. Its Unicode character counting differs from
JavaScript's UTF-16 counting; obey the server limit above as well.

```json
{"deviceKey":"sha256:REDACTED_DIGEST","label":"USB peripheral","captureMode":"ioreg snapshot poll","bootId":"boot-1"}
```

Time, sequence, confidence and provenance belong in the event envelope:
`observedAtMs` (UTC epoch milliseconds), increasing per-device `seq`,
`confidence` in `[0,1]`, `simulated`, and `schemaVersion:1`. Generated SDK
reducers use these camelCase names; the portable publisher envelope uses
`observed_at`, `schema_version`, etc., as documented in TEAMMATE_INTERFACE.md.
The observation must fall inside the committed armed interval, be no more than
120 seconds old, and no more than five seconds in the server's future. Preserve
real capture times; do not relabel a pre-arm observation to make it admissible.
Check clock synchronization if genuine post-arm observations are rejected.

## Full health status and component lists

`report_health` accepts exactly **`online`, `degraded`, `offline`,
`unavailable`**. Values such as `ok`, `healthy`, `error`, `starting`, and
`disconnected` are rejected; describe the condition using `detail` instead.

| Enrolled role | Allowed `component` values |
|---|---|
| `mac` | `watcher`, `usb`, `apps`, `voice` |
| `jetson` | `camera`, `jetson` |
| `service` | `model`, `gateway` |

`detail` is a string limited to 512 JavaScript UTF-16 code units before trimming.
The `capacity` component on device `database` is server-owned and cannot be
reported by an adapter. Device health is not a session or lock status enum.

```ts
await connection.reducers.reportHealth({
  workstationId, deviceId,
  component: "voice", status: "online",
  detail: "Incident subscription and local warning output available",
});
```

Report a component online only when it is actually available. A generic Mac
heartbeat refreshes `watcher`; it does not establish microphone/audio health.
USB/app observations refresh `usb`/`apps` respectively. Refresh active component
health every 3–5 seconds: online/degraded records become offline after more than
15 seconds without an update, processed by the five-second server maintenance
timer. Gateway liveness never makes model health online.

## End-to-end verification boundary

The regression verifier `scripts/airlock_mac_views_verify.ts` checks separate
authenticated owner/Mac/Jetson identities, same incident/event IDs, targeted
lock visibility, workstation isolation and revocation. Its observations are
explicit fixtures; it opens no camera, microphone, speaker or OS lock.

Dhruv's reported **262 ms local warning** remains his measurement, separate
from server-committed incident subscription-to-voice latency. After deployment,
reconnect using the same Mac token and test a fresh incident: old incidents
may be intentionally suppressed by the voice bridge's age/dedup rules.
