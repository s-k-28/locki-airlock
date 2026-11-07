/** Real database + child-process dry-run test. NEVER sets --execute or creates consent. */
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { promisify } from 'node:util';
import { call, closeAirlock, connectAirlock, rows, until, type AirlockClient } from '../airlock_sdk';
const executeFile=promisify(execFile);
async function main(){
const clients:AirlockClient[]=[];
const temporary=await mkdtemp(join(tmpdir(),'airlock-lock-preview-'));
try{
  const owner=await connectAirlock(),mac=await connectAirlock();clients.push(owner,mac);
  const workstationId=`lock-preview-${Date.now()}`,deviceId=`${workstationId}-mac`,sessionId=`${workstationId}-session`;
  await call(owner,'registerWorkstation',{workstationId,name:'SIMULATED lock-adapter dry-run protocol check'});
  await call(owner,'enrollDevice',{workstationId,deviceId,identityHex:mac.identity,role:'mac',label:'SIMULATED dry-run protocol Mac'});
  await call(owner,'setPolicy',{workstationId,autoLockEnabled:true,lockCooldownMs:30000n});
  await call(owner,'armSession',{workstationId,sessionId,baselineJson:'[]',approvedAppsJson:'[]',durationMs:60000n,simulated:true});
  await call(owner,'ingestDeviceEvent',{workstationId,sessionId,deviceId:'fixture',eventId:`${workstationId}-usb`,kind:'usb_added',observedAtMs:BigInt(Date.now()),seq:1n,confidence:1,metadataJson:'{"deviceKey":"fixture:harmless-usb","label":"SIMULATED dry-run protocol input"}',simulated:true,schemaVersion:1});
  const request=await until(()=>rows(mac,'myLockRequests',workstationId)[0],'target Mac lock request');
  assert.equal(request.simulated,true);
  const tokenFile=join(temporary,'mac-token.private.json'),stateFile=join(temporary,'state.private.json');
  await writeFile(tokenFile,JSON.stringify({token:mac.token}),{mode:0o600});
  const result=await executeFile(process.execPath,[resolve('spacetime/node_modules/tsx/dist/cli.mjs'),resolve('scripts/airlock_lock_adapter.ts'),'--workstation',workstationId,'--device',deviceId,'--token-file',tokenFile,'--state-file',stateFile,'--once'],{timeout:20000,maxBuffer:16384,env:process.env});
  const output=result.stdout.trim().split('\n').filter(line=>line.startsWith('{')).map(line=>JSON.parse(line));
  const preview=output.find(row=>row.actionId===request.id);
  assert.ok(preview,'Real adapter must inspect the authorized request');assert.equal(preview.dryRun,true);assert.equal(preview.executed,false);assert.match(preview.reason,/non-synthetic/);
  await assert.rejects(readFile(stateFile),/ENOENT/);
  assert.equal(rows(mac,'myLockRequests',workstationId).find(row=>row.id===request.id)?.status,'requested');
  await call(owner,'disarmSession',{workstationId});
  console.log(JSON.stringify({passed:true,database:process.env.AIRLOCK_DATABASE||'locki-airlock-sid',transport:'real generated SDK + fresh authorized HTTP views + child-process default dry-run',provenance:'SIMULATED input only',os_helper_invocations:0,actual_os_locks:0,execution_reports:0,state_reservations:0,request_unchanged_before_disarm:true}));
}finally{closeAirlock(clients);await rm(temporary,{recursive:true,force:true});}

}
void main().catch(error=>{console.error(error instanceof Error?error.message:String(error));process.exitCode=1;});
