#!/usr/bin/env node
/** Publish serial JSON-line observations, retaining each event through retries. */
import { readFile } from 'node:fs/promises';
import { createInterface } from 'node:readline';
import { AirlockHttp, AirlockHttpError } from './airlock_http.mjs';
import { publishSecurityEvent, validateSecurityEvent } from './airlock_publisher.mjs';

async function main() {
  if (!process.argv.includes('--real-observation')) throw new Error('Use --real-observation only for actual sensor output; fixtures must use airlock_publisher.mjs');
  const path = process.env.AIRLOCK_DEVICE_TOKEN_FILE;
  if (!path) throw new Error('Set AIRLOCK_DEVICE_TOKEN_FILE to the private enrolled device token file');
  const saved = JSON.parse(await readFile(path, 'utf8'));
  if (typeof saved.token !== 'string' || !saved.token) throw new Error('Token file has no token');
  const client = new AirlockHttp({ token: saved.token });
  const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
  for await (const line of lines) {
    if (!line.trim()) continue;
    const event = validateSecurityEvent(JSON.parse(line));
    if (event.simulated || event.source === 'fixture') throw new Error('This stream accepts real observations only; do not mix fixture and sensor provenance');
    let delivered = false;
    for (let attempt = 0; attempt < 5; attempt++) {
      try {
        await publishSecurityEvent(event, client);
        if (event.source === 'mac' && event.metadata.captureMode === 'NSWorkspace.didLaunchApplicationNotification') {
          await client.call('report_health', [event.workstation_id, event.device_id, 'apps', 'online', 'Native NSWorkspace visible-app observer; no background-agent coverage']);
        }
        delivered = true;
        break;
      } catch (error) {
        if (error instanceof AirlockHttpError && error.status < 500) throw error;
        if (Date.now() - event.observed_at > 120_000) throw new Error(`Undelivered event expired; observer degraded (${event.event_id})`);
        if (attempt === 4) throw error;
        console.error(`Observation delivery degraded; retrying same event ID (attempt ${attempt + 1})`);
        await new Promise(resolve => setTimeout(resolve, Math.min(500 * 2 ** attempt, 5_000)));
      }
    }
    if (!delivered) throw new Error('Observation was not delivered');
    console.log(JSON.stringify({ published: true, event_id: event.event_id, kind: event.kind, simulated: false }));
  }
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
