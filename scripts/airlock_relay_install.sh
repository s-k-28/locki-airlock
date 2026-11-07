#!/bin/sh
# Isolated, exact-version native runtime. Does not modify app package manifests.
set -eu
runtime_dir="${AIRLOCK_RELAY_RUNTIME_DIR:-$HOME/.local/share/locki-airlock/relay-runtime}"
npm install --prefix "$runtime_dir" --ignore-scripts --no-audit --no-fund --save-exact nemo-relay-node@0.11.0
printf 'Installed native Relay 0.11.0 at %s\n' "$runtime_dir"
printf 'For a custom location set AIRLOCK_RELAY_MODULE to its node_modules/nemo-relay-node directory.\n'
