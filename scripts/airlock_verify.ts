#!/usr/bin/env node
/** Independent black-box verification against a REAL deployed AIRLOCK module. */
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { AirlockHttp } from './airlock_http.mjs';
import { makeFixture, publishSecurityEvent, validateSecurityEvent } from './airlock_publisher.mjs';
import { call, closeAirlock, connectAirlock, rows, until, type AirlockClient } from './airlock_sdk';
import { executeApprovedAction, managedTool } from './airlock_gateway';

const runId = `verify-${Date.now()}-${randomUUID().slice(0, 8)}`;
const report: any = {
  run_id: runId, started_at: new Date().toISOString(), database: process.env.AIRLOCK_DATABASE || 'locki-airlock-sid',
  endpoint: process.env.AIRLOCK_SPACETIME_URI || 'http://127.0.0.1:3000',
  transport: 'real SpacetimeDB generated SDK subscriptions/reducers plus authenticated HTTP',
  provenance: 'Synthetic test-owned metadata only, including explicitly labeled live-arm contract baseline fixtures; no hardware observation or OS lock claimed',
  measurements: 'single local Node process, independent identities and connections; 10ms cache polling; not a two-laptop measurement',
  tests: [], latency_ms: { owner: [], reviewer: [] },
};
const clients: AirlockClient[] = [];
const paths = { output: resolve(process.env.AIRLOCK_VERIFY_OUTPUT || 'data/airlock-verification', runId) };
let owner: AirlockClient, reviewer: AirlockClient, approver: AirlockClient, mac: AirlockClient, jetson: AirlockClient, service: AirlockClient, outsider: AirlockClient;
let stationCounter = 0;
const sequences = new Map<string, bigint>();
const checkpoints: Array<{ workstationId: string; sessionId: string; incidentId: string }> = [];

async function test(name: string, fn: () => Promise<void> | void) {
  const started = performance.now();
  try {
    await fn();
    report.tests.push({ name, passed: true, elapsed_ms: +(performance.now() - started).toFixed(2) });
    console.log(`PASS ${name}`);
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    report.tests.push({ name, passed: false, detail, elapsed_ms: +(performance.now() - started).toFixed(2) });
    console.error(`FAIL ${name}: ${detail}`);
    throw error;
  }
}

async function deny(operation: () => Promise<unknown>, expected: RegExp = /owner|authoriz|member|enroll|role|permission|cannot|denied|expired|digest|used|consum|simulat|seq|stale|future|invalid|scope|time|baseline|active|session|required|schema|confidence|unsupported/i) {
  let error: unknown;
  try { await operation(); } catch (caught) { error = caught; }
  assert.ok(error, 'Operation unexpectedly succeeded');
  const message = error instanceof Error ? error.message : String(error);
  assert.ok(!/ECONN|timed out|no reducer|WebSocket|fetch failed/i.test(message), `Transport/configuration failure is not an authorization denial: ${message}`);
  assert.match(message, expected, `Denial must identify a policy/authentication reason: ${message}`);
}

type Station = { id: string; session: string; macId: string; jetsonId: string; serviceId: string; simulated: boolean };
async function station(label: string, autoLock = false): Promise<Station> {
  const id = `${runId}-${++stationCounter}`;
  const s = { id, session: `${id}-s1`, macId: `${id}-mac`, jetsonId: `${id}-jetson`, serviceId: `${id}-service`, simulated: true };
  await call(owner, 'registerWorkstation', { workstationId: id, name: `SIMULATED verifier: ${label}` });
  for (const [client, deviceId, role] of [[mac, s.macId, 'mac'], [jetson, s.jetsonId, 'jetson'], [service, s.serviceId, 'service']] as const) {
    await call(owner, 'enrollDevice', { workstationId: id, deviceId, identityHex: client.identity, role, label: `SIMULATED verifier ${role}` });
  }
  await call(owner, 'grantReviewer', { workstationId: id, identityHex: reviewer.identity, canApprove: false });
  await call(owner, 'grantReviewer', { workstationId: id, identityHex: approver.identity, canApprove: true });
  await call(owner, 'setPolicy', { workstationId: id, autoLockEnabled: autoLock, lockCooldownMs: 30_000n });
  await arm(s);
  await until(() => rows(reviewer, 'mySessions', id).find(row => row.id === s.session), 'reviewer armed session');
  return s;
}

// The verifier never reads hardware. Live-arm contract setup is confined to its
// freshly owned, explicitly SIMULATED verifier workstations and never emits a live event.
async function captureFixtureBaseline(s: Station, overrides: Record<string, unknown> = {}, actor = mac) {
  await call(actor, 'recordBaseline', { workstationId: s.id, deviceId: s.macId, observedAtMs: BigInt(Date.now()), baselineJson: '["fixture:approved-usb"]', activeAppId: 'fixture:baseline-contract-test-app', ...overrides });
}
async function arm(s: Station) {
  if (!s.simulated) await captureFixtureBaseline(s);
  await call(owner, 'armSession', { workstationId: s.id, sessionId: s.session, baselineJson: JSON.stringify(['fixture:approved-usb']), approvedAppsJson: JSON.stringify(['fixture:approved-app']), durationMs: 600_000n, simulated: s.simulated });
  await until(() => rows(owner, 'mySessions', s.id).find(row => row.id === s.session), 'owner committed armed session');
}

function event(s: Station, kind: string, source: 'mac' | 'jetson' = kind.startsWith('usb_') || kind === 'app_launched' ? 'mac' : 'jetson', options: any = {}) {
  const deviceId = source === 'mac' ? s.macId : s.jetsonId;
  const seq = options.seq ?? (sequences.get(deviceId) ?? 0n) + 1n;
  sequences.set(deviceId, seq);
  // Fixture timestamps must not precede the server's committed arm time.
  // Explicit time-boundary and malformed inputs below remain unchanged.
  const session = rows(owner, 'mySessions', s.id).find(row => row.id === s.session);
  assert.ok(session, 'Fixture requires the committed armed session');
  const fixtureTime = BigInt(Date.now()) > session.armedAtMs ? BigInt(Date.now()) : session.armedAtMs;
  return {
    workstationId: s.id, sessionId: s.session, deviceId, eventId: `${runId}-event-${randomUUID()}`,
    kind, observedAtMs: BigInt(options.observedAtMs ?? fixtureTime), seq, confidence: 0.92,
    metadataJson: JSON.stringify(kind.startsWith('usb_') ? { deviceKey: 'fixture:unknown-usb', label: 'SIMULATED harmless USB' } : kind === 'app_launched' ? { appId: 'fixture:unknown-app', label: 'SIMULATED test app' } : { detail: 'SIMULATED camera metadata; no video inference' }),
    simulated: true, schemaVersion: 1, ...options,
  };
}

async function ingest(s: Station, kind: string, source?: 'mac' | 'jetson', options?: any) {
  const args = event(s, kind, source, options);
  await call((source ?? (kind.startsWith('usb_') || kind === 'app_launched' ? 'mac' : 'jetson')) === 'mac' ? mac : jetson, 'ingestDeviceEvent', args);
  await until(() => rows(owner, 'myDeviceEvents', s.id).find(row => row.id === args.eventId), 'owner event subscription');
  return args;
}

function incidents(client: AirlockClient, s: Station) { return rows(client, 'myIncidents', s.id).filter(row => row.sessionId === s.session); }
async function bothIncident(s: Station, eventId: string, start?: number) {
  const get = (client: AirlockClient) => incidents(client, s).find(row => row.digitalEventId === eventId);
  const [a, b] = await Promise.all([
    until(() => get(owner), 'owner incident').then(row => { if (start !== undefined) report.latency_ms.owner.push(+(performance.now() - start).toFixed(2)); return row; }),
    until(() => get(reviewer), 'reviewer incident').then(row => { if (start !== undefined) report.latency_ms.reviewer.push(+(performance.now() - start).toFixed(2)); return row; }),
  ]);
  assert.equal(a.id, b.id, 'Independent clients must see the same committed incident ID');
  assert.equal(a.simulated, true);
  checkpoints.push({ workstationId: s.id, sessionId: s.session, incidentId: a.id });
  return a;
}

async function propose(s: Station, suffix: string, ttlMs = 30_000n, overrides: Record<string, unknown> = {}) {
  const actionId = `${runId}-action-${suffix}-${randomUUID().slice(0, 8)}`;
  await call(service, 'proposeProtectedAction', { workstationId: s.id, actionId, missionId: `${runId}-mission`, incidentId: '', toolName: 'fixture_word_count', argsJson: JSON.stringify({ text: 'SIMULATED harmless managed callback' }), scope: 'fixture:read-only', ttlMs, ...overrides });
  return await until(() => rows(owner, 'myProtectedActions', s.id).find(row => row.id === actionId), 'proposed action');
}

async function approve(s: Station, action: any, ttlMs = 30_000n, actor = owner) {
  await call(actor, 'decideAction', { actionId: action.id, approve: true, ttlMs, note: 'SIMULATED integration verification approval' });
  const current = await until(() => rows(service, 'myProtectedActions', s.id).find(row => row.id === action.id && row.approvalId), 'approved action');
  return { actionId: current.id, approvalId: current.approvalId, actionDigest: current.actionDigest, executionId: `${runId}-exec-${randomUUID()}` };
}

function summary(samples: number[]) {
  if (!samples.length) return { n: 0 };
  const values = [...samples].sort((a, b) => a - b);
  return { n: values.length, p50: values[Math.max(0, Math.ceil(values.length * 0.5) - 1)], p95: values[Math.max(0, Math.ceil(values.length * 0.95) - 1)], max: values.at(-1) };
}

async function checkCheckpoint(path: string) {
  const checkpoint = JSON.parse(await readFile(path, 'utf8'));
  const client = await connectAirlock(checkpoint.token);
  clients.push(client);
  assert.equal(client.identity, checkpoint.identity, 'Saved token must preserve identity after server/client reconnect');
  for (const expected of checkpoint.incidents) {
    const found = rows(client, 'myIncidents', expected.workstationId).find(row => row.id === expected.incidentId);
    assert.ok(found, `Persisted incident missing: ${expected.incidentId}`);
    assert.equal(found.sessionId, expected.sessionId);
  }
  const proof = { checked_at: new Date().toISOString(), identity_preserved: true, restored_incidents: checkpoint.incidents.length, endpoint: process.env.AIRLOCK_SPACETIME_URI || 'http://127.0.0.1:3000', database: process.env.AIRLOCK_DATABASE || 'locki-airlock-sid', passed: true };
  await writeFile(`${dirname(resolve(path))}/persistence-check.json`, `${JSON.stringify(proof, null, 2)}\n`);
  console.log(`PASS checkpoint restored ${checkpoint.incidents.length} committed incidents for the same identity`);
}

async function main() {
  const checkpointArg = process.argv.indexOf('--check-checkpoint');
  if (checkpointArg !== -1) { await checkCheckpoint(process.argv[checkpointArg + 1]); return; }
  console.log(`AIRLOCK real-database verification ${runId}`);
  [owner, reviewer, approver, mac, jetson, service, outsider] = await Promise.all(Array.from({ length: 7 }, () => connectAirlock()));
  clients.push(owner, reviewer, approver, mac, jetson, service, outsider);
  // Retain only this test identity for recovery even when a later group fails.
  await mkdir(paths.output, { recursive: true });
  await writeFile(`${paths.output}/checkpoint.private.json`, JSON.stringify({ run_id: runId, identity: owner.identity, token: owner.token, incidents: [] }), { mode: 0o600 });
  report.checkpoint_file = `${paths.output}/checkpoint.private.json`;
  await test('Seven independent authenticated identities and empty outsider views', () => {
    assert.equal(new Set(clients.map(client => client.identity)).size, 7);
    assert.equal(rows(outsider, 'myIncidents').length, 0);
    assert.equal(rows(outsider, 'mySessions').length, 0);
    assert.equal(rows(outsider, 'myApprovals').length, 0);
  });

  const base = await station('authorization and publisher');
  await test('Owner arm commits; outsider and reviewer cannot arm/disarm/enroll', async () => {
    assert.equal(rows(owner, 'mySessions', base.id)[0].state, 'armed');
    for (const actor of [outsider, reviewer]) {
      await deny(() => call(actor, 'disarmSession', { workstationId: base.id }));
      await deny(() => call(actor, 'armSession', { workstationId: base.id, sessionId: `${base.id}-forged`, baselineJson: '[]', approvedAppsJson: '[]', durationMs: 60_000n, simulated: true }));
      await deny(() => call(actor, 'enrollDevice', { workstationId: base.id, deviceId: `${base.id}-forged`, identityHex: outsider.identity, role: 'mac', label: 'forged' }));
    }
  });
  await test('HTTP direct private table reads denied while authorized views stay caller-scoped', async () => {
    const schema = await new AirlockHttp({ token: owner.token }).request(`/v1/database/${encodeURIComponent(process.env.AIRLOCK_DATABASE || 'locki-airlock-sid')}/schema?version=9`);
    for (const name of ['incidents', 'device_events', 'approvals', 'sessions', 'baseline_snapshots']) {
      const definition = schema.tables.find((table: any) => table.name === name);
      assert.ok(definition, `Private table ${name} must actually exist; a typo is not a privacy proof`);
      assert.ok(Object.hasOwn(definition.table_access, 'Private'), `${name} must be declared private in the deployed module`);
    }
    for (const actor of [outsider, owner]) {
      const http = new AirlockHttp({ token: actor.token });
      for (const table of ['incidents', 'device_events', 'approvals', 'sessions', 'baseline_snapshots']) {
        await deny(() => http.sql(`SELECT * FROM ${table}`), /private|permission|not found|not exist|unauthor|not.*access|unknown.*table/i);
      }
    }
    const outsiderHttp = new AirlockHttp({ token: outsider.token });
    const result: any = await outsiderHttp.sql('SELECT * FROM my_incidents');
    assert.ok(Array.isArray(result));
    assert.equal(result.reduce((count: number, statement: any) => count + (statement.rows?.length || 0), 0), 0);
  });
  await test('Live-arm contract requires a matching enrolled Mac capture; owner, role and station forgery are denied', async () => {
    const s = await station('baseline authorization fixture');
    await call(owner, 'disarmSession', {workstationId:s.id});
    const args = {workstationId:s.id, sessionId:`${s.id}-live-contract`, baselineJson:'[]', approvedAppsJson:'[]', durationMs:60000n, simulated:false};
    await deny(() => call(owner,'armSession',args), /fresh.*baseline/i);
    await deny(() => captureFixtureBaseline(s,{},owner), /matching Mac/i);
    await deny(() => captureFixtureBaseline(s,{},outsider), /member/i);
    await deny(() => captureFixtureBaseline(s,{deviceId:s.serviceId},service), /matching Mac/i);
    await deny(() => captureFixtureBaseline(s,{deviceId:base.macId}), /matching Mac/i);
    assert.equal(rows(owner,'myBaselineSnapshots',s.id).length,0);
    assert.equal(rows(owner,'mySessions',s.id).filter(row=>row.id===args.sessionId).length,0);
  });
  await test('Baseline fixture rejects stale/future observations and private snapshots remain caller-scoped', async () => {
    const s = await station('baseline clock and privacy fixture');
    await deny(() => captureFixtureBaseline(s,{observedAtMs:BigInt(Date.now()-31000)}), /stale/i);
    await deny(() => captureFixtureBaseline(s,{observedAtMs:BigInt(Date.now()+30000)}), /future/i);
    await captureFixtureBaseline(s);
    const stored = await until(()=>rows(owner,'myBaselineSnapshots',s.id).find(row=>row.id===`latest:${s.macId}`),'owner baseline capture');
    assert.equal(stored.publisher.toHexString(),mac.identity);
    assert.equal(stored.sessionId,'');
    assert.equal(stored.activeAppId,'fixture:baseline-contract-test-app');
    await until(()=>rows(reviewer,'myBaselineSnapshots',s.id).length===1,'reviewer baseline capture');
    await until(()=>rows(mac,'myBaselineSnapshots',s.id).length===1,'own Mac baseline capture');
    assert.equal(rows(jetson,'myBaselineSnapshots',s.id).length,0);
    assert.equal(rows(outsider,'myBaselineSnapshots').length,0);
    assert.equal(rows(owner,'myDeviceHealth',s.id).filter(row=>row.component==='apps').length,0,'Active-app capture must not fabricate continuous launch-observer health');
    const outsiderRows:any = await new AirlockHttp({token:outsider.token}).sql('SELECT * FROM my_baseline_snapshots');
    assert.equal(outsiderRows.reduce((n:number,row:any)=>n+(row.rows?.length||0),0),0);
  });
  await test('Live-arm fixture freezes captured inventory and app with actual server camera-offline state', async () => {
    const s = await station('baseline freeze live-arm contract fixture');
    await call(owner,'disarmSession',{workstationId:s.id});
    await call(jetson,'reportHealth',{workstationId:s.id,deviceId:s.jetsonId,component:'camera',status:'offline',detail:'SIMULATED health-state contract input; no camera operated'});
    await captureFixtureBaseline(s);
    const args = {workstationId:s.id,sessionId:`${s.id}-live-contract`,baselineJson:'["fixture:invented-usb"]',approvedAppsJson:'[]',durationMs:60000n,simulated:false};
    await deny(()=>call(owner,'armSession',args),/does not match/i);
    await call(owner,'armSession',{...args,baselineJson:''});
    const frozen = await until(()=>rows(owner,'myBaselineSnapshots',s.id).find(row=>row.sessionId===args.sessionId),'frozen live-arm contract snapshot');
    assert.equal(frozen.baselineJson,'["fixture:approved-usb"]'); assert.equal(frozen.activeAppId,'fixture:baseline-contract-test-app');
    assert.equal(frozen.cameraStatus,'offline'); assert.equal(frozen.publisher.toHexString(),mac.identity); assert.equal(frozen.deviceId,s.macId);
    assert.equal(JSON.parse(frozen.healthJson).find((h:any)=>h.component==='camera').status,'offline');
    await captureFixtureBaseline(s,{baselineJson:'["fixture:changed-after-arm"]',activeAppId:'unknown'});
    await until(()=>rows(owner,'myBaselineSnapshots',s.id).find(row=>row.sessionId==='' && row.activeAppId==='unknown'),'updated latest capture');
    assert.deepEqual(rows(owner,'myBaselineSnapshots',s.id).find(row=>row.id===frozen.id),frozen);
    assert.equal(rows(owner,'mySessions',s.id).find(row=>row.id===args.sessionId).baselineJson,'["fixture:approved-usb"]');
    assert.equal(rows(owner,'myDeviceEvents',s.id).length,0,'No fabricated live event may be emitted by baseline contract tests');
    assert.equal(rows(owner,'myLockRequests',s.id).length,0);
    await call(owner,'disarmSession',{workstationId:s.id});
  });
  await test('Live-arm contract rejects offline USB, newly aged captures and revoked capture publishers', async () => {
    const s = await station('baseline freshness and revocation fixture');
    await call(owner,'disarmSession',{workstationId:s.id});
    const args = {workstationId:s.id,sessionId:`${s.id}-live-contract`,baselineJson:'',approvedAppsJson:'[]',durationMs:60000n,simulated:false};
    await captureFixtureBaseline(s);
    await call(mac,'reportHealth',{workstationId:s.id,deviceId:s.macId,component:'usb',status:'offline',detail:'SIMULATED contract failure state'});
    await deny(()=>call(owner,'armSession',args),/fresh.*baseline/i);
    const aged = await station('baseline aged-capture fixture');
    await call(owner,'disarmSession',{workstationId:aged.id});
    await captureFixtureBaseline(aged,{observedAtMs:BigInt(Date.now()-29000)});
    await new Promise(resolve=>setTimeout(resolve,1250));
    await deny(()=>call(owner,'armSession',{...args,workstationId:aged.id,sessionId:`${aged.id}-live-contract`}),/fresh.*baseline/i);
    await captureFixtureBaseline(s);
    await call(owner,'revokeMember',{workstationId:s.id,identityHex:mac.identity});
    await deny(()=>call(owner,'armSession',args),/fresh.*baseline/i);
    assert.equal(rows(owner,'mySessions',s.id).filter(row=>row.id===args.sessionId).length,0);
  });
  await test('Unenrolled and wrong-role publishers cannot forge sensor evidence', async () => {
    const args = event(base, 'usb_added');
    await deny(() => call(outsider, 'ingestDeviceEvent', args));
    await deny(() => call(service, 'ingestDeviceEvent', { ...args, deviceId: base.serviceId }));
    await deny(() => call(mac, 'ingestDeviceEvent', { ...args, kind: 'person_in_zone', metadataJson: '{}' }));
    await deny(() => call(mac, 'ingestDeviceEvent', { ...args, simulated: false }));
  });
  await test('Fixture publisher preserves simulated provenance and reaches actual reducer', async () => {
    const fixture = makeFixture({ workstationId: base.id, sessionId: base.session, kind: 'heartbeat', seq: 1 });
    assert.throws(() => validateSecurityEvent({ ...fixture, simulated: false }), /simulated/);
    await publishSecurityEvent(fixture, { token: owner.token });
    const stored = await until(() => rows(owner, 'myDeviceEvents', base.id).find(row => row.id === fixture.event_id), 'HTTP fixture committed row');
    assert.equal(stored.simulated, true);
  });
  await test('Approved USB and approved app remain informational without incidents', async () => {
    const usb = await ingest(base, 'usb_added', 'mac', { metadataJson: JSON.stringify({ deviceKey: 'fixture:approved-usb', label: 'SIMULATED approved USB' }) });
    await ingest(base, 'app_launched', 'mac', { metadataJson: JSON.stringify({ appId: 'fixture:approved-app', label: 'SIMULATED approved app' }) });
    assert.equal(incidents(owner, base).length, 0);
    const stored = rows(owner, 'myDeviceEvents', base.id).find(row => row.id === usb.eventId);
    assert.match(stored.classification, /info|approved/i);
  });
  await test('Camera proximity alone stays a soft observation without lock', async () => {
    await ingest(base, 'person_in_zone');
    assert.equal(incidents(owner, base).length, 0);
    assert.equal(rows(owner, 'myLockRequests', base.id).length, 0);
  });

  const correlationStations: Array<{ station: Station; order: 'camera-first' | 'usb-first'; delta: number; observed: number }> = [];
  for (const order of ['camera-first', 'usb-first'] as const) {
    for (const delta of [-10_001, -10_000, -9_999, 0, 9_999, 10_000, 10_001]) {
      const s = await station(`${order} delta ${delta}`);
      const armed = rows(owner, 'mySessions', s.id).find(row => row.id === s.session);
      correlationStations.push({ station: s, order, delta, observed: Number(armed.armedAtMs) + 11_000 });
    }
  }
  // These timestamps must belong to genuinely armed sessions. Age all vectors
  // together instead of inventing a test-only clock bypass in the module.
  const readyAt = Math.max(...correlationStations.map(vector => vector.observed + Math.max(0, vector.delta))) + 100;
  const ageMs = Math.max(0, readyAt - Date.now());
  console.log(`Aging armed sessions ${ageMs}ms for real 10-second boundary vectors`);
  if (ageMs) await new Promise(resolve => setTimeout(resolve, ageMs));
  for (const vector of correlationStations) {
    const { station: s, order, delta, observed } = vector;
      await test(`Correlation ${order}, camera offset ${delta}ms: ${Math.abs(delta) <= 10_000 ? 'same high incident' : 'medium only'}`, async () => {
        const camera = event(s, 'person_in_zone', 'jetson', { observedAtMs: BigInt(observed + delta) });
        const usb = event(s, 'usb_added', 'mac', { observedAtMs: BigInt(observed) });
        let publishStarted: number;
        let originalId: string | undefined;
        if (order === 'camera-first') {
          await call(jetson, 'ingestDeviceEvent', camera);
          assert.equal(incidents(owner, s).length, 0);
          publishStarted = performance.now();
          await call(mac, 'ingestDeviceEvent', usb);
        } else {
          publishStarted = performance.now();
          await call(mac, 'ingestDeviceEvent', usb);
          const original = await bothIncident(s, usb.eventId, publishStarted);
          originalId = original.id;
          assert.equal(original.severity, 'medium');
          await call(jetson, 'ingestDeviceEvent', camera);
        }
        const correlated = Math.abs(delta) <= 10_000;
        const row = await bothIncident(s, usb.eventId, order === 'camera-first' ? publishStarted : undefined);
        if (correlated) await Promise.all([
          until(() => incidents(owner, s).find(item => item.id === row.id && item.severity === 'high'), 'owner correlation upgrade'),
          until(() => incidents(reviewer, s).find(item => item.id === row.id && item.severity === 'high'), 'reviewer correlation upgrade'),
        ]);
        const final = incidents(owner, s).find(item => item.id === row.id);
        assert.equal(final.severity, correlated ? 'high' : 'medium');
        assert.equal(incidents(owner, s).length, 1);
        if (originalId) assert.equal(final.id, originalId, 'USB-first correlation must upgrade the existing incident');
        assert.equal(final.physicalEventId, correlated ? camera.eventId : '');
        assert.equal(rows(owner, 'myLockRequests', s.id).length, 0, 'No automatic lock without owner opt-in');
      });
  }

  await test('Idle sensor health becomes offline through the server timer without a refresh call', async () => {
    // The base station received no reducer call during the shared >21s wait.
    const health = await until(() => rows(owner, 'myDeviceHealth', base.id).find(row => row.deviceId === base.jetsonId && row.status === 'offline'), 'autonomous server-side health expiry', 6_000);
    assert.match(health.detail, /heartbeat|stale|15 seconds/i);
    assert.ok(rows(reviewer, 'myDeviceHealth', base.id).some(row => row.id === health.id && row.status === 'offline'));
  });

  const replayStation = await station('replay and malformed events');
  let replayEvent: any, replayIncident: any;
  await test('Unknown USB alone opens the same medium incident on two clients', async () => {
    const start = performance.now();
    replayEvent = await ingest(replayStation, 'usb_added');
    replayIncident = await bothIncident(replayStation, replayEvent.eventId, start);
    assert.equal(replayIncident.severity, 'medium');
  });
  await test('Exact replay creates no second evidence row or incident', async () => {
    const count = rows(owner, 'myDeviceEvents', replayStation.id).length;
    await call(mac, 'ingestDeviceEvent', replayEvent);
    assert.equal(rows(owner, 'myDeviceEvents', replayStation.id).length, count);
    assert.equal(incidents(owner, replayStation).length, 1);
    assert.equal(incidents(owner, replayStation)[0].id, replayIncident.id);
  });
  await test('Reusing an event ID with modified payload is rejected', async () => {
    await deny(() => call(mac, 'ingestDeviceEvent', { ...replayEvent, metadataJson: JSON.stringify({ deviceKey: 'fixture:changed-usb' }) }), /conflict|replay|differ/i);
    assert.equal(incidents(owner, replayStation).length, 1);
  });
  await test('Repeated polling observations of one unknown USB merge into one unresolved case', async () => {
    const s = await station('grouped USB observations');
    const a = await ingest(s, 'usb_added');
    const original = await bothIncident(s, a.eventId);
    const b = await ingest(s, 'usb_added');
    await until(() => incidents(owner, s).find(row => row.id === original.id && JSON.parse(row.eventIdsJson).includes(b.eventId)), 'grouped evidence update');
    assert.equal(incidents(owner, s).length, 1);
    assert.equal(incidents(owner, s)[0].id, original.id);
    const different = await ingest(s, 'usb_added', 'mac', { metadataJson: JSON.stringify({ deviceKey: 'fixture:different-unknown-usb' }) });
    await bothIncident(s, different.eventId);
    assert.equal(incidents(owner, s).length, 2, 'A different USB key must not disappear into the original case');
    await call(owner, 'resolveIncident', { incidentId: original.id, note: 'SIMULATED first USB verified and removed' });
    const again = await ingest(s, 'usb_added');
    const next = await bothIncident(s, again.eventId);
    assert.notEqual(next.id, original.id, 'A later observation after human resolution opens a fresh case');
  });
  await test('Regressed sequences, stale/future times, invalid schema/confidence denied', async () => {
    for (const overrides of [
      { seq: replayEvent.seq }, { observedAtMs: BigInt(Date.now() - 180_000) },
      { observedAtMs: BigInt(Date.now() + 30_000) }, { schemaVersion: 2 }, { confidence: 1.01 },
    ]) await deny(() => call(mac, 'ingestDeviceEvent', event(replayStation, 'usb_added', 'mac', overrides)));
    assert.equal(incidents(owner, replayStation).length, 1);
  });
  await test('Bounded redacted metadata contract rejects malformed, raw, nested and oversized payloads', async () => {
    const metadataVectors = [
      'not json', '[]', 'null',
      JSON.stringify({ deviceKey: 7 }),
      JSON.stringify({ deviceKey: 'fixture:unknown-usb', rawFrame: 'untrusted image bytes' }),
      JSON.stringify({ deviceKey: 'fixture:unknown-usb', detail: { nested: 'untrusted object' } }),
      JSON.stringify({ deviceKey: 'x'.repeat(2_100) }),
    ];
    for (const metadataJson of metadataVectors) await deny(() => call(mac, 'ingestDeviceEvent', event(replayStation, 'usb_added', 'mac', { metadataJson })), /json|metadata|payload|bounded|deviceKey|object|expected|token|unexpected/i);
    await deny(() => call(mac, 'ingestDeviceEvent', event(replayStation, 'usb_added', 'mac', { seq: 1_000_100n })), /seq|jump/i);
    await deny(() => call(mac, 'ingestDeviceEvent', event(replayStation, 'usb_added', 'mac', { eventId: '../../forged-event' })), /eventId|id|letters|invalid/i);
    assert.equal(incidents(owner, replayStation).length, 1);
  });
  await test('Unauthorized review/resolve denied; reviewer acknowledgement commits on both clients', async () => {
    await deny(() => call(outsider, 'acknowledgeIncident', { incidentId: replayIncident.id, note: 'forged' }));
    await deny(() => call(reviewer, 'resolveIncident', { incidentId: replayIncident.id, note: 'forged resolution' }));
    await call(reviewer, 'acknowledgeIncident', { incidentId: replayIncident.id, note: 'SIMULATED reviewer verified fixture' });
    await until(() => incidents(owner, replayStation).find(row => row.id === replayIncident.id && row.state === 'acknowledged'), 'owner acknowledgement subscription');
    await call(owner, 'requestIncidentInfo', { incidentId: replayIncident.id, note: 'SIMULATED request evidence' });
    await until(() => incidents(reviewer, replayStation).find(row => row.id === replayIncident.id && row.state === 'reviewing'), 'reviewer request-info subscription');
  });
  await test('No owner policy consent means explicit lock request denied', async () => {
    await deny(() => call(owner, 'requestLock', { incidentId: replayIncident.id }), /consent|policy|enabled|simulat|lock/i);
    assert.equal(rows(owner, 'myLockRequests', replayStation.id).length, 0);
  });
  await test('Simulated sessions cannot emit a real lock even with configured opt-in', async () => {
    const s = await station('simulated lock isolation', true);
    const usb = await ingest(s, 'usb_added');
    const incident = await bothIncident(s, usb.eventId);
    try { await call(owner, 'requestLock', { incidentId: incident.id }); } catch (error) { assert.match(String(error), /simulat|lock|cooldown/i); }
    for (const request of rows(owner, 'myLockRequests', s.id)) assert.equal(request.simulated, true);
  });
  await test('Camera offline degrades coverage while independent USB protection remains active', async () => {
    const s = await station('camera offline');
    await ingest(s, 'camera_offline');
    const usb = await ingest(s, 'usb_added');
    const incident = await bothIncident(s, usb.eventId);
    assert.equal(incident.severity, 'medium');
    assert.equal(incident.physicalEventId, '');
    assert.ok(rows(owner, 'myDeviceHealth', s.id).some(row => ['offline', 'unavailable', 'degraded'].includes(row.status)));
  });
  await test('Physical evidence never crosses workstation scope', async () => {
    const a = await station('cross workstation camera');
    const b = await station('cross workstation USB');
    await ingest(a, 'person_in_zone');
    const usb = await ingest(b, 'usb_added');
    const incident = await bothIncident(b, usb.eventId);
    assert.equal(incident.severity, 'medium');
    assert.equal(incident.physicalEventId, '');
    assert.equal(incidents(owner, a).length, 0);
    await deny(() => call(mac, 'ingestDeviceEvent', { ...event(a, 'usb_added'), workstationId: b.id, sessionId: b.session }));
  });
  await test('Physical evidence never crosses a disarm/rearm session boundary', async () => {
    const s = await station('cross session');
    await ingest(s, 'person_in_zone');
    await call(owner, 'disarmSession', { workstationId: s.id });
    const oldSession = s.session;
    s.session = `${s.id}-s2`;
    await arm(s);
    await deny(() => call(mac, 'ingestDeviceEvent', event(s, 'usb_added', 'mac', { sessionId: oldSession })));
    const usb = await ingest(s, 'usb_added');
    const incident = await bothIncident(s, usb.eventId);
    assert.equal(incident.severity, 'medium');
    assert.equal(incident.physicalEventId, '');
  });

  const actionStation = await station('protected approvals');
  await test('Unauthorized/reviewer-without-grant approvals and self approvals denied', async () => {
    const action = await propose(actionStation, 'role-denials');
    for (const actor of [outsider, reviewer, service]) await deny(() => call(actor, 'decideAction', { actionId: action.id, approve: true, ttlMs: 30_000n, note: 'forged' }));
    const ownActionId = `${runId}-owner-action`;
    await call(owner, 'proposeProtectedAction', { workstationId: actionStation.id, actionId: ownActionId, missionId: `${runId}-mission`, incidentId: '', toolName: 'fixture_word_count', argsJson: '{"text":"fixture"}', scope: 'fixture:read-only', ttlMs: 30_000n });
    await deny(() => call(owner, 'decideAction', { actionId: ownActionId, approve: true, ttlMs: 30_000n, note: 'self' }));
  });
  await test('Modified digest and wrong approval denied before any managed callback', async () => {
    const action = await propose(actionStation, 'digest');
    const args = await approve(actionStation, action);
    let callbackCount = 0;
    await deny(async () => { await call(service, 'consumeApproval', { ...args, actionDigest: `${args.actionDigest[0] === '0' ? '1' : '0'}${args.actionDigest.slice(1)}` }); callbackCount++; });
    await deny(async () => { await call(service, 'consumeApproval', { ...args, approvalId: `${args.approvalId}-modified` }); callbackCount++; });
    await deny(() => call(owner, 'consumeApproval', args));
    assert.equal(callbackCount, 0);
    await call(service, 'consumeApproval', args);
    callbackCount++;
    await deny(async () => { await call(service, 'consumeApproval', { ...args, executionId: `${args.executionId}-replay` }); callbackCount++; });
    assert.equal(callbackCount, 1, 'Exactly one authorization reaches the harmless managed callback');
    await call(service, 'reportActionResult', { actionId: action.id, executionId: args.executionId, status: 'succeeded', detail: 'SIMULATED harmless verifier callback returned once' });
    const racing = await propose(actionStation, 'concurrent-consumption');
    const raceArgs = await approve(actionStation, racing);
    const outcomes = await Promise.allSettled([
      call(service, 'consumeApproval', raceArgs),
      call(service, 'consumeApproval', { ...raceArgs, executionId: `${raceArgs.executionId}-race` }),
    ]);
    assert.equal(outcomes.filter(outcome => outcome.status === 'fulfilled').length, 1, 'Concurrent consumers must obtain exactly one committed authorization');
    assert.equal(outcomes.filter(outcome => outcome.status === 'rejected').length, 1);
  });
  await test('Explicit approval-capable reviewer can approve, ordinary reviewer cannot', async () => {
    const action = await propose(actionStation, 'delegated-review');
    const args = await approve(actionStation, action, 30_000n, approver);
    await call(service, 'consumeApproval', args);
  });
  await test('Expired approval is denied transactionally without callback', async () => {
    const action = await propose(actionStation, 'expired', 2_000n);
    const args = await approve(actionStation, action, 1_000n);
    await new Promise(resolve => setTimeout(resolve, 1_150));
    const grant = rows(service,'myApprovals',actionStation.id).find(row=>row.id===args.approvalId);
    assert.ok(grant && grant.expiresAtMs <= BigInt(Date.now()), 'The exact tested grant must be past its committed expiry');
    assert.ok(['approved','expired'].includes(grant.status), 'Only pending expiry or autonomous scheduler expiry is acceptable');
    assert.equal(grant.consumedAtMs,0n);
    let callbackCount = 0;
    // The durable timer may already have changed approved to expired; consumption
    // then fails the earlier unused-grant guard rather than its later time guard.
    await deny(async () => { await call(service, 'consumeApproval', args); callbackCount++; }, /expired|expiry|time|valid|unused approval/i);
    assert.equal(callbackCount, 0);
  });
  await test('Cross-action approval substitution is denied', async () => {
    const a = await propose(actionStation, 'cross-action-a');
    const b = await propose(actionStation, 'cross-action-b');
    const argsA = await approve(actionStation, a);
    const argsB = await approve(actionStation, b);
    await deny(() => call(service, 'consumeApproval', { ...argsB, approvalId: argsA.approvalId }));
  });
  await test('Revoked approval authority invalidates an outstanding grant before execution', async () => {
    const s = await station('revoked approver');
    const action = await propose(s, 'revoked-approver');
    const args = await approve(s, action, 30_000n, approver);
    await call(owner, 'revokeMember', { workstationId: s.id, identityHex: approver.identity });
    await deny(() => call(service, 'consumeApproval', args), /approv|revok|authoriz|member|permission/i);
  });
  await test('Withdrawing reviewer approval permission invalidates an outstanding grant', async () => {
    const s = await station('downgraded approver');
    const action = await propose(s, 'downgraded-approver');
    const args = await approve(s, action, 30_000n, approver);
    await call(owner, 'grantReviewer', { workstationId: s.id, identityHex: approver.identity, canApprove: false });
    await deny(() => call(service, 'consumeApproval', args), /approv|revok|authoriz|member|permission/i);
  });
  await test('A second enrolled service cannot consume another service proposal', async () => {
    const second = await connectAirlock(); clients.push(second);
    await call(owner, 'enrollDevice', { workstationId: actionStation.id, deviceId: `${actionStation.id}-second-service`, identityHex: second.identity, role: 'service', label: 'SIMULATED independently enrolled second service' });
    const action = await propose(actionStation, 'service-binding');
    const args = await approve(actionStation, action);
    await deny(() => call(second, 'consumeApproval', args), /service|propos|identity|request/i);
    await call(service, 'consumeApproval', args);
  });
  await test('Managed runner rejects unknown tools and executes three actual approved read-only callbacks', async () => {
    assert.throws(() => managedTool('run_shell', { command: 'untrusted README request' }), /DENIED.*callback not executed/);
    const specs = [
      { station: actionStation, tool: 'fixture_word_count', args: { text: 'harmless test fixture' }, incidentId: '' },
      { station: replayStation, tool: 'read_incident', args: { incidentId: replayIncident.id }, incidentId: replayIncident.id },
      { station: replayStation, tool: 'read_health', args: {}, incidentId: '' },
    ];
    for (const spec of specs) {
      const action = await propose(spec.station, `gateway-${spec.tool}`, 30_000n, { toolName: spec.tool, argsJson: JSON.stringify(spec.args), incidentId: spec.incidentId, scope: 'airlock:read-only' });
      await approve(spec.station, action);
      const outcome = await executeApprovedAction(service, action.id);
      assert.equal(outcome.callback_executed, true);
      assert.equal(outcome.result.tool, spec.tool);
      if (spec.tool === 'fixture_word_count') assert.equal(outcome.result.words, 3);
      if (spec.tool === 'read_incident') assert.equal(outcome.result.incident_id, replayIncident.id);
      if (spec.tool === 'read_health') assert.ok(Array.isArray(outcome.result.components));
      await deny(() => executeApprovedAction(service, action.id));
    }
  });
  await test('Owner resolution commits, and revoked reviewer loses current read access', async () => {
    await call(owner, 'resolveIncident', { incidentId: replayIncident.id, note: 'SIMULATED fixture removed and owned workstation checked' });
    await until(() => incidents(reviewer, replayStation).find(row => row.id === replayIncident.id && row.state === 'resolved'), 'resolution on reviewer');
    await call(owner, 'revokeMember', { workstationId: replayStation.id, identityHex: reviewer.identity });
    await until(() => rows(reviewer, 'myIncidents', replayStation.id).length === 0, 'revocation removes view rows');
    await deny(() => call(reviewer, 'acknowledgeIncident', { incidentId: replayIncident.id, note: 'after revoke' }));
  });
  await test('Fresh connection with the same token restores committed incident snapshot', async () => {
    const originalIdentity = owner.identity;
    owner.conn.disconnect();
    const recovered = await connectAirlock(owner.token);
    clients.push(recovered);
    assert.equal(recovered.identity, originalIdentity);
    for (const checkpoint of checkpoints) assert.ok(rows(recovered, 'myIncidents', checkpoint.workstationId).some(row => row.id === checkpoint.incidentId));
    owner = recovered;
  });

  await mkdir(paths.output, { recursive: true });
  const checkpoint = { run_id: runId, identity: owner.identity, token: owner.token, incidents: checkpoints };
  await writeFile(`${paths.output}/checkpoint.private.json`, `${JSON.stringify(checkpoint, null, 2)}\n`, { mode: 0o600 });
  report.checkpoint_file = `${paths.output}/checkpoint.private.json`;
  report.latency_summary_ms = { owner: summary(report.latency_ms.owner), reviewer: summary(report.latency_ms.reviewer) };
  console.log(`Measured USB publication-to-subscribed-cache latency: ${JSON.stringify(report.latency_summary_ms)}`);
  console.log(`Persistence checkpoint (private token file, mode 0600): ${report.checkpoint_file}`);
}

async function run() {
  try { await main(); } catch (error) { process.exitCode = 1; console.error(error instanceof Error ? error.stack : error); }
  finally {
    if (owner && !process.argv.includes('--check-checkpoint')) {
      const cleanup = { disarmed: 0, failures: [] as string[], os_adapter_invocations: 0 };
      for (const station of rows(owner, 'myWorkstations').filter(row => row.id.startsWith(`${runId}-`))) {
        try { await call(owner, 'disarmSession', { workstationId: station.id }); cleanup.disarmed++; }
        catch (error) { cleanup.failures.push(`${station.id}: ${error instanceof Error ? error.message : String(error)}`); }
      }
      report.cleanup = cleanup;
      if (cleanup.failures.length) process.exitCode = 1;
    }
    closeAirlock(clients);
    report.finished_at = new Date().toISOString();
    report.passed = !process.exitCode;
    if (!process.argv.includes('--check-checkpoint')) {
      await mkdir(paths.output, { recursive: true });
      await writeFile(`${paths.output}/report.json`, `${JSON.stringify(report, null, 2)}\n`);
      console.log(`Verification report: ${paths.output}/report.json`);
    }
  }
}
void run();
