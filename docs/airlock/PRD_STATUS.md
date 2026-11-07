# AIRLOCK PRD implementation and acceptance status

This is a bounded evidence snapshot for the supplied `LOCKI_AIRLOCK_PRD.md`, sections 0–16, reconciled on 2026-11-07 after functional regression verification. The latest source release and commit-specific CI are recorded separately below. Machine-readable sanitized evidence is [software-verification.json](evidence/software-verification.json), whose historical runs retain their own source and timing boundaries. Full PRD acceptance remains incomplete. The source PRD remains unchanged. **Implemented** means code exists; **verified locally** means a described real execution occurred; **pending** means the acceptance evidence has not been collected. A synthetic observation through a real database does not become a physical observation.

## P0 requirements, individually

| PRD §2 P0 | Code and current evidence | Remaining acceptance work |
|---|---|---|
| 1. Explicit authenticated arm/disarm; captured baseline/app/camera/policy; panic/locked | Owner-only live arm requires a fresh enrolled-Mac capture and freezes inventory, active app, server-derived camera/health state, and policy. Actual cloud capture→live arm→frozen baseline→heartbeat→owner disarm passed with zero enumerated USB peripherals and camera unavailable. Chrome live arm also passed against the actual fresh Mac capture at 21:47 UTC, then was disarmed with automatic lock disabled. Owner-only manual panic is implemented and tested; the integration owner verified its truthful high-severity manual-intent UI flow on cloud at 21:43–21:44 UTC. | Genuine peripheral insertion/verification and an explicitly consented actual-Mac lock/unlock/confirmation trial remain pending. Manual panic is an owner request, not inferred hostile activity. |
| 2. Independent Logitech/Jetson physical evidence | Enrolled Jetson contract, authenticated publisher, persistent bounded queue, ROI/debounce, health API and CPU OpenCV HOG fallback implemented; 22 stdlib tests pass without a camera. Motion-only mode explicitly degrades person coverage and never emits person evidence. | No physical Jetson/Logitech run occurred. Identify hardware/JetPack/power, run ten-minute capture/unplug recovery, collect 20–40 consented labeled trials, and measure latency/FPS/CPU/GPU/RAM. No detector accuracy or GPU claim. |
| 3. Real Mac digital evidence | Read-only IORegistry polling fallback and native Swift `NSWorkspace.didLaunchApplicationNotification` observer implemented. Actual Calculator launch and real adapter publication were reverified in `observer-1794090942299-ef5abc`: an external CUA launch produced the expected native event/review incident among nine genuine committed events. The observer script itself did not request the launch; no auto-lock occurred. Snapshot metadata includes bundle ID, executable path and signature validation where available; shared metadata stays bounded/redacted. | Record a genuine harmless USB insertion/removal and its actual detection timing. App notifications do not cover all background agents. |
| 4. Deterministic correlation | Real database tests cover standalone unknown USB, approved baseline, soft physical presence, app allowlist, 14 inclusive 10-second/order boundary vectors, cross-workstation/session isolation, time/sequence validation and replay. | Real physical+digital full-system trial and an evidence-grounded interpretation of app activity. Ten seconds is the specified rule, not a learned threshold. |
| 5. Immediate model-independent audible/dashboard response; consented lock | Actual committed synthetic case reached the authenticated loopback bridge and existing P0 Speaker; one cached macOS rehearsal warning finished playback with the model disabled. Consent-gated Control-Command-Q adapter is implemented; cloud and local child-process dry-run tests prove synthetic requests cause zero helper calls, reports or reservations. | Measure actual hardware-event→audible-warning latency. Explicitly consented own-Mac execution/unlock trial and independent lock-state confirmation remain pending; current adapter truthfully supports executed but not confirmed. No real lock executed by verification scripts. |
| 6. SpacetimeDB authoritative realtime backend | Historical full deployed runs passed 49/49 groups on each endpoint. The latest focused Mac-observation/health authorization verifier passed 9/9 on local and cloud after the health-view fix. Reducers own enrollment, fresh captures, armed sessions, correlation, reviews, approvals, durable schedules/retention and audit. Private tables/caller-scoped views are enforced by the server. Separate cloud Chrome owner/reviewer controls and manual-panic flow were observed. | The functional implementation snapshot `1c394ec` has passing public CI; see the release evidence below. The private-origin CI reports `startup_failure`. Two physical-computer/network measurements remain pending. |
| 7. Operational local Nemotron E4B with genuine approved tools | Local adapter, bounded tool schemas, resource guard, factual run/step reporting and three genuine evidence callbacks implemented. Native Relay exported the real evidence-tool lifecycles; the run is labeled none:evidence-tool-verification. Current host is Apple M5/16 GiB; memory/swap preflight blocked actual E4B inference. The exact MLX E4B download completed at 22:03 UTC and its manifest/runtime were verified at 22:24 UTC; inference remains resource-blocked. | Actual nemotron-3.5-bolt:e4b inference, three model-selected tools, grounded report, LLM spans and model-quality/latency remain unverified. Download completion is not readiness or inference proof; no replacement or fabricated output is used. |
| 8. Native NeMo Relay spans and managed Code Quest boundary | Native NeMo Relay 0.11.0 is operational. Seven cloud protected-action groups passed: native export contains 27 events, seven rejected guardrails and exactly one allowed tool start after committed consumption. Separate cloud evidence reads produced 14 paired native agent/tool/guardrail events. Automated separate owner identity is explicitly a test fixture, not live human approval. | Automatic instrumentation of a separate existing Code Quest host, its exact allowed sandbox-test path, genuine model spans and model-mediated README-injection resistance remain pending. The verified native boundary protects only the new managed runner. |
| 9. Preserve and integrate existing live voice | Existing speech pipeline retained. The restricted local voice service and committed-incident bridge use existing Speaker priorities. The targeted backend run at 22:39 UTC passed 49 tests: 21 announcement/service, 23 explicit voice-input and 5 Mac-observer tests. The latest bounded Node run passed 87 tests, including the existing voice mappings. Prerecorded CPU-only Whisper ASR also passed on one official 11-second human-speech sample; no microphone was opened. These checks cover P0 interruption, auth/origin, durable dedup, mute and factual owner-panic templates. One actual cached P0 rehearsal playback completed; stop_talking calls no security reducer. | Full real microphone→VAD/ASR→Nemotron/fallback→audible-output conversation remains pending. Recorded proof used no live microphone/model; no additional audio was played for manual panic. Hardware-event-to-acoustic timing and live untrusted-voice trial remain separate. |
| 10. Recovery across two clients and reconnect | Independent authenticated SDK clients share incident IDs/review changes; revoked readers lose rows and saved-token reconnect restores state. Actual local SIGTERM/restart restored 29 incidents. Separate cloud Chrome owner/reviewer contexts completed synthetic arm→correlation→reviewer acknowledgement→owner resolve→disarm. Final cloud owner panic→factual note→resolve→disarm also succeeded without OS/audio/hardware action. | Two physical computers, real network-outage trials and genuine hardware recovery/unlock proof remain pending. |

## PRD §13 named acceptance evidence

| ID | Current status | Evidence needed to close |
|---|---|---|
| HW-01 | Pending hardware | On-device Jetson model, power interface, JetPack, RAM, webcam model. |
| HW-02 | Pending hardware | Ten-minute live camera stream, measured device metrics and unplug recovery. |
| PHY-01 | Contract/fixtures verified; physical trial pending | Actual person crosses marked zone, true capture time, benign walk-by labels/counts. |
| MAC-01 | Actual fresh cloud Mac capture/freeze passed with an empty USB inventory; approved-USB classification passed with explicit fixtures through real reducers | A genuine approved physical peripheral baseline observation during an armed live session remains untested. |
| MAC-02 | Real DB rule/publisher verified; actual insertion pending | Harmless physical USB insertion and receipt in enrolled live session; no USB file access/execution. |
| MAC-03 | Verified actual native launch locally | `observer-1794090942299-ef5abc` captured an actual externally launched Calculator event through NSWorkspace; nine genuine events committed and the expected review incident appeared. No physical USB insertion or OS lock occurred. |
| MAC-04 | Request/result policy and consent-gated adapter implemented; final default-dry-run child-process test passed on local and cloud with zero helper invocations | Explicit consent, actual own-Mac lock, truthful executed/confirmed distinction, normal unlock/disarm. |
| VOX-01 | Partial: actual cached P0 rehearsal and one official prerecorded CPU-ASR sample completed; 44 voice-specific backend tests and two Node mappings pass | Real microphone→ASR→model/fallback→speaker and acoustic interruption timing remain pending; recorded proof used no microphone/model. |
| NV-01 | Resource-blocked; no actual inference | Requested E4B tag, bounded adapter and resource guard exist. Actual local target-host response/runtime measurements remain pending; installation is verified, while inference remains blocked. |
| NV-02 | Three real cloud evidence callbacks and native spans verified without a model | Nemotron itself must select three or more genuine tools and ground its report. Deterministic evidence-only verification does not satisfy that requirement. |
| NV-03 | Native tool/guardrail tracing verified: 0.11.0, seven protected groups, trace 01a1b7d4-0a4c-7ef1-90ed-e0bc8f5dfd64 | Genuine LLM spans and correlation to actual Nemotron inference remain pending; application-authored JSON is not used as native trace proof. |
| NV-04 | Verified on the new native Relay managed boundary: forbidden/pending/denied/expired/replayed callbacks blocked; one approved harmless callback ran | Existing upstream Code Quest host integration and its exact sandbox test callback remain absent. Automated test approval is not live human approval. |
| ST-01 | Verified in stated local/cloud runs | Historical full runs passed 49/49 groups each, including fresh capture/arm authorization, private reads and atomic approvals. The latest narrow health/observation change separately passed 9/9 groups on both deployments; this is not a rerun of the full 49-group suite. The manual-panic Chrome flow also committed through the cloud server. |
| ST-02 | Verified independent SDK identities/connections and separate cloud browser tabs | Integration owner observed owner/reviewer Chrome contexts synchronize the same synthetic case and enforce reviewer role controls. Two physical computers and rendered timing remain a separate trial. |
| ST-03 | Verified on real local and cloud modules | Unauthorized acknowledgement and approval are denied by reducers, not just disabled controls. |
| ST-04 | Verified on real local and cloud modules | Expired/reused/modified/substituted grants, revoked/downgraded approvers, wrong service and racing consumption fail closed. |
| ST-05 | Client reconnect and actual local server restart verified | After coordinated SIGTERM and restart using the same persistent directory, the saved token restored 29 incidents: `data/airlock-verification/verify-1794085398044-bb08ed7e/persistence-check.json`. Physical-network outage trial remains separate. |
| SEC-01 | Forbidden callback and instruction-like tool-data checks verified on the new native Relay runner | Actual model/Code Quest README-injection trial remains pending. No real secret or arbitrary shell callback was accessed. |
| SEC-02 | Camera-offline event plus autonomous stale-health expiry verified with fixture publisher | Physically disconnect real camera and confirm UI plus detector stops reporting fresh visual evidence. |
| SEC-03 | Deterministic DB rule verified and actual cached rehearsal warning completed with model disabled | Repeat from genuine hardware evidence and demonstrate the separately consented endpoint response. |
| SEC-04 | Bounded redacted metadata contract plus default 24h live-database retention implemented. Real datastore tests verify ≤200 deletions/tick, reference-safe cleanup, preservation of unresolved/nonterminal cases and full grace after late action results. | Inspect genuine detector transport and apply consent/retention to optional external evidence and backups. Unresolved cases deliberately retain evidence beyond 24h; elapsed 24h cloud retention has not been observed in wall-clock time. |
| SEC-05 | Owner role required to disarm; voice does not directly grant owner credentials | Live untrusted voice trial proves no authenticated disarm occurs. |

## Other PRD sections and binding constraints

| PRD section | Disposition |
|---|---|
| §0 competition alignment | Core SpacetimeDB implementation is real and a source-only snapshot is public. Sponsor eligibility, physical hardware, Nemotron and native Relay claims require their corresponding evidence; not blanket-certified here. |
| §1 problem/scope and §3 trust boundaries | Unknown USB/person/app are observations, not guilt. Device roles cannot approve themselves or directly control a Mac. Local prototype host tokens need production identity recovery/credential management before broader use. |
| §4 architecture / §5 schema and authorization | Sensors/model are adapters. Module owns persistent decisions and audit. Caller-scoped private-state access and one-time consumption tested independently. A consumed authorization cannot make an external callback exactly-once across crashes. |
| §6 NVIDIA roles | Native Relay tool/guardrail paths are verified independently. Actual Jetson capture, E4B inference/model-selected investigation, LLM spans and preserved live-ASR conversation remain separate incomplete deliverables; none replaced by a logo or fabricated trace. |
| §7 endpoint behavior | USB polling is labeled; native app notifications are scoped to visible apps. No malware execution, USB driver prevention, arbitrary-process quarantine, root shell or automatic police contact. Actual lock requires opt-in and its own test. |
| §8 voice / §9 UI | Live control room, factual manual-panic flow, role-aware review and cached rehearsal voice path verified in their stated scopes. Reviewed-arm consent, revoked-health visibility and functional control-state fixes have bounded software tests. The visual redesign is deferred at the user's request; its draft is preserved only in ignored local data. Live microphone/ASR conversation and model/hardware/acoustic measurements remain pending; the separate prerecorded ASR engine proof passed; connection/sensor/model states stay distinct and fixtures are labeled. |
| §10 ownership | Sid owns module/dashboard/contracts, Dhruv owns endpoint/voice/model/Relay integration, Areeb owns Jetson/hardware. These scripts expose stable contracts rather than editing shared runtime orchestration. |
| §11 migration | Valuable voice/runtime/firmware source retained. No motor/flash/destructive cleanup is part of these proofs. Public source commit `623d250` retains attribution/license text and has passing commit-specific CI. Newer functional work is pending source release/CI. Private Git history and local runtime data remain excluded. |
| §12 schedule | Scope remains the full P0; blockers are explicit hardware/external/runtime proof gaps, not silent requirement deletion. |
| §14 demo | Synthetic fallback can show real DB authority with honest provenance; it does not substitute for required physical/model footage or sponsor evidence. |
| §15 gates | No large model download, microphone/camera enablement, actual screen lock, real external notification, firmware action or public push is authorized by a fixture/verifier command. Parent/user must apply existing explicit approvals for those actions. |
| §16 references | Official documentation and implementation rationale linked in `DECISIONS.md`; source PRD remains authoritative for product scope. |

P1 remains optional: second Zeus camera, electrically reviewed Grove controls/LCD, Jetson local playback, opt-in external notification, and signed sensor attestations. All wheel/motor/flash operations, facial recognition, malware demonstration, global quarantine, shell kill switches, automatic emergency calls and Apple Lockdown Mode toggles remain outside scope.

## Measurement rules

Record run ID, real versus simulated provenance, sample count, source/receipt/rendered timing boundaries, p50/p95/max, and failures. A passing algorithm invariant suite does not establish 90% camera accuracy, <=1/20 physical false alerts, or the 1.5/2/3-second hardware/audio/LAN goals. Those targets require the specific live trial data.

The integration owner also ran the existing backend regression suite after installing its vision extra and verifying its two small existing vision weights: **495 passed, 8 skipped**. That regression preserves the legacy software baseline; it is separate from the 49 AIRLOCK deployed-module groups and does not certify untested hardware/audio/model paths.

## Latest bounded proof details

At this reconciliation, completed local checks pass **28 policy tests and 60 real datastore tests**, plus **22 frontend tests and 87 Node adapter/boundary tests**. These counts precede the publisher-stream/stale-replay work now underway. The earlier 47-datastore and 74-Node results remain historical evidence, not current totals. Baseline integration inputs use explicit `fixture:` identifiers on freshly test-owned workstations. They exercise live-arm authorization and freezing without publishing live device events, operating sensors or requesting an OS lock. This is contract proof, separate from the genuine empty-inventory capture proof in [MAC_CAPTURE.md](MAC_CAPTURE.md); neither establishes a physical USB insertion trial.

The final default-dry-run lock adapter integration passed against both `locki-airlock-sid` (local) and `locki-w91bw` (cloud): real SDK subscriptions, fresh authorized HTTP views and a child process inspected a simulated request; **zero OS-helper invocations, actual locks, execution reports or state reservations** occurred.

Voice proof: `data/airlock-voice/proof/report.json`, one completed cached macOS rehearsal clip from a real committed synthetic incident. Recorded boundaries were P0 queue **0.9ms**, driver start **76.3ms**, local HTTP delivery **85.68ms**, and playback duration **6854.8ms**, **n=1**. These are software timing boundaries, not physical-event or acoustic latency. Microphone and model were disabled; safe default audio-disabled service state was restored afterward, and restart duplicate suppression passed.

Historical full post-review deployed verifier reports: **49/49 groups passed on both endpoints**. Local: `data/airlock-verification/verify-1794087496136-3626399e/report.json`, finished 21:38:43.576 UTC; cloud: `data/airlock-verification/verify-1794087569641-a44fb0fc/report.json`, finished 21:40:40.703 UTC. For 15 USB-publication→subscribed-cache samples, local owner p50/p95/max was **25.09/40.69/40.69ms**, local reviewer **25.11/40.71/40.71ms**, cloud owner **118.24/568.59/568.59ms**, and cloud reviewer **152.66/636.55/636.55ms**. Seven primary independent identities plus an additional service-isolation identity ran on one machine with 10ms cache polling. These are neither two-laptop, browser-rendering, hardware-event nor acoustic timing measurements; at n=15 nearest-rank p95 is the maximum. Later focused authorization/health reports are listed below with their narrower scope.

Two earlier cloud verifier attempts exposed test synchronization assumptions: one asserted the owner cache after waiting only for reviewer correlation; another required a particular expiry error despite autonomous scheduled expiry. The verifier now waits for both independent caches and proves exact committed grant expiry/unconsumed state before accepting either valid denial path. Their failed reports remain preserved; no module correctness claim rests on discarding them.

## Software verification counts and scope

| Completed check | Result | Evidence basis |
|---|---:|---|
| SpacetimeDB policy primitives | 28 passed | `data/airlock-module-tests.log` |
| Real in-memory transactional module/datastore tests | 60 passed | Completed local run after four revoked-health regressions; `data/airlock-module-tests.log` records the earlier historical 47-test run, not this total |
| Frontend consent and health-state tests | 22 passed | `data/airlock-functional-frontend-tests.log`; production build: `data/airlock-functional-build.log` |
| Node boundary/unit tests | 87 passed | `data/airlock-adapters-dashboard-fix.log`; the 74-test `data/airlock-node-resumed.log` is historical |
| Resumed targeted backend tests | 49 passed | Main run at 22:39 UTC: 21 announcement/service, 23 voice-input, 5 Mac USB; three `test_airlock_*.py` files |
| Model transport/resource guard | 10 passed | Integration-owner completed run and [NVIDIA.md](NVIDIA.md) |
| Edge adapter without camera | 22 passed | `data/airlock-edge-tests.log`; fake classifier/std-lib inputs |
| Full deployed local/cloud integration | Historical 49/49 each | Source/time-bounded full reports above |
| Focused deployed Mac observation/health authorization | 9/9 each | `data/airlock-health-views-local.log`, `data/airlock-health-views-cloud.log`; report paths below |
| Native Relay protected integration | 7/7 groups | Actual native report/export; 27 events, 7 rejected guardrails, exactly 1 tool start |
| Preserved backend regression | 495 passed, 8 skipped | Earlier historical run, not a new combined final run |

Do not sum these tiers or repeated runs into one acceptance score. They overlap, test different boundaries, and do not measure camera accuracy or sponsor completion. The public JSON selects aggregate facts and relative artifact references with SHA256; it copies no credentials, native payloads, private checkpoint contents, or absolute private paths.

Actual Mac capture evidence: cloud publication, fresh live arm and frozen empty USB baseline matched; native active-app capture succeeded, camera was unavailable, one genuine monitoring event committed, auto-lock stayed off, and the owner disarmed. The latest actual Calculator proof used an external CUA launch, native NSWorkspace notifications and nine genuine committed app/heartbeat events; its report is `data/airlock-observer-proof/observer-1794090942299-ef5abc/report.json`. No physical USB insertion was performed.

Native Relay evidence: version 0.11.0 produced the exported agent/tool/guardrail lifecycles itself. Three real read callbacks operated on explicitly synthetic case observations. Seven protected-action groups used an automated separate owner fixture and are not evidence of live human approval. No actual E4B inference or LLM span occurred; resource pressure blocked the model path. The exact `nemotron-3.5-bolt:e4b` MLX artifact finished downloading at 22:03 UTC; its installed digest, 7,199,272,376-byte size and Ollama 0.40.15 runtime were verified at 22:24 UTC. E4B remains unloaded/resource-blocked; installation is not inference proof.

The latest no-load preflight, `data/airlock-functional-preflight.json`, still reports `ready:false`: 3,442,769,920 bytes available on the 16 GiB host and 91.9% swap usage, against the guard's 10 GiB minimum available memory and 30% maximum swap. The guard was not bypassed and no model inference occurred.

Final safe UI observation at 21:43–21:44 UTC: the integration owner armed a synthetic session with actual lock suppressed, clicked Panic, observed the server-committed high `owner_panic` with manual-intent wording and no hostile-activity inference, added a factual review note, resolved and disarmed. No OS, audio or hardware action occurred.

Still incomplete: real Jetson/Logitech capture and human trials, physical USB insertion/removal, actual consented OS lock/unlock/confirmation, full microphone/ASR/model conversation, E4B model-selected multi-tool work and LLM spans, automatic existing Code Quest integration, two physical Macs, hardware/acoustic timing, elapsed 24h retention observation . Scope is retained; those requirements are not silently removed or claimed satisfied.

## Reviewed source release

The source-only competition snapshot is public at [s-k-28/locki-airlock](https://github.com/s-k-28/locki-airlock). The verified implementation is private commit `8cd9b85704754d43598218c6e1f77a5e4fa6e3a8` and byte-identical public commit [`1c394ec7f3addc3a590f08529eed0f4816cdc64e`](https://github.com/s-k-28/locki-airlock/commit/1c394ec7f3addc3a590f08529eed0f4816cdc64e), with 356 tracked files. This status stamp is a subsequent documentation-only update. Private history, credentials, runtime data, recordings, installed dependencies and model weights are excluded. `data/airlock-functional-gitleaks.log` and its JSON report record zero findings for that export. Firmware GPL text and team/upstream attribution remain intact.

[Public software CI run 42426024005](https://github.com/s-k-28/locki-airlock/actions/runs/42426024005) passed for `1c394ec`: 28 policy, 72 datastore, 22 frontend, 99 Node, 49 targeted Python, 10 resource-guard and 22 camera-free edge tests, plus type checks, production build and dependency audits with zero reported vulnerabilities. Raw log and job evidence are `data/airlock-functional-ci.log` and `data/airlock-functional-ci-result.json`. This run starts no camera, microphone, inference or OS lock.

Private Actions for `8cd9b85` still reports **startup_failure** with zero jobs, zero check-runs and no logs. The API exposes a `BuildFailed` placeholder but no concrete cause; billing, YAML and permissions are not inferred. The changes remain in [draft PR #1](https://github.com/Dhruvjain35/locked-in/pull/1), unmerged. Earlier passing public runs `42423149295` (`623d250`) and `42417726847` (`2edba898`) remain historical evidence. Full physical/model acceptance is incomplete.

## Resumed acceptance work

The required `run_pytest_in_sandbox` callback is now implemented with the exact `airlock:sandbox-tests` scope. It executes only a built-in trusted exercise in a disposable directory, with fixed arguments, scrubbed environment, plugin/conftest suppression and resource/time limits. It accepts no arbitrary repository code or shell command; it is not an OS isolation boundary for untrusted code.

Eight actual cloud/native Relay checks passed in `data/airlock/sandbox-verify-1794090435872/report.json`. The native trace contains exactly one allowed tool start, and its actual pytest child passed four tests. Pending, wrong-scope, forbidden, malformed and replayed requests did not reach that callback. Approval was an explicitly automated separate-owner test fixture. Existing upstream Code Quest source and model-mediated README testing remain unavailable. See [CODE_QUEST.md](CODE_QUEST.md).

The historical resumed Node regression passed **74 tests** (`data/airlock-node-resumed.log`); the subsequent bounded run passed **87** (`data/airlock-adapters-dashboard-fix.log`). The main targeted Python run at **22:39 UTC passed 49 tests** across the three `test_airlock_*.py` files: 44 voice/announcement/input and 5 Mac-observer tests. The functional dashboard production build and 22 frontend tests pass, retaining the keyboard/pointer hold-to-talk control. The visual redesign was deferred at the user's request and its draft preserved in ignored `data/airlock-ui-draft-deferred/`; it is not claimed shipped. The real Mac observer proof was repaired to commit a fresh enrolled-Mac capture before live arming; the earlier resumed run `observer-1794090339867-212343` passed four monitoring heartbeats. The subsequent cloud run `observer-1794090942299-ef5abc` verified an actual external CUA Calculator launch and nine committed events. Neither trial included physical USB insertion, camera, microphone or OS-lock activation. Native app-stream consumer durability is now tested separately; its producer-to-spool crash window and physical outage acceptance remain open.

Read-only hardware discovery found no attached USB peripheral or external camera, no configured Jetson SSH host, and no advertised SSH/workstation service during bounded discovery. Only the built-in Mac camera was enumerated; it was not opened. Device discovery is not evidence that a disconnected or isolated Jetson is powered off. Physical acceptance still needs the actual device connection and a person to perform labeled trials.


Actual prerecorded ASR proof: `data/airlock-asr-proof/report.json` records pywhispercpp 1.5.1, the verified pinned `base.en` weight, explicit `use_gpu:false`, one inference thread and one processor. The official JFK WAV is pinned to whisper.cpp commit `6a8addd258eadd6563f8c582e8acc535e6325364` and SHA-256 `59dfb9a4acb36fe2a2affc14bacbee2920ff435cb13cc314a08c13f66ba7860e`. The **11.0-second** recording transcribed in **616.06ms** after **7139.32ms** model loading, with **311.98MiB** peak worker RSS. Its 22 normalized reference words had zero edit errors, **n=1**; this is not a dataset accuracy estimate. The bounded worker exited successfully without microphone, camera, playback or automatic model download. Live VOX-01 acceptance remains incomplete.

The actual local voice service was safely restarted at **22:40:35 UTC**. `data/airlock-voice/service-resumed-proof.json` confirms microphone off, audio disabled, no conversation model loaded and no robot/control routes. An authorized start was refused with microphone permission disabled; an unauthorized start returned HTTP 401. No capture or playback occurred during this proof.

**Full PRD acceptance is still incomplete.** These resumed results add software, real app-observer and prerecorded-human-ASR evidence; they do not close physical Jetson/USB trials, live microphone→ASR→Nemotron/fallback→speaker conversation, actual consented lock/unlock/confirmation, E4B model-selected investigation, existing upstream Code Quest integration or two-physical-computer/acoustic timing. No P0 requirement was silently removed.


## Mac incident subscription correction

Enabled enrolled Mac devices now receive their own workstation's `my_incidents`
and `my_device_events`, including correlated Jetson observations. This fixes the
server-side gap reported by Dhruv for incident-to-voice delivery. Lock requests
remain restricted to their target Mac identity. Review/approval/agent/audit
permissions are unchanged; disabled, revoked or reassigned devices lose reads.

The initial correction passed **56 datastore tests**, **28 policy tests** and
**8/8 focused groups on each endpoint**. Those are historical results for the
Mac-read change. The subsequent health-view fix passed **60 datastore tests**
and the same **28 policy tests**. It hides rows from disabled/revoked publishers
without deleting storage or hiding enabled peers, and preserves the exact
server-owned `database`/`capacity` health row.

The expanded verifier passed **9/9 locally and 9/9 on cloud `locki-w91bw`**,
using independent identities, real SDK subscriptions and authenticated HTTP
views. Local evidence: `data/airlock-health-views-local.log` and
`data/airlock-mac-views-proof/mac-views-1794093445195-94f8a104/report.json`
(finished 23:17:25.786 UTC). Cloud evidence:
`data/airlock-health-views-cloud.log` and
`data/airlock-mac-views-proof/mac-views-1794093912882-b01499fd/report.json`
(finished 23:25:17.774 UTC). Both reported two fresh Mac health rows before
revocation, zero revoked rows visible afterward, and one preserved active row.
Both test stations were disarmed after each run with zero cleanup failures.
All observations and lock intents were explicitly synthetic, with zero OS
adapter invocations or execution claims. These focused groups do not claim a
new full-system 49-group run. Health deployment logs are
`data/airlock-health-publish-local.log` and
`data/airlock-health-publish-cloud.log`; the fix changes no schema or stored data.

An initial cloud verification attempt correctly rejected a fixture whose client
timestamp preceded server arming. That failed evidence is retained. The test
now uses the committed session timestamp for synthetic inputs; real event-time
validation is unchanged. See [CONTRACTS.md](CONTRACTS.md) for exact metadata and
health enums. Dhruv's unpushed `4277d99` was not available here and is not claimed
merged or end-to-end voice-tested. His reported 262ms local warning remains a
separate measurement. Full physical/model PRD acceptance remains incomplete.

Two independent verification agents repeated the earlier Mac-read checks: one passed type
checking plus all 28 policy and 56 datastore tests and audited the narrow view
change; the other passed all eight groups against the deployed cloud module
again (`mac-views-1794092247232-68ae23b8`). Neither found a blocking issue.

## Reviewed functional fixes

Reviewed live-arm consent is bound to the displayed canonical USB set, Mac
device/publisher identity, workstation ownership, policy, approved apps and
duration. Changed reviewed contents invalidate the prior checkbox; a periodic
fresh capture with identical contents does not silently change the approved
inventory. The submit guard still checks capture freshness. This has frontend
unit evidence, not a new physical-baseline trial.

The strict publisher now rejects unknown envelope fields, invalid counters and
out-of-contract metadata before sending reducer arguments, while preserving the
contract's optional-confidence default. The 87-test Node run includes these
checks. Existing backend authorization remains authoritative.

Native app-stream consumer durability and stale-replay health handling are
implemented and independently reviewed; their final evidence follows below.
Earlier source-specific CI results do not certify later commits. The visual redesign
remains deferred; functional health, consent and review-control corrections
are retained separately. All physical/model blockers listed above remain open,
and **full PRD acceptance remains incomplete**.


## Capture-time health and full deployed regression

The next functional module uses original observation time for ingestion-derived
health, capped at server receipt time. Accepted evidence older than 15 seconds
cannot revive online coverage; older camera transitions cannot overwrite a
newer health report. Explicit current-health reporting retains server receipt
time. Twelve new datastore cases cover seven publisher/component paths,
15-second boundaries, future clock skew, ordering, current-report precedence
and lost-acknowledgement replay. **72 datastore and 28 policy tests passed**;
receipt: `data/airlock-health-replay-proof/20261107T232948Z/validation-receipt.json`.

The changed module was published locally and to cloud with no schema changes
and `--delete-data=never`. The complete deployed regression passed **49/49 on
each endpoint**: local `verify-1794094199558-928f9da9` and cloud
`verify-1794094312967-822d7250`, under `data/airlock-verification/`. Local cleanup
explicitly disarmed all 30 run-owned stations; the cloud report records 30
disarms with no failures. No OS adapter was invoked.

The earlier cloud run `verify-1794094195373-b32242bc` failed after 26 passing
groups because a default synthetic timestamp preceded committed server arming.
Its report remains preserved. Fixture defaults now use at least committed arm
time; explicit stale/future and correlation-boundary inputs are unchanged.
That failed run exited before the old verifier saved its private identity, so
explicit disarm could not be performed; its synthetic sessions retain their
scheduled ten-minute expiry. The verifier now saves recovery credentials in a
private file before tests and disarms only its own run's stations in `finally`.
The successful rerun validates these changes without weakening server policy.

Cloud publication-to-subscribed-cache measurements on the successful rerun,
**n=15**, were owner p50 **104.60 ms**, p95/max **151.42 ms**, reviewer p50
**104.62 ms**, p95/max **187.60 ms**. These are one-host SDK measurements, not
hardware, browser paint, two-laptop or acoustic acceptance evidence.


## Durable consumer replay and final focused checks

The native app publication consumer now persists a bounded private FIFO before
network delivery and replays unchanged events after restart. Its 12 regression
cases passed, including an actual child-process SIGKILL and restart against a
loopback test server after a simulated committed request lost its ACK. Corrupt,
insecure or mismatched state fails closed; denied/expired heads remain saved
for owner review. Replayed events do not send a current-online report. Details,
recovery steps and the uncovered Swift capture-to-spool window are in
[PUBLISH_STREAM.md](PUBLISH_STREAM.md). This is not a live hardware outage trial.
Log: `data/airlock-publish-stream-tests.log`.

The expanded focused verifier passed **10/10 locally and 10/10 on cloud**:
`data/airlock-mac-views-proof/mac-views-1794094444779-6929cc01/report.json` and
`data/airlock-mac-views-proof/mac-views-1794094445848-e84adcd5/report.json`.
Actual SDK subscriptions and authenticated HTTP views retained original
capture timestamps and offline health for buffered app/camera fixtures older
than 15 seconds. Duplicate retries changed neither health time nor event count.
Both stations in each run were disarmed with no cleanup failures or OS actions.
These extend the earlier nine-group authorization checks; no physical device
or inference acceptance is inferred.

Final combined adapter regression: **99/99 passed**, including the 12 durable
stream cases (`data/airlock-functional-node-final.log`). Frontend functional
tests remain **22/22** and the production build passed.
