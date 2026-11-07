#!/usr/bin/env node
/** AIRLOCK's new managed runner. This is not a native NeMo Relay integration. */
import { randomUUID } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { call, closeAirlock, connectAirlock, rows, until, type AirlockClient } from './airlock_sdk';
import { sandboxTool, SANDBOX_SCOPE } from './airlock_sandbox';

type ToolContext = { client: AirlockClient; workstationId: string; incidentId: string };
type Tool = { validate: (args: unknown) => Record<string, any>; run: (args: Record<string, any>, context: ToolContext) => Promise<Record<string, unknown>> };
function objectWithKeys(value: unknown, keys: string[]): Record<string, any> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Tool arguments must be an object');
  const args = value as Record<string, unknown>;
  if (Object.keys(args).some(key => !keys.includes(key))) throw new Error('Unexpected tool argument');
  return args;
}

const tools: Record<string, Tool> = {
  run_pytest_in_sandbox: sandboxTool,
  fixture_word_count: {
    validate(value) {
      const args = objectWithKeys(value, ['text']);
      if (typeof args.text !== 'string' || args.text.length > 2_000) throw new Error('fixture_word_count requires text of at most 2000 characters');
      return args;
    },
    async run(args) {
      // Untrusted text is data. It is never interpreted as a command or prompt.
      return { tool: 'fixture_word_count', words: args.text.trim() ? args.text.trim().split(/\s+/u).length : 0, characters: args.text.length };
    },
  },
  read_incident: {
    validate(value) {
      const args = objectWithKeys(value, ['incidentId']);
      if (typeof args.incidentId !== 'string' || !args.incidentId || args.incidentId.length > 160) throw new Error('read_incident requires incidentId');
      return args;
    },
    async run(args, context) {
      if (context.incidentId && args.incidentId !== context.incidentId) throw new Error('Incident argument differs from the approved incident scope');
      const row = rows(context.client, 'myIncidents', context.workstationId).find(item => item.id === args.incidentId);
      if (!row) throw new Error('Incident is outside this service identity/workstation scope');
      return { tool: 'read_incident', incident_id: row.id, severity: row.severity, state: row.state, summary: row.summary, evidence_ids: JSON.parse(row.eventIdsJson), simulated: row.simulated };
    },
  },
  read_health: {
    validate(value) { return objectWithKeys(value, []); },
    async run(_args, context) {
      return { tool: 'read_health', workstation_id: context.workstationId, components: rows(context.client, 'myDeviceHealth', context.workstationId).map(row => ({ component: row.component, status: row.status, last_seen_ms: row.lastSeenMs.toString() })) };
    },
  },
};

export function managedTool(toolName: string, args: unknown) {
  if (!Object.hasOwn(tools, toolName)) throw new Error(`DENIED unknown managed tool: ${toolName}; callback not executed`);
  const tool = tools[toolName];
  return { tool, args: tool.validate(args) };
}

export function assertManagedScope(toolName: string, scope: string): void {
  if (toolName === 'run_pytest_in_sandbox' ? scope !== SANDBOX_SCOPE : !['airlock:read-only', 'fixture:read-only'].includes(scope)) {
    throw new Error('DENIED scope does not permit the exact managed tool');
  }
}

/** No callback occurs until consume_approval has committed successfully. */
export async function executeApprovedAction(client: AirlockClient, actionId: string) {
  const action = rows(client, 'myProtectedActions').find(row => row.id === actionId);
  if (!action) throw new Error('Action is unavailable to this service identity');
  const selected = managedTool(action.toolName, JSON.parse(action.argsJson));
  assertManagedScope(action.toolName, action.scope);
  if (!action.approvalId) throw new Error('DENIED pending action has no authenticated approval');
  const executionId = `runner-exec-${randomUUID()}`;
  await call(client, 'consumeApproval', { actionId: action.id, approvalId: action.approvalId, actionDigest: action.actionDigest, executionId });
  // The transaction above is the authorization boundary. Do not move the
  // callback before it, retry consumption, or substitute mutable caller args.
  const started = performance.now();
  let result: Record<string, unknown>;
  try {
    result = await selected.tool.run(selected.args, { client, workstationId: action.workstationId, incidentId: action.incidentId });
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    await call(client, 'reportActionResult', { actionId: action.id, executionId, status: 'failed', detail: detail.slice(0, 500) });
    throw error;
  }
  const durationMs = Math.max(0, Math.round(performance.now() - started));
  try {
    await call(client, 'reportActionResult', { actionId: action.id, executionId, status: 'succeeded', detail: JSON.stringify(result).slice(0, 500) });
  } catch (error) {
    // Execution already occurred. Never retry the callback after a reporting
    // error: the caller must treat the external outcome as uncertain.
    throw new Error(`Callback completed but outcome reporting failed; do not retry execution ${executionId}: ${String(error)}`);
  }
  return { action_id: action.id, execution_id: executionId, callback_executed: true, duration_ms: durationMs, result };
}

async function main() {
  const argv = process.argv.slice(2);
  const value = (name: string) => { const i = argv.indexOf(name); return i < 0 ? undefined : argv[i + 1]; };
  if (!argv.length || argv.includes('--help')) {
    console.log(`AIRLOCK managed runner — explicit callback allowlist\n\nNegative tool check (no DB needed):\n  frontend/node_modules/.bin/tsx scripts/airlock_gateway.ts --deny-demo\n\nPropose as an enrolled service and wait for SEPARATE owner/reviewer approval:\n  AIRLOCK_SERVICE_TOKEN_FILE=data/airlock/service-token.json frontend/node_modules/.bin/tsx scripts/airlock_gateway.ts --workstation ID --tool fixture_word_count --args '{"text":"harmless fixture"}' --wait-seconds 120\n\nExecute an existing approved action as its proposing service:\n  AIRLOCK_SERVICE_TOKEN_FILE=data/airlock/service-token.json frontend/node_modules/.bin/tsx scripts/airlock_gateway.ts --execute ACTION_ID\n\nAllowed callbacks: fixture_word_count {text}, read_incident {incidentId}, read_health {}, run_pytest_in_sandbox {fixture:positive-sum-v1,candidate:correct|buggy}.\nAll callback inputs come from the immutable, digest-bound DB action.\nNo shell, arbitrary file read, screen lock, motors or external notification.\nUse airlock_relay_gateway.ts for native Relay execution. The fixed pytest exercise does not instrument a separate Code Quest host or claim model inference.`);
    return;
  }
  if (argv.includes('--deny-demo')) {
    try { managedTool('run_shell', { command: 'untrusted README text' }); }
    catch (error) { console.log(JSON.stringify({ allowed: false, callback_executed: false, reason: (error as Error).message })); return; }
    throw new Error('Unknown tool was unexpectedly accepted');
  }
  const tokenFile = process.env.AIRLOCK_SERVICE_TOKEN_FILE;
  if (!tokenFile) throw new Error('Set AIRLOCK_SERVICE_TOKEN_FILE to the enrolled SERVICE identity token file');
  const saved = JSON.parse(await readFile(tokenFile, 'utf8'));
  if (typeof saved.token !== 'string') throw new Error('Service token file has no token');
  const client = await connectAirlock(saved.token);
  try {
    let actionId = value('--execute');
    if (!actionId) {
      const workstationId = value('--workstation');
      const toolName = value('--tool');
      if (!workstationId || !toolName) throw new Error('--workstation and --tool are required');
      const args = JSON.parse(value('--args') || '{}');
      managedTool(toolName, args); // Deny unknown tools/schema before proposing.
      actionId = value('--action-id') || `runner-action-${randomUUID()}`;
      await call(client, 'proposeProtectedAction', { workstationId, actionId, missionId: value('--mission') || `runner-mission-${randomUUID()}`, incidentId: value('--incident') || '', toolName, argsJson: JSON.stringify(args), scope: toolName === 'run_pytest_in_sandbox' ? SANDBOX_SCOPE : 'airlock:read-only', ttlMs: 120_000n });
      const action = await until(() => rows(client, 'myProtectedActions', workstationId).find(row => row.id === actionId), 'proposed managed action');
      console.log(JSON.stringify({ status: 'pending_separate_human_approval', action_id: action.id, action_digest: action.actionDigest, tool: toolName, args: JSON.parse(action.argsJson), expires_at_ms: action.expiresAtMs.toString() }));
      const waitSeconds = Number(value('--wait-seconds') || '120');
      if (!Number.isFinite(waitSeconds) || waitSeconds < 0 || waitSeconds > 300) throw new Error('--wait-seconds must be in [0,300]');
      if (!waitSeconds) return;
      await until(() => {
        const row = rows(client, 'myProtectedActions', workstationId).find(item => item.id === actionId);
        if (row && ['denied', 'expired', 'cancelled'].includes(row.status)) throw new Error(`Action ${row.status}; callback not executed`);
        return row?.approvalId ? row : undefined;
      }, 'separate authenticated approval (approve this action in the owner/reviewer dashboard)', waitSeconds * 1000);
    }
    console.log(JSON.stringify(await executeApprovedAction(client, actionId)));
  } finally { closeAirlock([client]); }
}

if (process.argv[1]?.endsWith('airlock_gateway.ts')) {
  main().catch(error => { console.error(error instanceof Error ? error.message : String(error)); process.exitCode = 1; });
}
