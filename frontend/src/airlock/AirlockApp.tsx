import { useEffect, useMemo, useRef, useState } from "react";
import { Activity, ArrowUpRight, Cable, Camera, Check, ChevronDown, CircleHelp, Copy, Fingerprint, FlaskConical, KeyRound, Laptop, LockKeyhole, Monitor, Plus, Radio, RefreshCw, Settings2, Shield, ShieldAlert, ShieldCheck, ShieldOff, SlidersHorizontal, Unplug, Users, Wifi, WifiOff, X } from "lucide-react";
import { ToastProvider } from "../components/Toasts";
import { type ConnectionConfig, readConfig, useAirlock } from "./client";
import { type Row, ago, clock, milliseconds, shortId, textValue, words } from "./format";
import { AccessForm, ArmForm, BaselineList, ConnectionForm, PolicyForm, RegisterForm, RequestActionForm, type Perform } from "./forms";
import { Investigation, ProtectedActions, ReviewPanel } from "./Investigation";
import { Timeline } from "./Timeline";
import { Badge, Button, Panel } from "./ui";
import { VoiceBridge } from "./VoiceBridge";
import { detectionCoverage, healthStatus, latestHealth } from "./coverage";
import "./airlock.css";

type Dialog = "connect" | "register" | "arm" | "access" | "policy" | "action" | null;

function AirlockMark({ size = 30 }: { size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 36 36" fill="none" aria-hidden="true"><path d="M7 7h22v22H7V7Z" stroke="currentColor" strokeWidth="2" /><path d="M13 2v32M23 2v32" stroke="currentColor" strokeWidth="2" /><path d="m17 14 4 4-4 4" stroke="currentColor" strokeWidth="2" /><path d="M2 13h5m22 0h5M2 23h5m22 0h5" stroke="currentColor" strokeWidth="2" /></svg>;
}

function StationDiagram({ armed, incident, simulated }: { armed: boolean; incident: boolean; simulated: boolean }) {
  return <div className={`al-station-diagram ${armed ? "is-armed" : ""} ${incident ? "has-incident" : ""}`} aria-label="Workstation schematic, not a live camera feed"><div className="al-zone-boundary"><span className="al-zone-label">Workstation boundary</span><div className="al-desk-monitor"><Monitor size={48} strokeWidth={1.2} /><div className="al-desk-keyboard" /></div><span className="al-camera-node"><Camera size={16} /><span>Camera</span></span><span className="al-usb-node"><Cable size={16} /><span>USB / apps</span></span><div className="al-desk-outline" /></div><span className="al-schematic-label">Schematic {simulated ? "· synthetic session" : "· no camera stream"}</span></div>;
}

function SensorRow({ icon, title, health, now, enrolled, connected }: { icon: React.ReactNode; title: string; health?: Row; now: number; enrolled: boolean; connected: boolean }) {
  const status = healthStatus(health, now, connected, enrolled);
  const online = status === "online";
  return <div className="al-sensor-row"><span className="al-sensor-icon">{icon}</span><div><strong>{title}</strong><small title={health?.detail}>{health ? `${ago(health.lastSeenMs, now)}${health.detail ? ` · ${health.detail}` : ""}` : enrolled ? "Waiting for publisher health" : "Enroll a trusted publisher"}</small></div><Badge tone={online ? "cyan" : "amber"} dot>{words(status)}</Badge></div>;
}

function FixturePanel({ station, session, devices, owner, role, connected, pending, perform }: { station?: Row; session?: Row; devices: Row[]; owner: boolean; role: string; connected: boolean; pending: boolean; perform: Perform }) {
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState("correlated");
  const [deviceKey, setDeviceKey] = useState("synthetic-usb-demo");
  const localSeq = useRef<Record<string, bigint>>({});
  const publisher = owner ? devices.find(d => d.id === `${station?.id}:fixture`) : devices.find(d => d.role === role && d.enabled);
  const canPublish = owner || ["mac", "jetson"].includes(role);
  const enabled = connected && session?.state === "armed" && session.simulated && canPublish;
  const emit = async () => {
    if (!station || !session || !enabled) return;
    const deviceId = owner ? "fixture" : publisher?.id;
    if (!deviceId) return;
    const actualKind = kind === "correlated" && !owner ? role === "jetson" ? "person_in_zone" : "usb_added" : kind;
    const kinds = actualKind === "correlated" ? ["person_in_zone", "usb_added"] : [actualKind];
    let seq = BigInt(publisher?.lastSeq || 0);
    if ((localSeq.current[deviceId] || 0n) > seq) seq = localSeq.current[deviceId];
    for (const eventKind of kinds) {
      seq += 1n;
      const metadata = eventKind.startsWith("usb") ? { deviceKey, label: "Synthetic harmless USB fixture" } : eventKind === "app_launched" ? { appId: "synthetic.remote-admin", label: "Synthetic remote-admin app fixture" } : { detail: `Synthetic ${words(eventKind)} fixture; no camera inference` };
      const ok = await perform("ingestDeviceEvent", { workstationId: station.id, sessionId: session.id, deviceId, eventId: `fixture-${crypto.randomUUID()}`, kind: eventKind, observedAtMs: BigInt(Math.max(Date.now(), milliseconds(session.armedAtMs))), seq, confidence: 1, metadataJson: JSON.stringify(metadata), simulated: true, schemaVersion: 1 }, kinds.length > 1 && eventKind === kinds[0] ? "Synthetic physical observation committed." : "Synthetic observation committed to SpacetimeDB.");
      if (!ok) return;
      localSeq.current[deviceId] = seq;
    }
  };
  return <section className="al-fixtures"><button className="al-fixture-heading" aria-expanded={open} onClick={() => setOpen(!open)}><span><FlaskConical size={16} /><strong>Demo fixtures</strong><Badge tone="amber">Synthetic only</Badge></span><ChevronDown size={15} className={open ? "rotated" : ""} /></button>{open && <div className="al-fixture-body"><p>Send labeled fixture observations through real reducers. These do not represent a physical sensor, model inference or real device insertion.</p><div className="al-fixture-controls"><select aria-label="Fixture scenario" value={kind} onChange={e => setKind(e.target.value)}>{owner && <option value="correlated">Person + unknown USB · correlated</option>}{(owner || role === "mac") && <><option value="usb_added">Unknown USB connected</option><option value="app_launched">Remote-admin app launched</option></>}{(owner || role === "jetson") && <><option value="person_in_zone">Person in desk zone</option><option value="camera_offline">Camera unavailable</option></>}</select><Button kind="warning" disabled={!enabled || pending} onClick={emit}><FlaskConical size={14} />Send fixture</Button></div>{kind.includes("usb") || kind === "correlated" ? <label className="al-form-field"><span>Fixture device key</span><input value={deviceKey} onChange={e => setDeviceKey(e.target.value)} /></label> : null}{!enabled && <p className="al-small al-text-amber">{!connected ? "Connect to the database to use fixtures." : !session || session.state !== "armed" ? "Arm a synthetic session first." : !session.simulated ? "Fixtures are blocked in a live session." : "Only an owner or enrolled sensor publisher can send fixtures."}</p>}<div className="al-fixture-guarantee"><LockKeyhole size={12} />Synthetic sessions never request a real screen lock.</div></div>}</section>;
}

function ControlRoom() {
  const [config, setConfig] = useState<ConnectionConfig>(readConfig);
  const live = useAirlock(config);
  const [dialog, setDialog] = useState<Dialog>(null);
  const [stationId, setStationId] = useState("");
  const [incidentId, setIncidentId] = useState("");
  const [now, setNow] = useState(Date.now());
  const [notice, setNotice] = useState<{ text: string; error: boolean } | null>(null);
  const [copied, setCopied] = useState(false);
  useEffect(() => { const id = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(id); }, []);
  useEffect(() => { if (!notice) return; const id = window.setTimeout(() => setNotice(null), notice.error ? 12000 : 5000); return () => window.clearTimeout(id); }, [notice]);
  const connected = live.status === "connected";
  const pending = Boolean(live.pending);
  const stations = live.data.workstations;
  const station = stations.find(s => s.id === stationId) || stations[0];
  const scoped = useMemo(() => Object.fromEntries(Object.entries(live.data).map(([key, rows]) => [key, rows.filter(r => r.workstationId === station?.id)])) as typeof live.data, [live.data, station?.id]);
  const membership = scoped.memberships.find(m => textValue(m.identity) === live.identity && m.enabled);
  const role = membership?.role || "unenrolled";
  const owner = role === "owner";
  const reviewer = owner || role === "reviewer";
  const canApprove = owner || Boolean(membership?.canApprove);
  const sessions = [...scoped.sessions].sort((a, b) => Number(b.armedAtMs) - Number(a.armedAtMs));
  const session = sessions.find(s => s.id === station?.activeSessionId);
  const armed = session?.state === "armed" && milliseconds(session.expiresAtMs) > now;
  const incidents = [...scoped.incidents].sort((a, b) => Number(b.createdAtMs) - Number(a.createdAtMs));
  const selectedIncident = incidents.find(i => i.id === incidentId) || incidents.find(i => i.state !== "resolved") || incidents[0];
  const unresolved = incidents.filter(i => i.state !== "resolved");
  const currentIncident = unresolved.find(i => i.sessionId === session?.id);
  const ownerPanic = currentIncident?.ruleId === "owner_panic";
  const locks = [...scoped.locks].sort((a, b) => Number(b.issuedAtMs) - Number(a.issuedAtMs));
  const currentLock = locks.find(l => l.sessionId === session?.id && !["expired", "cancelled", "failed"].includes(l.status));
  const deviceFor = (r: string) => scoped.devices.some(d => d.role === r && d.enabled);
  const camera = latestHealth(scoped.health, scoped.devices, "camera");
  const usb = latestHealth(scoped.health, scoped.devices, "usb");
  const appHealth = latestHealth(scoped.health, scoped.devices, "apps");
  const model = latestHealth(scoped.health, scoped.devices, "model");
  const capacity = scoped.health.find(h => h.deviceId === "database" && h.component === "capacity" && h.status === "degraded");
  const coverage = detectionCoverage(scoped.health, scoped.devices, now, connected);
  const degraded = coverage !== "available";
  const visibleGate = ["owner", "reviewer", "service"].includes(role);
  const title = !connected ? "Console disconnected" : !station ? "Your desk. Your boundary." : currentLock?.status === "confirmed" ? currentLock.simulated ? "Synthetic lock confirmation" : "Screen lock confirmed" : currentLock ? currentLock.simulated ? "Synthetic lock request committed" : "Screen lock requested" : ownerPanic ? "Owner security alert" : currentIncident ? "Activity needs your review" : armed ? "Your workstation is armed" : "Your workstation is disarmed";
  const status = !connected ? "Disconnected" : currentLock?.status === "confirmed" ? currentLock.simulated ? "Synthetic lock confirmation" : "Screen lock confirmed" : currentLock ? currentLock.simulated ? "Synthetic lock request committed" : "Screen lock requested" : ownerPanic ? "Panic requested" : currentIncident ? "Suspected intrusion" : armed ? "Armed" : "Disarmed";
  const perform: Perform = async (reducer, args, success) => {
    try { await live.call(reducer, args); if (success) setNotice({ text: success, error: false }); return true; }
    catch (e) { setNotice({ text: e instanceof Error ? e.message : String(e), error: true }); return false; }
  };
  const copyIdentity = async () => { if (!live.identity) return; try { await navigator.clipboard.writeText(live.identity); setCopied(true); window.setTimeout(() => setCopied(false), 2000); } catch { setDialog("connect"); } };
  const saveConnection = (next: ConnectionConfig) => {
    localStorage.setItem("airlock.connection.v1", JSON.stringify({ uri: next.uri, database: next.database }));
    const url = new URL(location.href); url.searchParams.set("client", next.profile); history.replaceState(null, "", url);
    setConfig(next);
  };
  const quickRegister = () => perform("registerWorkstation", { workstationId: `desk-${live.identity.slice(0, 8)}`, name: "My workstation" }, "Workstation created. You are its owner.");
  return <div className="airlock-app"><header className="al-header"><a className="al-brand" href={`?product=airlock&client=${encodeURIComponent(config.profile)}`}><AirlockMark /><div><span>LOCKI <b>AIRLOCK</b></span><small>Workstation security control room</small></div></a><div className="al-header-actions"><button className={`al-connection-pill ${connected ? "is-connected" : ""}`} aria-label={`Database connection: ${connected ? "connected" : live.status}. ${config.database}`} title={`${config.uri} / ${config.database}`} onClick={() => setDialog("connect")}>{connected ? <Wifi size={13} /> : <WifiOff size={13} />}<span>{connected ? "SpacetimeDB connected" : live.status === "offline" ? "SpacetimeDB offline" : "Connecting to SpacetimeDB"}</span><ChevronDown size={12} /></button><button className="al-profile" aria-label={`Identity settings for ${config.profile}`} title={`Public identity: ${live.identity || "not connected"}`} onClick={() => setDialog("connect")}><span className="al-avatar">{config.profile.slice(0, 1).toUpperCase()}</span><span>{config.profile}<small>{words(role)}</small></span><Settings2 size={14} /></button></div></header>
    <main className="al-main"><section className="al-status-section"><div className="al-status-copy"><div className="al-inline"><Badge tone={currentIncident ? "magenta" : armed ? "amber" : "quiet"} dot>{status}</Badge>{station && degraded && <Badge tone="amber">{connected ? "Degraded coverage" : "Coverage unverified"}</Badge>}{session?.simulated && <Badge tone="amber">Synthetic session</Badge>}</div><h1>{title}</h1><p>{!connected ? "Security state cannot be verified. Reconnecting with this console's persisted identity." : !station ? "Register your workstation, enroll trusted devices, and keep every security decision in one shared timeline." : ownerPanic ? "The authenticated owner requested this security alert. A lock requires the frozen response policy and local Mac consent; no compromise is inferred." : currentIncident ? "An unapproved digital observation triggered a deterministic incident. Review the evidence before taking action." : armed ? `Monitoring until ${clock(session?.expiresAtMs)}. Deterministic rules act independently of model availability.` : "Arm when you step away. Your device baseline and response policy are frozen for that session."}</p><div className="al-status-source"><Shield size={12} />{connected ? "Security state: server-authorized SpacetimeDB reducers" : "State source unavailable · last subscribed data may be stale"}</div></div><div className="al-primary-actions">{station ? <><Button kind={armed ? "default" : "primary"} disabled={!connected || !owner || pending} onClick={() => armed ? perform("disarmSession", { workstationId: station.id }, "Workstation disarmed by its owner.") : setDialog("arm")}>{armed ? <ShieldOff size={17} /> : <ShieldCheck size={17} />}{armed ? "Disarm workstation" : "Arm workstation"}</Button><Button kind="warning" disabled={!connected || !owner || !armed || pending} onClick={() => perform("panicSession", { workstationId: station.id, reason: "Owner requested a security alert from the authenticated console." }, "Owner panic intent committed. Lock execution still requires consent and the Mac adapter.")}><ShieldAlert size={15} />Panic</Button><Button disabled={!connected || !owner} onClick={() => setDialog("access")}><Users size={15} />Enroll identity</Button>{!owner && <span className="al-small al-muted">Only the owner can arm or disarm.</span>}</> : <><Button kind="primary" disabled={!connected || pending} onClick={quickRegister}><Plus size={17} />Create my workstation</Button><button className="al-text-button" disabled={!connected} onClick={() => setDialog("register")}>Use a custom workstation ID</button></>}</div></section>
      {!connected && <div className="al-connection-banner"><Unplug size={18} /><div><strong>{live.status === "offline" ? "Database connection unavailable" : "Establishing a private subscription"}</strong><p>{live.error || `Connecting to ${config.uri} / ${config.database}.`}</p></div><Button kind="ghost" onClick={live.reconnect}><RefreshCw size={14} />Retry</Button><Button onClick={() => setDialog("connect")}>Connection settings</Button></div>}
      {connected && !station && <div className="al-onboarding"><KeyRound size={21} /><div><strong>{config.profile === "reviewer" ? "This reviewer identity needs enrollment" : "Connected with a distinct identity"}</strong><p>{config.profile === "reviewer" ? "Copy your public identity and have the workstation owner grant reviewer access. Your cases appear automatically after enrollment." : "Create a workstation to become its owner, or copy your public identity for an existing owner to enroll you."}</p></div><Button onClick={copyIdentity}>{copied ? <Check size={14} /> : <Copy size={14} />}{copied ? "Copied" : "Copy public identity"}</Button></div>}
      <div className="al-control-grid"><aside className="al-left-column"><Panel title="Workstation" icon={<Laptop size={16} />} aside={station && <Badge tone="quiet">{words(role)}</Badge>}>
        <div className="al-station-name">{stations.length > 1 ? <select aria-label="Select workstation" value={station?.id} onChange={e => { setStationId(e.target.value); setIncidentId(""); }}>{stations.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select> : <h3>{station?.name || "No workstation enrolled"}</h3>}<code>{station?.id || "Register or request access"}</code></div><StationDiagram armed={Boolean(armed)} incident={Boolean(currentIncident)} simulated={Boolean(session?.simulated)} /><dl className="al-station-facts"><div><dt>Security session</dt><dd>{!connected ? "Unverified" : armed ? "Armed" : "Disarmed"}</dd></div><div><dt>Source</dt><dd>{session?.simulated ? "Synthetic fixtures" : session ? "Enrolled publishers" : "No active session"}</dd></div><div><dt>Open incidents</dt><dd className={unresolved.length ? "al-text-magenta" : ""}>{unresolved.length}</dd></div></dl>
      </Panel><Panel id="airlock-sensors" title="Sensor health" icon={<Radio size={16} />} aside={<Badge tone={degraded ? "amber" : "cyan"}>{words(coverage)}</Badge>}><SensorRow icon={<Camera size={17} />} title="Desk camera" health={camera} now={now} enrolled={deviceFor("jetson")} connected={connected} /><SensorRow icon={<Cable size={17} />} title="USB watcher" health={usb} now={now} enrolled={deviceFor("mac")} connected={connected} /><SensorRow icon={<Laptop size={17} />} title="App watcher" health={appHealth} now={now} enrolled={deviceFor("mac")} connected={connected} /><SensorRow icon={<Activity size={17} />} title="Model adapter" health={model} now={now} enrolled={deviceFor("service")} connected={connected} /><p className="al-panel-note">Detection coverage needs camera, USB and app health. Model availability is separate. Reports become stale after 15 seconds.</p>{capacity && <p className="al-small al-text-amber" role="alert">{capacity.detail || "Security event capacity reached. Owner review and rearm required."}</p>}</Panel>
      <Panel title="Response policy" icon={<SlidersHorizontal size={16} />} aside={<button className="al-icon-button" disabled={!owner || !connected} title="Configure response policy" aria-label="Configure response policy" onClick={() => setDialog("policy")}><Settings2 size={14} /></button>}><div className="al-policy-rows"><div><span>Automatic screen lock</span><Badge tone={session?.autoLockEnabled ? "amber" : "quiet"}>{session ? session.autoLockEnabled ? "Enabled" : "Disabled" : station?.autoLockEnabled ? "Next session: enabled" : "Disabled"}</Badge></div><div><span>Protected tool actions</span><Badge tone="cyan">Approval required</Badge></div><div><span>Physical observation alone</span><strong>No automatic lock</strong></div></div>{session?.simulated && <p className="al-policy-synthetic"><FlaskConical size={13} />Real lock requests are suppressed.</p>}<details className="al-policy-details"><summary>Frozen device and app allowlists<ChevronDown size={13} /></summary><h4>USB baseline</h4><BaselineList value={session?.baselineJson} empty="No approved USB keys in this session" /><h4>Approved applications</h4><BaselineList value={session?.approvedAppsJson} empty="No approved app IDs in this session" /><p>Policy {shortId(session?.ownerPolicyHash || station?.ownerPolicyHash)}</p></details></Panel>
      <div className="al-identity-card"><Fingerprint size={18} /><div><strong>This console's identity</strong><button className="al-identity-copy" onClick={copyIdentity} title={live.identity}><code>{shortId(live.identity || "Not connected")}</code>{copied ? <Check size={12} /> : <Copy size={12} />}</button><span>{config.profile} profile · permissions from server</span></div></div></aside>
      <div className="al-center-column"><Timeline events={scoped.events} audit={scoped.audit} incidents={incidents} selectedIncidentId={selectedIncident?.id || ""} onSelect={setIncidentId} /><FixturePanel station={station} session={session} devices={scoped.devices} owner={owner} role={role} connected={connected} pending={pending} perform={perform} /><div className="al-privacy-line"><ShieldCheck size={14} /><span>Camera presence is an observation, never an identity or accusation.</span><CircleHelp size={13} /></div></div>
      <aside className="al-right-column"><ReviewPanel key={selectedIncident?.id || "no-incident"} incident={selectedIncident} locks={scoped.locks} owner={owner} reviewer={reviewer} connected={connected} pending={pending} perform={perform} /><Investigation visible={visibleGate} runs={scoped.runs} steps={scoped.steps} incidentId={selectedIncident?.id || ""} /><ProtectedActions key={station?.id || "no-workstation"} visible={visibleGate} actions={scoped.actions} approvals={scoped.approvals} identity={live.identity} ownerIdentity={textValue(station?.owner)} canApprove={canApprove} connected={connected} pending={pending} perform={perform} now={now} onRequest={station && (owner || role === "service") ? () => setDialog("action") : undefined} /><VoiceBridge /></aside></div>
      <footer className="al-page-footer"><span><AirlockMark size={15} />LOCKI AIRLOCK</span><span>SpacetimeDB owns the security state</span><span>{connected ? `Subscribed · ${clock(live.lastSyncedAt)}` : "Reconnecting automatically"}</span><a href="?product=legacy">Legacy focus console<ArrowUpRight size={12} /></a></footer>
    </main>{notice && <div className={`al-notice ${notice.error ? "is-error" : ""}`} role={notice.error ? "alert" : "status"}>{notice.error ? <ShieldAlert size={17} /> : <Check size={17} />}<span>{notice.text}</span><button aria-label="Dismiss notification" onClick={() => setNotice(null)}><X size={15} /></button></div>}
    {dialog === "connect" && <ConnectionForm config={config} identity={live.identity} onSave={saveConnection} onClose={() => setDialog(null)} />}{dialog === "register" && <RegisterForm perform={perform} pending={pending} onClose={() => setDialog(null)} />}{dialog === "arm" && station && <ArmForm station={station} previous={sessions[0]} snapshots={scoped.baselines} perform={perform} pending={pending} onClose={() => setDialog(null)} />}{dialog === "access" && station && <AccessForm station={station} identity={live.identity} perform={perform} pending={pending} onClose={() => setDialog(null)} />}{dialog === "policy" && station && <PolicyForm station={station} perform={perform} pending={pending} onClose={() => setDialog(null)} />}{dialog === "action" && station && <RequestActionForm station={station} service={role === "service"} incidentId={selectedIncident?.id || ""} perform={perform} pending={pending} onClose={() => setDialog(null)} />}
  </div>;
}

export default function AirlockApp() { return <ToastProvider><ControlRoom /></ToastProvider>; }
