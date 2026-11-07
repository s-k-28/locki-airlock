#!/usr/bin/env node
/** Explicit real read-only macOS adapter proof, separate from fixture tests. */
import assert from 'node:assert/strict';
import { execFile, spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';
import { createInterface } from 'node:readline';
import { resolve } from 'node:path';
import { promisify } from 'node:util';
import { publishSecurityEvent } from './airlock_publisher.mjs';
import { AirlockHttp } from './airlock_http.mjs';
import { call, closeAirlock, connectAirlock, rows, until, type AirlockClient } from './airlock_sdk';

const exec = promisify(execFile);
async function main() {
  if (process.platform !== 'darwin') throw new Error('Real macOS observer proof requires macOS');
  const launchCalculator = process.argv.includes('--launch-calculator');
  const expectCalculator = launchCalculator || process.argv.includes('--expect-calculator');
  const run = `observer-${Date.now()}-${randomUUID().slice(0, 6)}`;
  const directory = resolve('data/airlock-observer-proof', run);
  await mkdir(directory, { recursive: true });
  const usb = JSON.parse((await exec('python3', ['backend/app/security/mac_observer.py', '--snapshot'])).stdout);
  const apps = JSON.parse((await exec('swift', ['scripts/airlock_app_observer.swift', '--snapshot'], { maxBuffer: 2 * 1024 * 1024 })).stdout);
  if (expectCalculator && apps.baseline.includes('com.apple.calculator')) throw new Error('Calculator is already running; native launch proof needs a newly launched harmless test app');
  const clients: AirlockClient[] = [];
  let owner: AirlockClient | undefined;
  const result: any = { run_id: run, simulated: false, test: 'Actual Mac IORegistry/native AppKit adapter publication', usb_snapshot_ms: usb.snapshot_duration_ms, usb_baseline_count: usb.baseline.length, visible_app_baseline_count: apps.baseline.length, physical_usb_insertion_tested: false, calculator_launch_requested: launchCalculator };
  try {
    const [primary, mac] = await Promise.all([connectAirlock(), connectAirlock()]);
    clients.push(primary, mac); owner = primary;
    const tokenFile = `${directory}/mac-token.private.json`;
    await writeFile(tokenFile, JSON.stringify({ identity: mac.identity, token: mac.token }), { mode: 0o600 });
    await call(owner, 'registerWorkstation', { workstationId: run, name: 'LIVE adapter proof: harmless application and read-only USB metadata' });
    for (const suffix of ['usb', 'apps']) await call(owner, 'enrollDevice', { workstationId: run, deviceId: `${run}-${suffix}`, identityHex: mac.identity, role: 'mac', label: `Actual macOS ${suffix} observer validation` });
    // Live arm must use a fresh capture committed by the enrolled Mac. Re-sample
    // after enrollment so compiler/network setup cannot age the initial inventory.
    const freshUsb = JSON.parse((await exec('python3', ['backend/app/security/mac_observer.py', '--snapshot'])).stdout);
    await call(mac, 'recordBaseline', { workstationId: run, deviceId: `${run}-usb`, observedAtMs: BigInt(freshUsb.observed_at), baselineJson: JSON.stringify(freshUsb.baseline), activeAppId: apps.frontmost_app_id === 'unavailable' ? 'unknown' : apps.frontmost_app_id });
    result.usb_baseline_count = freshUsb.baseline.length;
    result.usb_snapshot_ms = freshUsb.snapshot_duration_ms;
    await call(owner, 'armSession', { workstationId: run, sessionId: `${run}-session`, baselineJson: '', approvedAppsJson: JSON.stringify(apps.baseline), durationMs: 60_000n, simulated: false });
    const usbOutput = await exec('python3', ['backend/app/security/mac_observer.py', '--workstation', run, '--session', `${run}-session`, '--device', `${run}-usb`, '--publish', '--token-file', tokenFile, '--state-file', `${directory}/usb-seq.json`, '--duration', '2', '--uri', process.env.AIRLOCK_SPACETIME_URI || 'http://127.0.0.1:3000', '--database', process.env.AIRLOCK_DATABASE || 'locki-airlock-sid']);
    result.usb_publisher_receipts = usbOutput.stdout.trim().split('\n').filter(Boolean).map(line => JSON.parse(line));
    const observer = spawn('swift', ['scripts/airlock_app_observer.swift', '--workstation', run, '--session', `${run}-session`, '--device', `${run}-apps`, '--state-file', `${directory}/app-seq.json`, '--duration', expectCalculator ? '20' : '8'], { stdio: ['ignore', 'pipe', 'pipe'] });
    let stderr = '';
    observer.stderr.on('data', data => { stderr += String(data); });
    const done = new Promise<number | null>((resolve, reject) => { observer.on('error', reject); observer.on('close', resolve); });
    const http = new AirlockHttp({ token: mac.token });
    result.native_receipts = [];
    let launched = false;
    for await (const line of createInterface({ input: observer.stdout, crlfDelay: Infinity })) {
      if (!line.trim()) continue;
      const event = JSON.parse(line);
      assert.equal(event.simulated, false);
      await publishSecurityEvent(event, http);
      await http.call('report_health', [run, `${run}-apps`, 'apps', 'online', 'Native NSWorkspace observer validation; actual visible applications']);
      if (event.kind === 'heartbeat' && result.native_receipts.length === 0) console.log('Native observer ready for external application launch');
      result.native_receipts.push({ event_id: event.event_id, kind: event.kind, app_id: event.metadata.appId || null });
      if (launchCalculator && !launched && event.kind === 'heartbeat') {
        launched = true;
        await exec('/usr/bin/open', ['-a', 'Calculator']);
      }
    }
    const exitCode = await done;
    assert.equal(exitCode, 0, stderr);
    const committed = await until(() => rows(owner!, 'myDeviceEvents', run).some(row => row.deviceId === `${run}-apps`) && rows(owner!, 'myDeviceEvents', run), 'actual observer committed rows');
    assert.ok(committed.some(row => row.deviceId === `${run}-usb` && row.kind === 'heartbeat' && !row.simulated));
    assert.ok(committed.some(row => row.deviceId === `${run}-apps` && row.kind === 'heartbeat' && !row.simulated));
    if (expectCalculator) {
      const launch = committed.find(row => row.kind === 'app_launched' && JSON.parse(row.metadataJson).appId === 'com.apple.calculator');
      assert.ok(launch, 'Native NSWorkspace notification for the actual harmless Calculator launch must commit');
      const incident = await until(() => rows(owner!, 'myIncidents', run).find(row => row.digitalEventId === launch.id), 'actual app observation incident');
      result.calculator_event_id = launch.id;
      result.incident_id = incident.id;
      result.native_capture_mode = JSON.parse(launch.metadataJson).captureMode;
    }
    assert.equal(rows(owner, 'myLockRequests', run).length, 0, 'Live adapter smoke does not opt into screen lock');
    result.committed_events = committed.length;
    result.passed = true;
  } finally {
    if (owner) await call(owner, 'disarmSession', { workstationId: run }).catch(error => { result.disarm_error = String(error); });
    closeAirlock(clients);
    await writeFile(`${directory}/report.json`, `${JSON.stringify(result, null, 2)}\n`);
    console.log(JSON.stringify({ ...result, report_file: `${directory}/report.json` }, null, 2));
  }
}
main().catch(error => { console.error(error instanceof Error ? error.stack : String(error)); process.exitCode = 1; });
