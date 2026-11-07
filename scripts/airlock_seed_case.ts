#!/usr/bin/env node
/** Create a labeled synthetic integration case and private adapter credentials. */
import { randomUUID } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { call, closeAirlock, connectAirlock, rows, until } from './airlock_sdk';

async function main() {
  const run = `integration-${Date.now()}-${randomUUID().slice(0, 6)}`;
  const directory = resolve(process.env.AIRLOCK_CASE_OUTPUT || 'data/airlock-integration', run);
  await mkdir(directory, { recursive: true });
  const clients = await Promise.all([connectAirlock(), connectAirlock()]);
  const [owner, service] = clients;
  try {
    await call(owner, 'registerWorkstation', { workstationId: run, name: 'SYNTHETIC integration case; genuine adapter/model results separately labeled' });
    await call(owner, 'enrollDevice', { workstationId: run, deviceId: `${run}-service`, identityHex: service.identity, role: 'service', label: 'AIRLOCK integration service' });
    await call(owner, 'armSession', { workstationId: run, sessionId: `${run}-session`, baselineJson: '[]', approvedAppsJson: '[]', durationMs: 3_600_000n, simulated: true });
    const now = BigInt(Date.now());
    const events = [
      { kind: 'person_in_zone', seq: 1n, metadataJson: '{"detail":"SYNTHETIC person-zone integration fixture"}' },
      { kind: 'usb_added', seq: 2n, metadataJson: '{"deviceKey":"fixture:integration-usb","label":"SYNTHETIC harmless USB fixture"}' },
    ];
    for (const event of events) await call(owner, 'ingestDeviceEvent', { workstationId: run, sessionId: `${run}-session`, deviceId: 'fixture', eventId: `fixture-${randomUUID()}`, observedAtMs: now, confidence: 1, simulated: true, schemaVersion: 1, ...event });
    const incident = await until(() => rows(service, 'myIncidents', run).find(row => row.severity === 'high'), 'service-visible correlated fixture incident');
    const common = { uri: process.env.AIRLOCK_SPACETIME_URI || 'http://127.0.0.1:3000', database: process.env.AIRLOCK_DATABASE || 'locki-airlock-sid', workstation_id: run, session_id: `${run}-session`, incident_id: incident.id, simulated: true };
    for (const [role, client] of [['owner', owner], ['service', service]] as const) await writeFile(`${directory}/${role}.private.json`, `${JSON.stringify({ ...common, identity: client.identity, token: client.token }, null, 2)}\n`, { mode: 0o600 });
    const manifest = { ...common, owner_identity: owner.identity, service_identity: service.identity, owner_token_file: `${directory}/owner.private.json`, service_token_file: `${directory}/service.private.json` };
    await writeFile(`${directory}/manifest.json`, `${JSON.stringify(manifest, null, 2)}\n`);
    console.log(JSON.stringify(manifest, null, 2));
  } finally { closeAirlock(clients); }
}
main().catch(error => { console.error(error instanceof Error ? error.stack : String(error)); process.exitCode = 1; });
