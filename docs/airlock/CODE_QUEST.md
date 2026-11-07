# Managed coding exercise and protected pytest execution

The PRD section 6.3 requires a denied `read_demo_secret` / `deploy_dummy_artifact`
request and an allowed `run_pytest_in_sandbox` callback, with native Relay evidence.
The original approved word-count callback did not exercise the pytest path.

This checkout contains the retained focus/robot companion, but no existing Code
Quest host or reusable sandbox-test implementation. The new callback implements
the bounded coding-exercise path in AIRLOCK's managed gateway. It does not claim
to instrument a separate upstream Code Quest process, or to demonstrate a model
resisting instructions in a README. That host integration and actual model-led
workflow remain separate acceptance work.

## Exact callback contract

`run_pytest_in_sandbox` accepts exactly:

```json
{"fixture":"positive-sum-v1","candidate":"correct"}
```

The only other candidate is `buggy`. Both implementation variants and four tests
are constants in `scripts/airlock_sandbox_worker.py`; callers cannot supply code,
paths, commands, pytest flags, environment variables or repository contents.
The corrected candidate passes four tests. The buggy candidate returns two
passing and two failing tests. A failed exercise is returned as
`test_outcome: "failed"`; a completed action means the runner completed, not that
the exercise passed.

The tool requires the exact `airlock:sandbox-tests` scope. Read-only approvals
cannot authorize it. The gateway selects the stored immutable arguments, awaits
the committed one-time SpacetimeDB approval consumption, and only then starts
the callback. The native Relay entry point performs that check inside its
conditional-execution guardrail. Unknown tools remain absent from the callback
allowlist.

The callback invokes the fixed backend virtual-environment Python executable
without a shell. A private temporary directory contains only the generated
exercise, tests, and a clearly labeled malicious README fixture. The README
names forbidden tools but is never interpreted as instructions. Pytest uses
isolated Python mode, disabled external plugin autoload, no conftest discovery,
no inherited credentials or pytest options, a ten-second deadline, and bounded
CPU/file/descriptor resources. Linux also enforces a 512 MiB address-space cap.
The temporary directory is removed before success is reported. Results contain
test counts and a fixture digest, not raw output or local paths.

**This is a trusted-fixture runner, not an OS sandbox for untrusted code.** macOS
does not supply the Linux address-space limit, and no arbitrary repository or
model-generated program is accepted. The runner does not load a model, read host
secrets, access hardware, send notifications, or deploy artifacts.

## Run an approved exercise

Install the regular backend development environment and pinned native Relay
runtime first. Propose as an enrolled service without auto-executing:

```sh
frontend/node_modules/.bin/tsx scripts/airlock_gateway.ts \
  --workstation WORKSTATION_ID --tool run_pytest_in_sandbox \
  --args '{"fixture":"positive-sum-v1","candidate":"correct"}' \
  --mission code-quest-exercise --wait-seconds 0
```

Review the exact arguments and scope in a separate authorized owner/reviewer
console. After approval, execute as the original proposing service through
native Relay:

```sh
frontend/node_modules/.bin/tsx scripts/airlock_relay_gateway.ts --execute ACTION_ID
```

Both commands use `AIRLOCK_SERVICE_TOKEN_FILE`, `AIRLOCK_SPACETIME_URI`, and
`AIRLOCK_DATABASE`. Keep credentials outside source control. An uncertain result
must be reviewed; never replay an external action after reporting failure.

## Verification

Verified against cloud module `locki-w91bw` on 2026-11-07:

- Four local adapter tests passed, including actual pytest runs of both variants
  (correct: four passing; buggy: two passing and two failing).
- Eight native Relay/database integration checks passed. The exporter produced
  27 genuine ATOF events, with exactly one tool start for
  `run_pytest_in_sandbox`, paired agent/tool/guardrail scopes, and no LLM spans.
- The approved cloud action was
  `sandbox-test-91a47c5a-fa65-4baf-9554-ccdb0d47b745`; its callback returned four
  passing tests and confirmed temporary-workspace cleanup. A second dispatch
  using the consumed approval was denied.
- Trace: `01a1b81f-8545-70a1-af97-d87cad0c4939`. Private proof:
  `data/airlock/sandbox-verify-1794090435872/report.json` and `events.jsonl`.
- Type checking of the callback and both gateway paths, and Python lint passed.

These measurements demonstrate actual fixed pytest execution after a committed
approval and native pre-execution denials. They do not demonstrate a separate
Code Quest host, live human approval, model inference, or model-mediated prompt
injection resistance.

```sh
frontend/node_modules/.bin/tsx --test scripts/airlock_sandbox.test.ts
```

These tests execute the actual small trusted pytest exercise for both candidates,
plus argument/scope denials. They do not claim live human approval.

For native Relay plus the real database gate, configure an existing dedicated
synthetic test workstation with distinct service and automated test owner:

```sh
export AIRLOCK_SERVICE_TOKEN_FILE=/private/path/service.json
export AIRLOCK_TEST_OWNER_TOKEN_FILE=/private/path/test-owner.json
export AIRLOCK_TEST_WORKSTATION=DEDICATED_TEST_WORKSTATION
frontend/node_modules/.bin/tsx scripts/airlock_sandbox_verify.ts
```

The integration verifies denied unknown tools, pending approval, wrong scope,
extra code arguments, one actual approved pytest execution, and denied replay.
It asserts exactly one native tool-start record named `run_pytest_in_sandbox`
and no model spans. Private reports and native ATOF are written under ignored
`data/airlock/`. Automated test-owner approval is explicitly labeled as a fixture.
