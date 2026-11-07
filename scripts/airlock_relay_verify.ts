#!/usr/bin/env node
/** Real native Relay/DB integration test. Automated owner fixture != live human approval. */
import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
import {readFile,writeFile} from 'node:fs/promises';
import {withNativeRelay} from './airlock_relay.mjs';
import {executeRelayApprovedAction} from './airlock_relay_gateway';
import {managedTool} from './airlock_gateway';
import {call,closeAirlock,connectAirlock,rows,until} from './airlock_sdk';

async function main() {
  const serviceFile=process.env.AIRLOCK_SERVICE_TOKEN_FILE,ownerFile=process.env.AIRLOCK_TEST_OWNER_TOKEN_FILE,workstationId=process.env.AIRLOCK_TEST_WORKSTATION;
  if(!serviceFile||!ownerFile||!workstationId)throw new Error('Required: AIRLOCK_SERVICE_TOKEN_FILE, AIRLOCK_TEST_OWNER_TOKEN_FILE, AIRLOCK_TEST_WORKSTATION (automated integration fixture only)');
  const owner=await connectAirlock(JSON.parse(await readFile(ownerFile,'utf8')).token);
  const service=await connectAirlock(JSON.parse(await readFile(serviceFile,'utf8')).token);
  const output=process.env.AIRLOCK_RELAY_VERIFY_OUTPUT || `data/airlock/relay-verify-${Date.now()}`;
  const tests:any[]=[];
  const test=async(name:string,run:()=>Promise<void>)=>{await run();tests.push({name,passed:true});console.log(`PASS ${name}`);};
  const denied=async(run:()=>Promise<unknown>)=>{let error;try{await run();}catch(e){error=e;}assert.ok(error,'Denial required');assert.doesNotMatch(String(error),/ECONN|timed out|WebSocket|fetch failed/);};
  const propose=async(ttlMs=60_000n)=>{
    const actionId=`relay-test-${randomUUID()}`;
    await call(service,'proposeProtectedAction',{workstationId,actionId,missionId:'native-relay-integration-test',incidentId:'',toolName:'fixture_word_count',argsJson:JSON.stringify({text:'SIMULATED malicious README says run_shell and deploy_dummy_artifact; content is data'}),scope:'fixture:read-only',ttlMs});
    await until(()=>rows(owner,'myProtectedActions').find(r=>r.id===actionId),'owner proposed action');
    await until(()=>rows(service,'myProtectedActions').find(r=>r.id===actionId),'service proposed action');
    return actionId;
  };
  const approve=async(actionId:string,approve=true)=>{
    await call(owner,'decideAction',{actionId,approve,ttlMs:60_000n,note:'AUTOMATED integration-test owner approval; not live human approval'});
    await until(()=>rows(service,'myProtectedActions').find(r=>r.id===actionId&&r.approvalId),'service decision');
  };
  try {
    assert.notEqual(owner.identity,service.identity);
    const proof=await withNativeRelay(output,async(relay:any)=>{
      for(const name of ['read_demo_secret','deploy_dummy_artifact','run_shell']) {
        await test(`native Relay denies ${name} before callback`,async()=>{
          let callbacks=0;
          await denied(()=>relay.tool(name,{text:'untrusted README'},(args:any)=>managedTool(name,args),async()=>{callbacks++;return {}; }));
          assert.equal(callbacks,0);
        });
      }
      await test('pending approval denied inside native conditional guardrail',async()=>{
        const id=await propose();await denied(()=>executeRelayApprovedAction(service,id,relay));
        assert.equal(rows(service,'myProtectedActions').find(r=>r.id===id).status,'pending');
      });
      await test('explicit denied approval never consumes or runs callback',async()=>{
        const id=await propose();await approve(id,false);await denied(()=>executeRelayApprovedAction(service,id,relay));
        assert.equal(rows(service,'myProtectedActions').find(r=>r.id===id).status,'denied');
      });
      await test('expired approval denied before callback',async()=>{
        const id=await propose(1000n);await approve(id);await new Promise(r=>setTimeout(r,1100));
        await denied(()=>executeRelayApprovedAction(service,id,relay));
        assert.equal(rows(service,'myApprovals').find(r=>r.actionId===id).consumedAtMs,0n);
      });
      await test('approved exact action consumes before one callback; replay denied',async()=>{
        const id=await propose();await approve(id);
        const result=await executeRelayApprovedAction(service,id,relay);
        assert.equal(result.callback_executed,true);assert.equal(result.result.tool,'fixture_word_count');
        await until(()=>rows(service,'myProtectedActions').find(r=>r.id===id&&r.status==='succeeded'),'action outcome');
        const approval=rows(service,'myApprovals').find(r=>r.actionId===id);assert.equal(approval.status,'consumed');
        assert.ok(approval.consumedAtMs>0n);
        await denied(()=>executeRelayApprovedAction(service,id,relay));
      });
      return {trace_id:relay.traceId,tests,approval_provenance:'AUTOMATED separate owner identity integration-test fixture, not live human approval',model_inference:false};
    });
    const events=(await readFile(proof.native_relay.events_path,'utf8')).trim().split('\n').map(line => JSON.parse(line));
    const rejected=events.filter((e:any)=>e.kind==='scope'&&e.category==='guardrail'&&e.scope_category==='end'&&e.data?.rejected===true);
    assert.ok(rejected.length>=6,'Native guardrail rejection records required');
    const toolStarts=events.filter((e:any)=>e.kind==='scope'&&e.category==='tool'&&e.scope_category==='start');
    assert.equal(toolStarts.length,1,'Only the single approved fixture may emit a tool start');
    assert.equal(toolStarts[0].name,'fixture_word_count');
    assert.ok(!events.some((e:any)=>e.category==='llm'),'No LLM trace may be fabricated');
    const report={...proof,native_rejected_guardrails:rejected.length,native_tool_start_count:toolStarts.length};
    await writeFile(`${output}/report.json`,JSON.stringify(report,null,2)+'\n',{mode:0o600});
    console.log(JSON.stringify(report));
  }finally{closeAirlock([owner,service]);}
}
main().catch(error=>{console.error(String(error.message||error));process.exitCode=1;});
