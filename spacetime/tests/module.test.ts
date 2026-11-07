import { expect, it } from 'vitest';
import { ConnectionId, Timestamp } from 'spacetimedb';
import { createModuleTestHarness, TestAuth } from 'spacetimedb/server/test-utils';
import spacetimedb, * as moduleExports from '../src/index';
const auth = (name: string, id: bigint) => TestAuth.fromJwtPayload(JSON.stringify({iss:'https://airlock.test',sub:name}), new ConnectionId(id));
const owner = auth('owner', 1n);
const outsider = auth('outsider', 2n);
function setup() {
  const test = createModuleTestHarness(spacetimedb, moduleExports);
  test.clock.set(new Timestamp(1_000_000_000n));
  test.withReducerTx(owner, ctx => moduleExports.register_workstation(ctx, {workstationId:'desk',name:'Test desk'}));
  return test;
}
it('registers only the caller as owner and creates exactly one durable maintenance schedule', () => {
  const test = setup();
  expect(test.db.workstations.id.find('desk')?.owner.isEqual(test.viewContext(owner).sender)).toBe(true);
  test.withReducerTx(owner, ctx => moduleExports.refresh_workstation(ctx, {workstationId:'desk'}));
  test.withReducerTx(owner, ctx => moduleExports.refresh_workstation(ctx, {workstationId:'desk'}));
  expect(test.db.maintenanceSchedule.count()).toBe(1n);
  expect(moduleExports.my_workstations(test.viewContext(outsider))).toEqual([]);
});
it('denied owner mutations roll back and preserve armed policy', () => {
  const test = setup();
  test.withReducerTx(owner, ctx => moduleExports.arm_session(ctx, {workstationId:'desk',sessionId:'session',baselineJson:'[]',approvedAppsJson:'[]',durationMs:60000n,simulated:true}));
  expect(() => test.withReducerTx(outsider, ctx => moduleExports.disarm_session(ctx, {workstationId:'desk'}))).toThrow(/owner/);
  expect(test.db.sessions.id.find('session')?.state).toBe('armed');
});

const serviceA = auth('service-a', 3n);
const serviceB = auth('service-b', 4n);
const reviewer = auth('reviewer', 5n);
const mac = auth('mac', 6n);
const macB = auth('mac-b', 7n);
type Harness = ReturnType<typeof setup>;
function enroll(test: Harness, who: ReturnType<typeof auth>, id: string, role: string) {
  test.withReducerTx(owner, ctx => moduleExports.enroll_device(ctx, {workstationId:'desk',deviceId:id,identityHex:test.viewContext(who).sender.toHexString(),role,label:id}));
}
function arm(test: Harness, autoLock = false, simulated = true) {
  if (!simulated) capture(test);
  test.withReducerTx(owner, ctx => moduleExports.set_policy(ctx, {workstationId:'desk',autoLockEnabled:autoLock,lockCooldownMs:30000n}));
  test.withReducerTx(owner, ctx => moduleExports.arm_session(ctx, {workstationId:'desk',sessionId:'session',baselineJson:'["approved-key"]',approvedAppsJson:'["approved-app"]',durationMs:600000n,simulated}));
}
function time(test: Harness, ms: bigint) { test.clock.set(new Timestamp(ms * 1000n)); }
function emit(test: Harness, id: string, seq: bigint, kind = 'usb_added', overrides: Record<string, unknown> = {}, who = owner) {
  const observedAtMs = test.clock.now().microsSinceUnixEpoch / 1000n;
  const metadataJson = kind.startsWith('usb_') ? '{"deviceKey":"unknown-key"}' : '{}';
  const args = {workstationId:'desk',sessionId:'session',deviceId:'fixture',eventId:id,kind,observedAtMs,seq,confidence:1,metadataJson,simulated:true,schemaVersion:1,...overrides};
  test.withReducerTx(who, ctx => moduleExports.ingest_device_event(ctx, args));
  return args;
}
function propose(test: Harness, id = 'action') {
  test.withReducerTx(serviceA, ctx => moduleExports.propose_protected_action(ctx, {workstationId:'desk',actionId:id,missionId:'mission',incidentId:'',toolName:'safe_count',argsJson:'{"b":2,"a":1}',scope:'sandbox:read',ttlMs:30000n}));
}
function approve(test: Harness, who = owner) {
  test.withReducerTx(who, ctx => moduleExports.decide_action(ctx, {actionId:'action',approve:true,ttlMs:30000n,note:'Harmless test callback'}));
  const a = test.db.protectedActions.id.find('action')!;
  return {actionId:a.id,approvalId:a.approvalId,actionDigest:a.actionDigest,executionId:'execution'};
}

for (const order of ['physical-first','usb-first']) for (const delta of [9999n,10000n,10001n]) it(`real reducers correlate ${order} at ${delta}ms without duplicate cases`, () => {
  const test = setup(); arm(test); time(test,1_030_000n);
  const physical = () => emit(test,'physical',order === 'physical-first' ? 1n : 2n,'person_in_zone',{observedAtMs:1_010_000n});
  const digital = () => emit(test,'usb',order === 'physical-first' ? 2n : 1n,'usb_added',{observedAtMs:1_010_000n+delta});
  if (order === 'physical-first') {physical();digital();} else {digital();physical();}
  const incident = [...test.db.incidents.iter()];
  expect(incident).toHaveLength(1); expect(incident[0].severity).toBe(delta <= 10000n ? 'high' : 'medium');
  expect(test.db.lockRequests.count()).toBe(0n);
});
it('groups repeated USB polling into one unresolved case and one lock request even beyond cooldown', () => {
  const test = setup(); arm(test,true); emit(test,'usb-first',1n);
  time(test,1_031_000n); emit(test,'usb-repeated',2n);
  expect(test.db.incidents.count()).toBe(1n); expect(test.db.lockRequests.count()).toBe(1n);
  expect(JSON.parse(test.db.incidents.id.find('incident:usb-first')!.eventIdsJson)).toEqual(['usb-first','usb-repeated']);
  time(test,1_032_000n); emit(test,'physical-late',3n,'person_in_zone');
  const incident = test.db.incidents.id.find('incident:usb-first')!;
  expect(incident.severity).toBe('high'); expect(incident.digitalEventId).toBe('usb-repeated');
  expect(incident.physicalEventId).toBe('physical-late'); expect(test.db.lockRequests.count()).toBe(1n);
});
it('accepts old audit evidence without issuing a stale screen-lock request', () => {
  const test = setup(); arm(test,true); time(test,1_020_000n);
  emit(test,'old-usb',1n,'usb_added',{observedAtMs:1_000_000n});
  expect(test.db.incidents.count()).toBe(1n); expect(test.db.lockRequests.count()).toBe(0n);
});
it('rejects stale/prearm/conflicting metadata as useful policy errors and rolls back all rows', () => {
  const test = setup(); arm(test); time(test,1_200_000n);
  expect(() => emit(test,'stale',1n,'usb_added',{observedAtMs:1_000_000n})).toThrow(/stale/);
  expect(() => emit(test,'prearm',1n,'usb_added',{observedAtMs:999_999n})).toThrow(/stale/);
  expect(() => emit(test,'sensitive',1n,'usb_added',{metadataJson:'{"deviceKey":"key","rawSerial":"sensitive"}'})).toThrow(/redacted/);
  expect(() => emit(test,'malformed',1n,'usb_added',{metadataJson:'not JSON'})).toThrow(/JSON/);
  expect(test.db.deviceEvents.count()).toBe(0n); expect(test.db.incidents.count()).toBe(0n);
  time(test,1_201_000n); const first = emit(test,'once',1n);
  test.withReducerTx(owner, ctx => moduleExports.ingest_device_event(ctx, first));
  expect(() => test.withReducerTx(owner, ctx => moduleExports.ingest_device_event(ctx, {...first,metadataJson:'{"deviceKey":"changed"}'}))).toThrow(/conflict/);
  expect(test.db.deviceEvents.count()).toBe(1n);
});
it('revoking an approving reviewer invalidates existing unspent approval atomically', () => {
  const test = setup(); enroll(test,serviceA,'service-a','service');
  test.withReducerTx(owner,ctx => moduleExports.grant_reviewer(ctx,{workstationId:'desk',identityHex:test.viewContext(reviewer).sender.toHexString(),canApprove:true}));
  propose(test); const args = approve(test,reviewer);
  test.withReducerTx(owner,ctx => moduleExports.revoke_member(ctx,{workstationId:'desk',identityHex:test.viewContext(reviewer).sender.toHexString()}));
  expect(test.db.approvals.id.find(args.approvalId)?.status).toBe('revoked');
  expect(() => test.withReducerTx(serviceA,ctx => moduleExports.consume_approval(ctx,args))).toThrow(/unused approval/);
  expect(test.db.protectedActions.id.find('action')?.status).toBe('denied');
});
it('removing canApprove invalidates a grant while preserving the reviewer membership', () => {
  const test = setup(); enroll(test,serviceA,'service-a','service');
  const identityHex = test.viewContext(reviewer).sender.toHexString();
  test.withReducerTx(owner,ctx => moduleExports.grant_reviewer(ctx,{workstationId:'desk',identityHex,canApprove:true}));
  propose(test); const args = approve(test,reviewer);
  test.withReducerTx(owner,ctx => moduleExports.grant_reviewer(ctx,{workstationId:'desk',identityHex,canApprove:false}));
  expect(moduleExports.my_workstations(test.viewContext(reviewer))).toHaveLength(1);
  expect(() => test.withReducerTx(serviceA,ctx => moduleExports.consume_approval(ctx,args))).toThrow();
  expect(test.db.approvals.id.find(args.approvalId)?.status).toBe('revoked');
});
it('a second enrolled service cannot spend or report the first service approval', () => {
  const test = setup(); enroll(test,serviceA,'service-a','service'); enroll(test,serviceB,'service-b','service');
  propose(test); const args = approve(test);
  expect(() => test.withReducerTx(serviceB,ctx => moduleExports.consume_approval(ctx,args))).toThrow(/proposing service/);
  expect(() => test.withReducerTx(serviceA,ctx => moduleExports.consume_approval(ctx,{...args,actionDigest:'0'.repeat(64)}))).toThrow(/digest/);
  test.withReducerTx(serviceA,ctx => moduleExports.consume_approval(ctx,args));
  expect(() => test.withReducerTx(serviceA,ctx => moduleExports.consume_approval(ctx,args))).toThrow(/unused/);
  expect(() => test.withReducerTx(serviceB,ctx => moduleExports.report_action_result(ctx,{actionId:'action',executionId:'execution',status:'succeeded',detail:'forged'}))).toThrow(/consuming service/);
});
it('consumption rechecks the live permission even if an approval record still says approved', () => {
  const test = setup(); enroll(test,serviceA,'service-a','service'); propose(test); const args = approve(test);
  const membership = [...test.db.memberships.iter()].find(m=>m.role==='owner')!;
  test.db.memberships.id.update({...membership,canApprove:false});
  expect(() => test.withReducerTx(serviceA,ctx => moduleExports.consume_approval(ctx,args))).toThrow(/permission.*revoked/);
  expect(test.db.approvals.id.find(args.approvalId)?.status).toBe('approved');
});
it('scheduled maintenance expires stale health, sessions and unconsumed grants without client refresh', () => {
  const test = setup(); enroll(test,serviceA,'service-a','service'); arm(test); propose(test); const args = approve(test);
  test.withReducerTx(serviceA,ctx=>moduleExports.report_health(ctx,{workstationId:'desk',deviceId:'service-a',component:'model',status:'online',detail:'test model'}));
  time(test,1_700_000n);
  const timer = test.db.maintenanceSchedule.workstationId.find('desk')!;
  test.withReducerTx(TestAuth.internal(),ctx=>moduleExports.maintain_workstation(ctx,{timer}));
  expect(test.db.sessions.id.find('session')?.state).toBe('expired');
  expect(test.db.deviceHealth.id.find('desk:service-a:model')?.status).toBe('offline');
  expect(test.db.approvals.id.find(args.approvalId)?.status).toBe('expired');
  expect(test.db.protectedActions.id.find('action')?.status).toBe('expired');
  expect(test.db.workstations.id.find('desk')?.activeSessionId).toBe('');
  expect(test.db.maintenanceSchedule.count()).toBe(1n);
});
it('generic gateway heartbeat cannot mark the model online', () => {
  const test = setup(); enroll(test,serviceA,'service-a','service'); arm(test);
  emit(test,'gateway-heartbeat',1n,'heartbeat',{deviceId:'service-a'},serviceA);
  expect([...test.db.deviceHealth.iter()].map(h=>h.component)).toEqual(['gateway']);
});
it('binds agent run to its reporting service and immutable reported model/trace', () => {
  const test = setup(); enroll(test,serviceA,'service-a','service'); enroll(test,serviceB,'service-b','service'); arm(test); emit(test,'usb',1n);
  const args = {workstationId:'desk',runId:'run',incidentId:'incident:usb',stepId:'step1',step:1,toolName:'read_events',status:'running',evidenceId:'usb',durationMs:1n,inputType:'metadata',argsJson:'{}',resultSummary:'Actual test harness outcome',traceId:'trace',model:'reported-model'};
  test.withReducerTx(serviceA,ctx=>moduleExports.record_agent_step(ctx,args));
  expect(() => test.withReducerTx(serviceB,ctx=>moduleExports.record_agent_step(ctx,{...args,stepId:'forged',step:2}))).toThrow(/reporting service/);
  expect(() => test.withReducerTx(serviceA,ctx=>moduleExports.record_agent_step(ctx,{...args,stepId:'changed',step:2,model:'another-model'}))).toThrow(/immutable/);
  expect(() => test.withReducerTx(serviceA,ctx=>moduleExports.record_agent_step(ctx,{...args,stepId:'regressed'}))).toThrow(/sequence/);
  test.withReducerTx(serviceA,ctx=>moduleExports.record_agent_step(ctx,{...args,stepId:'step2',step:2,status:'complete'}));
  expect(() => test.withReducerTx(serviceA,ctx=>moduleExports.record_agent_step(ctx,{...args,stepId:'step3',step:3}))).toThrow(/terminal/);
  expect([...test.db.deviceHealth.iter()].find(h=>h.component==='model')).toBeUndefined();
  expect([...test.db.deviceHealth.iter()].find(h=>h.component==='gateway')?.status).toBe('online');
});
it('evidence-only tool success never asserts that a model performed inference', () => {
  const test = setup(); enroll(test,serviceA,'service-a','service'); arm(test); emit(test,'usb',1n);
  test.withReducerTx(serviceA,ctx=>moduleExports.record_agent_step(ctx,{workstationId:'desk',runId:'evidence-only',incidentId:'incident:usb',stepId:'evidence-step',step:1,toolName:'read_events',status:'succeeded',evidenceId:'usb',durationMs:1n,inputType:'deterministic_verification_not_model_selected',argsJson:'{}',resultSummary:'Real evidence read, no model called',traceId:'trace-evidence',model:'none:evidence-tool-verification'}));
  expect([...test.db.deviceHealth.iter()].find(h=>h.component==='model')).toBeUndefined();
  expect([...test.db.deviceHealth.iter()].find(h=>h.component==='gateway')?.status).toBe('online');
});
it('model health requires a successful inference report and fails degraded honestly', () => {
  const test = setup(); enroll(test,serviceA,'service-a','service'); arm(test); emit(test,'usb',1n);
  const step = {workstationId:'desk',runId:'inference-run',incidentId:'incident:usb',stepId:'starting',step:1,toolName:'read_events',status:'running',evidenceId:'usb',durationMs:1n,inputType:'metadata',argsJson:'{}',resultSummary:'Investigation starting',traceId:'trace-inference',model:'nemotron-3.5-bolt:e4b'};
  test.withReducerTx(serviceA,ctx=>moduleExports.record_agent_step(ctx,step));
  expect([...test.db.deviceHealth.iter()].find(h=>h.component==='model')).toBeUndefined();
  test.withReducerTx(serviceA,ctx=>moduleExports.record_agent_step(ctx,{...step,stepId:'inference',step:2,toolName:'nemotron_inference',status:'succeeded',inputType:'local_ollama_model_response'}));
  expect([...test.db.deviceHealth.iter()].find(h=>h.component==='model')?.status).toBe('online');
  test.withReducerTx(serviceA,ctx=>moduleExports.record_agent_step(ctx,{...step,stepId:'failed',step:3,toolName:'investigation_failed',status:'failed',inputType:'adapter_failure'}));
  expect([...test.db.deviceHealth.iter()].find(h=>h.component==='model')?.status).toBe('degraded');
});
it('rate limit allows 64 events per second and accepts the exact next-window boundary', () => {
  const test = setup(); arm(test);
  for (let n=1;n<=64;n++) emit(test,`event-${n}`,BigInt(n),'heartbeat');
  expect(() => emit(test,'rate-denied',65n,'heartbeat')).toThrow(/rate limit/);
  expect(test.db.deviceEvents.count()).toBe(64n);
  time(test,1_001_000n); emit(test,'next-second',65n,'heartbeat');
  expect(test.db.deviceEvents.count()).toBe(65n);
});
it('a live lock request can only be reported by its target Mac and cannot regress after confirmation', () => {
  const test = setup(); enroll(test,mac,'mac','mac'); enroll(test,macB,'mac-b','mac'); arm(test,true,false);
  emit(test,'live-usb',1n,'usb_added',{deviceId:'mac',simulated:false},mac);
  const lock = [...test.db.lockRequests.iter()][0]; expect(lock).toBeDefined(); expect(lock.simulated).toBe(false);
  const target = lock.targetDeviceId === 'mac' ? mac : macB; const other = target === mac ? macB : mac;
  expect(() => test.withReducerTx(other,ctx=>moduleExports.report_lock_result(ctx,{actionId:lock.id,status:'confirmed',detail:'forged'}))).toThrow(/target/);
  test.withReducerTx(target,ctx=>moduleExports.claim_lock(ctx,{actionId:lock.id,executionId:'lock-test-execution'}));
  test.withReducerTx(target,ctx=>moduleExports.report_lock_result(ctx,{actionId:lock.id,status:'executed',detail:'API invocation returned; OS not confirmed'}));
  expect(test.db.lockRequests.id.find(lock.id)?.status).toBe('executed');
  test.withReducerTx(target,ctx=>moduleExports.report_lock_result(ctx,{actionId:lock.id,status:'confirmed',detail:'Adapter independently checked OS lock state'}));
  expect(() => test.withReducerTx(target,ctx=>moduleExports.report_lock_result(ctx,{actionId:lock.id,status:'executed',detail:'regressed'}))).toThrow(/terminal/);
  expect(test.db.lockRequests.id.find(lock.id)?.status).toBe('confirmed');
});
it('simulated lock request cannot be reported as a real OS execution', () => {
  const test = setup(); enroll(test,mac,'mac','mac'); arm(test,true); emit(test,'usb',1n);
  const lock = [...test.db.lockRequests.iter()][0];
  expect(lock.simulated).toBe(true);
  expect(() => test.withReducerTx(mac,ctx=>moduleExports.report_lock_result(ctx,{actionId:lock.id,status:'executed',detail:'must not run'}))).toThrow(/Simulated/);
});
it('USB and app observations refresh only their actual sensor component', () => {
  const test = setup(); enroll(test,mac,'mac','mac'); arm(test);
  emit(test,'mac-heartbeat',1n,'heartbeat',{deviceId:'mac'},mac);
  expect([...test.db.deviceHealth.iter()].map(h=>h.component)).toEqual(['watcher']);
  emit(test,'usb',2n,'usb_added',{deviceId:'mac'},mac);
  expect([...test.db.deviceHealth.iter()].map(h=>h.component).sort()).toEqual(['usb','watcher']);
  emit(test,'app',3n,'app_launched',{deviceId:'mac',metadataJson:'{"appId":"unknown-app"}'},mac);
  expect([...test.db.deviceHealth.iter()].map(h=>h.component).sort()).toEqual(['apps','usb','watcher']);
});

function capture(test: Harness, baselineJson = '["approved-key"]', activeAppId = 'com.apple.finder', who = mac, deviceId = 'mac', observedAtMs = test.clock.now().microsSinceUnixEpoch / 1000n) {
  test.withReducerTx(who, ctx => moduleExports.record_baseline(ctx, {workstationId:'desk',deviceId,observedAtMs,baselineJson,activeAppId}));
}
function liveArm(test: Harness, baselineJson = '', sessionId = 'session') {
  test.withReducerTx(owner, ctx => moduleExports.arm_session(ctx, {workstationId:'desk',sessionId,baselineJson,approvedAppsJson:'[]',durationMs:60000n,simulated:false}));
}
function maintain(test: Harness) {
  const timer = test.db.maintenanceSchedule.workstationId.find('desk')!;
  test.withReducerTx(TestAuth.internal(), ctx => moduleExports.maintain_workstation(ctx, {timer}));
}
it('live arm refuses a manual owner baseline without a fresh authenticated Mac capture', () => {
  const test = setup(); enroll(test,mac,'mac','mac');
  expect(() => liveArm(test,'[]')).toThrow(/Fresh enrolled Mac baseline/);
  expect(() => capture(test,'[]','unknown',owner)).toThrow(/matching Mac/);
  expect(() => capture(test,'[]','unknown',outsider)).toThrow(/member/);
  expect(test.db.baselineSnapshots.count()).toBe(0n); expect(test.db.sessions.count()).toBe(0n);
});
it('baseline publication is bound to enrolled identity, role and workstation', () => {
  const test = setup(); enroll(test,mac,'mac','mac'); enroll(test,macB,'mac-b','mac'); enroll(test,serviceA,'service-a','service');
  expect(() => capture(test,'[]','unknown',macB,'mac')).toThrow(/matching Mac/);
  expect(() => capture(test,'[]','unknown',serviceA,'service-a')).toThrow(/matching Mac/);
  test.withReducerTx(owner,ctx=>moduleExports.register_workstation(ctx,{workstationId:'other',name:'Other'}));
  test.withReducerTx(owner,ctx=>moduleExports.enroll_device(ctx,{workstationId:'other',deviceId:'other-mac',identityHex:test.viewContext(mac).sender.toHexString(),role:'mac',label:'Other Mac'}));
  expect(() => capture(test,'[]','unknown',mac,'other-mac')).toThrow(/matching Mac/);
  capture(test);
  expect(moduleExports.my_baseline_snapshots(test.viewContext(outsider))).toEqual([]);
  expect(moduleExports.my_baseline_snapshots(test.viewContext(macB))).toEqual([]);
  expect(moduleExports.my_baseline_snapshots(test.viewContext(owner))).toHaveLength(1);
});
it('baseline timestamps reject stale, far future and regressing observations', () => {
  const test = setup(); enroll(test,mac,'mac','mac');
  expect(() => capture(test,'[]','unknown',mac,'mac',969999n)).toThrow(/stale/);
  expect(() => capture(test,'[]','unknown',mac,'mac',1005001n)).toThrow(/future/);
  capture(test,'[]','unknown');
  expect(() => capture(test,'[]','unknown',mac,'mac',999999n)).toThrow(/regress/);
  expect(test.db.baselineSnapshots.id.find('latest:mac')?.observedAtMs).toBe(1000000n);
});
it('live arm enforces both baseline freshness and independently stale USB health', () => {
  const test = setup(); enroll(test,mac,'mac','mac'); capture(test);
  time(test,1015001n); expect(() => liveArm(test)).toThrow(/Fresh enrolled Mac baseline/);
  test.withReducerTx(mac,ctx=>moduleExports.report_health(ctx,{workstationId:'desk',deviceId:'mac',component:'usb',status:'online',detail:'Actual continuing poll'}));
  time(test,1030001n);
  test.withReducerTx(mac,ctx=>moduleExports.report_health(ctx,{workstationId:'desk',deviceId:'mac',component:'usb',status:'online',detail:'Actual continuing poll'}));
  expect(() => liveArm(test)).toThrow(/Fresh enrolled Mac baseline/);
  time(test,1030000n); liveArm(test); // Exactly 30 seconds is accepted.
  expect(test.db.sessions.id.find('session')?.baselineJson).toBe('["approved-key"]');
});
it('live arm freezes captured inventory, active app and server camera health; later captures cannot rewrite it', () => {
  const test = setup(); enroll(test,mac,'mac','mac'); capture(test,'["b","a"]','com.apple.finder');
  expect(test.db.deviceHealth.id.find('desk:mac:apps')).toBeNull();
  expect(() => liveArm(test,'["invented-device"]')).toThrow(/does not match/);
  expect(test.db.baselineSnapshots.id.find('session:session')).toBeNull();
  liveArm(test,'["b","a","a"]');
  const frozen = test.db.baselineSnapshots.id.find('session:session')!;
  expect(frozen.baselineJson).toBe('["a","b"]'); expect(frozen.activeAppId).toBe('com.apple.finder'); expect(frozen.cameraStatus).toBe('unavailable');
  time(test,1001000n); capture(test,'["changed"]','unknown');
  expect(test.db.baselineSnapshots.id.find('session:session')).toEqual(frozen);
  expect(test.db.sessions.id.find('session')?.baselineJson).toBe('["a","b"]');
  expect(test.db.baselineSnapshots.id.find('latest:mac')?.activeAppId).toBe('unknown');
});
it('stale camera health is frozen offline without blocking healthy USB live arm', () => {
  const test = setup(); enroll(test,mac,'mac','mac'); enroll(test,serviceB,'jetson','jetson');
  test.withReducerTx(serviceB,ctx=>moduleExports.report_health(ctx,{workstationId:'desk',deviceId:'jetson',component:'camera',status:'online',detail:'Camera capture reported'}));
  time(test,1015001n); capture(test); liveArm(test);
  expect(test.db.baselineSnapshots.id.find('session:session')?.cameraStatus).toBe('offline');
  expect(JSON.parse(test.db.baselineSnapshots.id.find('session:session')!.healthJson).find((h:{component:string})=>h.component==='camera').status).toBe('offline');
});
it('revoked or replaced Mac enrollment cannot reuse a previously fresh baseline', () => {
  const test = setup(); enroll(test,mac,'mac','mac'); capture(test);
  test.withReducerTx(owner,ctx=>moduleExports.revoke_member(ctx,{workstationId:'desk',identityHex:test.viewContext(mac).sender.toHexString()}));
  expect(() => liveArm(test)).toThrow(/Fresh enrolled Mac baseline/);
  enroll(test,macB,'mac','mac');
  expect(() => liveArm(test)).toThrow(/Fresh enrolled Mac baseline/);
  capture(test,'[]','unknown',macB,'mac'); liveArm(test);
  expect(test.db.baselineSnapshots.id.find('session:session')?.publisher.isEqual(test.viewContext(macB).sender)).toBe(true);
});
it('live lock target is the Mac whose baseline was frozen, not the first enrolled Mac', () => {
  const test = setup(); enroll(test,mac,'mac','mac'); enroll(test,macB,'mac-b','mac');
  capture(test,'[]','unknown',macB,'mac-b');
  test.withReducerTx(owner,ctx=>moduleExports.set_policy(ctx,{workstationId:'desk',autoLockEnabled:true,lockCooldownMs:30000n}));
  liveArm(test); emit(test,'usb',1n,'usb_added',{deviceId:'mac-b',simulated:false},macB);
  expect([...test.db.lockRequests.iter()][0].targetDeviceId).toBe('mac-b');
});
const DAY = 86400000n;
it('retention preserves unresolved cases and all their evidence after the short retention interval', () => {
  const test = setup(); arm(test); emit(test,'usb',1n);
  test.withReducerTx(owner,ctx=>moduleExports.disarm_session(ctx,{workstationId:'desk'}));
  time(test,1000000n+DAY*2n); maintain(test);
  expect(test.db.incidents.id.find('incident:usb')).not.toBeNull(); expect(test.db.deviceEvents.id.find('usb')).not.toBeNull(); expect(test.db.sessions.id.find('session')).not.toBeNull();
  expect([...test.db.auditEvents.iter()].some(e=>e.kind==='incident_opened')).toBe(true);
});
it('retention waits 24 hours after case resolution and removes terminal metadata without dangling references', () => {
  const test = setup(); enroll(test,serviceA,'service-a','service'); arm(test,true); emit(test,'usb',1n);
  test.withReducerTx(serviceA,ctx=>moduleExports.record_agent_step(ctx,{workstationId:'desk',runId:'run',incidentId:'incident:usb',stepId:'step',step:1,toolName:'read_events',status:'complete',evidenceId:'usb',durationMs:1n,inputType:'metadata',argsJson:'{}',resultSummary:'Actual metadata read',traceId:'trace',model:'none:retention-test'}));
  test.withReducerTx(owner,ctx=>moduleExports.disarm_session(ctx,{workstationId:'desk'}));
  time(test,1000000n+DAY);
  test.withReducerTx(owner,ctx=>moduleExports.resolve_incident(ctx,{incidentId:'incident:usb',note:'Reviewed harmless test'}));
  maintain(test); expect(test.db.incidents.count()).toBe(1n);
  time(test,1000000n+DAY*2n); maintain(test);
  for (const table of [test.db.sessions,test.db.incidents,test.db.deviceEvents,test.db.lockRequests,test.db.acknowledgements,test.db.agentRuns,test.db.agentSteps]) expect(table.count()).toBe(0n);
  expect(test.db.auditEvents.count()).toBe(0n);
  expect(test.db.workstations.count()).toBe(1n); expect(test.db.maintenanceSchedule.count()).toBe(1n);
});
it('retention preserves nonterminal investigation attribution even on an old resolved case', () => {
  const test = setup(); enroll(test,serviceA,'service-a','service'); arm(test); emit(test,'usb',1n);
  const step = {workstationId:'desk',runId:'run',incidentId:'incident:usb',stepId:'step',step:1,toolName:'read_events',status:'running',evidenceId:'usb',durationMs:1n,inputType:'metadata',argsJson:'{}',resultSummary:'Actual metadata read',traceId:'trace',model:'none:retention-test'};
  test.withReducerTx(serviceA,ctx=>moduleExports.record_agent_step(ctx,step));
  test.withReducerTx(owner,ctx=>moduleExports.resolve_incident(ctx,{incidentId:'incident:usb',note:'Reviewed'}));
  test.withReducerTx(owner,ctx=>moduleExports.disarm_session(ctx,{workstationId:'desk'}));
  time(test,1000000n+DAY*2n); maintain(test);
  expect(test.db.deviceEvents.count()).toBe(1n);
  expect([...test.db.auditEvents.iter()].some(e=>e.kind==='agent_run_started')).toBe(true);
  test.withReducerTx(serviceA,ctx=>moduleExports.record_agent_step(ctx,{...step,stepId:'finish',step:2,status:'complete'}));
  maintain(test); expect(test.db.agentRuns.count()).toBe(1n);
  time(test,1000000n+DAY*3n); maintain(test); expect(test.db.agentRuns.count()).toBe(0n);
});
it('retention bounds each cleanup to 200 deletes and removes parents only after their children', () => {
  const test = setup(); arm(test);
  for (let n=0;n<220;n++) { time(test,1000000n+BigInt(n)*1000n); emit(test,`removed-${n}`,BigInt(n+1),'usb_removed'); }
  test.withReducerTx(owner,ctx=>moduleExports.disarm_session(ctx,{workstationId:'desk'}));
  time(test,1300000n+DAY); maintain(test);
  expect(test.db.deviceEvents.count()).toBe(20n); expect(test.db.sessions.count()).toBe(1n);
  maintain(test); expect(test.db.deviceEvents.count()).toBe(0n); expect(test.db.sessions.count()).toBe(0n);
});
it('retention deletes expired standalone approvals atomically with their action while keeping consumed execution', () => {
  const test = setup(); enroll(test,serviceA,'service-a','service'); propose(test); approve(test);
  propose(test,'consumed-action');
  test.withReducerTx(owner,ctx=>moduleExports.decide_action(ctx,{actionId:'consumed-action',approve:true,ttlMs:30000n,note:'Test'}));
  const consumed = test.db.protectedActions.id.find('consumed-action')!;
  test.withReducerTx(serviceA,ctx=>moduleExports.consume_approval(ctx,{actionId:consumed.id,approvalId:consumed.approvalId,actionDigest:consumed.actionDigest,executionId:'execution'}));
  time(test,1030001n+DAY); maintain(test);
  expect(test.db.protectedActions.id.find('action')).toBeNull(); expect(test.db.approvals.id.find('approval:action')).toBeNull();
  expect(test.db.protectedActions.id.find('consumed-action')).not.toBeNull(); expect(test.db.approvals.id.find(consumed.approvalId)).not.toBeNull();
});

it('reenrollment clears old capture and health even when the device identity is unchanged', () => {
  const test = setup(); enroll(test,mac,'mac','mac'); capture(test);
  enroll(test,mac,'mac','mac');
  expect(test.db.baselineSnapshots.id.find('latest:mac')).toBeNull();
  expect(test.db.deviceHealth.id.find('desk:mac:usb')).toBeNull();
  expect(() => liveArm(test)).toThrow(/Fresh enrolled Mac baseline/);
});

it('an old consumed action remains for 24 hours after its late terminal result', () => {
  const test = setup(); enroll(test,serviceA,'service-a','service'); propose(test); const grant = approve(test);
  test.withReducerTx(serviceA,ctx=>moduleExports.consume_approval(ctx,grant));
  time(test,1000000n+DAY*2n); maintain(test);
  test.withReducerTx(serviceA,ctx=>moduleExports.report_action_result(ctx,{actionId:'action',executionId:'execution',status:'succeeded',detail:'Delayed confirmed callback result'}));
  maintain(test); expect(test.db.protectedActions.id.find('action')).not.toBeNull();
  time(test,1000000n+DAY*3n-1n); maintain(test); expect(test.db.protectedActions.id.find('action')).not.toBeNull();
  time(test,1000000n+DAY*3n); maintain(test);
  expect(test.db.protectedActions.id.find('action')).toBeNull(); expect(test.db.approvals.id.find(grant.approvalId)).toBeNull();
});

it('atomic lock claim permits exactly one winner even for cloned target identities',()=>{
  const test=setup();enroll(test,mac,'mac','mac');arm(test,true,false);emit(test,'claim-usb',1n,'usb_added',{deviceId:'mac',simulated:false},mac);
  const lock=[...test.db.lockRequests.iter()][0];
  expect(()=>test.withReducerTx(mac,ctx=>moduleExports.report_lock_result(ctx,{actionId:lock.id,status:'executed',detail:'unclaimed'}))).toThrow(/claim/);
  test.withReducerTx(mac,ctx=>moduleExports.claim_lock(ctx,{actionId:lock.id,executionId:'first-process'}));
  expect(()=>test.withReducerTx(mac,ctx=>moduleExports.claim_lock(ctx,{actionId:lock.id,executionId:'cloned-process'}))).toThrow(/claimed|terminal/);
  expect(test.db.lockClaims.count()).toBe(1n);expect(test.db.lockRequests.id.find(lock.id)?.status).toBe('executing');
  time(test,1015000n);const timer=test.db.maintenanceSchedule.workstationId.find('desk')!;test.withReducerTx(TestAuth.internal(),ctx=>moduleExports.maintain_workstation(ctx,{timer}));
  expect(test.db.lockRequests.id.find(lock.id)?.status).toBe('expired');expect(test.db.lockClaims.count()).toBe(1n);
});
it('lock claim rechecks current consent and disarm rather than trusting a stale view',()=>{
  for(const change of ['policy','disarm','synthetic']){
    const test=setup();enroll(test,mac,'mac','mac');arm(test,true,change==='synthetic');emit(test,'usb',1n,'usb_added',change==='synthetic'?{}:{deviceId:'mac',simulated:false},change==='synthetic'?owner:mac);
    const lock=[...test.db.lockRequests.iter()][0];
    if(change==='policy')test.withReducerTx(owner,ctx=>moduleExports.set_policy(ctx,{workstationId:'desk',autoLockEnabled:false,lockCooldownMs:30000n}));
    if(change==='disarm')test.withReducerTx(owner,ctx=>moduleExports.disarm_session(ctx,{workstationId:'desk'}));
    expect(()=>test.withReducerTx(mac,ctx=>moduleExports.claim_lock(ctx,{actionId:lock.id,executionId:'process'}))).toThrow();expect(test.db.lockClaims.count()).toBe(0n);
  }
});
it('reassigning the last service device revokes old reads, proposals and outstanding grants',()=>{
  const test=setup();enroll(test,serviceA,'service','service');propose(test);const approval=approve(test);
  enroll(test,serviceB,'service','service');
  expect(moduleExports.my_workstations(test.viewContext(serviceA))).toEqual([]);expect(moduleExports.my_protected_actions(test.viewContext(serviceA))).toEqual([]);
  expect(()=>propose(test,'old-token-proposal')).toThrow(/member|device/);
  expect(()=>test.withReducerTx(serviceA,ctx=>moduleExports.consume_approval(ctx,approval))).toThrow(/member|device/);
  expect(test.db.protectedActions.id.find('action')?.status).toBe('denied');expect(test.db.approvals.id.find(approval.approvalId)?.status).toBe('revoked');
  const prior=[...test.db.memberships.iter()].find(m=>m.identity.isEqual(test.viewContext(serviceA).sender))!;test.db.memberships.id.update({...prior,enabled:true});
  expect(()=>propose(test,'orphan-grant')).toThrow(/enabled.*device/);expect(moduleExports.my_workstations(test.viewContext(serviceA))).toEqual([]);
});
it('multiple same-role devices deliberately preserve membership until the final one is reassigned',()=>{
  const test=setup();enroll(test,serviceA,'one','service');enroll(test,serviceA,'two','service');
  enroll(test,serviceB,'one','service');expect(moduleExports.my_workstations(test.viewContext(serviceA))).toHaveLength(1);propose(test);
  enroll(test,serviceB,'two','service');expect(moduleExports.my_workstations(test.viewContext(serviceA))).toEqual([]);
});
it('Mac and Jetson replacement revokes the old sensor identity without escalating roles',()=>{
  for(const role of ['mac','jetson']){const test=setup();enroll(test,mac,'sensor',role);enroll(test,macB,'sensor',role);expect(moduleExports.my_workstations(test.viewContext(mac))).toEqual([]);expect(moduleExports.my_workstations(test.viewContext(macB))).toHaveLength(1);}
  const test=setup();enroll(test,serviceA,'service','service');expect(()=>enroll(test,serviceA,'sensor','mac')).toThrow(/another role/);expect(test.db.devices.id.find('sensor')).toBeFalsy();
});

// Functional retention stress coverage, not a latency SLA: allow slower shared CI
// runners to complete all 10,001 transactions without weakening the assertions.
it('10001 valid heartbeats retain bounded rows and do not starve a later USB incident',()=>{
  const test=setup();test.withReducerTx(owner,ctx=>moduleExports.arm_session(ctx,{workstationId:'desk',sessionId:'session',baselineJson:'[]',approvedAppsJson:'[]',durationMs:86400000n,simulated:true}));
  for(let n=1;n<=10001;n++){time(test,1000000n+BigInt(n)*1001n);emit(test,'heartbeat-'+n,BigInt(n),'heartbeat');}
  expect(test.db.deviceEvents.count()).toBe(128n);expect([...test.db.auditEvents.iter()].filter(e=>e.kind==='device_event')).toHaveLength(0);
  const retry=test.db.deviceEvents.id.find('heartbeat-10001')!;
  test.withReducerTx(owner,ctx=>moduleExports.ingest_device_event(ctx,{workstationId:'desk',sessionId:'session',deviceId:'fixture',eventId:retry.id,kind:'heartbeat',observedAtMs:retry.observedAtMs,seq:retry.seq,confidence:retry.confidence,metadataJson:retry.metadataJson,simulated:true,schemaVersion:1}));
  time(test,1000000n+10002n*1001n);emit(test,'usb-after-heartbeats',10002n);
  expect(test.db.incidents.count()).toBe(1n);expect(test.db.deviceEvents.count()).toBe(129n);
},30_000);

it('owner panic is explicit manual intent and preserves all lock consent/claim gates',()=>{
  const test=setup();enroll(test,mac,'mac','mac');arm(test,true,false);
  expect(()=>test.withReducerTx(outsider,ctx=>moduleExports.panic_session(ctx,{workstationId:'desk',reason:'forged'}))).toThrow(/owner/);
  test.withReducerTx(owner,ctx=>moduleExports.panic_session(ctx,{workstationId:'desk',reason:'Please protect my workstation'}));
  expect(test.db.deviceEvents.count()).toBe(0n);const incident=[...test.db.incidents.iter()][0];expect(incident.ruleId).toBe('owner_panic');expect(incident.eventIdsJson).toBe('[]');
  expect(test.db.sessions.id.find('session')?.state).toBe('armed');const lock=[...test.db.lockRequests.iter()][0];
  test.withReducerTx(mac,ctx=>moduleExports.claim_lock(ctx,{actionId:lock.id,executionId:'panic-execution'}));expect(test.db.lockClaims.count()).toBe(1n);
  test.withReducerTx(owner,ctx=>moduleExports.panic_session(ctx,{workstationId:'desk',reason:'Repeated owner intent'}));expect(test.db.lockRequests.count()).toBe(1n);
});
it('owner panic without frozen auto-lock consent creates review intent without an OS request',()=>{
  const test=setup();arm(test,false);test.withReducerTx(owner,ctx=>moduleExports.panic_session(ctx,{workstationId:'desk',reason:'Review only'}));expect(test.db.incidents.count()).toBe(1n);expect(test.db.lockRequests.count()).toBe(0n);
});
