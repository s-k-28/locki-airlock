/** Private bounded consumer spool. Contains redacted events, never credentials. */
import { constants } from 'node:fs';
import { open, lstat, mkdir, rename, unlink, link } from 'node:fs/promises';
import { dirname, basename, resolve } from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { validateSecurityEvent } from './airlock_publisher.mjs';

export const MAX_PENDING_EVENTS = 128;
export const MAX_STATE_BYTES = 1024 * 1024;
const clone = value => JSON.parse(JSON.stringify(value));
const uid = () => process.getuid?.();

export function streamBinding(client) {
  return { uri: client.uri, database: client.database, tokenFingerprint: createHash('sha256').update(client.token).digest('hex') };
}

export function realStreamEvent(event) {
  validateSecurityEvent(event);
  if (event.simulated || event.source === 'fixture') throw new Error('This stream accepts real observations only; do not mix fixture and sensor provenance');
  return event;
}

async function privateFile(path) {
  const handle = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const info = await handle.stat();
    if (!info.isFile() || (uid() !== undefined && info.uid !== uid()) || (info.mode & 0o077)) throw new Error('Spool files must be operator-owned regular files with mode 0600');
    return handle;
  } catch (error) { await handle.close(); throw error; }
}

async function readPrivate(path, limit) {
  const handle = await privateFile(path);
  try {
    if ((await handle.stat()).size > limit) throw new Error('Spool file exceeds its bounded storage limit');
    const bytes = await handle.readFile();
    if (bytes.byteLength > limit) throw new Error('Spool file exceeds its bounded storage limit');
    return bytes.toString('utf8');
  } finally { await handle.close(); }
}

async function syncDirectory(path) {
  const directory = await open(path, constants.O_RDONLY);
  try { await directory.sync(); } finally { await directory.close(); }
}

async function writeExclusive(path, text) {
  const handle = await open(path, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
  try { await handle.writeFile(text); await handle.sync(); } finally { await handle.close(); }
}

async function atomicState(path, state) {
  const text = `${JSON.stringify(state)}\n`;
  if (Buffer.byteLength(text) > MAX_STATE_BYTES) throw new Error('Spool file exceeds its bounded storage limit');
  const temporary = resolve(dirname(path), `.${basename(path)}.${randomUUID()}.tmp`);
  try {
    await writeExclusive(temporary, text);
    await rename(temporary, path);
    await syncDirectory(dirname(path));
  } finally { await unlink(temporary).catch(error => { if (error.code !== 'ENOENT') throw error; }); }
}

function lockOwner(raw) {
  const value = JSON.parse(raw);
  if (!Number.isSafeInteger(value?.pid) || value.pid <= 0 || typeof value.nonce !== 'string' || !/^[a-f0-9-]{36}$/.test(value.nonce)) throw new Error('Unrecognized spool lock; inspect it manually without deleting an unknown lock');
  return value;
}

async function acquireLock(path) {
  const lock = `${path}.lock`, recovery = `${lock}.recovery`;
  const owner = { pid: process.pid, nonce: randomUUID() };
  // A recovery hard link serializes reclaimers. Never remove an unknown or
  // abandoned recovery marker automatically; that requires operator inspection.
  try { await lstat(recovery); throw new Error('Spool lock recovery is incomplete; inspect it manually'); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  try { await writeExclusive(lock, JSON.stringify(owner)); }
  catch (error) {
    if (error.code !== 'EEXIST') throw error;
    const previous = lockOwner(await readPrivate(lock, 1024));
    try { process.kill(previous.pid, 0); throw new Error('Spool is already in use by a live process'); }
    catch (status) { if (status.code !== 'ESRCH') throw status; }
    // Only a demonstrably dead PID is reclaimable. The hard link also lets us
    // verify the exact inode/nonce before touching the canonical lock.
    await link(lock, recovery);
    try {
      const snapshot = lockOwner(await readPrivate(recovery, 1024));
      const [currentInfo, savedInfo] = await Promise.all([lstat(lock), lstat(recovery)]);
      if (snapshot.nonce !== previous.nonce || currentInfo.ino !== savedInfo.ino || currentInfo.dev !== savedInfo.dev) throw new Error('Spool lock changed during recovery; manual review required');
      await unlink(lock);
      await writeExclusive(lock, JSON.stringify(owner));
    } finally { await unlink(recovery); }
  }
  await syncDirectory(dirname(path));
  return async () => {
    const current = lockOwner(await readPrivate(lock, 1024));
    if (current.nonce !== owner.nonce) throw new Error('Spool lock ownership changed; refusing to remove it');
    await unlink(lock);
    await syncDirectory(dirname(path));
  };
}

function validateState(state, binding) {
  if (!state || state.version !== 1 || Object.keys(state).sort().join(',') !== 'binding,pending,version' || JSON.stringify(state.binding) !== JSON.stringify(binding) || !Array.isArray(state.pending)) throw new Error('Spool is corrupt or belongs to different endpoint/database/credentials; retained for manual review');
  if (state.pending.length > MAX_PENDING_EVENTS) throw new Error('Spool contains too many pending events; retained for manual review');
  const ids = new Set();
  for (const event of state.pending) {
    realStreamEvent(event);
    if (ids.has(event.event_id)) throw new Error('Spool contains duplicate event IDs; retained for manual review');
    ids.add(event.event_id);
  }
}

export async function openPublicationQueue(statePath, binding) {
  const path = resolve(statePath), parent = dirname(path);
  await mkdir(parent, { recursive: true, mode: 0o700 });
  const info = await lstat(parent);
  if (!info.isDirectory() || info.isSymbolicLink() || (uid() !== undefined && info.uid !== uid()) || (info.mode & 0o077)) throw new Error('Spool directory must be operator-owned and private (0700)');
  const release = await acquireLock(path);
  try {
    let state;
    try { state = JSON.parse(await readPrivate(path, MAX_STATE_BYTES)); }
    catch (error) {
      if (error.code !== 'ENOENT') throw error;
      state = { version: 1, binding: clone(binding), pending: [] };
      await atomicState(path, state);
    }
    validateState(state, binding);
    let operations = Promise.resolve(), closed = false;
    const update = operation => {
      const next = operations.then(() => {
        if (closed) throw new Error('Spool is closed');
        return operation();
      });
      operations = next.catch(() => {});
      return next;
    };
    return {
      get size() { return state.pending.length; },
      peek() { return state.pending.length ? clone(state.pending[0]) : undefined; },
      snapshot() { return clone(state.pending); },
      enqueue(event) {
        const observation = clone(realStreamEvent(event));
        return update(async () => {
          const existing = state.pending.find(item => item.event_id === observation.event_id);
          if (existing) {
            if (JSON.stringify(existing) !== JSON.stringify(observation)) throw new Error('Pending event ID conflicts with a different payload');
            return;
          }
          if (state.pending.length >= MAX_PENDING_EVENTS) throw new Error('Spool is full; no pending evidence was dropped');
          const next = { ...state, pending: [...state.pending, observation] };
          await atomicState(path, next);
          state = next;
        });
      },
      acknowledge(eventId) {
        return update(async () => {
          if (state.pending[0]?.event_id !== eventId) throw new Error('Only the committed head event can be acknowledged');
          const next = { ...state, pending: state.pending.slice(1) };
          await atomicState(path, next);
          state = next;
        });
      },
      async close() { await operations; closed = true; await release(); },
    };
  } catch (error) { await release(); throw error; }
}
