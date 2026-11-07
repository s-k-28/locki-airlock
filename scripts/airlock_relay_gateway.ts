#!/usr/bin/env node
/** Real native Relay conditional middleware + one-time SpacetimeDB approval.
 * This protects only callbacks invoked through this managed entry point.
 */
import { randomUUID } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { managedTool, assertManagedScope } from './airlock_gateway';
import { call, closeAirlock, connectAirlock, rows, type AirlockClient } from './airlock_sdk';
import { withNativeRelay } from './airlock_relay.mjs';

export async function executeRelayApprovedAction(client: AirlockClient, actionId: string, relay: any) {
  const action = rows(client,'myProtectedActions').find(r=>r.id===actionId);
  if (!action) throw new Error('Action unavailable to this identity');
  const immutableArgs = JSON.parse(action.argsJson);
  const executionId = `relay-exec-${randomUUID()}`;
  let callbackExecuted = false;
  let consumed = false;
  const started = performance.now();
  let result;
  try {
    result = await relay.tool(action.toolName, immutableArgs, async (args:any) => {
      // This validator executes inside NVIDIA's conditional-execution guardrail.
      managedTool(action.toolName,args);
      assertManagedScope(action.toolName, action.scope);
      if (JSON.stringify(args)!==JSON.stringify(immutableArgs)) throw new Error('Immutable arguments changed');
      if (action.proposedBy.toHexString() !== client.identity) throw new Error('Only proposing service may execute');
      if (action.status!=='approved'||!action.approvalId) throw new Error('No unused approved action');
      await call(client,'consumeApproval',{actionId:action.id,approvalId:action.approvalId,actionDigest:action.actionDigest,executionId});
      consumed = true; // reducer promise resolves only after committed outcome
    },async() => {
      if (!consumed) throw new Error('Approval was not committed; callback refused');
      callbackExecuted = true;
      // Execute a fresh copy of the server's immutable digest-bound arguments.
      const execution = managedTool(action.toolName, JSON.parse(action.argsJson));
      return execution.tool.run(execution.args,{client,workstationId:action.workstationId,incidentId:action.incidentId});
    });
  } catch(error) {
    if (consumed) await call(client,'reportActionResult',{actionId:action.id,executionId,status:'failed',detail:`Native Relay managed callback failed: ${String((error as Error).message).slice(0,500)}`});
    throw error;
  }
  try {
    await call(client,'reportActionResult',{actionId:action.id,executionId,status:'succeeded',detail:JSON.stringify(result).slice(0,1000)});
  } catch {
    throw new Error(`Callback completed; reporting uncertain. Never retry execution ${executionId}`);
  }
  return {action_id:action.id,execution_id:executionId,trace_id:relay.traceId,callback_executed:callbackExecuted,duration_ms:Math.round(performance.now()-started),result};
}

async function main() {
  const index=process.argv.indexOf('--execute');
  if(index<0 || !process.argv[index+1]) throw new Error('Use --execute ACTION_ID with AIRLOCK_SERVICE_TOKEN_FILE; proposal and separate human approval must already exist');
  const file=process.env.AIRLOCK_SERVICE_TOKEN_FILE;
  if(!file) throw new Error('AIRLOCK_SERVICE_TOKEN_FILE required');
  const saved=JSON.parse(await readFile(file,'utf8'));
  const client=await connectAirlock(saved.token);
  try {console.log(JSON.stringify(await withNativeRelay(`data/airlock/relay-protected-${Date.now()}`, relay=>executeRelayApprovedAction(client,process.argv[index+1],relay))));}
  finally {closeAirlock([client]);}
}
if(process.argv[1]?.endsWith('airlock_relay_gateway.ts')) main().catch(error=>{console.error(String(error.message||error));process.exitCode=1;});
