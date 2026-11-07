# AIRLOCK NVIDIA integration — measured status

Verified on 2026-11-07 against cloud database `locki-w91bw` at `https://maincloud.spacetimedb.com`. This host is an Apple M5 with 16 GiB unified memory. It is not the originally assumed 24 GiB configuration.

## What is real

- **Native NeMo Relay 0.11.0 works on this Apple Silicon host.** The published `nemo-relay-node-darwin-arm64` native package loads. `withScope`, `toolCallExecuteAsync`, conditional-execution middleware, and the native observability exporter generated the ATOF files below. The application never constructs ATOF scope records itself.
- **Three genuine evidence callbacks ran against the authoritative cloud module:** `read_incident`, `inspect_digital_metadata`, and `recent_security_events`. An enrolled service published their actual outputs through `record_agent_step`. Their case observations were explicitly synthetic fixtures; database reads and Relay spans were real. The run is labeled `none:evidence-tool-verification`, not Nemotron inference.
- **The native conditional guardrail enforces the real approval boundary.** `airlock_relay_gateway.ts` awaits committed `consume_approval` inside Relay conditional-execution middleware before its tool callback can begin. It runs only the existing explicit managed-tool allowlist, using the server's immutable digest-bound arguments, then records the outcome. Pending, denied, expired, replayed, and unknown operations never reach a callback.
- **Nemotron is intentionally unavailable under current memory pressure.** The measured preflight saw approximately 2.7 GB available memory and 90.5% swap use. The exact requested model is `nemotron-3.5-bolt:e4b`; no replacement model or synthetic model response was used. A real failed run records that no inference occurred. No LLM span is emitted for the initial resource-preflight refusal.

## Evidence collected

| Evidence | Result |
|---|---|
| Cloud tool verification run | `nemotron-ee54736e-d88b-44ec-862f-ca9e29ff605b`, complete, four committed steps including completion |
| Its native Relay trace/scope ID | `01a1b7d1-7fa5-71f3-ad8b-e4f0fcb06737` |
| Tool ATOF | `data/airlock/nvidia-cloud-evidence/events.jsonl`: 14 events; paired agent/tool/guardrail lifecycles |
| Native protected-action test trace | `01a1b7d4-0a4c-7ef1-90ed-e0bc8f5dfd64` |
| Protected-action ATOF/report | `data/airlock/nvidia-cloud-protected-final/events.jsonl` and `report.json`: seven test groups passed, 27 native events, seven rejected guardrails, **exactly one tool start** |
| Resource-blocked model run | `nemotron-60bfd533-795b-40df-a4d0-1328f84d4413`, failed; trace `01a1b7d3-0118-76a3-9b6d-778082a11530`; no inference claim |
| Python transport safety tests | 10 passed, including no network/model load under pressure, exact model identity, output truncation, timeout, transport size, loopback-only endpoint, and no thinking export |
| Evidence-tool/schema tests | 5 passed, including cross-station isolation, untrusted instruction-like metadata as inert data, report citation validation, and no impersonated human review |

Protected-action tests use a **separate automated owner identity solely as an integration-test fixture**. This is not a claim of a live human approving an action. Tests verify native Relay denials for `read_demo_secret`, `deploy_dummy_artifact`, and `run_shell`; none of those callbacks exists in the allowlist. A harmless `fixture_word_count` callback executes once after approval. Its text includes malicious instruction-like words to demonstrate inert data handling; it does not test successful model resistance to prompt injection.

ATOF output directories are mode 0700 and files mode 0600. Native request/response sanitizers redact recorded tool/model payloads. Cloud step summaries retain bounded case metadata visible only to authorized case readers. Token files are never printed, copied into traces, or committed.

## Run it

Install the isolated native runtime; no application manifest is changed:

```sh
sh scripts/airlock_relay_install.sh
```

This pins `nemo-relay-node@0.11.0` under `~/.local/share/locki-airlock/relay-runtime`. `AIRLOCK_RELAY_MODULE` may point to another explicitly installed package directory. Node 24.13.1 or newer is required by NVIDIA. The actual installed host Node 24.14.1 meets that requirement.

Check resource readiness without loading a model:

```sh
frontend/node_modules/.bin/tsx scripts/airlock_nemotron.ts --preflight
```

Run on an incident the service is enrolled to read:

```sh
export AIRLOCK_SPACETIME_URI=https://maincloud.spacetimedb.com
export AIRLOCK_DATABASE=locki-w91bw
export AIRLOCK_SERVICE_TOKEN_FILE=/private/path/service.json
frontend/node_modules/.bin/tsx scripts/airlock_nemotron.ts \
  --workstation WORKSTATION_ID --incident INCIDENT_ID
```

Add `--evidence-only` to verify actual read callbacks and native traces without model inference. The evidence-only path is deliberately marked deterministic verification, never model-selected tool use. `--output DIRECTORY` selects a private local trace directory.

The model runner never downloads weights, never selects another model, never calls a remote model endpoint, and never changes the existing voice/runtime orchestration. It uses the existing backend Python 3.12 virtual environment and loopback Ollama `/api/chat`. It caps context at 2048 tokens, output at 400 tokens, model turns at five, executed tools at eight, response transport at 256 KB, and worker lifetime at 50 seconds. A lock file prevents concurrent investigations. An abandoned lock is left for explicit review rather than silently starting a second worker.

At least 10 GiB available memory and no more than 30% swap utilization are required before each inference request. These are conservative engineering guardrails, **not NVIDIA-certified sizing estimates**. `keep_alive: 0` requests unload after a completed call. The HTTP request times out after 45 seconds and the Python worker is killed at 50 seconds; cancelling the caller is not proof that every Ollama backend allocation has already been released. Do not bypass the guard on this pressured 16 GiB machine. A teammate can run the same local adapter on a suitable machine, using their enrolled service identity.

The model must genuinely select the three distinct evidence tools and `prepare_report` before the run may finish successfully. It must cite evidence previously returned by a tool and include uncertainties. `request_human_review` prepares an auditable recommendation only: it does not impersonate an authenticated reviewer, change incident state, approve an action, or send an external notification. Tool outputs label observation provenance, and the model cannot change deterministic warnings or lock policy.

To execute an already separately approved managed action through native Relay:

```sh
frontend/node_modules/.bin/tsx scripts/airlock_relay_gateway.ts --execute ACTION_ID
```

The service must be the original proposer. The authoritative reducer rechecks the approver's current permissions, scope-bound action digest, expiry, and one-time status. No automatic callback retry occurs after uncertain outcome reporting. This boundary protects only calls through this entry point; it is **not global OS protection**, and it does not assert an existing Code Quest process has been automatically instrumented.

## Repeat checks

```sh
backend/.venv/bin/python scripts/airlock_nemotron_test.py
frontend/node_modules/.bin/tsx --test scripts/airlock_nemotron_tools.test.ts
backend/.venv/bin/ruff check backend/app/intelligence/airlock_agent.py scripts/airlock_nemotron_test.py
```

For the cloud native gate test, provide `AIRLOCK_SERVICE_TOKEN_FILE`, `AIRLOCK_TEST_OWNER_TOKEN_FILE`, and `AIRLOCK_TEST_WORKSTATION` for a dedicated synthetic integration station, plus the deployment variables above. Run `frontend/node_modules/.bin/tsx scripts/airlock_relay_verify.ts`. The script creates and approves only its explicitly labeled test proposals. It does not modify existing grants or run any OS action.

## Official references checked

- [Ollama native tool calling](https://docs.ollama.com/capabilities/tool-calling): tool schemas, assistant tool calls, and `tool_name` results in the message loop.
- [Ollama chat endpoint](https://docs.ollama.com/api/chat): model attribution, nonstreaming responses, thinking controls, generation metrics, and keep-alive.
- [Exact Nemotron E4B tag](https://ollama.com/library/nemotron-3.5-bolt:e4b): Apple Silicon support and the E4B model distinction; the much larger 74B-A9B tag is not the requested model.
- [NVIDIA installation documentation](https://docs.nvidia.com/nemo/relay/latest/getting-started/installation): package boundaries and Node runtime requirement.
- [NVIDIA Node quick start](https://docs.nvidia.com/nemo/relay/latest/getting-started/quick-start/nodejs): native scope and ATOF exporter setup.
- [NVIDIA managed tool API](https://docs.nvidia.com/nemo/relay/latest/instrument-applications/instrument-tool-call) and [middleware semantics](https://docs.nvidia.com/nemo/relay/latest/instrument-applications/advanced-guide): conditional guards deny before execution; telemetry sanitizers do not rewrite callback arguments.
- [Published NVIDIA 0.11.0 artifacts](https://github.com/NVIDIA/NeMo-Relay/releases/tag/0.11.0): actual Darwin ARM64 CLI and Node artifacts. Documentation identified “latest” as 0.11.1 while the available GitHub release API identified 0.11.0; the implementation pins and checks the available tested 0.11.0 package. Its installed `index.d.ts` was checked for exact async call and guardrail signatures.

## Remaining acceptance gaps

Actual E4B inference, three **model-selected** evidence tools, model latency/quality, genuine LLM lifecycle spans, and model-mediated injection resistance remain unverified until a sufficiently resourced host runs the model. Native tool/guardrail integration is verified independently. This work does not claim real human approval, real camera observations, voice performance, OS-lock execution, or automatic integration into a separate Code Quest host.
