# AIRLOCK decisions and evidence ledger

Updated 2026-11-07. The local PRD is the product requirement; this file records implementation interpretations and measured evidence, rather than changing that PRD.

## Decisions

| Decision | Reason and source |
|---|---|
| Database reducers create incidents and authorize transitions | One transaction owns the event/incident/approval mutation. Failed reducers roll back changes, which supports atomic approval consumption. [SpacetimeDB reducers](https://spacetimedb.com/docs/functions/reducers/). |
| Private backing tables, public caller-scoped views | A frontend filter cannot protect sensitive rows. Server view membership determines exposure; device identities receive the limited views their roles need. [Table access permissions](https://spacetimedb.com/docs/tables/access-permissions/). |
| Distinct owner, reviewer, Mac, Jetson, and service identities | Reducers authenticate the server-provided caller. Payload labels and browser profiles do not grant roles. Persisting the SDK token preserves identity after reconnect. [TypeScript SDK authentication](https://spacetimedb.com/docs/clients/typescript/), [HTTP authorization](https://spacetimedb.com/docs/http/authorization/). |
| Native reducer HTTP endpoint for producer adapters | The host authenticates bearer tokens and supplies the reducer sender; the payload is a positional argument array. This avoids treating a custom HTTP handler's arbitrary identity as trusted. [Database HTTP API](https://spacetimedb.com/docs/http/database/). |
| Unknown USB alone opens medium; proximity alone stays soft | Detailed PRD policy requires independent digital protection and avoids accusing a nearby person. Physical evidence can upgrade the same incident within an inclusive 10 seconds in either arrival order. |
| Ten-second correlation is a specified heuristic | It is the PRD window, not an empirically learned/optimized threshold. Workstation, session, time, and provenance must all agree. Input-vector correctness is distinct from camera detection quality. |
| Frozen arm baseline/policy, bounded event times and monotonic sequences | Later configuration changes do not silently widen current consent. Exact replay is idempotent; payload conflicts, stale evidence and cross-session evidence are denied. The 120-second age/5-second future bounds are explicit prototype engineering choices. |
| Durable five-second maintenance timer | Missing devices cannot report their own failure. Server maintenance expires stale health and elapsed sessions/actions even without another client call. Health becomes offline after more than 15 seconds without a heartbeat. |
| Separate human approval and service consumption | Approval binds exact canonical action content/digest and is one-use. The requesting identity cannot approve itself. The managed runner executes only a narrow read-only allowlist after consumption commits. This applies least privilege and downstream authorization recommended by [OWASP Excessive Agency](https://genai.owasp.org/llmrisk/llm062025-excessive-agency/). |
| Simulation provenance is mandatory | Test fixtures remain visibly synthetic and never emit a real lock. A reducer-generated incident is real database state without implying its evidence was physically observed. |
| Screen lock and model execution stay adapter boundaries | A committed lock request is not proof of a locked Mac. This implementation does not invent model responses, Relay traces, or physical camera results. Existing voice behavior remains separately connected. |
| Native visible-app notifications | The Swift adapter registers on `NSWorkspace.notificationCenter`, as required for launch delivery; Apple excludes background/LSUIElement apps from this notification. Signature status is a bounded basic check at observation time, not a trust or malware verdict. [Apple launch notification](https://developer.apple.com/documentation/appkit/nsworkspace/didlaunchapplicationnotification), [static code validity](https://developer.apple.com/documentation/security/secstaticcodecheckvalidity(_:_:_:)). |

## Verified versus pending

| Claim | Evidence/status |
|---|---|
| Real SpacetimeDB application authority | Module built/published locally using CLI/SDK 2.14.1; generated SDK subscriptions/reducers used by the independent verifier. No mocked database transport. |
| Independent authorized clients share incidents | Verified with separate owner/reviewer tokens and connections in one local Node process; same committed incident IDs. Two physical laptops/LAN demonstration remains pending. |
| Private reads and role boundaries | Real SQL attempts against existing private `incidents`, `device_events`, `approvals`, `sessions` denied; caller-scoped views expose no other member's rows. |
| Correlation algorithm behavior | Fourteen real reducer vectors test camera-first/USB-first at -10001, -10000, -9999, 0, +9999, +10000, +10001 ms. These are invariant tests, not sensor-accuracy measurements. |
| Replay, stale input, approvals, reconnect | Real reducer verification covers exact replay, modified replay, sequence/time/schema rejection, cross-workstation/session evidence, approval permissions/digest/expiry/reuse/substitution, revocation, and new connection with the saved token. |
| Server-side offline health | An idle fixture station becomes offline through the durable schedule during a >21-second no-refresh period. |
| Managed protected callback | Three actual read-only callbacks are exercised after separate approval and committed consumption. Unknown tools are rejected. No native Relay middleware, existing Code Quest interception, or Nemotron call is claimed. |
| Mac USB/app observation | Actual local proof `observer-1794085079490-bc285c` committed a real IORegistry heartbeat and genuine native Calculator launch in a live auto-lock-disabled session. USB snapshot 25.979 ms; zero peripherals. This establishes actual app observation/publication, not a physical USB insertion trial. |
| Screen locking, motors, external alerts | No actual action executed by the launch/publisher/verifier/managed-runner scripts. Consent-based real lock integration remains Dhruv's separately tested adapter. |
| Jetson camera accuracy/false alarms | Not measured here. Requires genuine capture, labeled human trials and explicit sample counts. No 90% accuracy or <=1/20 false-alert claim. |
| Nemotron and NeMo Relay | Their production/model/native-trace work is separate and unverified by these scripts. No fabricated inference or trace is shown. |
| Disk persistence | Actual coordinated local SIGTERM/restart on the same data directory restored 29 committed incidents with the same saved identity. `data/airlock-verification/verify-1794085398044-bb08ed7e/persistence-check.json` records the successful check. This is graceful restart evidence, not a power-loss/crash durability claim. |

Machine-readable measured results live in gitignored `data/airlock-verification/<run>/report.json`. A sanitized final run summary should be recorded here only after the final run; do not copy the neighboring private token checkpoint into documentation or Git.

## Measured cloud run

`verify-1794085158961-a4e9dae6` completed **45/45 named integration groups** against the user's deployed `locki-w91bw` database at `https://maincloud.spacetimedb.com`, with unique fixture workstation IDs and no resets. It includes the 14 correlation vectors, same-key USB grouping, malformed metadata, revoked/downgraded approvers, service binding, and three actual managed callbacks. This is a deterministic invariant pass count, not hardware detection accuracy.

| USB reducer publication → subscribed cache | Samples | p50 | p95 | Maximum |
|---|---:|---:|---:|---:|
| Owner connection | 15 | 70.51 ms | 161.12 ms | 161.12 ms |
| Reviewer connection | 15 | 71.34 ms | 161.14 ms | 161.14 ms |

These are independent authenticated SDK connections from one Node process through the hosted cloud service. The boundary begins immediately before publishing USB metadata, ends when that client's SDK cache exposes the incident, and uses 10 ms cache polling. It includes the cloud network and excludes hardware capture, audio, browser rendering, and two-physical-laptop timing. At n=15, the nearest-rank p95 is the maximum. Raw samples and all named groups are in `data/airlock-verification/verify-1794085158961-a4e9dae6/report.json`.

## Measured local run

`verify-1794085398044-bb08ed7e` completed **45/45 groups** against the fresh persistent local `locki-airlock-sid` database at `http://127.0.0.1:3000`. This run additionally checks that the queried backing tables exist and are marked private in the actual deployed schema, and races two approval consumers to verify exactly one committed success.

| USB publication → subscribed cache | Samples | p50 | p95 | Maximum |
|---|---:|---:|---:|---:|
| Owner connection | 15 | 20.07 ms | 24.56 ms | 24.56 ms |
| Reviewer connection | 15 | 20.09 ms | 24.58 ms | 24.58 ms |

The same timing boundary and limitations apply, with loopback transport instead of the hosted cloud. Raw evidence is `data/airlock-verification/verify-1794085398044-bb08ed7e/report.json`.

After the integration owner stopped and restarted that actual server with the same `data/airlock-spacetime/` directory, the checkpoint check restored **29 committed incidents** for the same identity. The successful check timestamp is `2026-11-07T21:06:29.285Z`; evidence is the sibling `persistence-check.json`. No data reset occurred.

## Immediate voice warning evidence

The new restricted local voice service and SDK bridge use the existing `Speaker`/`CachedSystemTTS`; voice-engine/runtime source remains unchanged. Eighteen focused tests passed, including P0 interruption of a playing P2 and clearing queued P3 speech. One real committed synthetic incident then reached the authenticated loopback endpoint and completed actual cached macOS output: `system_cache`, one cache hit/zero misses, six prewarmed fixed lines, 0.9 ms queue wait, 76.3 ms audio-driver start, and 85.68 ms local HTTP delivery (n=1). Its line explicitly began “Rehearsal.” No microphone/model/camera/robot ran.

The same warning was suppressed after a real voice-service restart. The service was returned to audio-disabled mode. [VOICE.md](VOICE.md) documents the exact commands, limits, private capability, replay boundary, and absence of security/robot/microphone command routes. Evidence is `data/airlock-voice/proof/report.json`. This is not a hardware-capture-to-audible timing or complete live-ASR/model-conversation claim.
