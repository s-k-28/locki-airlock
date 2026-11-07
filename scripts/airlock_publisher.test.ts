import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { eventToReducerArgs, validateSecurityEvent } from './airlock_publisher.mjs';
import type { SecurityEventV1 } from './airlock_contract';

function event(): SecurityEventV1 {
  return {
    schema_version: 1, workstation_id: 'desk', session_id: 'session',
    device_id: 'mac-usb', event_id: 'mac-boot1-event1', source: 'mac',
    kind: 'usb_added', observed_at: 1794084000000, seq: 1, confidence: 0,
    metadata: { deviceKey: 'usb:sha256:redacted', label: 'USB peripheral' },
    simulated: false,
  };
}

for (const seq of [0, -1, 1.5, Number.NaN, Number.POSITIVE_INFINITY, Number.MAX_SAFE_INTEGER + 1]) {
  test(`publisher rejects invalid sequence ${seq}`, () => {
    assert.throws(() => validateSecurityEvent({ ...event(), seq }), /seq must be a positive safe integer/);
  });
}

for (const seq of [1, 1000001, Number.MAX_SAFE_INTEGER]) {
  test(`publisher accepts positive safe counter ${seq} without imposing a stateful delta cap`, () => {
    const observation = { ...event(), seq };
    assert.equal(validateSecurityEvent(observation), observation);
    assert.equal(eventToReducerArgs(observation)[6], seq);
  });
}

test('publisher rejects unknown envelope fields instead of silently dropping them', () => {
  for (const extra of [{ workstationId: 'wrong-naming' }, { debug: true }, { raw_audio: 'unwanted' }]) {
    assert.throws(() => eventToReducerArgs({ ...event(), ...extra }), /unknown envelope fields/);
  }
});

test('portable event maps to the exact eleven reducer arguments and preserves zero confidence', () => {
  assert.deepEqual(eventToReducerArgs(event()), [
    'desk', 'session', 'mac-usb', 'mac-boot1-event1', 'usb_added',
    1794084000000, 1, 0,
    '{"deviceKey":"usb:sha256:redacted","label":"USB peripheral"}', false, 1,
  ]);
});

test('omitting optional confidence retains the existing reducer default', () => {
  const observation = event();
  delete observation.confidence;
  const args = eventToReducerArgs(observation);
  assert.equal(args.length, 11);
  assert.equal(args[7], 1);
});

test('portable schema publishes the same positive counter floor and closed envelope', () => {
  const schema = JSON.parse(readFileSync(new URL('../docs/airlock/SecurityEventV1.schema.json', import.meta.url), 'utf8'));
  assert.equal(schema.properties.seq.minimum, 1);
  assert.equal(schema.properties.seq.maximum, Number.MAX_SAFE_INTEGER);
  assert.equal(schema.additionalProperties, false);
});
