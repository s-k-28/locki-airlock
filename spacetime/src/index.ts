import { schema, table, t, SenderError, type InferSchema, type ReducerCtx, type ViewCtx } from 'spacetimedb/server';
import { Identity, ScheduleAt } from 'spacetimedb';
import { canonicalJson as uncheckedCanonicalJson, correlates, digest, stringArray as uncheckedStringArray, validId as uncheckedValidId, validateEventTime as uncheckedValidateEventTime } from './policy';

// Security state is private. Every client-facing view follows the caller's membership index.
const workstations = table({ name: 'workstations', public: false }, {
  id: t.string().primaryKey(), name: t.string(), owner: t.identity().index('btree'), createdAtMs: t.u64(), activeSessionId: t.string(), autoLockEnabled: t.bool(), policyVersion: t.u32(), ownerPolicyHash: t.string(), lockCooldownMs: t.u64(), lastLockRequestedAtMs: t.u64(), returnGraceUntilMs: t.u64(),
});
const memberships = table({ name: 'memberships', public: false }, {
  id: t.string().primaryKey(), workstationId: t.string().index('btree'), identity: t.identity().index('btree'), role: t.string(), canApprove: t.bool(), enabled: t.bool(), createdAtMs: t.u64(),
});
const devices = table({ name: 'devices', public: false }, {
  id: t.string().primaryKey(), workstationId: t.string().index('btree'), identity: t.identity().index('btree'), role: t.string(), label: t.string(), enabled: t.bool(), lastSeq: t.u64(), lastEventAtMs: t.u64(),
});
const sessions = table({ name: 'sessions', public: false }, {
  id: t.string().primaryKey(), workstationId: t.string().index('btree'), state: t.string(), armedBy: t.identity(), armedAtMs: t.u64(), expiresAtMs: t.u64(), disarmedAtMs: t.u64(), baselineJson: t.string(), approvedAppsJson: t.string(), autoLockEnabled: t.bool(), ownerPolicyHash: t.string(), simulated: t.bool(),
});
// Latest authenticated adapter capture and immutable per-session copies; additive migration only.
const baselineSnapshots = table({ name: 'baseline_snapshots', public: false }, {
  id: t.string().primaryKey(), workstationId: t.string().index('btree'), deviceId: t.string(), publisher: t.identity(), observedAtMs: t.u64(), receivedAtMs: t.u64(), baselineJson: t.string(), activeAppId: t.string(), sessionId: t.string(), cameraStatus: t.string(), healthJson: t.string(),
});
const deviceEvents = table({ name: 'device_events', public: false }, {
  id: t.string().primaryKey(), workstationId: t.string().index('btree'), sessionId: t.string().index('btree'), deviceId: t.string(), source: t.string(), kind: t.string(), observedAtMs: t.u64(), receivedAtMs: t.u64(), seq: t.u64(), confidence: t.f64(), metadataJson: t.string(), simulated: t.bool(), schemaVersion: t.u32(), classification: t.string(),
});
const incidents = table({ name: 'incidents', public: false }, {
  id: t.string().primaryKey(), workstationId: t.string().index('btree'), sessionId: t.string().index('btree'), severity: t.string(), state: t.string(), ruleId: t.string(), summary: t.string(), eventIdsJson: t.string(), digitalEventId: t.string(), physicalEventId: t.string(), createdAtMs: t.u64(), updatedAtMs: t.u64(), acknowledgedBy: t.string(), resolvedBy: t.string(), resolutionNote: t.string(), actionRequired: t.bool(), simulated: t.bool(),
});
const deviceHealth = table({ name: 'device_health', public: false }, {
  id: t.string().primaryKey(), workstationId: t.string().index('btree'), deviceId: t.string(), component: t.string(), status: t.string(), lastSeenMs: t.u64(), detail: t.string(),
});
const lockRequests = table({ name: 'lock_requests', public: false }, {
  id: t.string().primaryKey(), workstationId: t.string().index('btree'), sessionId: t.string(), incidentId: t.string(), targetDeviceId: t.string(), status: t.string(), issuedAtMs: t.u64(), expiresAtMs: t.u64(), reason: t.string(), ownerPolicyHash: t.string(), simulated: t.bool(), resultDetail: t.string(), updatedAtMs: t.u64(),
});
const lockClaims = table({ name: 'lock_claims', public: false }, { actionId: t.string().primaryKey(), workstationId: t.string().index('btree'), executionId: t.string().unique(), claimedBy: t.identity(), targetDeviceId: t.string(), claimedAtMs: t.u64(), expiresAtMs: t.u64() });
const protectedActions = table({ name: 'protected_actions', public: false }, {
  id: t.string().primaryKey(), workstationId: t.string().index('btree'), missionId: t.string(), incidentId: t.string(), proposedBy: t.identity(), toolName: t.string(), argsJson: t.string(), scope: t.string(), actionDigest: t.string(), status: t.string(), createdAtMs: t.u64(), expiresAtMs: t.u64(), approvalId: t.string(), executionId: t.string(), consumedBy: t.string(), resultDetail: t.string(),
});
const approvals = table({ name: 'approvals', public: false }, {
  id: t.string().primaryKey(), workstationId: t.string().index('btree'), actionId: t.string(), actionDigest: t.string(), approvedBy: t.identity(), status: t.string(), createdAtMs: t.u64(), expiresAtMs: t.u64(), consumedAtMs: t.u64(), note: t.string(),
});
const agentRuns = table({ name: 'agent_runs', public: false }, {
  id: t.string().primaryKey(), workstationId: t.string().index('btree'), incidentId: t.string(), model: t.string(), status: t.string(), startedAtMs: t.u64(), updatedAtMs: t.u64(), traceId: t.string(),
});
const agentSteps = table({ name: 'agent_steps', public: false }, {
  id: t.string().primaryKey(), workstationId: t.string().index('btree'), runId: t.string(), incidentId: t.string(), step: t.u32(), toolName: t.string(), status: t.string(), evidenceId: t.string(), durationMs: t.u64(), inputType: t.string(), argsJson: t.string(), resultSummary: t.string(), traceId: t.string(), createdAtMs: t.u64(),
});
const acknowledgements = table({ name: 'acknowledgements', public: false }, {
  id: t.u64().primaryKey().autoInc(), workstationId: t.string().index('btree'), incidentId: t.string(), actor: t.identity(), kind: t.string(), note: t.string(), createdAtMs: t.u64(),
});
const auditEvents = table({ name: 'audit_events', public: false }, {
  id: t.u64().primaryKey().autoInc(), workstationId: t.string().index('btree'), actor: t.identity(), kind: t.string(), entityId: t.string(), detail: t.string(), createdAtMs: t.u64(),
});
const maintenanceSchedule = table({ name: 'maintenance_schedule', public: false }, {
  scheduledId: t.u64().primaryKey().autoInc(), scheduledAt: t.scheduleAt(), workstationId: t.string().unique(),
});
const spacetimedb = schema({ lockClaims, baselineSnapshots, maintenanceSchedule, workstations, memberships, devices, sessions, deviceEvents, incidents, deviceHealth, lockRequests, protectedActions, approvals, agentRuns, agentSteps, acknowledgements, auditEvents });
export default spacetimedb;
type Ctx = ReducerCtx<InferSchema<typeof spacetimedb>>;
type ReadCtx = ViewCtx<InferSchema<typeof spacetimedb>>;
const now = (ctx: Ctx) => ctx.timestamp.microsSinceUnixEpoch / 1000n;
function fail(message: string): never { throw new SenderError(message); }
// Spacetime exposes SenderError as a caller policy denial; an ordinary Error is a fatal reducer failure.
function validated<T>(fn: () => T): T { try { return fn(); } catch (error) { return fail(error instanceof Error ? error.message : 'Invalid input'); } }
const canonicalJson = (raw: string, limit?: number) => validated(() => uncheckedCanonicalJson(raw, limit));
const stringArray = (raw: string) => validated(() => uncheckedStringArray(raw));
const validId = (id: string, field?: string) => validated(() => uncheckedValidId(id, field));
const validateEventTime = (observed: bigint, received: bigint) => validated(() => uncheckedValidateEventTime(observed, received));
function text(value: string, limit = 512) { if (value.length > limit) fail('Text exceeds size limit'); return value.trim(); }
function identity(hex: string) { if (!/^[a-fA-F0-9]{64}$/.test(hex)) fail('Expected 64-hex identity'); return Identity.fromString(hex); }
function memberId(ws: string, who: Identity) { return ws + ':' + who.toHexString(); }
function member(ctx: Ctx | ReadCtx, ws: string) { const m = ctx.db.memberships.id.find(memberId(ws, ctx.sender)); if (!m?.enabled) fail('Not an enrolled member of this workstation'); return m; }
function owner(ctx: Ctx, ws: string) { const w = ctx.db.workstations.id.find(ws); if (!w || !w.owner.isEqual(ctx.sender)) fail('Workstation owner required'); member(ctx, ws); return w; }
function revokeOutstandingGrants(ctx: Ctx, ws: string, who: Identity, includeProposals: boolean) {
  for (const action of ctx.db.protectedActions.workstationId.filter(ws)) {
    const approval = action.approvalId ? ctx.db.approvals.id.find(action.approvalId) : undefined;
    if (!['pending', 'approved'].includes(action.status)) continue;
    if ((includeProposals && action.proposedBy.isEqual(who)) || (approval && approval.approvedBy.isEqual(who))) {
      if (approval?.status === 'approved') ctx.db.approvals.id.update({ ...approval, status: 'revoked' });
      ctx.db.protectedActions.id.update({ ...action, status: 'denied', resultDetail: 'Authorization withdrawn when membership or approval permission was revoked.' });
      audit(ctx, ws, 'action_authorization_revoked', action.id);
    }
  }
}
function review(ctx: Ctx, ws: string) { const m = member(ctx, ws); if (!['owner', 'reviewer'].includes(m.role)) fail('Owner or reviewer required'); return m; }
function service(ctx: Ctx, ws: string) { const m = member(ctx, ws); if (m.role !== 'service' || ![...ctx.db.devices.identity.filter(ctx.sender)].some(d => d.workstationId === ws && d.enabled && d.role === 'service')) fail('Matching enabled investigation/gateway service device required'); return m; }
function audit(ctx: Ctx, ws: string, kind: string, entityId: string, detail = '') { ctx.db.auditEvents.insert({ id: 0n, workstationId: ws, actor: ctx.sender, kind, entityId, detail: text(detail, 1024), createdAtMs: now(ctx) }); }
function activeSession(ctx: Ctx, ws: string, id?: string) {
  const w = ctx.db.workstations.id.find(ws); const s = w?.activeSessionId ? ctx.db.sessions.id.find(w.activeSessionId) : undefined;
  if (!s || s.state !== 'armed' || s.expiresAtMs <= now(ctx) || (id !== undefined && s.id !== id)) fail('No matching live armed session');
  return s;
}
function ensureMaintenance(ctx: Ctx, ws: string) {
  if (!ctx.db.maintenanceSchedule.workstationId.find(ws)) ctx.db.maintenanceSchedule.insert({ scheduledId: 0n, scheduledAt: ScheduleAt.interval(5_000_000n), workstationId: ws });
}
function refresh(ctx: Ctx, ws: string) {
  ensureMaintenance(ctx, ws);
  const time = now(ctx); const w = ctx.db.workstations.id.find(ws);
  if (!w) fail('Unknown workstation');
  if (w.activeSessionId) { const s = ctx.db.sessions.id.find(w.activeSessionId); if (s && s.state === 'armed' && s.expiresAtMs <= time) { ctx.db.sessions.id.update({ ...s, state: 'expired', disarmedAtMs: time }); ctx.db.workstations.id.update({ ...w, activeSessionId: '' }); audit(ctx, ws, 'session_expired', s.id); } }
  for (const l of ctx.db.lockRequests.workstationId.filter(ws)) if (['requested', 'executing'].includes(l.status) && l.expiresAtMs <= time) ctx.db.lockRequests.id.update({ ...l, status: 'expired', updatedAtMs: time });
  for (const a of ctx.db.protectedActions.workstationId.filter(ws)) if (['pending', 'approved'].includes(a.status) && a.expiresAtMs <= time) ctx.db.protectedActions.id.update({ ...a, status: 'expired' });
  for (const a of ctx.db.approvals.workstationId.filter(ws)) if (a.status === 'approved' && a.expiresAtMs <= time) ctx.db.approvals.id.update({ ...a, status: 'expired' });
  for (const h of ctx.db.deviceHealth.workstationId.filter(ws)) if (h.component !== 'capacity' && ['online', 'degraded'].includes(h.status) && h.lastSeenMs + 15_000n < time) ctx.db.deviceHealth.id.update({ ...h, status: 'offline', detail: 'No heartbeat in 15 seconds' });
}
// Short metadata retention is an explicit prototype policy, not an archive guarantee.
// Bound writes per tick; use workstation indexes and delete dependents before parents.
const RETENTION_MS = 86_400_000n;
const CLEANUP_DELETE_LIMIT = 200;
function cleanupMetadata(ctx: Ctx, ws: string) {
  const cutoff = now(ctx) - RETENTION_MS; let remaining = CLEANUP_DELETE_LIMIT;
  const remove = (fn: () => unknown) => { fn(); remaining--; };
  const terminalAction = (status: string) => ['succeeded', 'failed', 'uncertain', 'denied', 'expired'].includes(status);
  const actionChangedAt = new Map<string, bigint>();
  for (const entry of ctx.db.auditEvents.workstationId.filter(ws)) if (entry.kind.startsWith('action_') || entry.kind === 'approval_consumed') {
    if (entry.createdAtMs > (actionChangedAt.get(entry.entityId) ?? 0n)) actionChangedAt.set(entry.entityId, entry.createdAtMs);
  }
  const actionOld = (a: { id: string; status: string; createdAtMs: bigint; expiresAtMs: bigint }) => terminalAction(a.status) && a.expiresAtMs <= cutoff && (actionChangedAt.get(a.id) ?? a.createdAtMs) <= cutoff;
  for (const session of ctx.db.sessions.workstationId.filter(ws)) {
    if (session.state === 'armed' || session.disarmedAtMs > cutoff || ctx.db.workstations.id.find(ws)?.activeSessionId === session.id) continue;
    const cases = [...ctx.db.incidents.sessionId.filter(session.id)];
    if (cases.some(i => i.state !== 'resolved' || i.updatedAtMs > cutoff)) continue;
    const caseIds = new Set(cases.map(i => i.id));
    const actions = [...ctx.db.protectedActions.workstationId.filter(ws)].filter(a => caseIds.has(a.incidentId));
    const runs = [...ctx.db.agentRuns.workstationId.filter(ws)].filter(r => caseIds.has(r.incidentId));
    if (actions.some(a => !actionOld(a)) || runs.some(r => !['complete', 'failed'].includes(r.status) || r.updatedAtMs > cutoff)) continue;
    // A retained step can reference any event in its incident's session. Preserve the
    // entire session until every case is resolved and every investigation terminal.
    for (const action of actions) {
      const grants = [...ctx.db.approvals.workstationId.filter(ws)].filter(a => a.actionId === action.id);
      if (remaining < grants.length + 1) return;
      for (const grant of grants) remove(() => ctx.db.approvals.id.delete(grant.id));
      remove(() => ctx.db.protectedActions.id.delete(action.id));
    }
    for (const run of runs) {
      for (const step of ctx.db.agentSteps.workstationId.filter(ws)) if (step.runId === run.id) {
        if (!remaining) return; remove(() => ctx.db.agentSteps.id.delete(step.id));
      }
      if (!remaining) return; remove(() => ctx.db.agentRuns.id.delete(run.id));
    }
    for (const lock of ctx.db.lockRequests.workstationId.filter(ws)) if (lock.sessionId === session.id) {
      if (ctx.db.lockClaims.actionId.find(lock.id)) { if (!remaining) return; remove(() => ctx.db.lockClaims.actionId.delete(lock.id)); }
      if (!remaining) return; remove(() => ctx.db.lockRequests.id.delete(lock.id));
    }
    for (const ack of ctx.db.acknowledgements.workstationId.filter(ws)) if (caseIds.has(ack.incidentId)) {
      if (!remaining) return; remove(() => ctx.db.acknowledgements.id.delete(ack.id));
    }
    for (const incident of cases) {
      if (!remaining) return; remove(() => ctx.db.incidents.id.delete(incident.id));
    }
    for (const event of ctx.db.deviceEvents.sessionId.filter(session.id)) {
      if (!remaining) return; remove(() => ctx.db.deviceEvents.id.delete(event.id));
    }
    for (const baseline of ctx.db.baselineSnapshots.workstationId.filter(ws)) if (baseline.sessionId === session.id) {
      if (!remaining) return; remove(() => ctx.db.baselineSnapshots.id.delete(baseline.id));
    }
    if (!remaining) return; remove(() => ctx.db.sessions.id.delete(session.id));
  }
  // Standalone protected proposals and latest (not frozen) captures have no case parent.
  for (const action of ctx.db.protectedActions.workstationId.filter(ws)) if (!action.incidentId && actionOld(action)) {
    const grants = [...ctx.db.approvals.workstationId.filter(ws)].filter(a => a.actionId === action.id);
    if (remaining < grants.length + 1) return;
    for (const grant of grants) remove(() => ctx.db.approvals.id.delete(grant.id));
    remove(() => ctx.db.protectedActions.id.delete(action.id));
  }
  for (const baseline of ctx.db.baselineSnapshots.workstationId.filter(ws)) if (!baseline.sessionId && baseline.receivedAtMs <= cutoff) {
    if (!remaining) return; remove(() => ctx.db.baselineSnapshots.id.delete(baseline.id));
  }
  // Run origin audit rows are authorization records. Keep them, and case-linked
  // journal rows, for as long as their live parent survives retention.
  const retained = new Set<string>();
  for (const rows of [ctx.db.sessions.workstationId.filter(ws), ctx.db.incidents.workstationId.filter(ws), ctx.db.deviceEvents.workstationId.filter(ws), ctx.db.agentRuns.workstationId.filter(ws), ctx.db.agentSteps.workstationId.filter(ws), ctx.db.protectedActions.workstationId.filter(ws), ctx.db.lockRequests.workstationId.filter(ws)]) for (const row of rows) retained.add(row.id);
  for (const entry of ctx.db.auditEvents.workstationId.filter(ws)) if (entry.createdAtMs <= cutoff && !retained.has(entry.entityId)) {
    if (!remaining) return; remove(() => ctx.db.auditEvents.id.delete(entry.id));
  }
}
const SECURITY_EVENT_CAPACITY = 100_000;
function putHealth(ctx: Ctx, ws: string, deviceId: string, component: string, status: string, detail: string) {
  if (status === 'online' && component !== 'capacity' && ctx.db.workstations.id.find(ws)?.activeSessionId && ctx.db.deviceHealth.id.find(`${ws}:database:capacity`)?.status === 'degraded') { status = 'degraded'; detail = 'Security event capacity reached; disarm and rearm after owner review. Sensor liveness is not effective protection.'; }
  const row = { id: `${ws}:${deviceId}:${component}`, workstationId: ws, deviceId, component, status, lastSeenMs: now(ctx), detail: text(detail) };
  if (ctx.db.deviceHealth.id.find(row.id)) ctx.db.deviceHealth.id.update(row); else ctx.db.deviceHealth.insert(row);
}
function snapshotHealth(ctx: Ctx, ws: string) {
  const time = now(ctx);
  const health = [...ctx.db.deviceHealth.workstationId.filter(ws)].filter(h => {
    const d = ctx.db.devices.id.find(h.deviceId);
    return d?.enabled && d.workstationId === ws;
  }).map(h => ({ deviceId: h.deviceId, component: h.component, status: ['online', 'degraded'].includes(h.status) && h.lastSeenMs + 15_000n < time ? 'offline' : h.status, lastSeenMs: h.lastSeenMs.toString() }));
  const cameras = health.filter(h => h.component === 'camera');
  // No camera report is explicit unavailability, never inferred green health.
  const cameraStatus = cameras.some(h => h.status === 'online') ? 'online' : cameras.some(h => h.status === 'degraded') ? 'degraded' : cameras.some(h => h.status === 'offline') ? 'offline' : 'unavailable';
  return { cameraStatus, healthJson: JSON.stringify(health) };
}
function baselineTime(ctx: Ctx, observedAtMs: bigint) {
  if (observedAtMs > now(ctx) + 5_000n) fail('Baseline clock is more than 5s in the future');
  if (observedAtMs + 30_000n < now(ctx)) fail('Baseline is stale (older than 30s)');
}
function maybeLock(ctx: Ctx, incidentId: string) {
  const i = ctx.db.incidents.id.find(incidentId); if (!i || i.state === 'resolved') fail('Incident unavailable');
  const s = activeSession(ctx, i.workstationId, i.sessionId); const w = ctx.db.workstations.id.find(i.workstationId)!; const time = now(ctx);
  const ownerPanic = i.ruleId === 'owner_panic' && i.acknowledgedBy === w.owner.toHexString();
  if (!s.autoLockEnabled || (!ownerPanic && (!i.digitalEventId || !['unknown_usb', 'correlated_usb_physical'].includes(i.ruleId)))) return;
  const trigger = i.digitalEventId ? ctx.db.deviceEvents.id.find(i.digitalEventId) : undefined;
  // Accepted audit history can be 120s old; only a fresh trigger may request a local lock.
  if (ownerPanic ? i.updatedAtMs + 15000n < time : (!trigger || trigger.observedAtMs + 15_000n < time)) return;
  if (ctx.db.lockRequests.id.find(`lock:${i.id}`)) return;
  if (w.returnGraceUntilMs > time || (w.lastLockRequestedAtMs > 0n && w.lastLockRequestedAtMs + w.lockCooldownMs > time)) return;
  const frozen = ctx.db.baselineSnapshots.id.find(`session:${s.id}`);
  const target = [...ctx.db.devices.workstationId.filter(w.id)].find(d => d.enabled && d.role === 'mac' && (!frozen || (d.id === frozen.deviceId && d.identity.isEqual(frozen.publisher))));
  if (!target && !s.simulated) { audit(ctx, w.id, 'lock_unavailable', i.id, 'No enrolled Mac adapter'); return; }
  ctx.db.lockRequests.insert({ id: `lock:${i.id}`, workstationId: w.id, sessionId: s.id, incidentId: i.id, targetDeviceId: target?.id ?? 'fixture', status: 'requested', issuedAtMs: time, expiresAtMs: time + 15_000n, reason: i.summary, ownerPolicyHash: s.ownerPolicyHash, simulated: s.simulated, resultDetail: '', updatedAtMs: time });
  ctx.db.workstations.id.update({ ...w, lastLockRequestedAtMs: time }); audit(ctx, w.id, 'lock_requested', i.id, s.simulated ? 'SIMULATED: must not execute OS lock' : 'Owner-consented screen-lock request');
}
function ownMemberships(ctx: ReadCtx) { return [...ctx.db.memberships.identity.filter(ctx.sender)].filter(m => m.enabled && (!['mac', 'jetson', 'service'].includes(m.role) || [...ctx.db.devices.identity.filter(ctx.sender)].some(d => d.workstationId === m.workstationId && d.role === m.role && d.enabled))); }
function canReadCases(role: string) { return ['owner', 'reviewer', 'service'].includes(role); }

export const register_workstation = spacetimedb.reducer({ workstationId: t.string(), name: t.string() }, (ctx, a) => {
  validId(a.workstationId, 'workstationId'); if (!text(a.name, 80)) fail('Name required'); if (ctx.db.workstations.id.find(a.workstationId)) fail('Workstation id already registered');
  const hash = digest({ version: 1, autoLockEnabled: false, lockCooldownMs: '30000' });
  ctx.db.workstations.insert({ id: a.workstationId, name: a.name.trim(), owner: ctx.sender, createdAtMs: now(ctx), activeSessionId: '', autoLockEnabled: false, policyVersion: 1, ownerPolicyHash: hash, lockCooldownMs: 30_000n, lastLockRequestedAtMs: 0n, returnGraceUntilMs: 0n });
  ctx.db.memberships.insert({ id: memberId(a.workstationId, ctx.sender), workstationId: a.workstationId, identity: ctx.sender, role: 'owner', canApprove: true, enabled: true, createdAtMs: now(ctx) });
  ensureMaintenance(ctx, a.workstationId); audit(ctx, a.workstationId, 'workstation_registered', a.workstationId);
});
export const enroll_device = spacetimedb.reducer({ workstationId: t.string(), deviceId: t.string(), identityHex: t.string(), role: t.string(), label: t.string() }, (ctx, a) => {
  const w = owner(ctx, a.workstationId); validId(a.deviceId); if (!['mac', 'jetson', 'service'].includes(a.role)) fail('Invalid device role');
  const who = identity(a.identityHex); if (who.isEqual(w.owner)) fail('Owner identity cannot also be a device');
  const old = ctx.db.devices.id.find(a.deviceId); if (old && old.workstationId !== w.id) fail('Device id belongs to another workstation');
  if (old && w.activeSessionId) fail('Disarm before resetting a device sequence');
  if (old) {
    ctx.db.baselineSnapshots.id.delete(`latest:${old.id}`);
    for (const h of ctx.db.deviceHealth.workstationId.filter(w.id)) if (h.deviceId === old.id) ctx.db.deviceHealth.id.delete(h.id);
  }
  const mid = memberId(w.id, who); const m = ctx.db.memberships.id.find(mid); if (m && m.role !== a.role) fail('Identity already has another role');
  const d = { id: a.deviceId, workstationId: w.id, identity: who, role: a.role, label: text(a.label, 80), enabled: true, lastSeq: 0n, lastEventAtMs: 0n };
  if (old) ctx.db.devices.id.update(d); else ctx.db.devices.insert(d);
  const row = { id: mid, workstationId: w.id, identity: who, role: a.role, canApprove: false, enabled: true, createdAtMs: now(ctx) };
  if (m) ctx.db.memberships.id.update(row); else ctx.db.memberships.insert(row);
  if (old && !old.identity.isEqual(who) && ![...ctx.db.devices.identity.filter(old.identity)].some(device => device.workstationId === w.id && device.enabled)) {
    const prior = ctx.db.memberships.id.find(memberId(w.id, old.identity));
    if (prior) ctx.db.memberships.id.update({ ...prior, enabled: false });
    revokeOutstandingGrants(ctx, w.id, old.identity, true);
    audit(ctx, w.id, 'replaced_device_identity_revoked', old.identity.toHexString());
  }
  audit(ctx, w.id, 'device_enrolled', a.deviceId, a.role);
});
export const grant_reviewer = spacetimedb.reducer({ workstationId: t.string(), identityHex: t.string(), canApprove: t.bool() }, (ctx, a) => {
  const w = owner(ctx, a.workstationId); const who = identity(a.identityHex); if (who.isEqual(w.owner)) fail('Owner is already authorized');
  const id = memberId(w.id, who); const old = ctx.db.memberships.id.find(id); if (old && old.role !== 'reviewer') fail('Device identities cannot become reviewers');
  const row = { id, workstationId: w.id, identity: who, role: 'reviewer', canApprove: a.canApprove, enabled: true, createdAtMs: now(ctx) }; if (old) ctx.db.memberships.id.update(row); else ctx.db.memberships.insert(row); if (old?.canApprove && !a.canApprove) revokeOutstandingGrants(ctx, w.id, who, false); audit(ctx, w.id, 'reviewer_granted', who.toHexString());
});
export const revoke_member = spacetimedb.reducer({ workstationId: t.string(), identityHex: t.string() }, (ctx, a) => {
  const w = owner(ctx, a.workstationId); const who = identity(a.identityHex); if (who.isEqual(w.owner)) fail('Cannot revoke owner'); const m = ctx.db.memberships.id.find(memberId(w.id, who)); if (!m) fail('Unknown member'); ctx.db.memberships.id.update({ ...m, enabled: false }); for (const d of ctx.db.devices.identity.filter(who)) if (d.workstationId === w.id) ctx.db.devices.id.update({ ...d, enabled: false }); revokeOutstandingGrants(ctx, w.id, who, true); audit(ctx, w.id, 'member_revoked', who.toHexString());
});
export const set_policy = spacetimedb.reducer({ workstationId: t.string(), autoLockEnabled: t.bool(), lockCooldownMs: t.u64() }, (ctx, a) => {
  const w = owner(ctx, a.workstationId); if (a.lockCooldownMs < 30_000n || a.lockCooldownMs > 3_600_000n) fail('Cooldown must be 30s–1h');
  const version = w.policyVersion + 1; const hash = digest({ version, autoLockEnabled: a.autoLockEnabled, lockCooldownMs: a.lockCooldownMs.toString() });
  ctx.db.workstations.id.update({ ...w, autoLockEnabled: a.autoLockEnabled, lockCooldownMs: a.lockCooldownMs, policyVersion: version, ownerPolicyHash: hash }); audit(ctx, w.id, 'policy_updated', w.id, `Auto lock consent: ${a.autoLockEnabled}; effective next arm`);
});
export const record_baseline = spacetimedb.reducer({ workstationId: t.string(), deviceId: t.string(), observedAtMs: t.u64(), baselineJson: t.string(), activeAppId: t.string() }, (ctx, a) => {
  const m = member(ctx, a.workstationId); const d = ctx.db.devices.id.find(a.deviceId);
  if (m.role !== 'mac' || !d?.enabled || d.role !== 'mac' || d.workstationId !== a.workstationId || !d.identity.isEqual(ctx.sender)) fail('Baseline publisher must be the enrolled matching Mac');
  baselineTime(ctx, a.observedAtMs);
  const baselineJson = JSON.stringify(stringArray(a.baselineJson));
  const activeAppId = text(a.activeAppId, 256); if (!activeAppId) fail('Active application identifier required; use unknown when unavailable');
  const id = `latest:${d.id}`; const old = ctx.db.baselineSnapshots.id.find(id);
  if (old && old.publisher.isEqual(ctx.sender) && old.observedAtMs > a.observedAtMs) fail('Baseline observation time must not regress');
  refresh(ctx, a.workstationId);
  putHealth(ctx, a.workstationId, d.id, 'usb', 'online', 'Authenticated Mac reported a successful redacted USB inventory capture');
  const row = { id, workstationId: a.workstationId, deviceId: d.id, publisher: ctx.sender, observedAtMs: a.observedAtMs, receivedAtMs: now(ctx), baselineJson, activeAppId, sessionId: '', ...snapshotHealth(ctx, a.workstationId) };
  if (old) ctx.db.baselineSnapshots.id.update(row); else ctx.db.baselineSnapshots.insert(row);
});
export const arm_session = spacetimedb.reducer({ workstationId: t.string(), sessionId: t.string(), baselineJson: t.string(), approvedAppsJson: t.string(), durationMs: t.u64(), simulated: t.bool() }, (ctx, a) => {
  owner(ctx, a.workstationId); refresh(ctx, a.workstationId); const w = owner(ctx, a.workstationId); validId(a.sessionId); if (w.activeSessionId) fail('Disarm active session before rearming'); if (ctx.db.sessions.id.find(a.sessionId)) fail('Session id already used');
  if (a.durationMs < 60_000n || a.durationMs > 86_400_000n) fail('Duration must be 1 minute–24 hours');
  let baselineJson = a.baselineJson === '' && !a.simulated ? '' : JSON.stringify(stringArray(a.baselineJson)); const approvedAppsJson = JSON.stringify(stringArray(a.approvedAppsJson));
  if (!a.simulated) {
    const captures = [...ctx.db.baselineSnapshots.workstationId.filter(w.id)].filter(b => {
      const d = ctx.db.devices.id.find(b.deviceId); const m = ctx.db.memberships.id.find(memberId(w.id, b.publisher));
      const usb = ctx.db.deviceHealth.id.find(`${w.id}:${b.deviceId}:usb`);
      return b.sessionId === '' && d?.enabled && d.role === 'mac' && d.workstationId === w.id && d.identity.isEqual(b.publisher) && m?.enabled && m.role === 'mac' && b.observedAtMs + 30_000n >= now(ctx) && b.observedAtMs <= now(ctx) + 5_000n && usb?.status === 'online' && usb.lastSeenMs + 15_000n >= now(ctx);
    }).sort((a, b) => a.observedAtMs === b.observedAtMs ? a.id.localeCompare(b.id) : a.observedAtMs > b.observedAtMs ? -1 : 1);
    const capture = captures[0]; if (!capture) fail('Fresh enrolled Mac baseline and online USB health required before live arming');
    if (baselineJson && baselineJson !== capture.baselineJson) fail('Manual baseline does not match the fresh enrolled Mac capture');
    baselineJson = capture.baselineJson;
    ctx.db.baselineSnapshots.insert({ ...capture, id: `session:${a.sessionId}`, sessionId: a.sessionId, ...snapshotHealth(ctx, w.id) });
  }
  ctx.db.sessions.insert({ id: a.sessionId, workstationId: w.id, state: 'armed', armedBy: ctx.sender, armedAtMs: now(ctx), expiresAtMs: now(ctx) + a.durationMs, disarmedAtMs: 0n, baselineJson, approvedAppsJson, autoLockEnabled: w.autoLockEnabled, ownerPolicyHash: w.ownerPolicyHash, simulated: a.simulated });
  ctx.db.workstations.id.update({ ...w, activeSessionId: a.sessionId });
  if (a.simulated) { const id = `${w.id}:fixture`; if (!ctx.db.devices.id.find(id)) ctx.db.devices.insert({ id, workstationId: w.id, identity: ctx.sender, role: 'fixture', label: 'Owner simulated fixture publisher', enabled: true, lastSeq: 0n, lastEventAtMs: 0n }); }
  ctx.db.deviceHealth.id.delete(`${w.id}:database:capacity`);
  audit(ctx, w.id, 'session_armed', a.sessionId, a.simulated ? 'SIMULATED session' : 'LIVE session');
});
export const disarm_session = spacetimedb.reducer({ workstationId: t.string() }, (ctx, a) => {
  const w = owner(ctx, a.workstationId); const time = now(ctx); ctx.db.deviceHealth.id.delete(`${w.id}:database:capacity`); if (w.activeSessionId) { const s = ctx.db.sessions.id.find(w.activeSessionId); if (s) ctx.db.sessions.id.update({ ...s, state: 'disarmed', disarmedAtMs: time }); }
  for (const l of ctx.db.lockRequests.workstationId.filter(w.id)) if (['requested', 'executing'].includes(l.status)) ctx.db.lockRequests.id.update({ ...l, status: 'cancelled', updatedAtMs: time });
  ctx.db.workstations.id.update({ ...w, activeSessionId: '', returnGraceUntilMs: time + 30_000n }); audit(ctx, w.id, 'session_disarmed', w.activeSessionId);
});

export const ingest_device_event = spacetimedb.reducer({ workstationId: t.string(), sessionId: t.string(), deviceId: t.string(), eventId: t.string(), kind: t.string(), observedAtMs: t.u64(), seq: t.u64(), confidence: t.f64(), metadataJson: t.string(), simulated: t.bool(), schemaVersion: t.u32() }, (ctx, a) => {
  const m = member(ctx, a.workstationId); validId(a.eventId, 'eventId'); if (a.schemaVersion !== 1) fail('Unsupported schema version');
  const fixture = m.role === 'owner' && a.deviceId === 'fixture' && a.simulated;
  const d = ctx.db.devices.id.find(fixture ? `${a.workstationId}:fixture` : a.deviceId);
  if (!d || !d.enabled || d.workstationId !== a.workstationId || !d.identity.isEqual(ctx.sender)) fail('Publisher identity does not match device enrollment');
  const allowed: Record<string, string[]> = { mac: ['usb_added', 'usb_removed', 'app_launched', 'heartbeat'], jetson: ['person_in_zone', 'person_cleared', 'camera_offline', 'camera_online', 'heartbeat'], service: ['heartbeat'], fixture: ['usb_added', 'usb_removed', 'app_launched', 'person_in_zone', 'person_cleared', 'camera_offline', 'camera_online', 'heartbeat'] };
  if (!allowed[d.role]?.includes(a.kind)) fail('Event kind not permitted for publisher role');
  if (!Number.isFinite(a.confidence) || a.confidence < 0 || a.confidence > 1) fail('Confidence must be 0–1');
  const metadataJson = canonicalJson(a.metadataJson, 2048); const meta = JSON.parse(metadataJson) as Record<string, unknown>; if (meta === null || Array.isArray(meta) || typeof meta !== 'object') fail('Metadata must be an object');
  const allowedKeys = ['deviceKey', 'appId', 'label', 'detail', 'captureMode', 'bootId']; if (Object.keys(meta).some(k => !allowedKeys.includes(k)) || Object.values(meta).some(v => typeof v !== 'string' || v.length > 256)) fail('Metadata must contain only bounded redacted string fields');
  if (a.kind.startsWith('usb_') && (typeof meta.deviceKey !== 'string' || !meta.deviceKey)) fail('USB event requires deviceKey');
  if (a.kind === 'app_launched' && (typeof meta.appId !== 'string' || !meta.appId)) fail('App event requires appId');
  const existing = ctx.db.deviceEvents.id.find(a.eventId);
  if (existing) { if (existing.workstationId !== a.workstationId || existing.deviceId !== a.deviceId || existing.sessionId !== a.sessionId || existing.kind !== a.kind || existing.seq !== a.seq || existing.observedAtMs !== a.observedAtMs || existing.simulated !== a.simulated || existing.metadataJson !== metadataJson || existing.confidence !== a.confidence) fail('Event id conflict: replay payload differs'); return; }
  refresh(ctx, a.workstationId); const s = activeSession(ctx, a.workstationId, a.sessionId); if (s.simulated !== a.simulated || (fixture && !s.simulated)) fail('Simulation provenance must match session');
  validateEventTime(a.observedAtMs, now(ctx)); if (a.observedAtMs < s.armedAtMs || a.observedAtMs > s.expiresAtMs) fail('Observation is outside armed session'); if (a.seq <= d.lastSeq) fail('Sequence must increase; owner must disarm/re-enroll to reset device sequence');
  // Reject publication floods without dropping independent sensor events arriving in one millisecond.
  if (a.seq > d.lastSeq + 1_000_000n) fail('Sequence jump too large');
  const sessionEvents = [...ctx.db.deviceEvents.sessionId.filter(s.id)];
  const securityCount = sessionEvents.filter(e => e.kind !== 'heartbeat').length;
  if (a.kind !== 'heartbeat' && securityCount >= SECURITY_EVENT_CAPACITY) fail('Security event capacity reached (100000); coverage is degraded; owner must disarm and create a new session');
  if (sessionEvents.filter(e => e.deviceId === a.deviceId && e.receivedAtMs + 1000n > now(ctx)).length >= 64) fail('Publisher rate limit exceeded (64 events per second)');
  const approvedUsb = a.kind === 'usb_added' && stringArray(s.baselineJson).includes(meta.deviceKey as string);
  const approvedApp = a.kind === 'app_launched' && stringArray(s.approvedAppsJson).includes(meta.appId as string);
  const classification = a.kind === 'usb_added' ? (approvedUsb ? 'approved_usb' : 'unknown_usb') : a.kind === 'app_launched' ? (approvedApp ? 'approved_app' : 'unapproved_app') : a.kind === 'person_in_zone' ? 'soft_observation' : 'informational';
  const event = { id: a.eventId, workstationId: a.workstationId, sessionId: s.id, deviceId: a.deviceId, source: fixture ? 'fixture' : d.role, kind: a.kind, observedAtMs: a.observedAtMs, receivedAtMs: now(ctx), seq: a.seq, confidence: a.confidence, metadataJson, simulated: a.simulated, schemaVersion: 1, classification };
  ctx.db.deviceEvents.insert(event); ctx.db.devices.id.update({ ...d, lastSeq: a.seq, lastEventAtMs: now(ctx) });
  const component = a.kind.startsWith('camera_') || a.kind.startsWith('person_') ? 'camera' : a.kind.startsWith('usb_') ? 'usb' : a.kind === 'app_launched' ? 'apps' : d.role === 'service' ? 'gateway' : d.role === 'jetson' ? 'jetson' : 'watcher';
  putHealth(ctx, a.workstationId, a.deviceId, component, a.kind === 'camera_offline' ? 'offline' : 'online', typeof meta.detail === 'string' ? meta.detail : '');
  if (a.kind === 'heartbeat') {
    const protectedIds = new Set<string>();
    for (const i of ctx.db.incidents.sessionId.filter(s.id)) for (const id of JSON.parse(i.eventIdsJson) as string[]) protectedIds.add(id);
    for (const step of ctx.db.agentSteps.workstationId.filter(a.workstationId)) if (step.evidenceId) protectedIds.add(step.evidenceId);
    const heartbeats = [...sessionEvents.filter(e => e.kind === 'heartbeat' && e.deviceId === a.deviceId && !protectedIds.has(e.id)), event].sort((a,b) => a.seq < b.seq ? -1 : a.seq > b.seq ? 1 : 0);
    for (const old of heartbeats.slice(0, Math.max(0, heartbeats.length - 128))) ctx.db.deviceEvents.id.delete(old.id);
  } else {
    audit(ctx, a.workstationId, 'device_event', a.eventId, classification);
    if (securityCount + 1 >= SECURITY_EVENT_CAPACITY) {
      putHealth(ctx, a.workstationId, 'database', 'capacity', 'degraded', 'Security event capacity reached; owner review and rearm required.');
      for (const h of ctx.db.deviceHealth.workstationId.filter(a.workstationId)) if (h.component !== 'capacity' && h.status === 'online') ctx.db.deviceHealth.id.update({ ...h, status: 'degraded', detail: 'Security event capacity reached; liveness does not imply effective protection.' });
      audit(ctx, a.workstationId, 'security_capacity_reached', s.id);
    }
  }
  if (classification === 'unknown_usb' || classification === 'unapproved_app') {
    const physical = classification === 'unknown_usb' ? sessionEvents.filter(e => e.kind === 'person_in_zone' && correlates(e, event)).sort((a, b) => a.observedAtMs > b.observedAtMs ? -1 : 1)[0] : undefined;
    // A watcher may retry with a fresh transport event id. Group the same unapproved
    // USB key into its unresolved session case so polling cannot create repeated locks.
    const prior = classification === 'unknown_usb' ? [...ctx.db.incidents.sessionId.filter(s.id)].find(i => {
      if (i.state === 'resolved' || !['unknown_usb', 'correlated_usb_physical'].includes(i.ruleId)) return false;
      const original = ctx.db.deviceEvents.id.find(i.digitalEventId);
      return original?.workstationId === a.workstationId && JSON.parse(original.metadataJson).deviceKey === meta.deviceKey;
    }) : undefined;
    if (prior) {
      const ids = [...new Set([...JSON.parse(prior.eventIdsJson) as string[], event.id, ...(physical ? [physical.id] : [])])];
      const evidenceIds = ids.length <= 256 ? ids : [ids[0], ...ids.slice(-255)];
      const high = prior.severity === 'high' || !!physical;
      ctx.db.incidents.id.update({ ...prior, severity: high ? 'high' : 'medium', ruleId: high ? 'correlated_usb_physical' : 'unknown_usb', summary: high ? 'Unknown USB device detected within 10 seconds of a person in the desk zone.' : prior.summary, physicalEventId: physical?.id ?? prior.physicalEventId, digitalEventId: physical ? event.id : prior.digitalEventId, eventIdsJson: JSON.stringify(evidenceIds), updatedAtMs: now(ctx) });
      audit(ctx, a.workstationId, 'incident_evidence_added', prior.id, event.id);
    } else {
      const id = `incident:${a.eventId}`; const high = !!physical;
      ctx.db.incidents.insert({ id, workstationId: a.workstationId, sessionId: s.id, severity: high ? 'high' : 'medium', state: 'open', ruleId: high ? 'correlated_usb_physical' : classification, summary: high ? 'Unknown USB device detected within 10 seconds of a person in the desk zone.' : classification === 'unknown_usb' ? 'New unapproved USB device detected while armed.' : 'Unapproved application launched while armed; review required.', eventIdsJson: JSON.stringify(physical ? [event.id, physical.id] : [event.id]), digitalEventId: event.id, physicalEventId: physical?.id ?? '', createdAtMs: now(ctx), updatedAtMs: now(ctx), acknowledgedBy: '', resolvedBy: '', resolutionNote: '', actionRequired: true, simulated: a.simulated });
      audit(ctx, a.workstationId, 'incident_opened', id, high ? 'high' : 'medium'); maybeLock(ctx, id);
    }
  }
  if (a.kind === 'person_in_zone') for (const i of ctx.db.incidents.sessionId.filter(s.id)) {
    if (i.state === 'resolved' || i.ruleId !== 'unknown_usb') continue;
    const digital = (JSON.parse(i.eventIdsJson) as string[]).map(id => ctx.db.deviceEvents.id.find(id)).find(e => e?.classification === 'unknown_usb' && correlates(e, event)); if (!digital) continue;
    ctx.db.incidents.id.update({ ...i, severity: 'high', ruleId: 'correlated_usb_physical', physicalEventId: event.id, digitalEventId: digital.id, eventIdsJson: JSON.stringify([...new Set([...JSON.parse(i.eventIdsJson) as string[], digital.id, event.id])].slice(-256)), updatedAtMs: now(ctx), summary: 'Unknown USB device detected within 10 seconds of a person in the desk zone.' });
    audit(ctx, a.workstationId, 'incident_correlated', i.id, 'Physical evidence arrived after USB; inclusive 10s window');
  }
});

export const report_health = spacetimedb.reducer({ workstationId: t.string(), deviceId: t.string(), component: t.string(), status: t.string(), detail: t.string() }, (ctx, a) => {
  member(ctx, a.workstationId); const d = ctx.db.devices.id.find(a.deviceId); if (!d?.enabled || d.workstationId !== a.workstationId || !d.identity.isEqual(ctx.sender)) fail('Health publisher does not match device');
  const permitted: Record<string, string[]> = { mac: ['watcher', 'usb', 'apps', 'voice'], jetson: ['camera', 'jetson'], service: ['model', 'gateway'] }; if (!permitted[d.role]?.includes(a.component)) fail('Component not permitted for device role');
  if (!['online', 'degraded', 'offline', 'unavailable'].includes(a.status)) fail('Invalid health status'); refresh(ctx, a.workstationId); putHealth(ctx, a.workstationId, a.deviceId, a.component, a.status, a.detail);
});
function incidentReview(ctx: Ctx, id: string, kind: string, note: string) {
  const i = ctx.db.incidents.id.find(id); if (!i) fail('Unknown incident'); if (kind === 'resolved') owner(ctx, i.workstationId); else review(ctx, i.workstationId); if (i.state === 'resolved') fail('Incident already resolved');
  const n = text(note, 1024); if (kind === 'resolved' && !n) fail('Resolution note required');
  ctx.db.incidents.id.update({ ...i, state: kind, updatedAtMs: now(ctx), acknowledgedBy: kind === 'acknowledged' ? ctx.sender.toHexString() : i.acknowledgedBy, resolvedBy: kind === 'resolved' ? ctx.sender.toHexString() : '', resolutionNote: kind === 'resolved' ? n : '', actionRequired: kind !== 'resolved' });
  ctx.db.acknowledgements.insert({ id: 0n, workstationId: i.workstationId, incidentId: id, actor: ctx.sender, kind, note: n, createdAtMs: now(ctx) });
  if (kind === 'resolved') for (const l of ctx.db.lockRequests.workstationId.filter(i.workstationId)) if (l.incidentId === id && ['requested', 'executing'].includes(l.status)) ctx.db.lockRequests.id.update({ ...l, status: 'cancelled', updatedAtMs: now(ctx) });
  audit(ctx, i.workstationId, 'incident_' + kind, id, n);
}
export const acknowledge_incident = spacetimedb.reducer({ incidentId: t.string(), note: t.string() }, (ctx, a) => incidentReview(ctx, a.incidentId, 'acknowledged', a.note));
export const request_incident_info = spacetimedb.reducer({ incidentId: t.string(), note: t.string() }, (ctx, a) => incidentReview(ctx, a.incidentId, 'reviewing', a.note));
export const resolve_incident = spacetimedb.reducer({ incidentId: t.string(), note: t.string() }, (ctx, a) => incidentReview(ctx, a.incidentId, 'resolved', a.note));
export const request_lock = spacetimedb.reducer({ incidentId: t.string() }, (ctx, a) => {
  const i = ctx.db.incidents.id.find(a.incidentId); if (!i) fail('Unknown incident'); owner(ctx, i.workstationId); const s = activeSession(ctx, i.workstationId, i.sessionId); if (!s.autoLockEnabled) fail('No owner auto-lock consent in armed policy'); maybeLock(ctx, a.incidentId);
});
export const panic_session = spacetimedb.reducer({ workstationId: t.string(), reason: t.string() }, (ctx, a) => {
  const w = owner(ctx, a.workstationId), session = activeSession(ctx, a.workstationId); const reason = text(a.reason, 512);
  if (!reason) fail('Owner panic reason required');
  const id = `incident:owner-panic:${session.id}`, prior = ctx.db.incidents.id.find(id);
  const row = { id, workstationId: w.id, sessionId: session.id, severity: 'high', state: 'open', ruleId: 'owner_panic', summary: 'Authenticated owner requested manual panic review: ' + reason, eventIdsJson: '[]', digitalEventId: '', physicalEventId: '', createdAtMs: prior?.createdAtMs ?? now(ctx), updatedAtMs: now(ctx), acknowledgedBy: ctx.sender.toHexString(), resolvedBy: '', resolutionNote: '', actionRequired: true, simulated: session.simulated };
  if (prior) ctx.db.incidents.id.update(row); else ctx.db.incidents.insert(row);
  audit(ctx, w.id, 'owner_panic_requested', id, reason); maybeLock(ctx, id);
});
export const claim_lock = spacetimedb.reducer({ actionId: t.string(), executionId: t.string() }, (ctx, a) => {
  validId(a.executionId, 'executionId');
  const l = ctx.db.lockRequests.id.find(a.actionId); if (!l) fail('Unknown lock request');
  const m = member(ctx, l.workstationId); const d = ctx.db.devices.id.find(l.targetDeviceId);
  if (m.role !== 'mac' || !d?.enabled || d.role !== 'mac' || d.workstationId !== l.workstationId || !d.identity.isEqual(ctx.sender)) fail('Only target enrolled Mac may claim lock execution');
  if (l.simulated) fail('Simulated requests cannot claim OS execution');
  const s = activeSession(ctx, l.workstationId, l.sessionId), w = ctx.db.workstations.id.find(l.workstationId)!;
  if (!s.autoLockEnabled || !w.autoLockEnabled || s.simulated || s.ownerPolicyHash !== l.ownerPolicyHash || w.ownerPolicyHash !== l.ownerPolicyHash) fail('Current/frozen owner lock consent no longer matches');
  const baseline = ctx.db.baselineSnapshots.id.find(`session:${s.id}`);
  if (!baseline || baseline.deviceId !== d.id || !baseline.publisher.isEqual(ctx.sender)) fail('Lock target differs from frozen Mac baseline publisher');
  const incident = ctx.db.incidents.id.find(l.incidentId), trigger = incident ? ctx.db.deviceEvents.id.find(incident.digitalEventId) : undefined;
  const ownerPanic = incident?.ruleId === 'owner_panic' && incident.acknowledgedBy === w.owner.toHexString();
  if (!incident || incident.state === 'resolved' || (ownerPanic ? incident.updatedAtMs + 15000n < now(ctx) : (!trigger || trigger.observedAtMs + 15000n < now(ctx)))) fail('Lock incident or trigger is no longer actionable');
  if (l.status !== 'requested' || l.expiresAtMs <= now(ctx) || ctx.db.lockClaims.actionId.find(l.id)) fail('Lock request expired, terminal, or already claimed');
  if (w.returnGraceUntilMs > now(ctx) || w.lastLockRequestedAtMs !== l.issuedAtMs) fail('Owner return grace or superseding request prevents execution');
  ctx.db.lockClaims.insert({ actionId: l.id, workstationId: l.workstationId, executionId: a.executionId, claimedBy: ctx.sender, targetDeviceId: d.id, claimedAtMs: now(ctx), expiresAtMs: l.expiresAtMs });
  ctx.db.lockRequests.id.update({ ...l, status: 'executing', updatedAtMs: now(ctx), resultDetail: 'One-time execution authorization claimed; no OS execution reported yet.' });
  audit(ctx, l.workstationId, 'lock_execution_claimed', l.id, a.executionId);
});
export const report_lock_result = spacetimedb.reducer({ actionId: t.string(), status: t.string(), detail: t.string() }, (ctx, a) => {
  const l = ctx.db.lockRequests.id.find(a.actionId); if (!l) fail('Unknown lock request'); const d = ctx.db.devices.id.find(l.targetDeviceId); const m = member(ctx, l.workstationId); if (m.role !== 'mac' || !d?.enabled || !d.identity.isEqual(ctx.sender)) fail('Only target enrolled Mac may report lock result');
  if (l.simulated) fail('Simulated requests must never execute a real lock adapter'); const s = activeSession(ctx, l.workstationId, l.sessionId); if (s.ownerPolicyHash !== l.ownerPolicyHash || !s.autoLockEnabled) fail('Lock policy no longer valid');
  const claim = ctx.db.lockClaims.actionId.find(l.id); if (!claim || !claim.claimedBy.isEqual(ctx.sender) || claim.targetDeviceId !== d.id) fail('A committed one-time lock execution claim is required');
  if (now(ctx) >= l.expiresAtMs || !['executing', 'executed'].includes(l.status)) fail('Lock request expired or terminal'); if (!['executed', 'confirmed', 'failed'].includes(a.status)) fail('Invalid lock result');
  ctx.db.lockRequests.id.update({ ...l, status: a.status, resultDetail: text(a.detail, 1024), updatedAtMs: now(ctx) }); audit(ctx, l.workstationId, 'lock_' + a.status, l.id, a.detail);
});

export const propose_protected_action = spacetimedb.reducer({ workstationId: t.string(), actionId: t.string(), missionId: t.string(), incidentId: t.string(), toolName: t.string(), argsJson: t.string(), scope: t.string(), ttlMs: t.u64() }, (ctx, a) => {
  const m = member(ctx, a.workstationId); if (!['owner', 'service'].includes(m.role)) fail('Owner or service required'); if (m.role === 'service') service(ctx, a.workstationId); validId(a.actionId); validId(a.missionId); validId(a.toolName, 'toolName'); validId(a.scope, 'scope');
  if (a.ttlMs < 1000n || a.ttlMs > 300_000n) fail('Action ttl must be 1s–5min'); if (ctx.db.protectedActions.id.find(a.actionId)) fail('Action id already used'); if (a.incidentId) { const i = ctx.db.incidents.id.find(a.incidentId); if (!i || i.workstationId !== a.workstationId) fail('Incident does not belong to workstation'); }
  const argsJson = canonicalJson(a.argsJson); const expiresAtMs = now(ctx) + a.ttlMs;
  const actionDigest = digest({ workstationId: a.workstationId, actionId: a.actionId, missionId: a.missionId, incidentId: a.incidentId, toolName: a.toolName, args: JSON.parse(argsJson), scope: a.scope, expiresAtMs: expiresAtMs.toString() });
  ctx.db.protectedActions.insert({ id: a.actionId, workstationId: a.workstationId, missionId: a.missionId, incidentId: a.incidentId, proposedBy: ctx.sender, toolName: a.toolName, argsJson, scope: a.scope, actionDigest, status: 'pending', createdAtMs: now(ctx), expiresAtMs, approvalId: '', executionId: '', consumedBy: '', resultDetail: '' }); audit(ctx, a.workstationId, 'action_proposed', a.actionId, a.toolName);
});
export const decide_action = spacetimedb.reducer({ actionId: t.string(), approve: t.bool(), ttlMs: t.u64(), note: t.string() }, (ctx, a) => {
  const action = ctx.db.protectedActions.id.find(a.actionId); if (!action) fail('Unknown action'); const m = review(ctx, action.workstationId); if (!m.canApprove) fail('Reviewer lacks action approval permission'); if (action.proposedBy.isEqual(ctx.sender)) fail('Proposer cannot approve own action');
  if (action.status !== 'pending' || now(ctx) >= action.expiresAtMs) fail('Action is not pending or expired'); if (a.ttlMs < 1000n || a.ttlMs > 300_000n) fail('Approval ttl must be 1s–5min');
  const approvalId = `approval:${action.id}`; const expiresAtMs = now(ctx) + a.ttlMs < action.expiresAtMs ? now(ctx) + a.ttlMs : action.expiresAtMs;
  ctx.db.approvals.insert({ id: approvalId, workstationId: action.workstationId, actionId: action.id, actionDigest: action.actionDigest, approvedBy: ctx.sender, status: a.approve ? 'approved' : 'denied', createdAtMs: now(ctx), expiresAtMs, consumedAtMs: 0n, note: text(a.note, 1024) });
  ctx.db.protectedActions.id.update({ ...action, status: a.approve ? 'approved' : 'denied', approvalId }); audit(ctx, action.workstationId, a.approve ? 'action_approved' : 'action_denied', action.id, a.note);
});
export const consume_approval = spacetimedb.reducer({ actionId: t.string(), approvalId: t.string(), actionDigest: t.string(), executionId: t.string() }, (ctx, a) => {
  const action = ctx.db.protectedActions.id.find(a.actionId); if (!action) fail('Unknown action'); service(ctx, action.workstationId); if (!action.proposedBy.isEqual(ctx.sender)) fail('Only the proposing service may consume this action approval'); validId(a.executionId); const approval = ctx.db.approvals.id.find(a.approvalId);
  if (!approval || approval.actionId !== action.id || approval.workstationId !== action.workstationId || approval.id !== action.approvalId || approval.status !== 'approved' || action.status !== 'approved') fail('No unused approval for exact action');
  const approver = ctx.db.memberships.id.find(memberId(action.workstationId, approval.approvedBy));
  if (!approver?.enabled || !approver.canApprove || !['owner', 'reviewer'].includes(approver.role)) fail('Approval permission has been revoked');
  if (a.actionDigest !== action.actionDigest || a.actionDigest !== approval.actionDigest) fail('Action digest mismatch'); if (now(ctx) >= approval.expiresAtMs || now(ctx) >= action.expiresAtMs) fail('Approval/action expired');
  // One transaction spends permission before the external gateway may run its callback.
  ctx.db.approvals.id.update({ ...approval, status: 'consumed', consumedAtMs: now(ctx) }); ctx.db.protectedActions.id.update({ ...action, status: 'consumed', executionId: a.executionId, consumedBy: ctx.sender.toHexString() }); audit(ctx, action.workstationId, 'approval_consumed', action.id, a.executionId);
});
export const report_action_result = spacetimedb.reducer({ actionId: t.string(), executionId: t.string(), status: t.string(), detail: t.string() }, (ctx, a) => {
  const action = ctx.db.protectedActions.id.find(a.actionId); if (!action) fail('Unknown action'); service(ctx, action.workstationId); if (action.status !== 'consumed' || action.executionId !== a.executionId || action.consumedBy !== ctx.sender.toHexString()) fail('Result must match the consuming service and execution'); if (!['succeeded', 'failed', 'uncertain'].includes(a.status)) fail('Invalid action outcome'); ctx.db.protectedActions.id.update({ ...action, status: a.status, resultDetail: text(a.detail, 2048) }); audit(ctx, action.workstationId, 'action_' + a.status, action.id, a.detail);
});
export const record_agent_step = spacetimedb.reducer({ workstationId: t.string(), runId: t.string(), incidentId: t.string(), stepId: t.string(), step: t.u32(), toolName: t.string(), status: t.string(), evidenceId: t.string(), durationMs: t.u64(), inputType: t.string(), argsJson: t.string(), resultSummary: t.string(), traceId: t.string(), model: t.string() }, (ctx, a) => {
  service(ctx, a.workstationId); validId(a.runId); validId(a.stepId); if (!['running', 'succeeded', 'failed', 'complete'].includes(a.status)) fail('Invalid agent step status'); const i = ctx.db.incidents.id.find(a.incidentId); if (!i || i.workstationId !== a.workstationId) fail('Incident belongs to another workstation');
  if (a.evidenceId) { const e = ctx.db.deviceEvents.id.find(a.evidenceId); if (!e || e.workstationId !== a.workstationId || e.sessionId !== i.sessionId) fail('Evidence does not belong to incident session'); }
  const run = ctx.db.agentRuns.id.find(a.runId); if (run && (run.workstationId !== a.workstationId || run.incidentId !== i.id)) fail('Run id belongs to another case'); if (ctx.db.agentSteps.id.find(a.stepId)) fail('Step id already used');
  if (run) {
    const origin = [...ctx.db.auditEvents.workstationId.filter(a.workstationId)].find(e => e.kind === 'agent_run_started' && e.entityId === a.runId);
    if (!origin?.actor.isEqual(ctx.sender)) fail('Only the reporting service may append to its agent run');
    if (run.model !== text(a.model, 128) || run.traceId !== text(a.traceId, 256)) fail('Agent model and trace attribution are immutable within a run');
    if (['complete', 'failed'].includes(run.status)) fail('Agent run is terminal');
    if ([...ctx.db.agentSteps.workstationId.filter(a.workstationId)].some(step => step.runId === a.runId && step.step >= a.step)) fail('Agent step sequence must increase');
  } else audit(ctx, a.workstationId, 'agent_run_started', a.runId, 'Model and trace are adapter-reported, not independently verified.');
  const row = { id: a.runId, workstationId: a.workstationId, incidentId: a.incidentId, model: text(a.model, 128), status: a.status === 'complete' ? 'complete' : a.status === 'failed' ? 'failed' : 'running', startedAtMs: run?.startedAtMs ?? now(ctx), updatedAtMs: now(ctx), traceId: text(a.traceId, 256) }; if (run) ctx.db.agentRuns.id.update(row); else ctx.db.agentRuns.insert(row);
  ctx.db.agentSteps.insert({ id: a.stepId, workstationId: a.workstationId, runId: a.runId, incidentId: a.incidentId, step: a.step, toolName: text(a.toolName, 128), status: a.status, evidenceId: a.evidenceId, durationMs: a.durationMs, inputType: text(a.inputType, 128), argsJson: canonicalJson(a.argsJson), resultSummary: text(a.resultSummary, 2048), traceId: row.traceId, createdAtMs: now(ctx) });
  const reportingDevice = [...ctx.db.devices.identity.filter(ctx.sender)].find(d => d.workstationId === a.workstationId && d.enabled && d.role === 'service');
  if (reportingDevice) {
    putHealth(ctx, a.workstationId, reportingDevice.id, 'gateway', 'online', 'Authenticated investigation adapter reported a step');
    const selectedModel = text(a.model, 128);
    const inferenceSucceeded = a.toolName === 'nemotron_inference' && a.inputType === 'local_ollama_model_response' && a.status === 'succeeded';
    // Reading evidence or opening a model request does not prove model availability.
    if (selectedModel && !selectedModel.startsWith('none:') && inferenceSucceeded) putHealth(ctx, a.workstationId, reportingDevice.id, 'model', 'online', 'Adapter-reported successful local inference: ' + selectedModel);
    else if (selectedModel && !selectedModel.startsWith('none:') && a.status === 'failed') putHealth(ctx, a.workstationId, reportingDevice.id, 'model', 'degraded', 'Investigation failed or model unavailable: ' + selectedModel);
  }
  audit(ctx, a.workstationId, 'agent_step', a.stepId, a.status);
});
// Interval schedules are durable and the private unique workstation index prevents duplicate timers.
export const maintain_workstation = spacetimedb.reducer({ onSchedule: maintenanceSchedule }, { timer: maintenanceSchedule.rowType }, (ctx, { timer }) => {
  if (ctx.db.workstations.id.find(timer.workstationId)) { refresh(ctx, timer.workstationId); cleanupMetadata(ctx, timer.workstationId); }
});
export const refresh_workstation = spacetimedb.reducer({ workstationId: t.string() }, (ctx, a) => { member(ctx, a.workstationId); refresh(ctx, a.workstationId); });

export const my_workstations = spacetimedb.view({ name: 'my_workstations', public: true }, t.array(workstations.rowType), ctx => ownMemberships(ctx).flatMap(m => { const w = ctx.db.workstations.id.find(m.workstationId); return w ? [w] : []; }));
export const my_memberships = spacetimedb.view({ name: 'my_memberships', public: true }, t.array(memberships.rowType), ctx => ownMemberships(ctx));
export const my_devices = spacetimedb.view({ name: 'my_devices', public: true }, t.array(devices.rowType), ctx => ownMemberships(ctx).flatMap(m => [...ctx.db.devices.workstationId.filter(m.workstationId)].filter(d => canReadCases(m.role) || d.identity.isEqual(ctx.sender))));
export const my_sessions = spacetimedb.view({ name: 'my_sessions', public: true }, t.array(sessions.rowType), ctx => ownMemberships(ctx).flatMap(m => [...ctx.db.sessions.workstationId.filter(m.workstationId)]));
export const my_device_events = spacetimedb.view({ name: 'my_device_events', public: true }, t.array(deviceEvents.rowType), ctx => ownMemberships(ctx).filter(m => canReadCases(m.role)).flatMap(m => [...ctx.db.deviceEvents.workstationId.filter(m.workstationId)]));
export const my_incidents = spacetimedb.view({ name: 'my_incidents', public: true }, t.array(incidents.rowType), ctx => ownMemberships(ctx).filter(m => canReadCases(m.role)).flatMap(m => [...ctx.db.incidents.workstationId.filter(m.workstationId)]));
export const my_device_health = spacetimedb.view({ name: 'my_device_health', public: true }, t.array(deviceHealth.rowType), ctx => ownMemberships(ctx).flatMap(m => [...ctx.db.deviceHealth.workstationId.filter(m.workstationId)]));
export const my_lock_requests = spacetimedb.view({ name: 'my_lock_requests', public: true }, t.array(lockRequests.rowType), ctx => ownMemberships(ctx).filter(m => canReadCases(m.role) || m.role === 'mac').flatMap(m => [...ctx.db.lockRequests.workstationId.filter(m.workstationId)].filter(l => canReadCases(m.role) || ctx.db.devices.id.find(l.targetDeviceId)?.identity.isEqual(ctx.sender))));
export const my_protected_actions = spacetimedb.view({ name: 'my_protected_actions', public: true }, t.array(protectedActions.rowType), ctx => ownMemberships(ctx).filter(m => canReadCases(m.role)).flatMap(m => [...ctx.db.protectedActions.workstationId.filter(m.workstationId)]));
export const my_approvals = spacetimedb.view({ name: 'my_approvals', public: true }, t.array(approvals.rowType), ctx => ownMemberships(ctx).filter(m => m.role === 'owner' || m.role === 'service' || (m.role === 'reviewer' && m.canApprove)).flatMap(m => [...ctx.db.approvals.workstationId.filter(m.workstationId)]));
export const my_agent_runs = spacetimedb.view({ name: 'my_agent_runs', public: true }, t.array(agentRuns.rowType), ctx => ownMemberships(ctx).filter(m => canReadCases(m.role)).flatMap(m => [...ctx.db.agentRuns.workstationId.filter(m.workstationId)]));
export const my_agent_steps = spacetimedb.view({ name: 'my_agent_steps', public: true }, t.array(agentSteps.rowType), ctx => ownMemberships(ctx).filter(m => canReadCases(m.role)).flatMap(m => [...ctx.db.agentSteps.workstationId.filter(m.workstationId)]));
export const my_acknowledgements = spacetimedb.view({ name: 'my_acknowledgements', public: true }, t.array(acknowledgements.rowType), ctx => ownMemberships(ctx).filter(m => canReadCases(m.role)).flatMap(m => [...ctx.db.acknowledgements.workstationId.filter(m.workstationId)]));
export const my_audit_events = spacetimedb.view({ name: 'my_audit_events', public: true }, t.array(auditEvents.rowType), ctx => ownMemberships(ctx).filter(m => canReadCases(m.role)).flatMap(m => [...ctx.db.auditEvents.workstationId.filter(m.workstationId)]));

export const my_baseline_snapshots = spacetimedb.view({ name: 'my_baseline_snapshots', public: true }, t.array(baselineSnapshots.rowType), ctx => ownMemberships(ctx).flatMap(m => [...ctx.db.baselineSnapshots.workstationId.filter(m.workstationId)].filter(b => canReadCases(m.role) || (m.role === 'mac' && b.publisher.isEqual(ctx.sender)))));
