#!/usr/bin/env node
/** Consent-gated local Mac screen-lock adapter. Default dry-run never invokes a helper. */
import { execFile } from 'node:child_process';
import { constants } from 'node:fs';
import { mkdir, open, rename, unlink } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { randomUUID, createHash } from 'node:crypto';
import { userInfo } from 'node:os';
import { DbConnection } from '../frontend/src/airlock-bindings/index';
import { call, closeAirlock, rows, type AirlockClient } from './airlock_sdk';

const runFile = promisify(execFile);
const MAX_PROCESSED = 1024;
const MAX_PRIVATE_BYTES = 2_000_000;
const HELPER_PATH = resolve(dirname(fileURLToPath(import.meta.url)), '../data/airlock/airlock_lock_helper');
const idPattern = /^[a-zA-Z0-9_.:-]{1,128}$/;
export type Consent = { schemaVersion: 1; enabled: boolean; workstationId: string; deviceId: string; ownerPolicyHash: string; expiresAtMs: number };
export type LockSnapshot = {
  connected: boolean; fetchedAtMs: number; identity: string; workstationId: string; deviceId: string;
  workstation: any; session: any; membership: any; device: any; request: any;
};
type Processed = { executionId?: string; claimed?: boolean; reservedAtMs: number; expiresAtMs: number; state: 'reserved'|'executed'|'failed'|'uncertain'; detail: string; reported: boolean };
export type LocalState = { schemaVersion: 1; workstationId: string; deviceId: string; identity: string; lastAttemptAtMs: number; processed: Record<string, Processed> };
export type Decision = { allowed: boolean; reason: string };

function epoch(value: unknown): number {
  if (typeof value === 'bigint') return value >= 0n && value <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(value) : NaN;
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : NaN;
}
function identityHex(value: any): string { return typeof value?.toHexString === 'function' ? value.toHexString() : typeof value === 'string' ? value : ''; }
function invalid(reason: string): Decision { return { allowed: false, reason }; }
export function validateConsent(value: unknown, nowMs: number): Consent {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Local consent must be an object');
  const c = value as Consent;
  const allowed = new Set(['schemaVersion','enabled','workstationId','deviceId','ownerPolicyHash','expiresAtMs']);
  if (Object.keys(c).some(key=>!allowed.has(key)) || c.schemaVersion !== 1 || typeof c.enabled !== 'boolean' || !idPattern.test(c.workstationId) || !idPattern.test(c.deviceId) || !/^[a-f0-9]{64}$/.test(c.ownerPolicyHash)) throw new Error('Local consent schema/binding is invalid');
  if (!c.enabled) throw new Error('Local operator consent is disabled');
  if (!Number.isSafeInteger(c.expiresAtMs) || c.expiresAtMs <= nowMs || c.expiresAtMs > nowMs + 86_400_000) throw new Error('Local consent must be unexpired and no more than 24 hours ahead');
  return c;
}
/** Fail closed on every local/database policy dimension. No operating-system side effects. */
export function evaluateLock(s: LockSnapshot, consent: Consent | undefined, state: LocalState, nowMs: number): Decision {
  if (!s.connected || nowMs - s.fetchedAtMs > 2000 || s.fetchedAtMs > nowMs + 1000) return invalid('No fresh authenticated database snapshot');
  const { request: r, workstation: w, session: session, device: d, membership: m } = s;
  if (!r || !w || !session || !d || !m) return invalid('Required authorized workstation, device, membership, session or request is absent');
  if (state.workstationId !== s.workstationId || state.deviceId !== s.deviceId || state.identity !== s.identity) return invalid('Persistent adapter state belongs to another identity/workstation/device');
  if (w.id !== s.workstationId || r.workstationId !== s.workstationId || d.workstationId !== s.workstationId || m.workstationId !== s.workstationId || session.workstationId !== s.workstationId) return invalid('Cross-workstation request rejected');
  if (d.id !== s.deviceId || r.targetDeviceId !== s.deviceId || d.role !== 'mac' || d.enabled !== true || m.role !== 'mac' || m.enabled !== true || identityHex(d.identity) !== s.identity) return invalid('Request is not addressed to this enrolled Mac identity');
  if (r.status !== 'requested' || r.simulated !== false || session.simulated !== false) return invalid('Only a new, non-synthetic requested lock can execute');
  if (typeof r.id !== 'string' || !r.id.startsWith('lock:incident:') || r.id.length > 300) return invalid('Lock request identifier is invalid');
  if (Object.hasOwn(state.processed,r.id)) return invalid('Action already reserved/processed; never replay an OS action');
  if (Object.keys(state.processed).length >= MAX_PROCESSED) return invalid('Processed-action capacity reached; owner review required');
  const expiry=epoch(r.expiresAtMs), issued=epoch(r.issuedAtMs), sessionExpiry=epoch(session.expiresAtMs);
  if (![expiry,issued,sessionExpiry].every(Number.isFinite) || expiry <= nowMs || issued > nowMs+5000 || expiry-issued > 15000 || expiry <= issued) return invalid('Lock request deadline is expired or invalid');
  if (w.activeSessionId !== r.sessionId || session.id !== r.sessionId || session.state !== 'armed' || sessionExpiry <= nowMs) return invalid('Workstation has disarmed or changed session');
  if (session.autoLockEnabled !== true || w.autoLockEnabled !== true) return invalid('Owner auto-lock consent is not enabled');
  if (session.ownerPolicyHash !== r.ownerPolicyHash || w.ownerPolicyHash !== r.ownerPolicyHash) return invalid('Current/frozen owner policy no longer matches the request');
  if (!/^[a-f0-9]{64}$/.test(r.ownerPolicyHash)) return invalid('Owner policy digest is invalid');
  if (!Number.isFinite(epoch(w.returnGraceUntilMs)) || epoch(w.returnGraceUntilMs) > nowMs) return invalid('Owner-return grace period is active');
  const cooldown=epoch(w.lockCooldownMs), lastIssued=epoch(w.lastLockRequestedAtMs);
  if (!Number.isFinite(cooldown) || cooldown < 30000 || cooldown > 3600000 || !Number.isFinite(lastIssued) || lastIssued !== issued) return invalid('Request is superseded or cooldown policy is invalid');
  if (state.lastAttemptAtMs > 0 && state.lastAttemptAtMs + cooldown > nowMs) return invalid('Local screen-lock attempt cooldown is active');
  if (!consent) return invalid('No private local operator consent file is enabled');
  try { validateConsent(consent,nowMs); } catch (error) { return invalid((error as Error).message); }
  if (consent.workstationId !== s.workstationId || consent.deviceId !== s.deviceId || consent.ownerPolicyHash !== r.ownerPolicyHash) return invalid('Local consent does not match this workstation/device/policy');
  return { allowed: true, reason: 'Fresh targeted request, frozen/current policy and explicit local consent match' };
}

export async function readPrivateJson(path: string, maxBytes=MAX_PRIVATE_BYTES): Promise<any> {
  const handle=await open(path,constants.O_RDONLY|constants.O_NOFOLLOW);
  try {
    const stat=await handle.stat();
    if (!stat.isFile() || stat.size > maxBytes || stat.size < 1 || (stat.mode & 0o077)!==0 || (process.getuid && stat.uid!==process.getuid())) throw new Error('Private file must be a small regular owner-only file owned by this operator');
    return JSON.parse(await handle.readFile('utf8'));
  } finally { await handle.close(); }
}
async function stateDirectory(path:string):Promise<void>{
  await mkdir(dirname(path),{recursive:true,mode:0o700});
  const handle=await open(dirname(path),constants.O_RDONLY|constants.O_NOFOLLOW);
  try{const stat=await handle.stat();if(!stat.isDirectory()||(stat.mode&0o022)!==0||(process.getuid&&stat.uid!==process.getuid()))throw new Error('State directory must be owned by this operator and not writable by others');}finally{await handle.close();}
}
async function atomicState(path: string,value: LocalState): Promise<void> {
  await stateDirectory(path);
  const temporary=`${path}.${randomUUID()}.tmp`;
  const handle=await open(temporary,'wx',0o600);
  try { await handle.writeFile(JSON.stringify(value));await handle.sync(); } finally { await handle.close(); }
  await rename(temporary,path);
}
export class StateStore {
  constructor(readonly path: string,readonly value: LocalState) {}
  static async load(path: string,workstationId: string,deviceId: string,identity: string): Promise<StateStore> {
    let saved: LocalState;
    try { saved=await readPrivateJson(path); }
    catch(error:any) { if(error.code!=='ENOENT')throw error; saved={schemaVersion:1,workstationId,deviceId,identity,lastAttemptAtMs:0,processed:{}}; }
    if(saved.schemaVersion!==1||saved.workstationId!==workstationId||saved.deviceId!==deviceId||saved.identity!==identity||!Number.isSafeInteger(saved.lastAttemptAtMs)||saved.lastAttemptAtMs<0||!saved.processed||typeof saved.processed!=='object'||Array.isArray(saved.processed)||Object.keys(saved.processed).length>MAX_PROCESSED) throw new Error('Persistent state is invalid or belongs to another enrolled Mac');
    for(const [id,row] of Object.entries(saved.processed)) if(id.length>300||!['reserved','executed','failed','uncertain'].includes(row.state)||!Number.isSafeInteger(row.expiresAtMs)||!Number.isSafeInteger(row.reservedAtMs)||typeof row.reported!=='boolean'||typeof row.detail!=='string'||row.detail.length>1024)throw new Error('Persistent processed-action entry is invalid');
    return new StateStore(path,saved);
  }
  async reserve(actionId:string,expiresAtMs:number,nowMs:number):Promise<void> {
    if(Object.hasOwn(this.value.processed,actionId))throw new Error('Action already reserved');
    if(Object.keys(this.value.processed).length>=MAX_PROCESSED)throw new Error('Processed-action capacity reached');
    this.value.processed[actionId]={executionId:'lock-exec-'+randomUUID(),claimed:false,reservedAtMs:nowMs,expiresAtMs,state:'reserved',detail:'Reserved before helper invocation; crash outcome must be reviewed, never retried.',reported:false};
    this.value.lastAttemptAtMs=nowMs;
    await atomicState(this.path,this.value);
  }
  async outcome(actionId:string,state:Processed['state'],detail:string):Promise<void>{
    const row=this.value.processed[actionId];if(!row)throw new Error('Action was not reserved');
    this.value.processed[actionId]={...row,state,detail:detail.slice(0,1024)};await atomicState(this.path,this.value);
  }
  async claimed(actionId:string):Promise<void>{this.value.processed[actionId].claimed=true;await atomicState(this.path,this.value);}
  async reported(actionId:string):Promise<void>{this.value.processed[actionId].reported=true;await atomicState(this.path,this.value);}
  prune(nowMs:number):void { for(const [id,row]of Object.entries(this.value.processed))if(row.expiresAtMs+86_400_000<nowMs)delete this.value.processed[id]; }
}
export function canonicalExecutionLedger(workstationId:string,deviceId:string,identity:string):string {
  const key=createHash('sha256').update(workstationId+'\0'+deviceId+'\0'+identity).digest('hex');
  return resolve(userInfo().homedir,'Library','Application Support','LOCKI AIRLOCK','lock-execution',key+'.json');
}
export async function acquireProcessLock(path:string):Promise<()=>Promise<void>> {
  await stateDirectory(path);
  const lockPath=path+'.process.lock';const handle=await open(lockPath,'wx',0o600);
  await handle.writeFile(JSON.stringify({pid:process.pid,startedAtMs:Date.now()}));await handle.sync();
  return async()=>{await handle.close();await unlink(lockPath);};
}
type HelperResult={shortcutPosted:boolean;screenLocked:null;confirmationSupported:false;reason:string};
type Effects={claim:(actionId:string,executionId:string)=>Promise<void>;executeShortcut:(snapshot:LockSnapshot)=>Promise<HelperResult>;observe:()=>Promise<unknown>;report:(actionId:string,status:'executed'|'failed',detail:string)=>Promise<void>;fresh:()=>Promise<LockSnapshot>;now:()=>number;shouldStop?:()=>boolean};
/** Effects are injected only for tests; the CLI supplies one fixed Swift executable. */
export async function processLockRequest(snapshot:LockSnapshot,consent:Consent|undefined,store:StateStore,execute:boolean,effects:Effects):Promise<Record<string,unknown>>{
  if(effects.shouldStop?.())return{actionId:snapshot.request?.id,executed:false,reason:'Local operator stopped the adapter'};
  let decision=evaluateLock(snapshot,consent,store.value,effects.now());
  if(!decision.allowed||!execute)return{actionId:snapshot.request?.id,dryRun:!execute,wouldPostShortcut:decision.allowed,executed:false,reason:decision.reason};
  const requestedId=snapshot.request.id;
  snapshot=await effects.fresh();if(snapshot.request?.id!==requestedId)return{actionId:requestedId,executed:false,reason:'Request identity changed during the authorization refresh'};decision=evaluateLock(snapshot,consent,store.value,effects.now());
  if(!decision.allowed)return{actionId:snapshot.request?.id,executed:false,reason:decision.reason};
  if(effects.shouldStop?.())return{actionId:snapshot.request?.id,executed:false,reason:'Local operator stopped the adapter'};
  const id=snapshot.request.id;
  await store.reserve(id,epoch(snapshot.request.expiresAtMs),effects.now());
  try { await effects.claim(id,store.value.processed[id].executionId!);await store.claimed(id); } catch {
    await store.outcome(id,'failed','One-time server execution claim denied or uncertain; no helper invoked and no OS retry.');
    return{actionId:id,executed:false,reason:'Server execution claim denied or uncertain; no helper invoked'};
  }
  let result:HelperResult;
  try { if(effects.shouldStop?.())throw new Error('Local adapter stopped before helper invocation');result=await effects.executeShortcut(snapshot); }
  catch(error){
    const detail=`Helper outcome uncertain; no OS-action retry will occur. ${(error as Error).name}`;
    await store.outcome(id,'uncertain',detail);
    try{await effects.report(id,'failed',detail);await store.reported(id);}catch{/* Keep durable outcome; never rerun shortcut. */}
    return{actionId:id,executed:null,outcome:'uncertain',screenLocked:null,reason:detail};
  }
  const posted=result.shortcutPosted===true;
  let observation:unknown={screenLocked:null,confirmationSupported:false};
  if(posted){try{observation=await effects.observe();}catch{observation={screenLocked:null,confirmationSupported:false,reason:'Independent observation unavailable'};}}
  const detail=posted?'Control-Command-Q shortcut posted. Screen-lock confirmation unavailable; verify locally.':`No shortcut posted: ${String(result.reason).slice(0,600)}`;
  await store.outcome(id,posted?'executed':'failed',detail);
  try{await effects.report(id,posted?'executed':'failed',detail);await store.reported(id);}catch{/* Already handled locally; outcome reporting may be uncertain. */}
  return{actionId:id,executed:posted,screenLocked:null,confirmed:false,observation,reason:detail};
}

function sqlObjects(statement:any):any[]{
  const columns=statement.schema.elements.map((e:any)=>e.name.some.replace(/_([a-z])/g,(_:string,c:string)=>c.toUpperCase()));
  return statement.rows.map((values:any[])=>Object.fromEntries(columns.map((name:string,i:number)=>[name,values[i]])));
}
async function connectMacAdapter(token:string,workstationId:string,deviceId:string):Promise<AirlockClient>{
  return new Promise((resolveClient,reject)=>{
    let connection:DbConnection|undefined;
    const timeout=setTimeout(()=>{connection?.disconnect();reject(new Error('Mac adapter connection/subscription timed out'));},15000);
    const fail=(error:unknown)=>{clearTimeout(timeout);connection?.disconnect();reject(error instanceof Error?error:new Error(String(error)));};
    connection=DbConnection.builder().withUri(process.env.AIRLOCK_SPACETIME_URI||'http://127.0.0.1:3000').withDatabaseName(process.env.AIRLOCK_DATABASE||'locki-airlock-sid').withToken(token)
      .onConnect((conn,identity,savedToken)=>{
        conn.subscriptionBuilder().onApplied(()=>{clearTimeout(timeout);resolveClient({conn,identity:identity.toHexString(),token:savedToken});}).onError(ctx=>fail(ctx.event)).subscribe([
          `SELECT * FROM my_workstations WHERE id = '${workstationId}'`,
          `SELECT * FROM my_sessions WHERE workstation_id = '${workstationId}' AND state = 'armed'`,
          `SELECT * FROM my_devices WHERE id = '${deviceId}'`,
          `SELECT * FROM my_memberships WHERE workstation_id = '${workstationId}'`,
          `SELECT * FROM my_lock_requests WHERE workstation_id = '${workstationId}' AND target_device_id = '${deviceId}' AND (status = 'requested' OR status = 'executing')`,
        ]);
      }).onConnectError((_ctx,error)=>fail(error)).build();
  });
}
async function freshSnapshots(client:AirlockClient,workstationId:string,deviceId:string):Promise<LockSnapshot[]>{
  if(!client.conn.isActive)throw new Error('Database subscription is disconnected');
  const raw=process.env.AIRLOCK_SPACETIME_URI||'http://127.0.0.1:3000';
  const uri=new URL(raw.replace(/^ws:/,'http:').replace(/^wss:/,'https:'));
  if(!['http:','https:'].includes(uri.protocol)||uri.username||uri.password||uri.pathname!=='/'||uri.search||uri.hash)throw new Error('SpacetimeDB URI must be an HTTP(S)/WS(S) origin without credentials');
  const database=process.env.AIRLOCK_DATABASE||'locki-airlock-sid';
  const queries=[`SELECT * FROM my_workstations WHERE id = '${workstationId}'`,`SELECT * FROM my_sessions WHERE workstation_id = '${workstationId}' AND state = 'armed'`,`SELECT * FROM my_devices WHERE id = '${deviceId}'`,`SELECT * FROM my_memberships WHERE workstation_id = '${workstationId}'`,`SELECT * FROM my_lock_requests WHERE workstation_id = '${workstationId}' AND target_device_id = '${deviceId}' AND (status = 'requested' OR status = 'executing')`];
  const fetchedAtMs=Date.now();
  // 2.14.1 rejects multiple SQL statements despite older HTTP documentation.
  // Read the five scoped views concurrently and fail if any response is absent.
  const results=await Promise.allSettled(queries.map(async query=>{
    const response=await fetch(`${uri.origin}/v1/database/${encodeURIComponent(database)}/sql`,{method:'POST',headers:{Authorization:`Bearer ${client.token}`,'Content-Type':'text/plain'},body:query,signal:AbortSignal.timeout(2000)});
    if(!response.ok)throw new Error(`Fresh authorized snapshot failed (HTTP${response.status})`);
    const body=await response.text();if(body.length>1_000_000)throw new Error('Authorized snapshot exceeds size limit');
    const result=JSON.parse(body);if(!Array.isArray(result)||result.length!==1)throw new Error('Unexpected authorized snapshot shape');
    return result[0];
  }));
  const statements=results.map(result=>{if(result.status==='rejected')throw result.reason;return result.value;});
  const [workstations,sessions,devices,memberships,requests]=statements.map(sqlObjects);
  // For a Mac caller, my_devices is server-filtered to its own identity. The
  // independently refreshed membership/device views enforce enrollment even
  // if a subscription cache has not yet received revocation. Cached Identity
  // decoding is an additional check, never the permission authority.
  const cached=rows(client,'myDevices',workstationId).find(d=>d.id===deviceId);
  return requests.map((r:any)=>({connected:client.conn.isActive,fetchedAtMs,identity:client.identity,workstationId,deviceId,workstation:workstations[0],session:sessions.find((s:any)=>s.id===r.sessionId),device:devices[0]?{...devices[0],identity:cached?.identity}:undefined,membership:memberships[0],request:r}));
}
async function fixedHelper(mode:'execute'|'observe',snapshot:LockSnapshot|undefined,consentFile:string):Promise<any>{
  if(process.platform!=='darwin')throw new Error('Real screen locking requires macOS');
  const handle=await open(HELPER_PATH,constants.O_RDONLY|constants.O_NOFOLLOW);
  try{const stat=await handle.stat();if(!stat.isFile()||(stat.mode&0o077)!==0||(stat.mode&0o100)===0||(process.getuid&&stat.uid!==process.getuid()))throw new Error('Fixed Swift helper must be an owner-only executable owned by this operator');}finally{await handle.close();}
  const args=mode==='observe'?['--observe']:['--post-control-command-q','--operator-consented','--consent-file',consentFile];
  if(mode==='observe'){
    const result=await runFile(HELPER_PATH,args,{timeout:2000,maxBuffer:8192,windowsHide:true});return JSON.parse(result.stdout);
  }
  return await new Promise((resolveResult,reject)=>{
    const child=execFile(HELPER_PATH,args,{timeout:2000,maxBuffer:8192,windowsHide:true,env:{...process.env,AIRLOCK_LOCAL_LOCK_EXECUTION:'explicit-operator-consent'}},(error,stdout)=>{
      try{const result=JSON.parse(stdout);if(typeof result.shortcutPosted==='boolean')resolveResult(result);else reject(error||new Error('Unexpected Swift result'));}catch{reject(error||new Error('Swift result unavailable'));}
    });
    child.stdin?.end(JSON.stringify({actionId:snapshot!.request.id,workstationId:snapshot!.workstationId,deviceId:snapshot!.deviceId,ownerPolicyHash:snapshot!.request.ownerPolicyHash,expiresAtMs:epoch(snapshot!.request.expiresAtMs)}));
  });
}

async function main(){
  const argv=process.argv.slice(2);const value=(flag:string)=>{const i=argv.indexOf(flag);return i<0?undefined:argv[i+1];};
  if(!argv.length||argv.includes('--help')){console.log(`AIRLOCK Mac lock adapter — DEFAULT DRY RUN\n\nPreview authorized requests (no helper invocation or result mutation):\n  frontend/node_modules/.bin/tsx scripts/airlock_lock_adapter.ts --workstation ID --device MAC_ID --token-file PRIVATE_TOKEN_JSON --state-file PRIVATE_STATE_JSON --once\n\nReal execution uses a fixed canonical per-device/identity ledger and a one-time database execution claim; --state-file is preview-only. Real execution requires the operator to supply BOTH --execute and --consent-file PRIVATE_CONSENT_JSON. The local consent must bind workstation, enrolled Mac device and exact policy hash, be enabled and unexpired. A separately compiled fixed Swift helper must be installed at data/airlock/airlock_lock_helper (mode700).\nWrite a DISABLED owner-only local consent template with --write-consent-template PATH, then review it locally. No lock confirmation is inferred from shortcut/process success. See docs/airlock/LOCK_ADAPTER.md.\nThis command never changes Accessibility permission or requests a prompt.`);return;}
  const workstationId=value('--workstation')||'',deviceId=value('--device')||'',tokenFile=value('--token-file'),stateFile=value('--state-file'),consentFile=value('--consent-file');
  if(!idPattern.test(workstationId)||!idPattern.test(deviceId)||!tokenFile||!stateFile)throw new Error('Provide valid --workstation/--device and private --token-file/--state-file');
  const execute=argv.includes('--execute');if(execute&&(!consentFile||process.platform!=='darwin'))throw new Error('Real execution requires macOS and --consent-file plus explicit --execute');
  const pollMs=Number(value('--poll-ms')||250),duration=Number(value('--duration')||0);if(!Number.isFinite(pollMs)||pollMs<100||pollMs>5000||!Number.isFinite(duration)||duration<0)throw new Error('Invalid poll interval/duration');
  const saved=await readPrivateJson(tokenFile,16384);if(typeof saved.token!=='string'||saved.token.length<16)throw new Error('Token file must contain an enrolled Mac token');
  const client=await connectMacAdapter(saved.token,workstationId,deviceId);let release:(()=>Promise<void>)|undefined;
  const seen=new Set<string>();const started=Date.now();let stopped=false;const stop=()=>{stopped=true;};process.once('SIGINT',stop);process.once('SIGTERM',stop);
  try{
    const templatePath=value('--write-consent-template');
    if(templatePath){
      const station=rows(client,'myWorkstations',workstationId).find(w=>w.id===workstationId);
      const device=rows(client,'myDevices',workstationId).find(d=>d.id===deviceId);
      const membership=rows(client,'myMemberships',workstationId)[0];
      if(!station||!device||device.role!=='mac'||device.enabled!==true||membership?.role!=='mac'||membership.enabled!==true||identityHex(device.identity)!==client.identity||!/^[a-f0-9]{64}$/.test(station.ownerPolicyHash))throw new Error('Consent template requires this enrolled Mac identity and workstation');
      const output=resolve(templatePath);await mkdir(dirname(output),{recursive:true,mode:0o700});
      const file=await open(output,'wx',0o600);try{await file.writeFile(JSON.stringify({schemaVersion:1,enabled:false,workstationId,deviceId,ownerPolicyHash:station.ownerPolicyHash,expiresAtMs:Date.now()+3600000},null,2)+'\n');await file.sync();}finally{await file.close();}
      console.log(JSON.stringify({consentTemplateWritten:output,enabled:false,osActionInvoked:false}));return;
    }
    const ledgerPath=execute?canonicalExecutionLedger(workstationId,deviceId,client.identity):resolve(stateFile);
    if(execute)release=await acquireProcessLock(ledgerPath);
    // The canonical ledger is read only AFTER taking the canonical execution mutex.
    const store=await StateStore.load(ledgerPath,workstationId,deviceId,client.identity);
    console.log(JSON.stringify({adapter:'Mac screen-lock adapter',mode:execute?'explicit-consented-execution':'dry-run',workstationId,deviceId,confirmationSupported:false}));
    do{
      if(!client.conn.isActive)throw new Error('Database disconnected; real OS actions fail closed');
      store.prune(Date.now());
      const snapshots=await freshSnapshots(client,workstationId,deviceId);
      if(execute)for(const snapshot of snapshots){
        const prior=store.value.processed[snapshot.request.id];
        if(!prior||prior.claimed!==true||prior.reported||prior.expiresAtMs<=Date.now())continue;
        if(prior.state==='reserved')await store.outcome(snapshot.request.id,'uncertain','A prior process reserved this action; its OS outcome is uncertain. Never retry the shortcut.');
        const current=store.value.processed[snapshot.request.id];
        try{await call(client,'reportLockResult',{actionId:snapshot.request.id,status:current.state==='executed'?'executed':'failed',detail:current.detail});await store.reported(snapshot.request.id);}catch{/* Keep local evidence; do not repeat the OS action. */}
      }
      for(const snapshot of snapshots){
        if(stopped)break;
        let consent:Consent|undefined;
        if(consentFile){try{consent=validateConsent(await readPrivateJson(consentFile,16384),Date.now());}catch(error){console.error(`Local consent denied: ${(error as Error).message}`);}}
        const effects:Effects={claim:async(actionId,executionId)=>call(client,'claimLock',{actionId,executionId}),now:()=>Date.now(),shouldStop:()=>stopped,fresh:async()=>{const fresh=(await freshSnapshots(client,workstationId,deviceId)).find(s=>s.request.id===snapshot.request.id);if(!fresh)throw new Error('Request withdrawn before execution');return fresh;},executeShortcut:async(s)=>{const latest=(await freshSnapshots(client,workstationId,deviceId)).find(row=>row.request.id===s.request.id);if(!latest||latest.request.status!=='executing')throw new Error('Claimed request withdrawn before helper');const pending={...store.value,processed:{...store.value.processed},lastAttemptAtMs:0};delete pending.processed[s.request.id];const check=evaluateLock({...latest,request:{...latest.request,status:'requested'}},consent,pending,Date.now());if(!check.allowed||stopped)throw new Error('Claimed request policy withdrawn before helper');return fixedHelper('execute',s,consentFile!);},observe:async()=>fixedHelper('observe',undefined,consentFile!),report:async(actionId,status,detail)=>call(client,'reportLockResult',{actionId,status,detail})};
        const result=await processLockRequest(snapshot,consent,store,execute,effects);
        const key=`${snapshot.request.id}:${result.reason}`;if(!seen.has(key)){console.log(JSON.stringify(result));if(seen.size>=1024)seen.clear();seen.add(key);}
      }
      if(stopped||argv.includes('--once')||(duration&&Date.now()-started>=duration*1000))break;
      await new Promise(resolveWait=>setTimeout(resolveWait,pollMs));
    }while(!stopped);
  }finally{process.removeListener('SIGINT',stop);process.removeListener('SIGTERM',stop);closeAirlock([client]);if(release)await release();}
}
if(process.argv[1]?.endsWith('airlock_lock_adapter.ts'))main().catch(error=>{console.error(error instanceof Error?error.message:String(error));process.exitCode=1;});
