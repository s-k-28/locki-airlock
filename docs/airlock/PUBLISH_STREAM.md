# Durable native-app publication

`airlock_publish_stream.mjs` consumes real `SecurityEventV1` JSON lines. It does
not open a sensor, launch an application, or execute a lock. The native Swift
observer remains the producer; use its dedicated enrolled Mac device and counter.

```bash
swift scripts/airlock_app_observer.swift --workstation WORKSTATION_ID \
  --session SESSION_ID --device APP_DEVICE_ID \
  --state-file data/airlock/app-seq.json --duration 30 | \
  AIRLOCK_DEVICE_TOKEN_FILE=data/airlock/app-token.json \
  node scripts/airlock_publish_stream.mjs --real-observation \
  --state-file data/airlock/app-publisher/state.json
```

The spool directory must belong to the current operator and have mode **0700**;
the script creates new directories privately. State and lock files are regular
operator-owned **0600** files; symlinks and permissive files are rejected. If
`--state-file` is omitted, the default is
`<token-file>.stream-spool/state.json`. Keep the same state path across restarts.
The file binds to the normalized endpoint, database, and a credential fingerprint;
it contains no token. A changed destination/token or corrupt state stops
publication without resetting or deleting pending evidence.

Each valid line is saved using a private temporary file, file fsync, atomic
rename, and directory fsync **before its first network call**. Intake can spool
later lines while an earlier call waits. A single FIFO worker submits the
original event ID, sequence, capture timestamp, and metadata; only a successful
confirmed ingest removes that head from the spool. If ingestion committed but
its response or local ACK was lost, restart replays the identical request and
uses the server's existing idempotent event handling. Health-report failure is
separate from the committed event ACK.

Storage is limited to **128 pending events / 1 MiB**, and each input line to
**8192 bytes**. A full spool backpressures stdin rather than evicting an event.
There are at most five delivery attempts per event per run. Retry exhaustion or
an explicit reducer denial stops publication with pending evidence retained.
An exclusive process lock prevents concurrent consumers of one spool. A lock
is reclaimed automatically only when its recorded PID is demonstrably absent;
unknown, active, permission-denied, or incomplete-recovery locks are preserved
for manual inspection. A reused PID may conservatively require manual review.

Loaded/replayed events never send an explicit `apps:online` report. Only fresh
new input with the native observer capture marker may report it, within 15s of
capture, while the input pipeline is still open and has not failed. Buffered
evidence does not prove current observer availability.

## Restart and blocked-head recovery

After an ordinary network failure, restore the same destination/credentials and
rerun the **same consumer/state path**. To drain its backlog without starting
the producer, use:

```bash
AIRLOCK_DEVICE_TOKEN_FILE=data/airlock/app-token.json \
node scripts/airlock_publish_stream.mjs --real-observation \
  --state-file data/airlock/app-publisher/state.json < /dev/null
```

Even an old head gets a server attempt: an already committed event can deduplicate
after its freshness window. An uncommitted event may instead be denied because
the original session ended, its capture is too old, enrollment changed, or its
sequence is no longer admissible. The head stays intact and blocks later FIFO
delivery. Do not rewrite its timestamp, ID, session, or provenance to force it
through, and do not silently remove it.

For such a permanent denial, stop the producer/consumer; have the workstation
owner inspect the actual denial and committed views. Preserve the complete
private spool as review evidence. If the owner deliberately accepts that some
observations cannot be committed, move that stopped spool to a separate private
archive (retain the original 0600 file; do not publish it). Start a fresh spool
path only after the normal enrollment/session/counter workflow is reconciled.
This is an acknowledged evidence gap, not successful replay. An unknown lock,
recovery marker, or corrupt file also needs manual inspection; do not delete it
while any associated consumer might still be running.

## Verification boundary

`scripts/airlock_publish_stream.test.ts` uses explicit transport fixtures and a
loopback fake HTTP server, including an actual consumer SIGKILL/restart after a
simulated committed request loses its ACK. It tests persistence before send,
FIFO replay, reducer-denial retention, corrupt/insecure state, bounded storage,
and freshness-safe health reporting. It is not a live sensor or deployed-database
outage trial.

The guarantee is **consumer-spooled events survive restart**. A producer crash,
partial stdout write, or consumer crash before a line reaches durable storage
can still lose that observation. The current Swift producer persists its counter
before emitting the payload and has no producer journal/ACK protocol. This change
does not close that earlier capture-to-spool window or claim lossless hardware
capture. Expired evidence and optional archived copies require explicit human
review and deployment retention handling.

The implementation uses Node's [file handle sync and filesystem APIs](https://nodejs.org/api/fs.html)
to request persistence and atomic replacement on the same filesystem. Actual
crash/power-loss behavior still depends on the operating system, filesystem,
and storage honoring those operations; no physical power-loss trial is claimed.
