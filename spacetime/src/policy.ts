/** Deterministic policy primitives, shared by the transactional module and tests. */
import { sha256 } from '@noble/hashes/sha2.js';
import { bytesToHex, utf8ToBytes } from '@noble/hashes/utils.js';
export const CORRELATION_WINDOW_MS = 10_000n;
export const MAX_EVENT_AGE_MS = 120_000n;
export const MAX_FUTURE_SKEW_MS = 5_000n;
export type Evidence = { workstationId: string; sessionId: string; observedAtMs: bigint; simulated: boolean };
export function correlates(a: Evidence, b: Evidence): boolean {
  const delta = a.observedAtMs > b.observedAtMs ? a.observedAtMs - b.observedAtMs : b.observedAtMs - a.observedAtMs;
  return a.workstationId === b.workstationId && a.sessionId === b.sessionId && a.simulated === b.simulated && delta <= CORRELATION_WINDOW_MS;
}
export function validateEventTime(observedAtMs: bigint, receivedAtMs: bigint): void {
  if (observedAtMs > receivedAtMs + MAX_FUTURE_SKEW_MS) throw new Error('Event clock is more than 5s in the future');
  if (observedAtMs + MAX_EVENT_AGE_MS < receivedAtMs) throw new Error('Event is stale (older than 120s)');
}
function canonical(value: unknown, depth = 0): string {
  if (depth > 12) throw new Error('JSON nesting too deep');
  if (value === null || typeof value === 'boolean' || typeof value === 'string') return JSON.stringify(value);
  if (typeof value === 'number') { if (!Number.isFinite(value)) throw new Error('Nonfinite number'); return JSON.stringify(value); }
  if (Array.isArray(value)) return '[' + value.map(v => canonical(v, depth + 1)).join(',') + ']';
  if (typeof value === 'object') return '{' + Object.keys(value as object).sort().map(k => JSON.stringify(k) + ':' + canonical((value as Record<string, unknown>)[k], depth + 1)).join(',') + '}';
  throw new Error('Unsupported JSON value');
}
export function canonicalJson(raw: string, maxBytes = 8192): string {
  if (raw.length > maxBytes || utf8ToBytes(raw).byteLength > maxBytes) throw new Error('JSON payload too large');
  const result = canonical(JSON.parse(raw));
  if (result.length > maxBytes || utf8ToBytes(result).byteLength > maxBytes) throw new Error('JSON payload too large');
  return result;
}
export function digest(value: unknown): string { return bytesToHex(sha256(utf8ToBytes(canonical(value)))); }
export function stringArray(raw: string): string[] {
  if (raw.length > 70_000 || utf8ToBytes(raw).byteLength > 70_000) throw new Error('Baseline JSON payload too large');
  const value: unknown = JSON.parse(raw);
  if (!Array.isArray(value) || value.length > 256 || value.some(v => typeof v !== 'string' || v.length < 1 || v.length > 256)) throw new Error('Expected at most 256 bounded string identifiers');
  return [...new Set(value)].sort();
}
export function validId(value: string, field = 'id'): string {
  if (!/^[a-zA-Z0-9_.:-]{1,128}$/.test(value)) throw new Error(`${field} must contain 1-128 letters, digits, _, ., :, or -`);
  return value;
}
