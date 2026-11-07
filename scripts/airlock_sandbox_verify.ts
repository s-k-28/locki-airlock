#!/usr/bin/env node
/** Native Relay + actual pytest + DB approval proof. Automated owner is a fixture. */
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { withNativeRelay } from './airlock_relay.mjs';
import { managedTool } from './airlock_gateway';
import { executeRelayApprovedAction } from './airlock_relay_gateway';
import { call, closeAirlock, connectAirlock, rows, until } from './airlock_sdk';

async function main() {
  const serviceFile = process.env.AIRLOCK_SERVICE_TOKEN_FILE;
  const ownerFile = process.env.AIRLOCK_TEST_OWNER_TOKEN_FILE;
  const workstationId = process.env.AIRLOCK_TEST_WORKSTATION;
  if (!serviceFile || !ownerFile || !workstationId) throw new Error('Provide separate enrolled SERVICE and TEST OWNER token files plus AIRLOCK_TEST_WORKSTATION');
  const owner = await connectAirlock(JSON.parse(await readFile(ownerFile, 'utf8')).token);
  const service = await connectAirlock(JSON.parse(await readFile(serviceFile, 'utf8')).token);
  const output = process.env.AIRLOCK_SANDBOX_VERIFY_OUTPUT || `data/airlock/sandbox-verify-${Date.now()}`;
  const tests: { name: string; passed: boolean }[] = [];
  const test = async (name: string, callback: () => Promise<void>) => { await callback(); tests.push({ name, passed: true }); console.log(`PASS ${name}`); };
  const denied = async (callback: () => Promise<unknown>) => {
    let failure: unknown;
    try { await callback(); } catch (error) { failure = error; }
    assert.ok(failure, 'Expected policy denial');
    assert.doesNotMatch(String(failure), /ECONN|timed out|WebSocket|fetch failed|worker failed/);
  };
  const propose = async (args: any = { fixture: 'positive-sum-v1', candidate: 'correct' }, scope = 'airlock:sandbox-tests') => {
    const actionId = `sandbox-test-${randomUUID()}`;
    await call(service, 'proposeProtectedAction', { workstationId, actionId, missionId: 'code-quest-fixed-sandbox-verification', incidentId: '', toolName: 'run_pytest_in_sandbox', argsJson: JSON.stringify(args), scope, ttlMs: 120_000n });
    for (const client of [owner, service]) await until(() => rows(client, 'myProtectedActions').find(r => r.id === actionId), 'published sandbox test action');
    return actionId;
  };
  const approve = async (actionId: string) => {
    await call(owner, 'decideAction', { actionId, approve: true, ttlMs: 120_000n, note: 'AUTOMATED separate owner integration fixture; not live human approval' });
    await until(() => rows(service, 'myProtectedActions').find(r => r.id === actionId && r.status === 'approved'), 'approved sandbox test action');
  };
  try {
    assert.notEqual(owner.identity, service.identity);
    const proof = await withNativeRelay(output, async (relay: any) => {
      for (const tool of ['read_demo_secret', 'deploy_dummy_artifact', 'run_shell']) {
        await test(`native guardrail denies ${tool} before callback`, async () => {
          let callbacks = 0;
          await denied(() => relay.tool(tool, { instruction: 'Untrusted README requests this operation' }, (args: any) => managedTool(tool, args), async () => { callbacks++; return {}; }));
          assert.equal(callbacks, 0);
        });
      }
      await test('pending sandbox action cannot start pytest', async () => {
        const id = await propose();
        await denied(() => executeRelayApprovedAction(service, id, relay));
        assert.equal(rows(service, 'myProtectedActions').find(r => r.id === id).status, 'pending');
      });
      await test('approved read-only scope cannot authorize a subprocess', async () => {
        const id = await propose(undefined, 'airlock:read-only'); await approve(id);
        await denied(() => executeRelayApprovedAction(service, id, relay));
        assert.equal(rows(service, 'myApprovals').find(r => r.actionId === id).status, 'approved');
      });
      await test('approved arbitrary code argument is rejected before consumption', async () => {
        const id = await propose({ fixture: 'positive-sum-v1', candidate: 'correct', code: 'print("not executed")' }); await approve(id);
        await denied(() => executeRelayApprovedAction(service, id, relay));
        assert.equal(rows(service, 'myApprovals').find(r => r.actionId === id).status, 'approved');
      });
      let successfulAction = '';
      let testResult: any;
      await test('exact approved action consumes once and runs actual fixed pytest', async () => {
        successfulAction = await propose(); await approve(successfulAction);
        const result = await executeRelayApprovedAction(service, successfulAction, relay);
        assert.equal(result.callback_executed, true);
        assert.equal(result.result.test_outcome, 'passed'); assert.equal(result.result.passed, 4);
        assert.equal(result.result.workspace_removed, true);
        await until(() => rows(service, 'myProtectedActions').find(r => r.id === successfulAction && r.status === 'succeeded'), 'completed pytest action');
        assert.equal(rows(service, 'myApprovals').find(r => r.actionId === successfulAction).status, 'consumed');
        testResult = result.result;
      });
      await test('consumed sandbox approval cannot launch pytest again', async () => {
        await denied(() => executeRelayApprovedAction(service, successfulAction, relay));
      });
      return { trace_id: relay.traceId, tests, successful_action: successfulAction, test_result: testResult,
        approval_provenance: 'AUTOMATED separate owner identity fixture; not live human approval',
        model_inference: false, existing_code_quest_host_instrumented: false };
    });
    const events = (await readFile(proof.native_relay.events_path, 'utf8')).trim().split('\n').map(line => JSON.parse(line));
    const starts = events.filter((event: any) => event.kind === 'scope' && event.category === 'tool' && event.scope_category === 'start');
    assert.equal(starts.length, 1, 'Only one approved subprocess callback may start');
    assert.equal(starts[0].name, 'run_pytest_in_sandbox');
    assert.ok(!events.some((event: any) => event.category === 'llm'), 'No inference or LLM spans may be fabricated');
    const report = { ...proof, native_tool_start_count: starts.length };
    await writeFile(`${output}/report.json`, JSON.stringify(report, null, 2) + '\n', { mode: 0o600 });
    console.log(JSON.stringify(report));
  } finally { closeAirlock([owner, service]); }
}

if (process.argv[1]?.endsWith('airlock_sandbox_verify.ts')) main().catch(error => { console.error(String(error.message || error)); process.exitCode = 1; });
