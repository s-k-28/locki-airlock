#!/usr/bin/env node
/** Committed SpacetimeDB incidents -> narrowly authenticated LOOPBACK voice API. */
import { createHash } from 'node:crypto';
import { readFile, writeFile, mkdir, rename, stat } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { closeAirlock, connectAirlock, rows, type AirlockClient } from './airlock_sdk';

export function incidentAnnouncement(row: any) {
  if (!['unknown_usb', 'correlated_usb_physical', 'unapproved_app', 'owner_panic'].includes(row.ruleId)) throw new Error('Unsupported committed incident rule');
  if (!['medium', 'high'].includes(row.severity) || typeof row.simulated !== 'boolean') throw new Error('Invalid committed incident facts');
  return { workstation_id: row.workstationId, session_id: row.sessionId, incident_id: row.id, severity: row.severity, rule_id: row.ruleId, simulated: row.simulated, updated_at_ms: Number(row.updatedAtMs) };
}

function warningId(body: ReturnType<typeof incidentAnnouncement>) {
  return createHash('sha256').update(JSON.stringify([body.workstation_id, body.session_id, body.incident_id, body.severity, body.rule_id, body.simulated])).digest('hex');
}

async function credential(path: string) {
  if ((await stat(path)).mode & 0o077) throw new Error(`Credential file must have permissions 0600: ${path}`);
  const saved = JSON.parse(await readFile(path, 'utf8'));
  if (typeof saved.token !== 'string' || !saved.token) throw new Error('Credential file has no token');
  return saved;
}

async function main() {
  const argv = process.argv.slice(2);
  const value = (name: string) => { const index = argv.indexOf(name); return index < 0 ? undefined : argv[index + 1]; };
  if (argv.includes('--help')) {
    console.log(`AIRLOCK committed-incident -> local P0 voice bridge\n\nAIRLOCK_SERVICE_TOKEN_FILE=data/.../service.private.json \\\nAIRLOCK_VOICE_LOCAL_TOKEN_FILE=data/airlock-voice/local-token.private.json \\\n  selected-node frontend/node_modules/tsx/dist/cli.mjs scripts/airlock_voice_bridge.ts \\\n  --workstation ID [--include-synthetic] [--duration 30]\n\nUses current AIRLOCK_SPACETIME_URI/AIRLOCK_DATABASE, or the service credential's context.\nSynthetic cases are skipped unless explicitly included; their fixed lines always say Rehearsal.\nAudio itself remains disabled unless the separate local voice service starts with --enable-audio.\nThe bridge never invokes a microphone, model, robot, lock, notification, or security control.`);
    return;
  }
  const servicePath = value('--service-token-file') || process.env.AIRLOCK_SERVICE_TOKEN_FILE;
  const localPath = value('--local-token-file') || process.env.AIRLOCK_VOICE_LOCAL_TOKEN_FILE;
  if (!servicePath || !localPath) throw new Error('Provide private service and local announcement token files');
  const service = await credential(servicePath);
  const local = await credential(localPath);
  if (!process.env.AIRLOCK_SPACETIME_URI) process.env.AIRLOCK_SPACETIME_URI = service.uri || 'http://127.0.0.1:3000';
  if (!process.env.AIRLOCK_DATABASE) process.env.AIRLOCK_DATABASE = service.database || 'locki-airlock-sid';
  const workstationId = value('--workstation') || service.workstation_id;
  if (!workstationId) throw new Error('Scope the voice adapter to --workstation ID');
  const endpoint = new URL(value('--voice-uri') || 'http://127.0.0.1:8077');
  if (endpoint.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(endpoint.hostname) || endpoint.username || endpoint.password) throw new Error('Voice announcements must use an HTTP loopback endpoint without embedded credentials');
  const duration = Number(value('--duration') || '0');
  if (!Number.isFinite(duration) || duration < 0) throw new Error('--duration must be nonnegative');
  const stateFile = resolve(value('--state-file') || `data/airlock-voice/bridge-${service.identity?.slice(0, 16) || 'service'}-${workstationId}.json`);
  const scope = `${process.env.AIRLOCK_SPACETIME_URI}:${process.env.AIRLOCK_DATABASE}:${workstationId}`;
  const delivered = new Set<string>();
  try {
    const old = JSON.parse(await readFile(stateFile, 'utf8'));
    if (old.scope !== scope) throw new Error('Voice bridge state belongs to a different database/workstation');
    for (const id of old.delivered || []) delivered.add(id);
  } catch (error: any) { if (error.code !== 'ENOENT') throw error; }
  const save = async () => {
    await mkdir(dirname(stateFile), { recursive: true });
    await writeFile(`${stateFile}.tmp`, JSON.stringify({ scope, delivered: [...delivered].slice(-1024) }), { mode: 0o600 });
    await rename(`${stateFile}.tmp`, stateFile);
  };
  const failed = new Map<string, { attempts: number; after: number }>();
  const unsupported = new Set<string>();
  let stopped = false;
  let client: AirlockClient | undefined;
  process.on('SIGINT', () => { stopped = true; });
  process.on('SIGTERM', () => { stopped = true; });
  const started = Date.now();
  try {
    while (!stopped && (!duration || Date.now() - started < duration * 1000)) {
      if (!client?.conn.isActive) {
        if (client) closeAirlock([client]);
        try { client = await connectAirlock(service.token); }
        catch (error) { console.error(`Database unavailable; no incident speech is fabricated: ${String(error)}`); await new Promise(resolve => setTimeout(resolve, 1000)); continue; }
        console.log(JSON.stringify({ status: 'subscribed', workstation_id: workstationId, synthetic_enabled: argv.includes('--include-synthetic'), endpoint: endpoint.origin }));
      }
      for (const row of rows(client, 'myIncidents', workstationId)) {
        if (row.state !== 'open' || (row.simulated && !argv.includes('--include-synthetic'))) continue;
        if (Date.now() - Number(row.updatedAtMs) > 30_000) continue;
        let body: ReturnType<typeof incidentAnnouncement>;
        try { body = incidentAnnouncement(row); }
        catch {
          if (!unsupported.has(row.id)) {
            unsupported.add(row.id);
            if (unsupported.size > 64) unsupported.delete(unsupported.values().next().value!);
            console.error(JSON.stringify({ status: 'unsupported_incident_facts', incident_id: row.id, speech_queued: false }));
          }
          continue;
        }
        const id = warningId(body);
        if (delivered.has(id)) continue;
        const retry = failed.get(id);
        if (retry && (retry.attempts >= 5 || retry.after > Date.now())) continue;
        const t0 = performance.now();
        try {
          const response = await fetch(`${endpoint.origin}/api/airlock/announce`, { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${local.token}` }, body: JSON.stringify(body), signal: AbortSignal.timeout(5000) });
          const result = await response.json();
          if (!response.ok) {
            if ([401, 403].includes(response.status)) throw new Error(`Local announcement authentication denied (${response.status})`);
            if (response.status === 409) { delivered.add(id); await save(); continue; }
            throw new Error(`Local announcement rejected (${response.status}): ${JSON.stringify(result)}`);
          }
          delivered.add(id); if (delivered.size > 1024) delivered.delete(delivered.values().next().value!);
          failed.delete(id); await save();
          console.log(JSON.stringify({ status: 'announcement_accepted', incident_id: row.id, warning_id: result.warning_id, simulated: row.simulated, queued: result.queued, duplicate: result.duplicate, audio_enabled: result.audio_enabled, local_delivery_ms: +(performance.now() - t0).toFixed(2) }));
        } catch (error) {
          const attempts = (retry?.attempts || 0) + 1;
          failed.set(id, { attempts, after: Date.now() + Math.min(500 * 2 ** attempts, 5000) });
          if (failed.size > 64) failed.delete(failed.keys().next().value!);
          console.error(JSON.stringify({ status: 'announcement_degraded', incident_id: row.id, attempts, detail: String(error), callback_fabricated: false }));
        }
      }
      await new Promise(resolve => setTimeout(resolve, 100));
    }
  } finally { if (client) closeAirlock([client]); }
}

if (process.argv[1]?.endsWith('airlock_voice_bridge.ts')) main().catch(error => { console.error(error instanceof Error ? error.stack : String(error)); process.exitCode = 1; });
