import { describe,it } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { acquireProcessLock, canonicalExecutionLedger, evaluateLock, processLockRequest, readPrivateJson, StateStore, validateConsent, type Consent, type LocalState, type LockSnapshot } from '../airlock_lock_adapter';

const NOW=2_000_000,HASH='a'.repeat(64),IDENTITY='b'.repeat(64);
function valid(){
  const consent:Consent={schemaVersion:1,enabled:true,workstationId:'desk',deviceId:'mac',ownerPolicyHash:HASH,expiresAtMs:NOW+3600000};
  const state:LocalState={schemaVersion:1,workstationId:'desk',deviceId:'mac',identity:IDENTITY,lastAttemptAtMs:0,processed:{}};
  const snapshot:LockSnapshot={connected:true,fetchedAtMs:NOW,identity:IDENTITY,workstationId:'desk',deviceId:'mac',
    workstation:{id:'desk',activeSessionId:'session',autoLockEnabled:true,ownerPolicyHash:HASH,returnGraceUntilMs:0n,lockCooldownMs:30000n,lastLockRequestedAtMs:BigInt(NOW-1000)},
    session:{id:'session',workstationId:'desk',state:'armed',expiresAtMs:BigInt(NOW+600000),simulated:false,autoLockEnabled:true,ownerPolicyHash:HASH},
    membership:{workstationId:'desk',role:'mac',enabled:true},device:{id:'mac',workstationId:'desk',role:'mac',enabled:true,identity:IDENTITY},
    request:{id:'lock:incident:event',workstationId:'desk',sessionId:'session',targetDeviceId:'mac',status:'requested',simulated:false,issuedAtMs:BigInt(NOW-1000),expiresAtMs:BigInt(NOW+14000),ownerPolicyHash:HASH}};
  return {snapshot,consent,state};
}

describe('lock policy gate: every independent dimension fails closed',()=>{
  it('accepts the exact authorized test fixture without causing an OS action',()=>{const{s:_,...v}={s:0,...valid()};assert.equal(evaluateLock(v.snapshot,v.consent,v.state,NOW).allowed,true);});
  const changes:Array<[string,(s:LockSnapshot,c:Consent,state:LocalState)=>void]>=[
    ['disconnect',s=>{s.connected=false;}],['stale subscription proof',s=>{s.fetchedAtMs=NOW-2001;}],['future snapshot',s=>{s.fetchedAtMs=NOW+1001;}],
    ['wrong request workstation',s=>{s.request.workstationId='other';}],['wrong device workstation',s=>{s.device.workstationId='other';}],['wrong session workstation',s=>{s.session.workstationId='other';}],
    ['wrong target Mac',s=>{s.request.targetDeviceId='other';}],['different authenticated identity',s=>{s.device.identity='c'.repeat(64);}],['disabled device',s=>{s.device.enabled=false;}],['reviewer role',s=>{s.membership.role='reviewer';}],['revoked membership',s=>{s.membership.enabled=false;}],
    ['already executed request',s=>{s.request.status='executed';}],['simulated request',s=>{s.request.simulated=true;}],['simulated session',s=>{s.session.simulated=true;}],['invalid action ID',s=>{s.request.id='arbitrary-action';}],
    ['exact expiry boundary',s=>{s.request.expiresAtMs=BigInt(NOW);}],['future issue clock',s=>{s.request.issuedAtMs=BigInt(NOW+5001);}],['widened request deadline',s=>{s.request.expiresAtMs=s.request.issuedAtMs+15001n;}],
    ['owner disarmed',s=>{s.workstation.activeSessionId='';}],['rearmed session',s=>{s.workstation.activeSessionId='new-session';}],['session expiry',s=>{s.session.expiresAtMs=BigInt(NOW);}],['session not armed',s=>{s.session.state='disarmed';}],
    ['no frozen consent',s=>{s.session.autoLockEnabled=false;}],['current consent withdrawn',s=>{s.workstation.autoLockEnabled=false;}],['frozen policy changed',s=>{s.session.ownerPolicyHash='c'.repeat(64);}],['current policy changed',s=>{s.workstation.ownerPolicyHash='c'.repeat(64);}],
    ['owner return grace',s=>{s.workstation.returnGraceUntilMs=BigInt(NOW+1);}],['missing grace field',s=>{delete s.workstation.returnGraceUntilMs;}],['invalid cooldown',s=>{s.workstation.lockCooldownMs=1n;}],['superseded request',s=>{s.workstation.lastLockRequestedAtMs=BigInt(NOW);}],
    ['local attempt cooldown',(s,c,state)=>{state.lastAttemptAtMs=NOW-29999;}],['persisted reservation',(s,c,state)=>{state.processed[s.request.id]={reservedAtMs:NOW-1,expiresAtMs:NOW+1000,state:'reserved',detail:'uncertain',reported:false};}],
    ['wrong local-state identity',(s,c,state)=>{state.identity='d'.repeat(64);}],['disabled local consent',(s,c)=>{c.enabled=false;}],['local consent workstation mismatch',(s,c)=>{c.workstationId='other';}],['local consent device mismatch',(s,c)=>{c.deviceId='other';}],['local policy mismatch',(s,c)=>{c.ownerPolicyHash='d'.repeat(64);}],['expired local consent',(s,c)=>{c.expiresAtMs=NOW;}],['overbroad consent lifetime',(s,c)=>{c.expiresAtMs=NOW+86400001;}],
  ];
  for(const[name,change]of changes)it(name,()=>{const{snapshot,consent,state}=valid();change(snapshot,consent,state);assert.equal(evaluateLock(snapshot,consent,state,NOW).allowed,false);});
  it('missing local consent denies',()=>{const{snapshot,state}=valid();assert.equal(evaluateLock(snapshot,undefined,state,NOW).allowed,false);});
  it('accepts exact freshness and cooldown end boundaries',()=>{const{snapshot,consent,state}=valid();snapshot.fetchedAtMs=NOW-2000;state.lastAttemptAtMs=NOW-30000;assert.equal(evaluateLock(snapshot,consent,state,NOW).allowed,true);});
  it('rejects consent unknown keys and truthy nonbooleans',()=>{const{consent}=valid();assert.throws(()=>validateConsent({...consent,enabled:'true'},NOW));assert.throws(()=>validateConsent({...consent,command:'anything'},NOW));});
});

async function withStore(fn:(store:StateStore,directory:string)=>Promise<void>){const directory=await mkdtemp(join(tmpdir(),'airlock-lock-test-'));try{const{state}=valid();await fn(new StateStore(join(directory,'state.json'),state),directory);}finally{await rm(directory,{recursive:true,force:true});}}
function effects(snapshot:LockSnapshot,overrides:Record<string,any>={}){return{claim:async()=>{},now:()=>NOW,fresh:async()=>snapshot,executeShortcut:async()=>({shortcutPosted:true,screenLocked:null,confirmationSupported:false as const,reason:'FAKE executor for unit testing'}),observe:async()=>({guiSessionAvailable:true,screenLocked:null,confirmationSupported:false}),report:async()=>{},...overrides};}

describe('OS-action boundary uses injected test functions only',()=>{
  it('default dry run invokes no helper, observation, result mutation or persistent reservation',async()=>withStore(async(store,directory)=>{
    const{snapshot,consent}=valid();let invoked=0;const forbid=async()=>{invoked++;throw new Error('Side effect forbidden during dry-run');};
    const result=await processLockRequest(snapshot,consent,store,false,effects(snapshot,{executeShortcut:forbid,observe:forbid,report:forbid,fresh:forbid}));
    assert.equal(result.dryRun,true);assert.equal(result.executed,false);assert.equal(result.wouldPostShortcut,true);assert.equal(invoked,0);assert.deepEqual(store.value.processed,{});
    await assert.rejects(readFile(join(directory,'state.json')),/ENOENT/);
  }));
  it('durably reserves before the mocked invocation and never calls confirmed',async()=>withStore(async(store)=>{
    const{snapshot,consent}=valid();let calls=0;const statuses:string[]=[];
    const result=await processLockRequest(snapshot,consent,store,true,effects(snapshot,{executeShortcut:async()=>{calls++;const disk=await readPrivateJson(store.path);assert.equal(disk.processed[snapshot.request.id].state,'reserved');return{shortcutPosted:true,screenLocked:null,confirmationSupported:false,reason:'Mock shortcut'};},report:async(_id:string,status:string)=>{statuses.push(status);}}));
    assert.equal(calls,1);assert.equal(result.executed,true);assert.equal(result.confirmed,false);assert.equal(result.screenLocked,null);assert.deepEqual(statuses,['executed']);
    const restored=await StateStore.load(store.path,'desk','mac',IDENTITY);
    await processLockRequest(snapshot,consent,restored,true,effects(snapshot,{executeShortcut:async()=>{calls++;throw new Error('REPLAY');}}));
    assert.equal(calls,1,'Restart cannot replay the same OS action');
  }));
  it('disarm on the fresh server read prevents reservation and invocation',async()=>withStore(async(store)=>{
    const{snapshot,consent}=valid();const changed=structuredClone(snapshot);changed.workstation.activeSessionId='';let calls=0;
    const result=await processLockRequest(snapshot,consent,store,true,effects(snapshot,{fresh:async()=>changed,executeShortcut:async()=>{calls++;throw new Error('forbidden');}}));
    assert.equal(result.executed,false);assert.equal(calls,0);assert.deepEqual(store.value.processed,{});
  }));
  it('local operator stop during refresh prevents reservation and invocation',async()=>withStore(async(store)=>{
    const{snapshot,consent}=valid();let stopped=false,calls=0;
    const result=await processLockRequest(snapshot,consent,store,true,effects(snapshot,{shouldStop:()=>stopped,fresh:async()=>{stopped=true;return snapshot;},executeShortcut:async()=>{calls++;throw new Error('forbidden');}}));
    assert.equal(result.executed,false);assert.equal(calls,0);assert.deepEqual(store.value.processed,{});
  }));
  it('a replaced request ID cannot be substituted at the refresh boundary',async()=>withStore(async(store)=>{
    const{snapshot,consent}=valid();const changed=structuredClone(snapshot);changed.request.id='lock:incident:other';let calls=0;
    const result=await processLockRequest(snapshot,consent,store,true,effects(snapshot,{fresh:async()=>changed,executeShortcut:async()=>{calls++;throw new Error('forbidden');}}));
    assert.equal(result.executed,false);assert.equal(calls,0);assert.match(String(result.reason),/identity changed/);
  }));
  it('a crash/timeout outcome is uncertain and never triggers a shortcut retry',async()=>withStore(async(store)=>{
    const{snapshot,consent}=valid();let calls=0;
    const fx=effects(snapshot,{executeShortcut:async()=>{calls++;throw new Error('Mock helper timeout');}});
    const result=await processLockRequest(snapshot,consent,store,true,fx);
    assert.equal(result.outcome,'uncertain');assert.equal(result.executed,null);
    assert.equal(store.value.processed[snapshot.request.id].state,'uncertain');
    await processLockRequest(snapshot,consent,store,true,fx);assert.equal(calls,1);
  }));
  it('reporting failure after mock execution never permits OS replay',async()=>withStore(async(store)=>{
    const{snapshot,consent}=valid();let calls=0;
    const fx=effects(snapshot,{executeShortcut:async()=>{calls++;return{shortcutPosted:true,screenLocked:null,confirmationSupported:false,reason:'Mock'};},report:async()=>{throw new Error('Mock DB outage');}});
    await processLockRequest(snapshot,consent,store,true,fx);
    assert.equal(store.value.processed[snapshot.request.id].reported,false);
    await processLockRequest(snapshot,consent,store,true,fx);assert.equal(calls,1);
  }));
  it('Accessibility rejection from the mocked helper is failed without any confirmation',async()=>withStore(async(store)=>{
    const{snapshot,consent}=valid();const statuses:string[]=[];
    const result=await processLockRequest(snapshot,consent,store,true,effects(snapshot,{executeShortcut:async()=>({shortcutPosted:false,screenLocked:null,confirmationSupported:false,reason:'Accessibility not trusted; no prompt'}),report:async(_id:string,status:string)=>{statuses.push(status);}}));
    assert.equal(result.executed,false);assert.deepEqual(statuses,['failed']);assert.equal(result.confirmed,false);
  }));
});

describe('private local files and bounded process state',()=>{
  it('rejects readable-by-others and symlink credential/consent files',async()=>withStore(async(_store,directory)=>{
    const file=join(directory,'consent.json');await writeFile(file,'{}',{mode:0o644});await assert.rejects(readPrivateJson(file),/owner-only/);
    const secure=join(directory,'secure.json');await writeFile(secure,'{}',{mode:0o600});const link=join(directory,'link.json');await symlink(secure,link);await assert.rejects(readPrivateJson(link));
  }));
  it('refuses process state in directories writable by other users',async()=>withStore(async(store,directory)=>{
    const {chmod}=await import('node:fs/promises');await chmod(directory,0o777);
    try{await assert.rejects(acquireProcessLock(store.path),/not writable by others/);}finally{await chmod(directory,0o700);}
  }));
  it('exclusive process lock prevents two adapters from using one state file',async()=>withStore(async(store)=>{
    const release=await acquireProcessLock(store.path);try{await assert.rejects(acquireProcessLock(store.path),/EEXIST/);}finally{await release();}
  }));
  it('rejects state scope substitution and negative clocks',async()=>withStore(async(store)=>{
    await store.reserve('lock:incident:test',NOW+1000,NOW);
    await assert.rejects(StateStore.load(store.path,'other-desk','mac',IDENTITY),/another enrolled Mac/);
    const saved=JSON.parse(await readFile(store.path,'utf8'));saved.lastAttemptAtMs=-1;await writeFile(store.path,JSON.stringify(saved),{mode:0o600});
    await assert.rejects(StateStore.load(store.path,'desk','mac',IDENTITY),/invalid/);
  }));
  it('keeps reservations for24h past expiry and prunes only beyond that boundary',async()=>withStore(async(store)=>{
    await store.reserve('lock:incident:test',NOW+1000,NOW);store.prune(NOW+1000+86400000);assert.ok(store.value.processed['lock:incident:test']);
    store.prune(NOW+1001+86400000);assert.equal(Object.keys(store.value.processed).length,0);
  }));
});

it('canonical execution ledger ignores caller state paths and is scoped to enrolled device identity',()=>{assert.equal(canonicalExecutionLedger('desk','mac',IDENTITY),canonicalExecutionLedger('desk','mac',IDENTITY));assert.notEqual(canonicalExecutionLedger('desk','mac',IDENTITY),canonicalExecutionLedger('desk','other',IDENTITY));assert.ok(canonicalExecutionLedger('desk','mac',IDENTITY).includes('LOCKI AIRLOCK'));});
it('two independent ledgers/cloned clients cannot pass the same atomic server claim',async()=>withStore(async(store,directory)=>{
  const {snapshot,consent,state}=valid();const second=new StateStore(join(directory,'different-ledger.json'),structuredClone(state));let claimed=false,calls=0;
  const claim=async()=>{if(claimed)throw new Error('already claimed');claimed=true;};
  const fx=effects(snapshot,{claim,executeShortcut:async()=>{calls++;return{shortcutPosted:true,screenLocked:null,confirmationSupported:false,reason:'Mock only'};}});
  await Promise.all([processLockRequest(snapshot,consent,store,true,fx),processLockRequest(snapshot,consent,second,true,fx)]);
  assert.equal(calls,1);assert.equal(Object.values(store.value.processed).filter(row=>row.claimed).length+Object.values(second.value.processed).filter(row=>row.claimed).length,1);
}));
it('denied server claim invokes neither helper nor result reporting',async()=>withStore(async(store)=>{const{snapshot,consent}=valid();let calls=0;const result=await processLockRequest(snapshot,consent,store,true,effects(snapshot,{claim:async()=>{throw new Error('claimed elsewhere');},executeShortcut:async()=>{calls++;throw new Error('forbidden');},report:async()=>{calls++;}}));assert.equal(result.executed,false);assert.equal(calls,0);}));
