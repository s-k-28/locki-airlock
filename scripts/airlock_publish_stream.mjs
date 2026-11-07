#!/usr/bin/env node
/** Publish real JSON-line observations through a bounded private durable spool. */
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { AirlockHttp, AirlockHttpError } from './airlock_http.mjs';
import { publishSecurityEvent } from './airlock_publisher.mjs';
import { MAX_PENDING_EVENTS, openPublicationQueue, realStreamEvent, streamBinding } from './airlock_publish_stream_state.mjs';

export const MAX_LINE_BYTES = 8192;

/** Frame at most one bounded line; async iteration propagates backpressure. */
export async function* boundedJsonLines(input) {
  let parts = [], length = 0;
  for await (const data of input) {
    const chunk = Buffer.isBuffer(data) ? data : Buffer.from(data);
    let offset = 0;
    while (offset < chunk.length) {
      const newline = chunk.indexOf(10, offset), end = newline < 0 ? chunk.length : newline;
      const part = chunk.subarray(offset, end);
      length += part.length;
      if (length > MAX_LINE_BYTES) throw new Error('Observation line exceeds 8192 bytes; persisted events retained');
      parts.push(part);
      if (newline < 0) break;
      yield Buffer.concat(parts, length).toString('utf8');
      parts = []; length = 0; offset = end + 1;
    }
  }
  if (length) yield Buffer.concat(parts, length).toString('utf8');
}

export async function runPublicationStream({ input, client, statePath, attempts = 5, sleep = ms => new Promise(done => setTimeout(done, ms)), now = Date.now, onPublished = () => {}, onDegraded = message => console.error(message) }) {
  const queue = await openPublicationQueue(statePath, streamBinding(client));
  const replayedIds = new Set(queue.snapshot().map(event => event.event_id));
  let inputDone = false, failure;
  const waiters = new Set();
  const changed = () => { for (const notify of waiters) notify(); waiters.clear(); };
  const wait = () => new Promise(done => waiters.add(done));
  const stop = error => { if (!failure) failure = error; input.destroy(); changed(); };
  const intake = async () => {
    try {
      for await (const line of boundedJsonLines(input)) {
        if (failure) break;
        if (!line.trim()) continue;
        const event = realStreamEvent(JSON.parse(line));
        while (queue.size >= MAX_PENDING_EVENTS && !failure) await wait();
        if (failure) break;
        // Delivery runs independently: persist each accepted line before
        // waiting for the preceding event's network result.
        await queue.enqueue(event);
        changed();
      }
    } catch (error) { stop(error); }
    finally { inputDone = true; changed(); }
  };
  const delivery = async () => {
    try {
      for (;;) {
        if (failure) break;
        const event = queue.peek();
        if (!event) { if (inputDone) break; await wait(); continue; }
        for (let attempt = 0; ; attempt++) {
          try { await publishSecurityEvent(event, client); break; }
          catch (error) {
            // Try an old head once too: the original request may have committed
            // before its ACK was lost. Server dedup precedes freshness checks.
            const permanent = error instanceof AirlockHttpError && error.status < 500 && ![408, 429].includes(error.status);
            if (failure || permanent || attempt + 1 >= attempts || now() - event.observed_at > 120000) throw error;
            onDegraded(`Observation delivery degraded; retrying persisted event ${event.event_id} (attempt ${attempt + 1})`);
            await sleep(Math.min(500 * 2 ** attempt, 5000));
          }
        }
        await queue.acknowledge(event.event_id);
        changed();
        onPublished({ published: true, event_id: event.event_id, kind: event.kind, simulated: false, replayed: replayedIds.has(event.event_id) });
        const age = now() - event.observed_at;
        if (!failure && !inputDone && !replayedIds.has(event.event_id) && age >= -5000 && age <= 15000 && event.source === 'mac' && event.metadata.captureMode === 'NSWorkspace.didLaunchApplicationNotification') {
          try { await client.call('report_health', [event.workstation_id, event.device_id, 'apps', 'online', 'Native NSWorkspace visible-app observer; no background-agent coverage']); }
          catch { onDegraded('Observation committed, but current app coverage health could not be reported'); }
        }
      }
    } catch (error) { stop(error); }
  };
  try {
    await Promise.all([intake(), delivery()]);
    if (failure) throw failure;
  } finally { await queue.close(); }
}

async function main() {
  if (!process.argv.includes('--real-observation')) throw new Error('Use --real-observation only for actual sensor output; fixtures must use airlock_publisher.mjs');
  const path = process.env.AIRLOCK_DEVICE_TOKEN_FILE;
  if (!path) throw new Error('Set AIRLOCK_DEVICE_TOKEN_FILE to the private enrolled device token file');
  const saved = JSON.parse(await readFile(path, 'utf8'));
  if (typeof saved.token !== 'string' || !saved.token) throw new Error('Token file has no token');
  const client = new AirlockHttp({ token: saved.token });
  const index = process.argv.indexOf('--state-file');
  if (index >= 0 && (!process.argv[index + 1] || process.argv[index + 1].startsWith('--'))) throw new Error('--state-file requires a path');
  const statePath = index < 0 ? `${resolve(path)}.stream-spool/state.json` : resolve(process.argv[index + 1]);
  await runPublicationStream({ input: process.stdin, client, statePath, onPublished: result => console.log(JSON.stringify(result)) });
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => { console.error(`Observation publication stopped; pending spool retained: ${error.message}`); process.exitCode = 1; });
}
