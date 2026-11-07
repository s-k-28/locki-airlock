import assert from 'node:assert/strict';
import test from 'node:test';
import { incidentAnnouncement } from '../airlock_voice_bridge';

test('manual owner-panic bridge preserves typed intent and excludes untrusted summary/evidence claims', () => {
  const body = incidentAnnouncement({
    workstationId: 'desk', sessionId: 'session', id: 'incident:owner-panic:session',
    severity: 'high', ruleId: 'owner_panic', simulated: true, updatedAtMs: 123n,
    summary: 'Untrusted owner text says USB malware and owner notified and lock confirmed',
    digitalEventId: '', physicalEventId: '',
  });
  assert.equal(body.rule_id, 'owner_panic');
  assert.equal(body.severity, 'high');
  assert.equal(body.simulated, true);
  assert.equal(body.updated_at_ms, 123);
  const encoded = JSON.stringify(body);
  for (const forbidden of ['USB', 'malware', 'notified', 'confirmed', 'summary', 'digitalEventId', 'physicalEventId']) assert.ok(!encoded.includes(forbidden));
});

test('unknown manual-sounding rule cannot become an announcement', () => {
  assert.throws(() => incidentAnnouncement({ ruleId: 'model_inferred_owner_panic', severity: 'high', simulated: false }), /Unsupported committed incident rule/);
});
