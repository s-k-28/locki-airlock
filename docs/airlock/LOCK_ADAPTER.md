# Local Mac screen-lock adapter

The screen-lock path is implemented in `scripts/airlock_lock_adapter.ts` and the fixed Swift boundary `scripts/airlock_lock_helper.swift`. **It defaults to dry run. No actual OS lock, keyboard event or Accessibility prompt was executed during implementation or tests.** A real lock still needs the local operator's explicit opt-in and hardware validation on that Mac.

The adapter reads reducer-committed requests addressed to its enrolled **Mac device identity**. Live arming also requires a fresh authenticated Mac baseline; the database freezes the baseline publisher as that session's lock target. It does not accept voice, untrusted README text, shell commands, arbitrary executable paths, model instructions or a frontend claim of authorization as permission to lock.

## Preview first

Use that Mac publisher's private token file, not an owner or reviewer token. Configure the primary cloud database with `AIRLOCK_SPACETIME_URI=https://maincloud.spacetimedb.com` and `AIRLOCK_DATABASE=locki-w91bw`, or the local fallback URI with `AIRLOCK_DATABASE=locki-airlock-sid`.

```sh
frontend/node_modules/.bin/tsx scripts/airlock_lock_adapter.ts \
  --workstation YOUR_WORKSTATION --device YOUR_ENROLLED_MAC_ID \
  --token-file data/airlock/mac-token.private.json \
  --state-file data/airlock/mac-lock-state.private.json --once
```

Preview subscribes only to five filtered caller-authorized views, fetches fresh authorized state, and prints why a request would be allowed or blocked. It never invokes the Swift helper, writes a processed-action reservation, or reports an executed lock. A synthetic request is always blocked. Without `--once`, it observes until interrupted; `--duration SECONDS` bounds a trial.

## Explicit local consent

Three independent requirements must all hold before a real OS action:

1. The authenticated owner enabled auto-lock in the database policy and armed a **live** session with that frozen consent. The request must be non-synthetic, unexpired, addressed to this enrolled Mac and tied to that same armed session and policy hash.
2. The local operator created an owner-only consent file binding **workstation ID, Mac device ID, exact policy hash and expiry**, and explicitly changed `enabled` to `true` after reviewing it. A consent lifetime may be at most 24 hours. The adapter rereads the file for each request; the helper rechecks the actual file before posting any event.
3. The operator launched the adapter with `--execute`. That flag alone is insufficient. The fixed compiled helper also requires existing Accessibility trust and never requests a permission prompt.

A disabled template can be created from the enrolled Mac's authorized station state:

```sh
frontend/node_modules/.bin/tsx scripts/airlock_lock_adapter.ts \
  --workstation YOUR_WORKSTATION --device YOUR_ENROLLED_MAC_ID \
  --token-file data/airlock/mac-token.private.json \
  --state-file data/airlock/mac-lock-state.private.json \
  --write-consent-template data/airlock/mac-lock-consent.private.json
```

This writes a new mode-0600 file with `enabled: false`; it refuses to overwrite an existing file. Its shape is:

```json
{
  "schemaVersion": 1,
  "enabled": false,
  "workstationId": "YOUR_WORKSTATION",
  "deviceId": "YOUR_ENROLLED_MAC_ID",
  "ownerPolicyHash": "64-lowercase-hex-characters-from-the-current-policy",
  "expiresAtMs": 0
}
```

The template command fills actual IDs/hash and a one-hour expiry. The example above is deliberately non-executable. Consent and token files must be regular files owned by the current operator, mode 0600, and not symlinks. Disabling/removing the consent file blocks subsequent OS actions. Changing the owner policy also invalidates the existing local consent binding. The adapter conservatively requires current and frozen hashes to agree, even though the database preserves a frozen session policy until disarm.

## Compile; do not assume compilation proved a lock

On the target Mac, build the helper at its **fixed** location and keep it owner-only:

```sh
mkdir -p data/airlock
swiftc scripts/airlock_lock_helper.swift -o data/airlock/airlock_lock_helper
chmod 700 data/airlock/airlock_lock_helper
```

The implementation check compiled a separate binary under `/tmp` and **did not run it**. Compilation checks the Swift/API types; it does not establish Accessibility permission, shortcut delivery, keyboard layout behavior or an OS lock.

Only after the local operator has reviewed the consent file, tested recovery and explicitly enabled it should they choose to run:

```sh
frontend/node_modules/.bin/tsx scripts/airlock_lock_adapter.ts \
  --workstation YOUR_WORKSTATION --device YOUR_ENROLLED_MAC_ID \
  --token-file data/airlock/mac-token.private.json \
  --state-file data/airlock/mac-lock-state.private.json \
  --consent-file data/airlock/mac-lock-consent.private.json --execute
```

This command was not run during implementation. There is no automatic execution fallback. Without existing Accessibility trust, the helper returns a failed result without posting a key or opening a permission prompt. The helper's only posted key is Apple's Control-Command-Q combination through Core Graphics. It cannot select another command, run a shell, terminate a process, change Lockdown Mode, control a motor or unlock the Mac.

## Lifecycle, replay and uncertainty

Immediately before invoking the helper, the adapter rechecks a fresh authenticated database snapshot. The membership must still be enabled with role Mac, the device must still belong to that caller, the session must still be armed, and the target request and hashes must still match. It checks deadline, owner-return grace, server cooldown settings, superseding requests and a local attempt cooldown of at least 30 seconds. Disconnected/stale snapshots fail closed. Actual external execution and a database transition cannot be made one atomic transaction; a policy/disarm race after the final check remains an inherent adapter boundary and must be considered during the owner trial.

Real execution uses a mandatory canonical ledger and mutex under the operator's account home, `Library/Application Support/LOCKI AIRLOCK/lock-execution/`, keyed by SHA-256 of workstation/device/authenticated identity. The account home comes from the OS user database, not the `HOME` environment variable. `--state-file` controls dry-run preview state only; it cannot select a different real execution ledger. The canonical mutex is acquired **before** loading that ledger.

Before invoking the helper, the adapter writes and fsyncs a private **reservation for that action ID**, then awaits the committed `claim_lock({actionId, executionId})` reducer. The private database claim table permits exactly one winner per request, including cloned identities on different hosts. A successful claim changes the request to `executing`; it does not assert an OS event. A failed or uncertain claim never invokes the helper and never reports an outcome on another claimant's behalf. The adapter rechecks fresh policy once more after the claim and before the fixed helper. It retains processed IDs for 24 hours past request expiry, bounds them to 1024 entries, and refuses new OS actions if capacity is reached. The canonical exclusive process lock prevents concurrent adapters from selecting separate ledgers for the same enrolled Mac, and the state directory must be operator-owned and not writable by others. SIGINT/SIGTERM stops new actions and releases the process lock during normal shutdown. Every reserved ID remains non-retryable after a crash or restart, including a helper timeout or failed result report. A reserved action with no known outcome is marked uncertain for human review; it is never silently executed again.

The outcome report may be retried while the request is still valid, but the OS action is never retried. Reporting failures and uncertain outcomes remain in the local state file. Do not delete or edit processed-action state to obtain a new lock attempt. If a crash left `.process.lock`, first verify the recorded process is no longer running and inspect its action reservations; only then may the local operator remove that stale process-lock file. This does not authorize resetting processed-action history.

The durable states distinguish:

- **Requested:** the database committed a consented command for the target Mac.
- **Executing:** one-time server authorization was claimed. A crash after this point never reissues that authorization.
- **Executed:** the helper reported posting the fixed shortcut. That is an invocation fact, not proof that macOS accepted the shortcut or remained locked.
- **Failed/uncertain:** permission, helper or transport failed; an uncertain helper result may have posted a key and must be checked locally.
- **Confirmed:** requires a separate reliable observation of the OS lock state. This adapter **never automatically reports confirmed**.

After posting a shortcut, the adapter separately reads public Quartz session availability. The documented session API does not provide a reliable public screen-lock Boolean. The often-used `CGSSessionScreenIsLocked` dictionary key is undocumented and has not been validated here, so it is not used as proof. Process exit, a running screensaver, an inactive GUI session and helper success are not promoted to lock confirmation. The UI retains `executed` with an explicit unconfirmed detail until the owner verifies the actual behavior; no invented green confirmation is sent.

## Completed verification

```sh
spacetime/node_modules/.bin/tsx --test scripts/tests/airlock_lock_adapter.test.ts
AIRLOCK_DATABASE=locki-airlock-sid AIRLOCK_SPACETIME_URI=http://127.0.0.1:3000 \
  spacetime/node_modules/.bin/tsx scripts/tests/airlock_lock_preview.integration.ts
```

The **59 targeted tests** inject fake invocation/report functions. They cover each policy dimension, stale/disconnected state, synthetic isolation, scope/identity changes, consent withdrawal/mismatch/expiry, return grace, cooldown and deadline boundaries, private-file/symlink checks, reservation-before-invocation, crash/restart replay denial, changed request substitution and outcome-report failure. They never start the Swift helper or post any OS event.

The integration check creates a labeled **simulated** request through a real local SpacetimeDB instance, starts the actual adapter in its default child-process dry run, verifies authenticated SDK subscriptions and fresh HTTP view reads, and confirms no helper invocation, execution report or state reservation occurs. It passed against the local `locki-airlock-sid` database. SpacetimeDB 2.14.1 rejected multiple SQL statements despite older HTTP documentation, so the adapter issues five bounded scoped reads concurrently and checks snapshot freshness.

The remaining acceptance work is an explicit, owner-consented actual-Mac trial: verify the exact shortcut/layout and Accessibility grant, confirm the physical lock by an independent trustworthy observation, unlock, disarm, test reconnect without relocking, and record timings. These tests do not claim that trial happened.

Official references checked: [Apple's Control-Command-Q shortcut](https://support.apple.com/en-us/102650), [non-prompting Accessibility trust options](https://developer.apple.com/documentation/applicationservices/1459186-axisprocesstrustedwithoptions), [Core Graphics event flags](https://developer.apple.com/documentation/coregraphics/cgeventflags), [public Quartz session dictionary API](https://developer.apple.com/documentation/coregraphics/cgsessioncopycurrentdictionary()). The installed macOS SDK headers were also checked and the Swift file compiled successfully.
