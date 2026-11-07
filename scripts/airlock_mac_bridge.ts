#!/usr/bin/env node
/** Read-only USB polling + fresh baseline publication. No screen lock, shell or device-file access. */
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { readFile, writeFile, mkdir, rename, open, unlink, stat } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { connectAirlock, call, rows, closeAirlock, type AirlockClient } from './airlock_sdk';

const exec = promisify(execFile);
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
export type UsbSnapshot = { baseline: string[]; devices: { deviceKey: string; label: string }[]; observed_at: number; snapshot_duration_ms: number; simulated: boolean };
type Pending = { eventId: string; sessionId: string; kind: string; observedAtMs: string; seq: string; metadata: Record<string,string> };
type State = { workstation: string; device: string; sequence: string; pending: Pending[] };

export function validateSnapshot(value: unknown): UsbSnapshot {
  const s = value as UsbSnapshot;
  if (!s || s.simulated !== false || !Array.isArray(s.baseline) || !Array.isArray(s.devices) || s.baseline.length > 256 || !Number.isSafeInteger(s.observed_at)) throw new Error('Invalid real Mac USB snapshot');
  if (s.baseline.some(k => typeof k !== 'string' || !/^usb:sha256:[a-f0-9]{64}$/.test(k))) throw new Error('USB identifiers must be redacted SHA256 device keys');
  if (s.devices.some(d => !s.baseline.includes(d.deviceKey) || !/^USB [a-f0-9]{4}:[a-f0-9]{4}$/.test(d.label))) throw new Error('Unexpected unredacted USB metadata');
  return s;
}

export function changes(previous: string[], current: UsbSnapshot) {
  const before = new Set(previous), after = new Set(current.baseline);
  return [
    ...current.devices.filter(d => !before.has(d.deviceKey)).map(d => ({ kind: 'usb_added', metadata: { deviceKey: d.deviceKey, label: `${d.label} (snapshot poll)` } })),
    ...previous.filter(key => !after.has(key)).map(key => ({ kind: 'usb_removed', metadata: { deviceKey: key, label: 'USB removed (snapshot poll)' } })),
  ];
}

async function save(path: string, state: State) {
  await mkdir(dirname(path), { recursive: true });
  const temporary = `${path}.${process.pid}.tmp`;
  await writeFile(temporary, `${JSON.stringify(state)}\n`, { mode: 0o600 });
  await rename(temporary, path);
}

async function readPrivate(path: string) {
  if ((await stat(path)).mode & 0o077) throw new Error('Token file must have owner-only permissions (0600)');
  const data = JSON.parse(await readFile(path, 'utf8'));
  if (typeof data.token !== 'string' || !data.token) throw new Error('Token file must contain the enrolled Mac token');
  return data.token as string;
}

async function sample(): Promise<UsbSnapshot> {
  const result = await exec(process.env.AIRLOCK_PYTHON || 'python3', ['backend/app/security/mac_observer.py', '--snapshot'], { cwd: root, timeout: 5000, maxBuffer: 256 * 1024 });
  return validateSnapshot(JSON.parse(result.stdout));
}

async function frontmostApp(): Promise<string> {
  // This fixed, separately compiled AppKit helper only reads visible application metadata.
  try {
    const result = await exec(resolve(root, 'data/airlock/bin/app-observer'), ['--snapshot'], { cwd: root, timeout: 5000, maxBuffer: 2 * 1024 * 1024 });
    const id = JSON.parse(result.stdout).frontmost_app_id;
    return typeof id === 'string' && id.length <= 256 && id !== 'unavailable' ? id : 'unknown';
  } catch { return 'unknown'; }
}

async function acquire(path: string): Promise<() => Promise<void>> {
  await mkdir(dirname(path), { recursive: true });
  try {
    const old = JSON.parse(await readFile(path, 'utf8'));
    if (!Number.isSafeInteger(old.pid)) throw new Error('Malformed lock; inspect it before removing');
    try { process.kill(old.pid, 0); throw new Error('Another Mac bridge owns this device sequence'); }
    catch (e: any) { if (e.code !== 'ESRCH') throw e; }
    await unlink(path);
  } catch (e: any) { if (e.code !== 'ENOENT') throw e; }
  const lock = await open(path, 'wx', 0o600);
  await lock.writeFile(JSON.stringify({ pid: process.pid }));
  return async () => { await lock.close(); await unlink(path).catch(() => {}); };
}

export async function runMacBridge(args: string[]) {
  const arg = (name: string) => { const at = args.indexOf(name); return at < 0 ? undefined : args[at + 1]; };
  if (args.includes('--help') || !args.length) {
    console.log('AIRLOCK read-only Mac USB bridge\n--workstation ID --device ID --token-file PRIVATE.json [--duration SECONDS]\nPublishes fresh real baselines every 5s and actual USB snapshot changes only in live armed sessions.\nNo camera, microphone, model, screen lock or robot command is started.\nCompile optional active-app helper once: swiftc scripts/airlock_app_observer.swift -o data/airlock/bin/app-observer');
    return;
  }
  const workstation = arg('--workstation'), device = arg('--device');
  const tokenFile = arg('--token-file') || process.env.AIRLOCK_MAC_TOKEN_FILE;
  if (!workstation || !device || !tokenFile || !/^[\w.:-]{1,128}$/.test(workstation) || !/^[\w.:-]{1,128}$/.test(device)) throw new Error('Provide valid workstation/device IDs and a private enrolled Mac token file');
  const duration = Number(arg('--duration') || 0);
  if (!Number.isFinite(duration) || duration < 0) throw new Error('Duration must be nonnegative');
  const statePath = resolve(arg('--state-file') || `data/airlock/mac-bridge-${workstation}-${device}.json`);
  const release = await acquire(`${statePath}.lock`);
  let token: string;
  try { token = await readPrivate(resolve(tokenFile)); }
  catch (error) { await release(); throw error; }
  let state: State = { workstation, device, sequence: '0', pending: [] };
  try {
    const saved = JSON.parse(await readFile(statePath, 'utf8'));
    if (saved.workstation !== workstation || saved.device !== device) throw new Error('Sequence state belongs to another device');
    state = saved;
  } catch (e: any) { if (e.code !== 'ENOENT') { await release(); throw e; } }
  let client: AirlockClient | undefined, previous: string[] = [], sessionId = '', baselineAt = 0, heartbeatAt = 0;
  let stopping = false;
  const stop = () => { stopping = true; };
  process.on('SIGINT', stop); process.on('SIGTERM', stop);
  const started = Date.now();
  try {
    console.log(JSON.stringify({ adapter: 'read-only Mac USB snapshot polling', workstation, device, simulated: false }));
    while (!stopping && (!duration || Date.now() - started < duration * 1000)) {
      const cycle = Date.now();
      try {
        if (!client) { client = await connectAirlock(token); baselineAt = 0; sessionId = ''; }
        const enrollment = rows(client, 'myDevices', workstation).find(d => d.id === device && d.enabled && d.role === 'mac');
        if (!enrollment || enrollment.identity.toHexString() !== client.identity) throw new Error('This token is not the enrolled Mac device');
        if (BigInt(enrollment.lastSeq) > BigInt(state.sequence)) state.sequence = String(enrollment.lastSeq);
        const snapshot = await sample();
        if (cycle - baselineAt >= 5000) {
          await call(client, 'recordBaseline', { workstationId: workstation, deviceId: device, observedAtMs: BigInt(snapshot.observed_at), baselineJson: JSON.stringify(snapshot.baseline), activeAppId: await frontmostApp() });
          baselineAt = cycle;
        }
        const station = rows(client, 'myWorkstations').find(w => w.id === workstation);
        const session = rows(client, 'mySessions', workstation).find(s => s.id === station?.activeSessionId && s.state === 'armed' && !s.simulated && BigInt(s.expiresAtMs) > BigInt(Date.now()));
        if (!session) { state.pending = []; sessionId = ''; previous = snapshot.baseline; await save(statePath, state); }
        else {
          if (sessionId !== session.id) { sessionId = session.id; previous = JSON.parse(session.baselineJson); state.pending = state.pending.filter(e => e.sessionId === sessionId); }
          const events = changes(previous, snapshot);
          if (cycle - heartbeatAt >= 3000) { events.push({ kind: 'heartbeat', metadata: { deviceKey: '', label: 'USB snapshot poll healthy; app launch watcher is separate' } }); heartbeatAt = cycle; }
          if (state.pending.length + events.length > 128) throw new Error('USB observation queue is full; coverage is degraded');
          for (const e of events) {
            state.sequence = String(BigInt(state.sequence) + 1n);
            state.pending.push({ eventId: `mac-bridge-${randomUUID()}`, sessionId, kind: e.kind, observedAtMs: String(snapshot.observed_at), seq: state.sequence, metadata: e.kind === 'heartbeat' ? { detail: e.metadata.label } : e.metadata });
          }
          previous = snapshot.baseline;
          await save(statePath, state);
          let sent = 0;
          while (state.pending.length && sent < 32) {
            const e = state.pending[0];
            if (Date.now() - Number(e.observedAtMs) > 120_000) { state.pending.shift(); console.error('Expired undelivered USB observation; coverage is degraded'); continue; }
            await call(client, 'ingestDeviceEvent', { workstationId: workstation, sessionId: e.sessionId, deviceId: device, eventId: e.eventId, kind: e.kind, observedAtMs: BigInt(e.observedAtMs), seq: BigInt(e.seq), confidence: 1, metadataJson: JSON.stringify(e.metadata), simulated: false, schemaVersion: 1 });
            state.pending.shift(); sent++;
            console.log(JSON.stringify({ published: true, event_id: e.eventId, kind: e.kind, simulated: false }));
            await save(statePath, state);
          }
        }
      } catch (error: any) {
        console.error(`Mac bridge degraded: ${error.message}`);
        if (client) closeAirlock([client]); client = undefined;
        await new Promise(r => setTimeout(r, 2000));
      }
      await new Promise(r => setTimeout(r, Math.max(0, 1000 - (Date.now() - cycle))));
    }
  } finally {
    if (client) closeAirlock([client]);
    process.removeListener('SIGINT', stop); process.removeListener('SIGTERM', stop);
    await release();
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) runMacBridge(process.argv.slice(2)).catch(error => { console.error(error.message); process.exitCode = 1; });
