/** Transport fixtures only: no sensor, microphone, model, or live database. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile, rm, stat, chmod, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { PassThrough, Readable } from 'node:stream';
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { AirlockHttp, AirlockHttpError } from './airlock_http.mjs';
import { boundedJsonLines, MAX_LINE_BYTES, runPublicationStream } from './airlock_publish_stream.mjs';
import { MAX_PENDING_EVENTS, MAX_STATE_BYTES, openPublicationQueue, streamBinding } from './airlock_publish_stream_state.mjs';

function event(seq = 1, extra = {}) {
  return { schema_version: 1, workstation_id: 'transport-test', session_id: 'test-session', device_id: 'test-mac',
    event_id: `test-event-${seq}`, source: 'mac', kind: 'app_launched', observed_at: Date.now(), seq,
    metadata: { appId: 'fixture:transport-only', label: 'TEST fixture, no genuine app capture' }, simulated: false, ...extra };
}
function client(call: (reducer: string, args: any[]) => Promise<unknown>) {
  const result = new AirlockHttp({ uri: 'http://127.0.0.1:3000', database: 'transport-test', token: 'PRIVATE_TEST_TOKEN' });
  result.call = call;
  return result;
}
async function fixture(t: any) {
  const directory = await mkdtemp(join(tmpdir(), 'airlock-stream-test-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  return { directory, statePath: join(directory, 'state.json') };
}
const empty = () => Readable.from([]);
const lines = (...events: any[]) => Readable.from(events.map(value => `${JSON.stringify(value)}\n`));
const quiet = () => {};
async function eventually(predicate: () => Promise<boolean>) {
  for (let i = 0; i < 200; i++) { if (await predicate()) return; await new Promise(done => setTimeout(done, 10)); }
  throw new Error('Condition did not become true');
}

test('event payload is durable and private before network delivery and ACK removes it', async t => {
  const { statePath } = await fixture(t), observation = event();
  let sent = 0;
  const transport = client(async (_reducer, args) => {
    sent++;
    const state = JSON.parse(await readFile(statePath, 'utf8'));
    assert.deepEqual(state.pending, [observation]);
    assert.equal(args[3], observation.event_id);
    assert.equal((await stat(statePath)).mode & 0o777, 0o600);
  });
  await runPublicationStream({ input: lines(observation), client: transport, statePath, onDegraded: quiet });
  assert.equal(sent, 1);
  assert.deepEqual(JSON.parse(await readFile(statePath, 'utf8')).pending, []);
  assert.ok(!(await readFile(statePath, 'utf8')).includes('PRIVATE_TEST_TOKEN'));
});

test('retry exhaustion retains exact payload and restart replays before new higher sequences', async t => {
  const { statePath } = await fixture(t), first = event(), second = event(2);
  let attempts = 0;
  const broken = client(async () => { attempts++; throw new Error('transport offline'); });
  await assert.rejects(runPublicationStream({ input: lines(first), client: broken, statePath, attempts: 2, sleep: async () => {}, onDegraded: quiet }), /offline/);
  assert.equal(attempts, 2);
  assert.deepEqual(JSON.parse(await readFile(statePath, 'utf8')).pending, [first]);
  const sent: string[] = [];
  await runPublicationStream({ input: lines(second), client: client(async (_name, args) => { sent.push(args[3]); }), statePath, onDegraded: quiet });
  assert.deepEqual(sent, [first.event_id, second.event_id]);
  assert.deepEqual(JSON.parse(await readFile(statePath, 'utf8')).pending, []);
});

test('an explicit reducer denial preserves the head for operator review', async t => {
  const { statePath } = await fixture(t), observation = event();
  let calls = 0;
  await assert.rejects(runPublicationStream({ input: lines(observation), client: client(async () => {
    calls++; throw new AirlockHttpError('ingest_device_event', 400, 'No matching live armed session');
  }), statePath, onDegraded: quiet }), /live armed session/);
  assert.equal(calls, 1);
  assert.deepEqual(JSON.parse(await readFile(statePath, 'utf8')).pending, [observation]);
});

test('old loaded events get a dedup attempt but never claim current app coverage', async t => {
  const { statePath } = await fixture(t);
  const transport = client(async () => {}), observation = event(1, { observed_at: Date.now() - 180000, metadata: { appId: 'fixture:old', captureMode: 'NSWorkspace.didLaunchApplicationNotification' } });
  const queue = await openPublicationQueue(statePath, streamBinding(transport));
  await queue.enqueue(observation); await queue.close();
  const calls: string[] = [], outcomes: any[] = [];
  transport.call = async (name: string) => { calls.push(name); };
  await runPublicationStream({ input: empty(), client: transport, statePath, onPublished: (value: any) => outcomes.push(value), onDegraded: quiet });
  assert.deepEqual(calls, ['ingest_device_event']);
  assert.equal(outcomes[0].replayed, true);
  assert.deepEqual(JSON.parse(await readFile(statePath, 'utf8')).pending, []);
});

test('only fresh new input reports app health; health failure does not undo committed ingestion', async t => {
  const { statePath } = await fixture(t), calls: string[] = [], warnings: string[] = [];
  const meta = { appId: 'fixture:app', captureMode: 'NSWorkspace.didLaunchApplicationNotification' };
  const transport = client(async name => { calls.push(name); if (name === 'report_health') throw new Error('health transport offline'); });
  const input = new PassThrough();
  const running = runPublicationStream({ input, client: transport, statePath, onDegraded: (message: string) => warnings.push(message) });
  input.write(`${JSON.stringify(event(1, { metadata: meta }))}\n${JSON.stringify(event(2, { observed_at: Date.now() - 16000, metadata: meta }))}\n`);
  try { await eventually(async () => calls.length === 3); } finally { input.end(); }
  await running;
  assert.deepEqual(calls, ['ingest_device_event', 'report_health', 'ingest_device_event']);
  assert.match(warnings[0], /committed/);
  assert.deepEqual(JSON.parse(await readFile(statePath, 'utf8')).pending, []);
});

test('malformed input during an in-flight commit cannot advertise the stopped observer online', async t => {
  const { statePath } = await fixture(t), input = new PassThrough(), calls: string[] = [];
  let release!: () => void;
  const blocked = new Promise<void>(done => { release = done; });
  const transport = client(async name => { calls.push(name); await blocked; });
  const running = runPublicationStream({ input, client: transport, statePath, onDegraded: quiet });
  const rejected = assert.rejects(running, /JSON/);
  input.write(`${JSON.stringify(event(1, { metadata: { appId: 'fixture:app', captureMode: 'NSWorkspace.didLaunchApplicationNotification' } }))}\n`);
  try {
    await eventually(async () => calls.length === 1);
    input.end('{partial invalid JSON');
    await eventually(async () => input.destroyed);
  } finally { release(); }
  await rejected;
  assert.deepEqual(calls, ['ingest_device_event']);
  assert.deepEqual(JSON.parse(await readFile(statePath, 'utf8')).pending, []);
});

test('intake spools later lines while an earlier network call is waiting', async t => {
  const { statePath } = await fixture(t), observations = [event(1), event(2), event(3)], sent: string[] = [];
  let release!: () => void;
  const blocked = new Promise<void>(done => { release = done; });
  const transport = client(async (_name, args) => { sent.push(args[3]); if (args[3] === observations[0].event_id) await blocked; });
  const running = runPublicationStream({ input: lines(...observations), client: transport, statePath, onDegraded: quiet });
  try {
    await eventually(async () => { try { return JSON.parse(await readFile(statePath, 'utf8')).pending.length === 3; } catch { return false; } });
    assert.deepEqual(sent, [observations[0].event_id]);
  } finally { release(); }
  await running;
  assert.deepEqual(sent, observations.map(value => value.event_id));
});

test('queue bounds do not evict evidence, duplicate IDs are idempotent, changed payloads fail', async t => {
  const { statePath } = await fixture(t), transport = client(async () => {});
  const queue = await openPublicationQueue(statePath, streamBinding(transport));
  try {
    const first = event();
    await queue.enqueue(first); await queue.enqueue(first);
    assert.equal(queue.size, 1);
    await assert.rejects(queue.enqueue({ ...first, kind: 'heartbeat', metadata: {} }), /conflict/);
    for (let seq = 2; seq <= MAX_PENDING_EVENTS; seq++) await queue.enqueue(event(seq));
    await assert.rejects(queue.enqueue(event(MAX_PENDING_EVENTS + 1)), /full/);
    assert.equal(queue.size, 128);
    assert.ok((await stat(statePath)).size <= MAX_STATE_BYTES);
    assert.equal(queue.peek().event_id, first.event_id);
  } finally { await queue.close(); }
});

test('corrupt, oversized, or destination-mismatched state is retained and fails closed', async t => {
  const { statePath } = await fixture(t), binding = streamBinding(client(async () => {}));
  await writeFile(statePath, 'not JSON', { mode: 0o600 });
  await assert.rejects(openPublicationQueue(statePath, binding), /JSON/);
  assert.equal(await readFile(statePath, 'utf8'), 'not JSON');
  await writeFile(statePath, 'x'.repeat(MAX_STATE_BYTES + 1));
  await assert.rejects(openPublicationQueue(statePath, binding), /storage limit/);
  await rm(statePath);
  const queue = await openPublicationQueue(statePath, binding); await queue.enqueue(event()); await queue.close();
  const before = await readFile(statePath, 'utf8');
  await assert.rejects(openPublicationQueue(statePath, { ...binding, database: 'other-destination' }), /different endpoint/);
  assert.equal(await readFile(statePath, 'utf8'), before);
});

test('insecure files, symlinks, live locks, and unknown locks are never silently replaced', async t => {
  const { directory, statePath } = await fixture(t), binding = streamBinding(client(async () => {}));
  const queue = await openPublicationQueue(statePath, binding);
  await assert.rejects(openPublicationQueue(statePath, binding), /live process/);
  await queue.close();
  await chmod(statePath, 0o644);
  await assert.rejects(openPublicationQueue(statePath, binding), /0600/);
  await chmod(statePath, 0o600);
  const target = join(directory, 'target.json');
  await writeFile(target, '{}', { mode: 0o600 }); await rm(statePath); await symlink(target, statePath);
  await assert.rejects(openPublicationQueue(statePath, binding));
  assert.equal(await readFile(target, 'utf8'), '{}');
  await rm(statePath);
  await writeFile(`${statePath}.lock`, 'unknown lock', { mode: 0o600 });
  await assert.rejects(openPublicationQueue(statePath, binding));
  assert.equal(await readFile(`${statePath}.lock`, 'utf8'), 'unknown lock');
});

test('line framing bounds bytes across chunks and rejects malformed partial JSON without a spool write', async () => {
  const text = `${JSON.stringify(event(1, { metadata: { appId: 'fixture:café' } }))}\r\n`;
  const bytes = Buffer.from(text), split = bytes.indexOf(Buffer.from('é')) + 1;
  const framed = [];
  for await (const line of boundedJsonLines(Readable.from([bytes.subarray(0, split), bytes.subarray(split)]))) framed.push(JSON.parse(line));
  assert.equal(framed[0].metadata.appId, 'fixture:café');
  await assert.rejects(async () => { for await (const _line of boundedJsonLines(Readable.from([Buffer.alloc(MAX_LINE_BYTES), Buffer.from('x')]))) {} }, /8192/);
});

test('actual consumer process survives commit/ACK loss and SIGKILL via exact replay on restart', { timeout: 15000 }, async t => {
  const { directory, statePath } = await fixture(t), tokenFile = join(directory, 'token.json'), observation = event();
  await writeFile(tokenFile, JSON.stringify({ token: 'PRIVATE_TEST_TOKEN' }), { mode: 0o600 });
  const requests: any[][] = [], commits = new Map<string, string>();
  let firstRequest!: () => void;
  const committed = new Promise<void>(done => { firstRequest = done; });
  const server = createServer(async (request, response) => {
    const chunks = []; for await (const chunk of request) chunks.push(chunk);
    const args = JSON.parse(Buffer.concat(chunks).toString());
    requests.push(args);
    const payload = JSON.stringify(args);
    if (commits.has(args[3])) assert.equal(commits.get(args[3]), payload);
    else commits.set(args[3], payload);
    if (requests.length === 1) {
      assert.deepEqual(JSON.parse(await readFile(statePath, 'utf8')).pending, [observation]);
      request.socket.destroy(); firstRequest();
    } else { response.writeHead(200); response.end(); }
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(() => new Promise<void>(done => server.close(() => done())));
  const address = server.address() as { port: number };
  const start = () => {
    const child = spawn(process.execPath, [resolve('scripts/airlock_publish_stream.mjs'), '--real-observation', '--state-file', statePath], {
      env: { ...process.env, AIRLOCK_DEVICE_TOKEN_FILE: tokenFile, AIRLOCK_SPACETIME_URI: `http://127.0.0.1:${address.port}`, AIRLOCK_DATABASE: 'transport-test' }, stdio: ['pipe', 'pipe', 'pipe'],
    });
    t.after(() => { if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL'); });
    let stderr = ''; child.stderr.on('data', value => { stderr += value; });
    const exited = once(child, 'exit');
    return { child, exited, errors: () => stderr };
  };
  const first = start(); first.child.stdin.end(`${JSON.stringify(observation)}\n`);
  await committed; first.child.kill('SIGKILL'); await first.exited;
  assert.deepEqual(JSON.parse(await readFile(statePath, 'utf8')).pending, [observation]);
  const second = start(); second.child.stdin.end();
  const [code] = await second.exited;
  assert.equal(code, 0, second.errors());
  assert.equal(commits.size, 1);
  assert.equal(requests.length, 2);
  assert.deepEqual(requests[1], requests[0]);
  assert.deepEqual(JSON.parse(await readFile(statePath, 'utf8')).pending, []);
});
