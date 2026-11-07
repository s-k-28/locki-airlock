#!/usr/bin/env node
/** Focused real-DB Mac observation-access proof. Synthetic fixtures; no OS adapter. */
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { AirlockHttp } from './airlock_http.mjs';
import { call, closeAirlock, connectAirlock, rows, until, type AirlockClient } from './airlock_sdk';

const runId = `mac-views-${Date.now()}-${randomUUID().slice(0, 8)}`;
const clients: AirlockClient[] = [];
const stations: string[] = [];
const report: any = { run_id: runId, database: process.env.AIRLOCK_DATABASE || 'locki-airlock-sid', started_at: new Date().toISOString(),
  provenance: 'Synthetic test-owned station/events only; no camera, microphone, lock adapter, OS action or execution claim', tests: [], ids: {} };
function errorMessage(error: unknown): string {
  let message = error instanceof Error ? error.message : String(error);
  for (const client of clients) if (client.token) message = message.split(client.token).join('[redacted]');
  return message.replace(/Bearer\s+\S+/gi, 'Bearer [redacted]').replace(/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/g, '[redacted]').replace(/[\r\n]+/g, ' ').slice(0, 400);
}
async function test(name: string, fn: () => Promise<void>) {
  try { await fn(); report.tests.push({ name, passed: true }); console.log(`PASS ${name}`); }
  catch (error) { report.tests.push({ name, passed: false, error_type: error instanceof Error ? error.name : 'Error' }); throw error; }
}
async function denied(operation: () => Promise<unknown>) {
  let caught: unknown; try { await operation(); } catch (error) { caught = error; }
  assert.ok(caught, 'Unauthorized reducer unexpectedly succeeded');
  assert.match(caught instanceof Error ? caught.message : String(caught), /owner|reviewer|member|enroll|permission|required|device/i);
}
async function httpIds(actor: AirlockClient, view: string, ws: string): Promise<string[]> {
  const result: any = await new AirlockHttp({ token: actor.token }).sql(`SELECT * FROM ${view} WHERE workstation_id = '${ws}'`);
  return result.flatMap((statement: any) => {
    const index = statement.schema.elements.findIndex((field: any) => field.name.some === 'id');
    assert.ok(index >= 0); return statement.rows.map((row: any[]) => String(row[index]));
  }).sort();
}
const sdkIds = (actor: AirlockClient, view: string, ws: string) => rows(actor, view, ws).map(row => String(row.id)).sort();
async function emptyObservations(actor: AirlockClient, ws: string) {
  await until(() => sdkIds(actor, 'myIncidents', ws).length === 0 && sdkIds(actor, 'myDeviceEvents', ws).length === 0, 'observation rows removed');
  assert.deepEqual(await httpIds(actor, 'my_incidents', ws), []); assert.deepEqual(await httpIds(actor, 'my_device_events', ws), []);
}
async function station(owner: AirlockClient, ws: string, autoLock = false) {
  await call(owner, 'registerWorkstation', { workstationId: ws, name: 'SIMULATED Mac-view authorization proof' }); stations.push(ws);
  await call(owner, 'setPolicy', { workstationId: ws, autoLockEnabled: autoLock, lockCooldownMs: 30000n });
}
async function arm(owner: AirlockClient, ws: string) {
  await call(owner, 'armSession', { workstationId: ws, sessionId: `${ws}-session`, baselineJson: '[]', approvedAppsJson: '[]', durationMs: 600000n, simulated: true });
  const session = await until(() => rows(owner, 'mySessions', ws).find(row => row.id === `${ws}-session` && row.state === 'armed'), 'committed synthetic session fence');
  assert.equal(session.simulated, true); return session.armedAtMs as bigint;
}
async function main() {
  // Sixth identity only creates a nonempty approval row, making negative read checks meaningful.
  const [owner, mac, secondMac, jetson, outsider, approver] = await Promise.all(Array.from({ length: 6 }, () => connectAirlock()));
  clients.push(owner, mac, secondMac, jetson, outsider, approver);
  const ws = `${runId}-desk`, unrelated = `${runId}-unrelated`, deviceIds = [`${runId}-mac-a`, `${runId}-mac-b`, `${runId}-jetson`];
  report.independent_identities = new Set(clients.map(client => client.identity)).size; assert.equal(report.independent_identities, 6);
  report.ids.workstation = ws; report.ids.unrelated_workstation = unrelated;
  await station(owner, ws, true);
  for (const [actor, id, role] of [[mac, deviceIds[0], 'mac'], [secondMac, deviceIds[1], 'mac'], [jetson, deviceIds[2], 'jetson']] as const)
    await call(owner, 'enrollDevice', { workstationId: ws, deviceId: id, identityHex: actor.identity, role, label: `SIMULATED ${role} view fixture` });
  // Synthetic test evidence is fenced to the server's committed armed timestamp.
  const observedAtMs = await arm(owner, ws); report.fixture_clock_source = 'committed synthetic session armedAtMs';
  const usbId = `${runId}-usb`, physicalId = `${runId}-physical`;
  await call(owner, 'ingestDeviceEvent', { workstationId: ws, sessionId: `${ws}-session`, deviceId: 'fixture', eventId: usbId, kind: 'usb_added', observedAtMs, seq: 1n, confidence: 1, metadataJson: '{"deviceKey":"fixture:harmless-usb"}', simulated: true, schemaVersion: 1 });
  await call(jetson, 'ingestDeviceEvent', { workstationId: ws, sessionId: `${ws}-session`, deviceId: deviceIds[2], eventId: physicalId, kind: 'person_in_zone', observedAtMs, seq: 1n, confidence: 1, metadataJson: '{"detail":"SIMULATED zone input, no camera opened"}', simulated: true, schemaVersion: 1 });
  const incident = await until(() => rows(owner, 'myIncidents', ws).find(row => row.digitalEventId === usbId && row.severity === 'high'), 'owner correlated incident');
  const lock = await until(() => rows(owner, 'myLockRequests', ws).find(row => row.incidentId === incident.id), 'synthetic targeted lock request');
  assert.equal(lock.simulated, true); assert.ok(deviceIds.slice(0, 2).includes(lock.targetDeviceId));
  const target = lock.targetDeviceId === deviceIds[0] ? mac : secondMac, otherMac = target === mac ? secondMac : mac;
  report.ids = { ...report.ids, usb_event: usbId, physical_event: physicalId, incident: incident.id, lock_request: lock.id, target_device: lock.targetDeviceId };
  await test('Both enrolled Macs receive the same correlated observations through SDK and HTTP', async () => {
    assert.deepEqual(await httpIds(owner, 'my_incidents', ws), [incident.id]); assert.deepEqual(await httpIds(owner, 'my_device_events', ws), [usbId, physicalId].sort());
    for (const actor of [mac, secondMac]) {
      await until(() => rows(actor, 'myIncidents', ws).find(row => row.id === incident.id && row.severity === 'high' && row.physicalEventId === physicalId), 'Mac correlated incident');
      await until(() => rows(actor, 'myDeviceEvents', ws).filter(row => [usbId, physicalId].includes(row.id)).length === 2, 'Mac USB and Jetson events');
      assert.deepEqual(sdkIds(actor, 'myIncidents', ws), sdkIds(owner, 'myIncidents', ws));
      assert.deepEqual(sdkIds(actor, 'myDeviceEvents', ws), sdkIds(owner, 'myDeviceEvents', ws));
      assert.deepEqual(await httpIds(actor, 'my_incidents', ws), [incident.id]);
      assert.deepEqual(await httpIds(actor, 'my_device_events', ws), [usbId, physicalId].sort());
    }
  });
  await test('Only the target Mac receives its lock queue', async () => {
    await until(() => rows(target, 'myLockRequests', ws).find(row => row.id === lock.id), 'target lock queue');
    assert.deepEqual(await httpIds(target, 'my_lock_requests', ws), [lock.id]);
    assert.deepEqual(sdkIds(otherMac, 'myLockRequests', ws), []); assert.deepEqual(await httpIds(otherMac, 'my_lock_requests', ws), []);
  });
  await test('Outsider and Jetson have no incident/event reads', async () => {
    await emptyObservations(outsider, ws); await emptyObservations(jetson, ws);
  });
  await test('Mac observation access never grants acknowledgement or disarm', async () => {
    for (const actor of [mac, secondMac]) { await denied(() => call(actor, 'acknowledgeIncident', { incidentId: incident.id, note: 'forbidden fixture acknowledgement' })); await denied(() => call(actor, 'disarmSession', { workstationId: ws })); }
  });
  await test('Protected actions, approvals and audit stay hidden from Macs', async () => {
    const actionId = `${runId}-action`;
    await call(owner, 'proposeProtectedAction', { workstationId: ws, actionId, missionId: `${runId}-mission`, incidentId: incident.id, toolName: 'fixture_word_count', argsJson: '{"text":"fixture"}', scope: 'fixture:read-only', ttlMs: 120000n });
    await call(owner, 'grantReviewer', { workstationId: ws, identityHex: approver.identity, canApprove: true });
    await call(approver, 'decideAction', { actionId, approve: true, ttlMs: 60000n, note: 'AUTOMATED fixture approval; no callback executes' });
    await until(() => rows(owner, 'myApprovals', ws).length > 0, 'nonempty owner approval');
    assert.ok(rows(owner, 'myProtectedActions', ws).length > 0); assert.ok(rows(owner, 'myAuditEvents', ws).length > 0);
    for (const actor of [mac, secondMac]) for (const [sdk, sql] of [['myProtectedActions', 'my_protected_actions'], ['myApprovals', 'my_approvals'], ['myAuditEvents', 'my_audit_events']]) { assert.deepEqual(sdkIds(actor, sdk, ws), []); assert.deepEqual(await httpIds(actor, sql, ws), []); }
  });
  await test('An unrelated workstation remains invisible', async () => {
    await station(owner, unrelated); const unrelatedObservedAtMs = await arm(owner, unrelated);
    await call(owner, 'ingestDeviceEvent', { workstationId: unrelated, sessionId: `${unrelated}-session`, deviceId: 'fixture', eventId: `${runId}-unrelated-usb`, kind: 'usb_added', observedAtMs: unrelatedObservedAtMs, seq: 1n, confidence: 1, metadataJson: '{"deviceKey":"fixture:unrelated-usb"}', simulated: true, schemaVersion: 1 });
    await until(() => rows(owner, 'myIncidents', unrelated).length === 1, 'unrelated owner incident');
    for (const actor of [mac, secondMac, jetson, outsider]) await emptyObservations(actor, unrelated);
  });
  await test('Revocation removes Mac observations and token reconnect cannot restore them', async () => {
    await call(owner, 'revokeMember', { workstationId: ws, identityHex: otherMac.identity }); await emptyObservations(otherMac, ws);
    otherMac.conn.disconnect(); const reconnected = await connectAirlock(otherMac.token); clients.push(reconnected); assert.equal(reconnected.identity, otherMac.identity); await emptyObservations(reconnected, ws);
  });
  await test('Device reassignment removes the old Mac observations across reconnect', async () => {
    await call(owner, 'disarmSession', { workstationId: ws });
    await call(owner, 'enrollDevice', { workstationId: ws, deviceId: lock.targetDeviceId, identityHex: outsider.identity, role: 'mac', label: 'SIMULATED replacement identity' });
    await emptyObservations(target, ws); target.conn.disconnect(); const reconnected = await connectAirlock(target.token); clients.push(reconnected); assert.equal(reconnected.identity, target.identity); await emptyObservations(reconnected, ws);
    await until(() => rows(outsider, 'myIncidents', ws).find(row => row.id === incident.id), 'explicitly enrolled replacement reads');
    await emptyObservations(outsider, unrelated);
  });
  report.counts = { observation_events: 2, correlated_incidents: 1, synthetic_lock_requests: 1, os_adapter_invocations: 0, execution_claims: 0 };
}
void (async () => {
  let failed = false;
  try { await main(); } catch (error) { failed = true; report.error_type = error instanceof Error ? error.name : 'Error'; report.error_message = errorMessage(error); console.error(`Mac view verification failed: ${report.error_type}: ${report.error_message}`); }
  finally {
    const owner = clients[0]; report.cleanup = { disarmed_stations: 0, failures: 0 };
    if (owner) for (const ws of stations) try { await call(owner, 'disarmSession', { workstationId: ws }); report.cleanup.disarmed_stations++; } catch { report.cleanup.failures++; failed = true; }
    closeAirlock(clients); report.passed = !failed; report.finished_at = new Date().toISOString();
    const directory = resolve('data/airlock-mac-views-proof', runId); await mkdir(directory, { recursive: true, mode: 0o700 });
    await writeFile(`${directory}/report.json`, `${JSON.stringify(report, null, 2)}\n`, { mode: 0o600 });
    console.log(JSON.stringify({ passed: report.passed, groups_passed: report.tests.filter((t: any) => t.passed).length, groups_total: report.tests.length, report: `data/airlock-mac-views-proof/${runId}/report.json` }));
    if (failed) process.exitCode = 1;
  }
})();
