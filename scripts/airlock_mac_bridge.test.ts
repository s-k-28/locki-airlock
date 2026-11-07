import { test } from 'node:test';
import assert from 'node:assert/strict';
import { changes, validateSnapshot } from './airlock_mac_bridge';
const key = (n: string) => `usb:sha256:${n.repeat(64)}`;
const snapshot = { baseline: [key('a')], devices: [{ deviceKey: key('a'), label: 'USB 05ac:1234' }], observed_at: Date.now(), snapshot_duration_ms: 12, simulated: false };
test('real snapshots require redacted stable device keys and honest provenance', () => {
  assert.equal(validateSnapshot(snapshot), snapshot);
  assert.throws(() => validateSnapshot({ ...snapshot, simulated: true }));
  assert.throws(() => validateSnapshot({ ...snapshot, baseline: ['RAW-SERIAL'] }));
  assert.throws(() => validateSnapshot({ ...snapshot, devices: [{ deviceKey: key('a'), label: 'Personal serial number' }] }));
});
test('unchanged and reordered USB sets do not produce duplicate insertion warnings', () => {
  assert.deepEqual(changes([key('a')], snapshot), []);
  const both = { ...snapshot, baseline: [key('b'), key('a')], devices: [{ deviceKey: key('b'), label: 'USB 1234:5678' }, ...snapshot.devices] };
  assert.deepEqual(changes([key('a'), key('b')], both), []);
});
test('USB additions and removals retain observation type and only redacted metadata', () => {
  const result = changes([key('b')], snapshot);
  assert.equal(result.length, 2);
  assert.equal(result[0].kind, 'usb_added');
  assert.equal(result[0].metadata.deviceKey, key('a'));
  assert.equal(result[1].kind, 'usb_removed');
  assert.equal(result[1].metadata.deviceKey, key('b'));
});
