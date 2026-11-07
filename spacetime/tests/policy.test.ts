import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { canonicalJson, correlates, CORRELATION_WINDOW_MS, digest, stringArray, validId, validateEventTime } from '../src/policy';

const event = (time: bigint, changes = {}) => ({ workstationId: 'desk-a', sessionId: 'session-a', observedAtMs: time, simulated: false, ...changes });
describe('deterministic physical/digital correlation', () => {
  for (const delta of [0n, 1n, 9999n, 10000n, 10001n, 60000n]) {
    for (const direction of [-1n, 1n]) it(`inclusive ±10s boundary: ${delta * direction}ms`, () => {
      const physical = event(100000n); const usb = event(100000n + delta * direction);
      assert.equal(correlates(physical, usb), delta <= CORRELATION_WINDOW_MS);
      assert.equal(correlates(usb, physical), delta <= CORRELATION_WINDOW_MS, 'arrival order must not change result');
    });
  }
  for (const changed of [{ workstationId: 'desk-b' }, { sessionId: 'session-b' }, { simulated: true }]) it(`never correlates across ${Object.keys(changed)[0]}`, () => assert.equal(correlates(event(100000n), event(100001n, changed)), false));
  it('is symmetric across a deterministic 2000-case time sweep', () => {
    let seed = 48271;
    for (let n = 0; n < 2000; n++) { seed = (seed * 16807) % 2147483647; const delta = BigInt(seed % 50001 - 25000); const a = event(100000n); const b = event(100000n + delta); assert.equal(correlates(a, b), correlates(b, a)); assert.equal(correlates(a, b), delta >= -10000n && delta <= 10000n); }
  });
});
describe('event freshness', () => {
  it('accepts exact maximum age and rejects 1ms older', () => { validateEventTime(1000n, 121000n); assert.throws(() => validateEventTime(999n, 121000n), /stale/); });
  it('accepts exact future skew and rejects 1ms newer', () => { validateEventTime(126000n, 121000n); assert.throws(() => validateEventTime(126001n, 121000n), /future/); });
});
describe('canonical exact action binding', () => {
  it('ignores object insertion order, preserves array order and exact values', () => {
    assert.equal(canonicalJson('{"z":1,"a":{"b":2,"a":1}}'), '{"a":{"a":1,"b":2},"z":1}');
    assert.equal(digest({args: {b:2,a:1}, scope:'tests'}), digest({scope:'tests',args:{a:1,b:2}}));
    assert.notEqual(digest({args:[1,2]}), digest({args:[2,1]}));
  });
  it('every permission-bearing action field changes the SHA256 digest', () => {
    const action = { workstationId: 'desk', actionId: 'a', missionId: 'm', incidentId: 'i', toolName: 'pytest', args: {path:'tests'}, scope:'sandbox', expiresAtMs:'1000' };
    const original = digest(action); assert.match(original, /^[a-f0-9]{64}$/);
    for (const field of Object.keys(action)) assert.notEqual(digest({...action,[field]:field === 'args' ? {path:'secrets'} : 'changed'}), original, field);
  });
  it('rejects oversized, malformed and deeply nested JSON', () => {
    assert.throws(() => canonicalJson('not JSON')); assert.throws(() => canonicalJson(JSON.stringify('x'.repeat(9000))), /large/);
    assert.throws(() => canonicalJson('['.repeat(20) + '1' + ']'.repeat(20)), /deep/);
  });
});
describe('bounded owner baselines and identifiers', () => {
  it('normalizes baseline identifiers and removes duplicates', () => assert.deepEqual(stringArray('["b","a","b"]'), ['a','b']));
  for (const raw of ['{}', '[1]', '[""]', JSON.stringify(Array.from({length:257}, (_,i)=>String(i)))]) it(`rejects invalid baseline ${raw.slice(0,15)}`, () => assert.throws(() => stringArray(raw)));
  it('rejects paths, whitespace, shell text and overlong ids', () => { for (const id of ['../../secrets', 'a b', '$()','x'.repeat(129),'']) assert.throws(() => validId(id)); assert.equal(validId('safe-id:1.a'), 'safe-id:1.a'); });
});
it('JSON limits measure actual UTF8 bytes rather than UTF16 character counts', () => {
  assert.throws(() => canonicalJson(JSON.stringify('界'.repeat(3000))), /large/);
  assert.throws(() => stringArray(JSON.stringify(['界'.repeat(30000)])), /large/);
});
