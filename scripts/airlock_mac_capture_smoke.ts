/** Actual read-only Mac capture → enrolled baseline → frozen live session. Never locks or opens a sensor. */
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { connectAirlock, call, rows, until, closeAirlock, type AirlockClient } from './airlock_sdk';

async function main() {
const run = `capture-${Date.now()}-${randomUUID().slice(0, 6)}`;
const directory = resolve('data/airlock-capture-proof', run);
const clients: AirlockClient[] = [];
let child: ReturnType<typeof spawn> | undefined;
let owner: AirlockClient | undefined;
try {
  await mkdir(directory, { recursive: true });
  owner = await connectAirlock(); clients.push(owner);
  const mac = await connectAirlock(); clients.push(mac);
  await call(owner, 'registerWorkstation', { workstationId: run, name: 'Actual read-only Mac baseline validation; auto-lock OFF' });
  await call(owner, 'enrollDevice', { workstationId: run, deviceId: `${run}-mac`, identityHex: mac.identity, role: 'mac', label: 'Actual IORegistry snapshot publisher on this Mac' });
  const tokenPath = resolve(directory, 'mac.private.json');
  await writeFile(tokenPath, JSON.stringify({ identity: mac.identity, token: mac.token }), { mode: 0o600 });
  await writeFile(resolve(directory, 'owner.private.json'), JSON.stringify({ identity: owner.identity, token: owner.token }), { mode: 0o600 });
  child = spawn(process.execPath, ['frontend/node_modules/tsx/dist/cli.mjs', 'scripts/airlock_mac_bridge.ts', '--workstation', run, '--device', `${run}-mac`, '--token-file', tokenPath, '--duration', '14'], { stdio: ['ignore', 'pipe', 'pipe'] });
  let stderr = '', stdout = '';
  child.stderr?.on('data', value => { stderr = (stderr + value).slice(-10000); });
  child.stdout?.on('data', value => { stdout = (stdout + value).slice(-10000); });
  const captured = await until(() => rows(owner!, 'myBaselineSnapshots', run).find(r => !r.sessionId), 'actual enrolled Mac capture', 12000);
  const sessionId = `${run}-session`;
  await call(owner, 'armSession', { workstationId: run, sessionId, baselineJson: '', approvedAppsJson: captured.activeAppId === 'unknown' ? '[]' : JSON.stringify([captured.activeAppId]), durationMs: 60000n, simulated: false });
  const frozen = await until(() => rows(owner!, 'myBaselineSnapshots', run).find(r => r.sessionId === sessionId), 'frozen live baseline');
  assert.equal(frozen.deviceId, `${run}-mac`);
  assert.equal(frozen.baselineJson, captured.baselineJson);
  assert.equal(frozen.activeAppId, captured.activeAppId);
  const heartbeat = await until(() => rows(owner!, 'myDeviceEvents', run).find(r => r.sessionId === sessionId && r.kind === 'heartbeat'), 'actual Mac monitoring heartbeat', 8000);
  assert.equal(heartbeat.simulated, false);
  assert.equal(rows(owner, 'myLockRequests', run).length, 0);
  const observedEvents = rows(owner, 'myDeviceEvents', run);
  await call(owner, 'disarmSession', { workstationId: run });
  const report = {
    passed: true, run_id: run, uri: process.env.AIRLOCK_SPACETIME_URI || 'http://127.0.0.1:3000', database: process.env.AIRLOCK_DATABASE || 'locki-airlock-sid',
    source: 'Actual macOS IORegistry USB snapshots and native AppKit active-app snapshot',
    workstation_id: run, device_id: `${run}-mac`, session_id: sessionId,
    usb_baseline_count: JSON.parse(frozen.baselineJson).length, active_app_id: frozen.activeAppId,
    camera_status_at_arm: frozen.cameraStatus, frozen_usb_baseline_matches_capture: true,
    actual_observation_events: observedEvents.length, physical_usb_insertion_tested: false,
    auto_lock_enabled: false, real_lock_executed: false, camera_opened: false, microphone_opened: false,
    state_after_proof: 'disarmed', captured_at: new Date(Number(frozen.observedAtMs)).toISOString(),
  };
  await writeFile(resolve(directory, 'report.json'), `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify(report, null, 2));
  if (stderr) console.error(stderr);
  if (stdout) console.log(stdout);
} finally {
  if (child && child.exitCode === null) child.kill('SIGTERM');
  if (owner) await call(owner, 'disarmSession', { workstationId: run }).catch(() => {});
  closeAirlock(clients);
}
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
