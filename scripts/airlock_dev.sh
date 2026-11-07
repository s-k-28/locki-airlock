#!/usr/bin/env bash
set -euo pipefail

AIRLOCK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$AIRLOCK_ROOT"
AIRLOCK_COMMAND="${1:-start}"
AIRLOCK_SPACETIME_BIN="${AIRLOCK_SPACETIME_BIN:-$HOME/.local/bin/spacetime}"
AIRLOCK_SPACETIME_URI="${AIRLOCK_SPACETIME_URI:-http://127.0.0.1:3000}"
AIRLOCK_DATABASE="${AIRLOCK_DATABASE:-locki-airlock-sid}"
AIRLOCK_DATA_DIR="${AIRLOCK_DATA_DIR:-$AIRLOCK_ROOT/data/airlock-spacetime}"
AIRLOCK_LISTEN_ADDR="${AIRLOCK_LISTEN_ADDR:-127.0.0.1:3000}"
AIRLOCK_NODE_BIN="${AIRLOCK_NODE_BIN:-$HOME/.local/share/locki-airlock/tools/node_modules/node/bin/node}"
AIRLOCK_STARTED_SERVER_PID=""
if [[ ! -x "$AIRLOCK_NODE_BIN" ]]; then AIRLOCK_NODE_BIN="$(command -v node || true)"; fi
if [[ -z "$AIRLOCK_NODE_BIN" ]]; then echo 'Node.js 24.22.1+ is required; set AIRLOCK_NODE_BIN.' >&2; exit 1; fi
export PATH="$(dirname "$AIRLOCK_NODE_BIN"):$PATH"
export AIRLOCK_SPACETIME_URI AIRLOCK_DATABASE AIRLOCK_NODE_BIN

help() {
  cat <<'HELP'
Usage: scripts/airlock_dev.sh [start|server|build|verify|frontend|status]
  start     Reuse/start the persistent local DB, publish without deleting data,
            regenerate client bindings, then run the dashboard (default).
  server    Run only the persistent SpacetimeDB server in the foreground.
  build     Check module, publish without deleting data, regenerate SDK, build UI.
  verify    Run independent real-database integration scenarios.
  frontend  Run Vite using the configured database.
  status    Check server health; does not create or change data.

Configuration: AIRLOCK_SPACETIME_URI, AIRLOCK_DATABASE, AIRLOCK_DATA_DIR,
AIRLOCK_LISTEN_ADDR, AIRLOCK_NODE_BIN, AIRLOCK_SPACETIME_BIN, AIRLOCK_VITE_HOST.
Defaults bind to loopback. To share on a trusted LAN, explicitly set listen
and Vite host; browser clients must use the server Mac's reachable LAN URL.
No hardware adapter, microphone, camera, model, real lock, or motor starts here.
HELP
}

preflight() {
  "$AIRLOCK_NODE_BIN" -e 'const [a,b,c]=process.versions.node.split(".").map(Number);if(a!==24||b<22||(b===22&&c<1)){console.error("AIRLOCK requires Node 24.22.1+ within the Node24 release line; set AIRLOCK_NODE_BIN");process.exit(1)}'
  if [[ ! -x "$AIRLOCK_SPACETIME_BIN" ]]; then echo "SpacetimeDB CLI missing: $AIRLOCK_SPACETIME_BIN" >&2; exit 1; fi
  if [[ ! -d frontend/node_modules/spacetimedb || ! -d spacetime/node_modules/spacetimedb ]]; then
    echo 'Install project dependencies first: npm ci --prefix frontend && npm ci --prefix spacetime' >&2
    exit 1
  fi
}

local_publish_guard() {
  "$AIRLOCK_NODE_BIN" -e 'const u=new URL(process.env.AIRLOCK_SPACETIME_URI);if(!["127.0.0.1","localhost","[::1]"].includes(u.hostname)){console.error("This convenience publisher is local-only. Use an explicitly reviewed deployment command for a remote database.");process.exit(1)}'
}

health() { curl --fail --silent --max-time 2 "${AIRLOCK_SPACETIME_URI%/}/v1/ping" >/dev/null 2>&1; }

start_server() {
  if health; then echo "Using existing SpacetimeDB at $AIRLOCK_SPACETIME_URI"; return; fi
  mkdir -p "$AIRLOCK_DATA_DIR"
  "$AIRLOCK_SPACETIME_BIN" start --listen-addr "$AIRLOCK_LISTEN_ADDR" --data-dir "$AIRLOCK_DATA_DIR" --page_pool_max_size 268435456 --non-interactive >"$AIRLOCK_DATA_DIR/server.log" 2>&1 &
  AIRLOCK_STARTED_SERVER_PID=$!
  for _ in {1..100}; do
    if health; then echo "Persistent SpacetimeDB ready (PID $AIRLOCK_STARTED_SERVER_PID)"; return; fi
    if ! kill -0 "$AIRLOCK_STARTED_SERVER_PID" 2>/dev/null; then tail -30 "$AIRLOCK_DATA_DIR/server.log" >&2; exit 1; fi
    sleep 0.2
  done
  echo "SpacetimeDB startup timed out; inspect $AIRLOCK_DATA_DIR/server.log" >&2
  exit 1
}

build_airlock() {
  local_publish_guard
  if ! health; then echo 'Start SpacetimeDB first: scripts/airlock_dev.sh server' >&2; exit 1; fi
  npm run check --prefix spacetime
  "$AIRLOCK_SPACETIME_BIN" publish --module-path "$AIRLOCK_ROOT/spacetime" --server "$AIRLOCK_SPACETIME_URI" --delete-data=never --yes=skip-login,migrate "$AIRLOCK_DATABASE"
  "$AIRLOCK_SPACETIME_BIN" generate --lang typescript --module-path "$AIRLOCK_ROOT/spacetime" --out-dir "$AIRLOCK_ROOT/frontend/src/airlock-bindings" --yes
  npm run build --prefix frontend
}

frontend() {
  export VITE_SPACETIMEDB_URI="$AIRLOCK_SPACETIME_URI"
  export VITE_SPACETIMEDB_DATABASE="$AIRLOCK_DATABASE"
  npm run dev --prefix frontend -- --host "${AIRLOCK_VITE_HOST:-127.0.0.1}"
}

cleanup() {
  if [[ -n "$AIRLOCK_STARTED_SERVER_PID" ]] && kill -0 "$AIRLOCK_STARTED_SERVER_PID" 2>/dev/null; then
    kill -TERM "$AIRLOCK_STARTED_SERVER_PID"
    wait "$AIRLOCK_STARTED_SERVER_PID" || true
  fi
}

case "$AIRLOCK_COMMAND" in
  help|--help|-h) help ;;
  status) if health; then echo "SpacetimeDB reachable: $AIRLOCK_SPACETIME_URI ($AIRLOCK_DATABASE)"; else echo "SpacetimeDB unavailable: $AIRLOCK_SPACETIME_URI" >&2; exit 1; fi ;;
  server) preflight; mkdir -p "$AIRLOCK_DATA_DIR"; exec "$AIRLOCK_SPACETIME_BIN" start --listen-addr "$AIRLOCK_LISTEN_ADDR" --data-dir "$AIRLOCK_DATA_DIR" --page_pool_max_size 268435456 --non-interactive ;;
  build|--build-only) preflight; build_airlock ;;
  verify) preflight; exec "$AIRLOCK_NODE_BIN" frontend/node_modules/tsx/dist/cli.mjs scripts/airlock_verify.ts ;;
  frontend) preflight; frontend ;;
  start) preflight; trap cleanup EXIT INT TERM; start_server; build_airlock; frontend ;;
  *) help >&2; exit 2 ;;
esac
