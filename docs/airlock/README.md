# LOCKI AIRLOCK: local run and integration

AIRLOCK's authority is the TypeScript module in `spacetime/`. The React console reads caller-scoped SpacetimeDB views. FastAPI's existing voice interface remains a separate optional telemetry connection. The default demo uses explicitly labeled synthetic observations and does not start a camera, model, microphone, notification, robot, or screen-lock adapter.

## Start the local prototype

Requirements: SpacetimeDB CLI **2.14.1**, Node **24.22.1+ in the Node 24 line**, project npm dependencies. The launch script first uses `~/.local/share/locki-airlock/tools/node_modules/node/bin/node`, then the current Node; override with `AIRLOCK_NODE_BIN`.

```bash
npm ci --prefix spacetime
npm ci --prefix frontend
make airlock
```

Open the Vite URL printed by the command, normally `http://127.0.0.1:5174`. `make airlock` reuses a healthy local database or starts one with persistent files under `data/airlock-spacetime/`, publishes with `--delete-data=never`, generates the SDK, builds the UI, and starts Vite. Stopping it terminates only a database process it started; an existing server is left running. Data is retained.

Useful individual commands:

```bash
scripts/airlock_dev.sh server      # persistent DB in foreground
make airlock-build                # check, nondestructive publish, generate, UI build
scripts/airlock_dev.sh frontend   # UI only
make airlock-verify               # real DB integration scenarios
scripts/airlock_dev.sh status
```

`AIRLOCK_SPACETIME_URI` defaults to `http://127.0.0.1:3000`; `AIRLOCK_DATABASE` defaults to `locki-airlock-sid`. The build helper deliberately publishes only to a loopback host. LAN sharing needs an explicit server listen address, a reachable server URL in each console's connection settings, and `AIRLOCK_VITE_HOST=0.0.0.0`. Keep the Vite and database ports reachable only on the intended network.

The earlier local `locki-airlock` fixture database is retained. Cloud CLI login changed the publishing identity, so the offline workflow uses a fresh owned `locki-airlock-sid` database rather than deleting or attempting to seize the old one.

The user-supplied cloud module is `locki-w91bw` at `https://maincloud.spacetimedb.com`. To use its existing deployment without republishing it:

```bash
AIRLOCK_SPACETIME_URI=https://maincloud.spacetimedb.com AIRLOCK_DATABASE=locki-w91bw \
  scripts/airlock_dev.sh frontend
```

Use the same environment before `scripts/airlock_dev.sh verify` to run the independent tests against that real cloud module. Test runs create uniquely named fixture workstations and preserve existing data.

## Two distinct authorized consoles

1. Open `/?client=operator` and create a workstation. This identity becomes its owner.
2. Open `/?client=reviewer` in a second browser context or on a second computer. Copy its **public identity**.
3. In the owner console, choose **Enroll identity**, grant that identity the reviewer role, and optionally grant protected-action approval rights.
4. Arm a **synthetic session** from the owner console. Send the labeled person + unknown USB fixture. Both clients should show the same committed incident ID.
5. Acknowledge from the reviewer, resolve from the owner, reload/reconnect, and inspect the server audit order.

Profiles create separate token-storage slots, not roles. The server enrollment determines authority. A new identity has no existing workstation access. Tokens are bearer credentials; copy only public identities when enrolling another person. This local prototype uses host-issued identities, not a deployed organizational SSO/account-recovery system.

## Independent verification

`scripts/airlock_verify.ts` creates seven independent identities, unique workstation/session IDs, and uses the real generated SDK and HTTP API. It never resets an existing database. It verifies private-read isolation, control/publisher roles, USB baseline behavior, camera soft observations, 14 correlation boundary/order vectors, server-timer health expiry, replay conflicts, stale/malformed events, workstation/session separation, approval permissions/digest/expiry/one-time use, actual managed callbacks, revocation, and reconnect.

The correlation vectors wait about 21 seconds once so every observation genuinely occurs inside its armed session. The timing report measures **local USB reducer publication to each independently subscribed SDK cache** with 10 ms cache polling. It does not measure hardware detection, rendering, LAN latency, or a second physical laptop.

Each run writes `data/airlock-verification/<run>/report.json`. The sibling `checkpoint.private.json` contains a saved owner token with mode `0600`, and is intentionally under gitignored `data/`. To verify disk persistence, stop and restart the same SpacetimeDB server with the same data directory, then run:

```bash
"${AIRLOCK_NODE_BIN:-$HOME/.local/share/locki-airlock/tools/node_modules/node/bin/node}" \
  frontend/node_modules/tsx/dist/cli.mjs scripts/airlock_verify.ts \
  --check-checkpoint data/airlock-verification/RUN/checkpoint.private.json
```

The check writes a sanitized `persistence-check.json` beside the private checkpoint. The actual coordinated local server restart was verified for run `verify-1794085398044-bb08ed7e`: the same token recovered 29 committed incidents.

See [DECISIONS.md](DECISIONS.md) for rationale and the claims ledger, [TEAMMATE_INTERFACE.md](TEAMMATE_INTERFACE.md) for device publishers and managed callbacks, [VOICE.md](VOICE.md) for the optional model-independent P0 warning service, and [`spacetime/CONTRACT.md`](../../spacetime/CONTRACT.md) for exact reducer names and generated field types.
