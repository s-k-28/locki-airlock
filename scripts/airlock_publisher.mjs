#!/usr/bin/env node
import { randomUUID } from 'node:crypto';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { AirlockHttp } from './airlock_http.mjs';

const kinds = new Set(['usb_added', 'usb_removed', 'app_launched', 'person_in_zone', 'person_cleared', 'camera_offline', 'camera_online', 'heartbeat']);
const envelopeKeys = new Set(['schema_version', 'workstation_id', 'session_id', 'device_id', 'event_id', 'source', 'kind', 'observed_at', 'seq', 'confidence', 'metadata', 'simulated']);
const sourceKinds = {
  mac: new Set(['usb_added', 'usb_removed', 'app_launched', 'heartbeat']),
  jetson: new Set(['person_in_zone', 'person_cleared', 'camera_offline', 'camera_online', 'heartbeat']),
  fixture: kinds,
  service: new Set(['heartbeat']),
};

/** @param {import('./airlock_contract').SecurityEventV1} event */
export function validateSecurityEvent(event) {
  if (!event || typeof event !== 'object' || Array.isArray(event)) throw new Error('SecurityEventV1 must be an object');
  if (Object.keys(event).some(key => !envelopeKeys.has(key))) throw new Error('SecurityEventV1 contains unknown envelope fields');
  if (event.schema_version !== 1) throw new Error('schema_version must be 1');
  for (const key of ['workstation_id', 'session_id', 'device_id', 'event_id']) {
    if (typeof event[key] !== 'string' || !/^[a-zA-Z0-9_.:-]{1,128}$/.test(event[key])) throw new Error(`${key} must contain 1-128 letters, digits, _, ., :, or -`);
  }
  if (!sourceKinds[event.source]?.has(event.kind)) throw new Error('Event kind does not belong to the declared source');
  if (!Number.isSafeInteger(event.observed_at) || event.observed_at < 0) throw new Error('observed_at must be a nonnegative safe integer');
  if (!Number.isSafeInteger(event.seq) || event.seq < 1) throw new Error('seq must be a positive safe integer starting at 1');
  if (event.confidence !== undefined && (typeof event.confidence !== 'number' || !Number.isFinite(event.confidence) || event.confidence < 0 || event.confidence > 1)) throw new Error('confidence must be in [0,1]');
  if (typeof event.simulated !== 'boolean') throw new Error('simulated provenance is mandatory');
  if (event.source === 'fixture' && !event.simulated) throw new Error('Fixtures must be labeled simulated:true');
  if (event.source === 'fixture' && event.device_id !== 'fixture') throw new Error('Fixture source must use device_id:fixture');
  if (event.device_id === 'fixture' && (event.source !== 'fixture' || !event.simulated)) throw new Error('fixture device ID requires fixture source and simulated:true');
  if (!event.metadata || typeof event.metadata !== 'object' || Array.isArray(event.metadata)) throw new Error('metadata must be an object');
  const metadata = JSON.stringify(event.metadata);
  if (Buffer.byteLength(metadata) > 2048) throw new Error('metadata exceeds 2048 bytes');
  const metadataKeys = new Set(['deviceKey', 'appId', 'label', 'detail', 'captureMode', 'bootId']);
  if (Object.keys(event.metadata).some(key => !metadataKeys.has(key)) || Object.values(event.metadata).some(value => typeof value !== 'string' || value.length > 256)) throw new Error('metadata must contain only bounded redacted string fields from the contract');
  if (['usb_added', 'usb_removed'].includes(event.kind) && (typeof event.metadata.deviceKey !== 'string' || !event.metadata.deviceKey)) throw new Error('USB events require metadata.deviceKey');
  if (event.kind === 'app_launched' && (typeof event.metadata.appId !== 'string' || !event.metadata.appId)) throw new Error('App events require metadata.appId');
  return event;
}

/** Convert the JSON envelope into the frozen reducer positional argument order. */
export function eventToReducerArgs(event) {
  validateSecurityEvent(event);
  return [event.workstation_id, event.session_id, event.device_id, event.event_id, event.kind, event.observed_at, event.seq, event.confidence ?? 1, JSON.stringify(event.metadata), event.simulated, event.schema_version];
}

/** Publish observation metadata only. No lock, motors, notifications, or file execution. */
export async function publishSecurityEvent(event, options) {
  const client = options instanceof AirlockHttp ? options : new AirlockHttp(options);
  await client.call('ingest_device_event', eventToReducerArgs(event));
}

export function makeFixture({ workstationId, sessionId, kind = 'usb_added', seq = 1, metadata, observedAt = Date.now() }) {
  return validateSecurityEvent({
    schema_version: 1, workstation_id: workstationId, session_id: sessionId,
    device_id: 'fixture', event_id: `fixture-${randomUUID()}`, source: 'fixture',
    kind, observed_at: observedAt, seq, confidence: 1,
    metadata: metadata ?? (kind.startsWith('usb_') ? { deviceKey: 'fixture:harmless-usb', label: 'SYNTHETIC harmless USB' } : kind === 'app_launched' ? { appId: 'fixture:test-app', label: 'SYNTHETIC test app' } : { detail: 'SYNTHETIC observation; no camera inference' }),
    simulated: true,
  });
}

async function main() {
  const args = process.argv.slice(2);
  const value = name => { const i = args.indexOf(name); return i < 0 ? undefined : args[i + 1]; };
  if (args.includes('--help') || !args.length) {
    console.log(`AIRLOCK observation publisher (no system actions)\n\nCreate an independent device identity:\n  node scripts/airlock_publisher.mjs --new-identity data/airlock/mac-token.json\n\nPrint a labeled fixture, without publishing:\n  node scripts/airlock_publisher.mjs --fixture --workstation ID --session ID --kind usb_added --seq 1\n\nPublish a fixture after the owner arms a SIMULATED session:\n  AIRLOCK_DEVICE_TOKEN_FILE=data/airlock/owner-token.json node scripts/airlock_publisher.mjs --fixture --workstation ID --session ID --kind usb_added --seq 1 --publish\n\nPublish a JSON observation from an enrolled Mac/Jetson:\n  AIRLOCK_DEVICE_TOKEN_FILE=data/airlock/mac-token.json node scripts/airlock_publisher.mjs --file EVENT.json --publish [--real-observation]\n\nEnvironment: AIRLOCK_SPACETIME_URI, AIRLOCK_DATABASE, AIRLOCK_DEVICE_TOKEN_FILE.\nNever put a token in arguments or source control.`);
    return;
  }
  const client = new AirlockHttp();
  const identityFile = value('--new-identity');
  if (identityFile) {
    const identity = await client.newIdentity();
    await mkdir(dirname(resolve(identityFile)), { recursive: true });
    await writeFile(identityFile, `${JSON.stringify(identity, null, 2)}\n`, { mode: 0o600, flag: 'wx' });
    console.log(JSON.stringify({ identity: identity.identity, token_file: resolve(identityFile) }));
    return;
  }
  let event;
  if (args.includes('--fixture')) {
    event = makeFixture({ workstationId: value('--workstation'), sessionId: value('--session'), kind: value('--kind') || 'usb_added', seq: Number(value('--seq') || '1'), ...(value('--metadata') ? { metadata: JSON.parse(value('--metadata')) } : {}) });
  } else if (value('--file')) {
    event = validateSecurityEvent(JSON.parse(await readFile(value('--file'), 'utf8')));
  } else throw new Error('Choose --fixture or --file EVENT.json');
  if (!args.includes('--publish')) { console.log(JSON.stringify(event, null, 2)); return; }
  if (!event.simulated && !args.includes('--real-observation')) throw new Error('Real sensor observations require --real-observation; fixtures must remain simulated:true');
  const tokenFile = process.env.AIRLOCK_DEVICE_TOKEN_FILE;
  if (!tokenFile) throw new Error('Set AIRLOCK_DEVICE_TOKEN_FILE to the private enrolled identity token file');
  const saved = JSON.parse(await readFile(tokenFile, 'utf8'));
  if (!saved.token || typeof saved.token !== 'string') throw new Error('Token file must contain a token string');
  client.token = saved.token;
  await publishSecurityEvent(event, client);
  console.log(JSON.stringify({ published: true, event_id: event.event_id, provenance: event.simulated ? 'SIMULATED' : 'sensor-observation', kind: event.kind }));
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => { console.error(error.message); process.exitCode = 1; });
}
