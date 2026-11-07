# LOCKI AIRLOCK: authoritative SpacetimeDB module

AIRLOCK session, event, incident, health, review, lock-request and protected-action state is committed by the TypeScript reducers in `src/index.ts`. The dashboard and hardware adapters subscribe to caller-scoped views. FastAPI does not maintain a second incident database.

Read [CONTRACT.md](CONTRACT.md) for the exact integration API and [fixtures/security-events.v1.json](fixtures/security-events.v1.json) for labeled input examples. Fixture JSON has placeholder timestamps: publishers must fill current timestamps and increasing device sequences. A fixture is an input to the real reducers, never a second backend.

## Build and run

Validated with SpacetimeDB CLI/module SDK **2.14.1**, TypeScript 5.8.3 and Vitest 5.1.0. The SDK declares Node `^22.23.4 || ^24.22.1 || >=26.12.1`; use a supported Node version for deployment. The development machine's Node 24.14.1 emits an engine warning, but type checking, both test suites, module build, publish and generated-client integration were exercised on that machine.

From the repository root:

```sh
npm ci --prefix spacetime
npm run check --prefix spacetime
npm test --prefix spacetime
spacetime build --module-path spacetime
```

Start a local SpacetimeDB server in a separate terminal, then publish without deleting existing data:

```sh
spacetime start --listen-addr 127.0.0.1:3000
spacetime publish --server http://127.0.0.1:3000 --module-path spacetime --delete-data=never --yes locki-airlock-sid
spacetime generate --lang typescript --module-path spacetime --out-dir frontend/src/airlock/module_bindings
```

The current local fallback database is `locki-airlock-sid`; the previously used `locki-airlock` fixture database is preserved. The primary cloud database is `locki-w91bw` on `https://maincloud.spacetimedb.com`. The repository integration scripts and frontend configuration select the actual URI and database. A cloud database needs the deployment owner's authenticated permission; a supplied URL alone does not grant publish authority. Do not use `--delete-data` to work around migration errors. No reducer performs a filesystem, network, OS-lock or motor operation.

## Security decisions

All 16 backing tables, including baseline captures and the schedule table, are private. The 15 `my_*` views first read the caller's indexed membership and expose only the assigned workstation data. Owners and reviewers see case data; sensor devices receive the station/session/health state and their own enrollment. Only the target Mac receives its lock queue. A database administrator remains a trusted administrator, as in SpacetimeDB's access model.

A new workstation records **the authenticated caller** as its owner. Creating another connection does not grant rights to an existing workstation. Device enrollment binds the server sender identity, workstation, device ID and role. Revocation removes subscribed view rows and prevents subsequent mutations. Removing review approval permission invalidates outstanding grants; consumption also checks the approver's current permission.

A live armed session requires an authenticated enrolled Mac USB capture observed within 30 seconds and same-device online USB health within 15 seconds. `record_baseline` accepts the actual redacted inventory and active application ID (or explicit `unknown`), refreshes USB health, and exposes caller-scoped latest/frozen captures. Arming uses the capture automatically when baselineJson is empty, or rejects a manual inventory that differs. The session freezes that inventory, active app, server-derived camera/health state, app allowlist, auto-lock consent and policy hash. Missing/offline camera health stays explicit and does not block healthy USB protection. Simulated sessions retain explicit owner fixture baselines. Captures are adapter reports, not cryptographic proof of hardware; an active-app snapshot does not assert continuous app-observer health. Policy changes apply to the next armed session; disarm immediately cancels pending lock requests and starts a 30-second owner-return grace period.

Unknown USB creates a medium incident independently of camera availability. A person observation alone is soft evidence. Same-workstation, same-session, same-provenance physical and unknown-USB observations correlate when their observed times differ by **at most 10,000ms**, regardless of arrival order. They raise the same incident to high priority. Repeated USB observations of the same key join its unresolved case, preventing polling from creating repeated lock requests; approved USB is informational.

Events require schema version 1, bounded metadata, monotonic device sequence, a unique event ID, a timestamp in the armed interval, maximum age 120 seconds and future skew at most 5 seconds. An exact duplicate is idempotent; a changed duplicate is denied. Owner-controlled disarm/re-enrollment is required to reset a device sequence after a boot loss. Input is bounded to 64 accepted events per device per second and 100,000 non-heartbeat events per session. JSON limits count UTF-8 bytes, and baseline parsing has a 70KB cap.

Auto-lock requires frozen owner consent, a narrow unknown-USB trigger observed in the last 15 seconds, a live armed session, the Mac identity/device frozen with the baseline, one request per incident, cooldown and return-grace checks. Simulated requests are labeled and cannot be reported as real execution. `requested`, `executed` and `confirmed` are different facts; an API invocation is not independent OS confirmation. Expired, cancelled and terminal requests cannot regress. The local Mac adapter must check current request/session policy before invoking an OS action.

Protected actions bind canonical JSON arguments, workstation, action, mission, incident, tool, scope and expiry with server-computed SHA-256. A proposer cannot approve its own action. Only the **same enrolled service that proposed** the action may spend its matching one-time approval, and it must await the committed reducer success before calling the managed callback. The consuming service alone can report the execution result. An owner-created proposal is **review-only**, because it has no assigned executing service. Authorization consumption does not promise exactly-once external execution across a gateway crash; an uncertain result must be reviewed, not silently retried with a new grant.

Agent runs are bound to their first reporting service through the append-only audit journal. Model and trace identifiers stay fixed during that run; steps increase monotonically and cannot continue a terminal run. These are **adapter-reported** observations, not independent proof of a particular model/runtime or a native Relay trace. A generic gateway heartbeat does not mark a model online.

A private durable schedule runs every 5 seconds per workstation and marks silent health sources offline after 15 seconds, expires sessions and pending requests/grants, and leaves security-session and incident state independent of health. The unique workstation schedule index prevents timer multiplication. Existing workstations created before the scheduler upgrade receive their timer at their next ingest or refresh.

## Verification and limits

`npm test --prefix spacetime` runs 28 pure policy tests, including a deterministic 2,000-case time sweep, plus 39 module tests using the official **real in-memory SpacetimeDB datastore and transactional harness**. The latter cover reducer correlation boundaries/order, polling grouping, stale-action suppression, role denials/rollback, duplicate payload conflict, revoked grants, exact consumer binding, scheduled expiry, per-sensor health, run attribution, rate limits, lock lifecycle, baseline identity/freshness/freeze, camera degradation and bounded retention with reference preservation. These tests do not open a camera, perform inference, invoke an OS lock or move hardware.

The independent repository verifier uses actual generated SDK connections and authenticated HTTP against a deployed server to check subscription isolation, multiple identities, reconnection, correlation, role denials and one-time managed callbacks. Its output records the exact tested scope. Local-process subscription timings are not measurements from two physical laptops, and the 2,000-case algorithm sweep is not camera accuracy.

No raw video, image, audio, device file or arbitrary process payload is accepted. Adapters must redact the contents of allowed metadata strings and use salted local device digests rather than serial numbers. Scheduled maintenance applies a 24-hour metadata retention default with at most 200 row deletions per workstation per tick. Active sessions, unresolved cases and nonterminal actions/investigations retain their evidence; terminal session graphs are removed in reference-safe order after the full grace interval, potentially over several ticks. Associated audit attribution survives as long as its entity does. Latest captures and unreferenced old audit metadata also expire. See the exact retention scope in [CONTRACT.md](CONTRACT.md). Deployment exposure, identity issuance/rate limiting, backups, external evidence files and hardware calibration still need an operational policy. The prototype's 10s correlation window, 15s freshness/heartbeat bounds, 5s maintenance tick and rate caps are explicit engineering constants, not calibrated hardware results.

## Official references checked

- [TypeScript reducers and transactional behavior](https://spacetimedb.com/docs/functions/reducers/)
- [Reducer sender/context](https://spacetimedb.com/docs/functions/reducers/reducer-context/)
- [Private tables and caller-scoped views](https://spacetimedb.com/docs/tables/access-permissions/)
- [Views and indexed read-only access](https://spacetimedb.com/docs/functions/views/)
- [Durable interval schedules and current TypeScript `onSchedule`](https://spacetimedb.com/docs/tables/schedule-tables/)
- [Compatible additive schema migrations](https://spacetimedb.com/docs/databases/automatic-migrations/)
- [Official module test harness](https://spacetimedb.com/docs/how-to/unit-testing/)
- [TypeScript client subscriptions/accessors](https://spacetimedb.com/docs/clients/typescript/)

Official docs, installed SDK types/source and CLI help were checked on 2026-11-07. The published module's CLI build, schema migration and generated bindings provide the executable version check.

Final review added atomic one-time lock execution claims, canonical local execution state, stale enrollment revocation and bounded heartbeat retention. See CONTRACT.md for exact signatures and revised capacity semantics. Owner panic is explicit manual intent and preserves all lock-consent gates.
