import { useEffect, useState, type FormEvent } from "react";
import { Cable, Check, Copy, KeyRound, Laptop, LockKeyhole, ShieldCheck, Users, Wifi } from "lucide-react";
import type { ConnectionConfig } from "./client";
import { type Row, textValue, shortId, parseJson } from "./format";
import { Badge, Button, FormField, Modal } from "./ui";
import { armConsent, maySubmitLiveArm } from "./armConsent";

export type Perform = (reducer: string, args: Row, success?: string) => Promise<boolean>;
const asList = (s: string) => s.split(/[\n,]/).map(x => x.trim()).filter(Boolean);

export function ConnectionForm({ config, identity, onSave, onClose }: { config: ConnectionConfig; identity: string; onSave: (c: ConnectionConfig) => void; onClose: () => void }) {
  const [draft, setDraft] = useState(config);
  const [copied, setCopied] = useState(false);
  const submit = (e: FormEvent) => { e.preventDefault(); onSave(draft); onClose(); };
  return <Modal title="Connect this console" subtitle="Connect directly to your team's SpacetimeDB server." onClose={onClose}><form onSubmit={submit} className="al-form">
    <FormField label="Server address" hint="On a second Mac, use the first Mac's LAN address instead of 127.0.0.1."><input required type="url" value={draft.uri} onChange={e => setDraft({ ...draft, uri: e.target.value })} placeholder="http://127.0.0.1:3000" /></FormField>
    <FormField label="Database name"><input required value={draft.database} onChange={e => setDraft({ ...draft, database: e.target.value })} /></FormField>
    <FormField label="Local identity profile" hint="Each profile gets its own persisted identity. A profile name gives no permissions; an owner must enroll it."><input required pattern="[a-zA-Z0-9_-]{1,40}" value={draft.profile} onChange={e => setDraft({ ...draft, profile: e.target.value })} placeholder="operator" /></FormField>
    <div className="al-form-note"><KeyRound size={16} /><div><strong>Your public identity</strong><code className="al-identity-full">{identity || "Available after the server accepts this connection"}</code>{identity && <button type="button" className="al-text-button" onClick={async () => { await navigator.clipboard.writeText(identity); setCopied(true); }}>{copied ? <Check size={13} /> : <Copy size={13} />}{copied ? "Copied public identity" : "Copy for workstation owner"}</button>}<p>Your private token stays in this browser. Share only this public identity.</p></div></div>
    <div className="al-modal-actions"><Button type="button" onClick={onClose}>Cancel</Button><Button kind="primary" type="submit"><Wifi size={15} />Connect console</Button></div>
  </form></Modal>;
}

export function RegisterForm({ perform, onClose, pending }: { perform: Perform; onClose: () => void; pending: boolean }) {
  const [name, setName] = useState("Sid's workstation");
  const [id, setId] = useState("sid-desk");
  return <Modal title="Register a workstation" subtitle="This authenticated identity becomes the owner of a new workstation." onClose={onClose}><form className="al-form" onSubmit={async e => { e.preventDefault(); if (await perform("registerWorkstation", { workstationId: id, name }, "Workstation registered. Your identity is its owner.")) onClose(); }}>
    <FormField label="Workstation name"><input required maxLength={80} value={name} onChange={e => setName(e.target.value)} /></FormField>
    <FormField label="Unique workstation ID" hint="Other enrolled clients use this ID. An existing workstation cannot be claimed."><input required pattern="[a-zA-Z0-9_-]{1,80}" value={id} onChange={e => setId(e.target.value)} /></FormField>
    <div className="al-form-note"><ShieldCheck size={17} /><p>Registration creates private, owner-scoped state in SpacetimeDB. Devices and reviewers receive access only after enrollment.</p></div>
    <div className="al-modal-actions"><Button type="button" onClick={onClose}>Cancel</Button><Button kind="primary" disabled={pending} type="submit">Register workstation</Button></div>
  </form></Modal>;
}

export function ArmForm({ station, previous, snapshots, perform, onClose, pending }: { station: Row; previous?: Row; snapshots: Row[]; perform: Perform; onClose: () => void; pending: boolean }) {
  const [simulated, setSimulated] = useState(true);
  const [baseline, setBaseline] = useState((parseJson(previous?.baselineJson, []) as string[]).join("\n"));
  const [apps, setApps] = useState((parseJson(previous?.approvedAppsJson, []) as string[]).join("\n"));
  const [consentedFingerprint, setConsentedFingerprint] = useState<string | null>(null);
  const [duration, setDuration] = useState("60");
  const latest = [...snapshots].filter(s => !s.sessionId).sort((a, b) => Number(b.observedAtMs) - Number(a.observedAtMs))[0];
  const captureAge = latest ? Date.now() - Number(latest.observedAtMs) : Infinity;
  const fresh = Boolean(latest) && captureAge >= -5000 && captureAge <= 30000;
  const reviewed = armConsent(station, latest, asList(apps), duration);
  const fingerprint = reviewed?.fingerprint ?? null;
  const verified = fingerprint !== null && consentedFingerprint === fingerprint;
  // The derived match also gates submit immediately, before this effect runs.
  useEffect(() => { setConsentedFingerprint(null); }, [fingerprint, simulated]);
  const capturedKeys = reviewed ? JSON.parse(reviewed.baselineJson) as string[] : [];
  return <Modal title="Arm the workstation" subtitle="Freeze a device baseline and response policy for this session." onClose={onClose}><form className="al-form" onSubmit={async e => {
    e.preventDefault();
    if (!simulated && !maySubmitLiveArm(consentedFingerprint, reviewed, latest?.observedAtMs, Date.now())) return;
    const ok = await perform("armSession", { workstationId: station.id, sessionId: `session-${crypto.randomUUID()}`, baselineJson: simulated ? JSON.stringify(asList(baseline)) : reviewed!.baselineJson, approvedAppsJson: JSON.stringify(asList(apps)), durationMs: BigInt(Number(duration) * 60000), simulated }, simulated ? "Synthetic session armed. Real screen locks are disabled." : "Live session armed with the enrolled Mac's fresh baseline.");
    if (ok) onClose();
  }}>
    <div className="al-mode-choice"><button type="button" aria-pressed={simulated} onClick={() => setSimulated(true)} className={simulated ? "selected" : ""}><Badge tone="amber">Synthetic</Badge><strong>Fixture rehearsal</strong><span>DB-backed events. Never requests a real lock.</span></button><button type="button" aria-pressed={!simulated} onClick={() => setSimulated(false)} className={!simulated ? "selected" : ""}><Badge tone="cyan">Live</Badge><strong>Owned workstation</strong><span>Uses enrolled Mac and Jetson publishers.</span></button></div>
    <FormField label={simulated ? "Approved fixture USB keys" : "Captured USB baseline"} hint={simulated ? "One fixture identifier per line." : fresh ? `Captured by ${latest.deviceId} ${Math.max(0, Math.round(captureAge / 1000))}s ago. The server freezes this inventory when arming.` : "Connect the enrolled Mac publisher. Live arming requires a successful capture from the last 30 seconds."}><textarea rows={3} readOnly={!simulated} value={simulated ? baseline : capturedKeys.join("\n")} onChange={e => setBaseline(e.target.value)} placeholder={simulated ? "synthetic-approved-usb" : fresh ? "No USB peripherals currently enumerated" : "Waiting for a fresh Mac capture"} /></FormField>
    {!simulated && <div className="al-form-note"><Laptop size={17} /><div><strong>{fresh ? "Fresh authenticated capture" : "Mac capture unavailable or stale"}</strong><p>Active app: {latest?.activeAppId || "unknown"}. Camera at capture: {latest?.cameraStatus || "unavailable"}. Camera loss does not prevent USB protection.</p></div></div>}
    <FormField label="Approved app identifiers" hint="One app identifier per line. Only metadata is monitored."><textarea rows={2} value={apps} onChange={e => setApps(e.target.value)} placeholder="com.apple.Terminal" /></FormField>
    <div className="al-form-inline"><FormField label="Session length"><select value={duration} onChange={e => setDuration(e.target.value)}><option value="15">15 minutes</option><option value="60">1 hour</option><option value="240">4 hours</option><option value="480">8 hours</option></select></FormField><div className="al-policy-readout"><LockKeyhole size={15} /><span>Auto-lock policy</span><strong>{station.autoLockEnabled ? "Enabled" : "Disabled"}</strong><small>{simulated ? "Suppressed for synthetic sessions" : `Policy version ${station.policyVersion}`}</small></div></div>
    {!simulated && <label className="al-checkbox"><input type="checkbox" required checked={verified} disabled={!reviewed || !fresh} onChange={e => setConsentedFingerprint(e.target.checked ? fingerprint : null)} /><span>I verified this Mac baseline and consent to the displayed policy on my workstation.</span></label>}
    <div className="al-modal-actions"><Button type="button" onClick={onClose}>Cancel</Button><Button kind="primary" disabled={pending || (!simulated && (!verified || !fresh))} type="submit"><ShieldCheck size={15} />Arm {simulated ? "synthetic" : "live"} session</Button></div>
  </form></Modal>;
}

export function AccessForm({ station, identity, perform, onClose, pending }: { station: Row; identity: string; perform: Perform; onClose: () => void; pending: boolean }) {
  const [kind, setKind] = useState("reviewer");
  const [target, setTarget] = useState("");
  const [deviceId, setDeviceId] = useState("");
  const [label, setLabel] = useState("");
  const [canApprove, setCanApprove] = useState(false);
  return <Modal title="Manage trusted identities" subtitle={`Access to ${station.name} is enforced by the server.`} onClose={onClose}><form className="al-form" onSubmit={async e => {
    e.preventDefault();
    const args = kind === "reviewer" ? { workstationId: station.id, identityHex: target, canApprove } : kind === "revoke" ? { workstationId: station.id, identityHex: target } : { workstationId: station.id, identityHex: target, deviceId, role: kind, label };
    if (await perform(kind === "reviewer" ? "grantReviewer" : kind === "revoke" ? "revokeMember" : "enrollDevice", args, kind === "revoke" ? "Identity access revoked." : `${kind === "reviewer" ? "Reviewer" : "Device"} enrolled.`)) onClose();
  }}>
    <div className="al-form-note"><Users size={18} /><div><strong>Use a second identity</strong><p>Open <a href="?client=reviewer" target="_blank" rel="noreferrer">a reviewer console</a> or a separate browser on another Mac. Copy its public identity from Connection, then enroll it here. Each profile persists independently.</p></div></div>
    <FormField label="Access action"><select value={kind} onChange={e => setKind(e.target.value)}><option value="reviewer">Grant human reviewer access</option><option value="mac">Enroll Mac sensor / lock adapter</option><option value="jetson">Enroll Jetson camera publisher</option><option value="service">Enroll investigation / protected tool gateway</option><option value="revoke">Revoke an identity's access</option></select></FormField>
    <FormField label="Public identity" hint={`Your owner identity is ${shortId(identity)}. Enroll the other client's public identity, never its private token.`}><input required pattern="[a-fA-F0-9]{64}" value={target} onChange={e => setTarget(e.target.value.trim().replace(/^0x/, ""))} placeholder="64 hexadecimal characters" /></FormField>
    {kind === "reviewer" ? <label className="al-checkbox"><input type="checkbox" checked={canApprove} onChange={e => setCanApprove(e.target.checked)} /><span>Allow this reviewer to approve protected actions. Owners alone can resolve incidents and change policy.</span></label> : kind === "revoke" ? <p className="al-small al-text-amber">Revocation removes this member's workstation access and invalidates their outstanding action authorization. The owner cannot be revoked.</p> : <div className="al-form-inline"><FormField label="Device ID"><input required value={deviceId} onChange={e => setDeviceId(e.target.value)} placeholder={`${station.id}-${kind}`} /></FormField><FormField label="Display label"><input required value={label} onChange={e => setLabel(e.target.value)} placeholder={kind === "mac" ? "Sid's Mac" : "Camera node"} /></FormField></div>}
    <div className="al-modal-actions"><Button type="button" onClick={onClose}>Cancel</Button><Button kind={kind === "revoke" ? "warning" : "primary"} disabled={pending} type="submit"><Cable size={15} />{kind === "revoke" ? "Revoke access" : `Enroll ${kind === "reviewer" ? "reviewer" : "device"}`}</Button></div>
  </form></Modal>;
}

export function PolicyForm({ station, perform, onClose, pending }: { station: Row; perform: Perform; onClose: () => void; pending: boolean }) {
  const [autoLock, setAutoLock] = useState(Boolean(station.autoLockEnabled));
  const [consent, setConsent] = useState(false);
  return <Modal title="Response policy" subtitle="Policy changes are frozen when the next session is armed." onClose={onClose}><form className="al-form" onSubmit={async e => { e.preventDefault(); if (await perform("setPolicy", { workstationId: station.id, autoLockEnabled: autoLock, lockCooldownMs: 30000n }, "Response policy saved for the next session.")) onClose(); }}>
    <div className="al-policy-option"><div><strong>Request a Mac screen lock</strong><p>For an unapproved USB while armed, request the enrolled Mac adapter to lock the screen. Physical proximity and app observations alone do not request a lock.</p></div><input aria-label="Enable auto-lock" type="checkbox" checked={autoLock} onChange={e => setAutoLock(e.target.checked)} /></div>
    <div className="al-form-note"><LockKeyhole size={17} /><p>SpacetimeDB records a request. Only the Mac adapter can report execution and confirmation. Synthetic sessions never generate a real lock request.</p></div>
    {autoLock && <label className="al-checkbox"><input type="checkbox" required checked={consent} onChange={e => setConsent(e.target.checked)} /><span>I own this workstation and explicitly consent to automatic screen-lock requests.</span></label>}
    <div className="al-modal-actions"><Button type="button" onClick={onClose}>Cancel</Button><Button kind="primary" disabled={pending || (autoLock && !consent)} type="submit">Save policy</Button></div>
  </form></Modal>;
}

export function RequestActionForm({ station, incidentId, perform, onClose, pending, service = false }: { station: Row; incidentId: string; perform: Perform; onClose: () => void; pending: boolean; service?: boolean }) {
  const [tool, setTool] = useState("fixture_word_count");
  const [args, setArgs] = useState('{"text":"AIRLOCK validates exact actions"}');
  const [scope, setScope] = useState("airlock:read-only");
  return <Modal title="Request a protected action" subtitle={service ? "Creates a pending request from this service identity. An authorized human must approve it." : "Owner requests are review-only. An enrolled gateway must propose its own action before executing a callback."} onClose={onClose}><form className="al-form" onSubmit={async e => { e.preventDefault(); if (await perform("proposeProtectedAction", { workstationId: station.id, actionId: `action-${crypto.randomUUID()}`, missionId: "airlock-demo", incidentId, toolName: tool, argsJson: args, scope, ttlMs: 120000n }, service ? "Protected action requested. An authorized human must approve it." : "Review-only request created. A gateway cannot execute an owner-proposed action.")) onClose(); }}>
    <FormField label="Tool name"><input required value={tool} onChange={e => setTool(e.target.value)} /></FormField><FormField label="Permission scope"><input required value={scope} onChange={e => setScope(e.target.value)} /></FormField><FormField label="Exact arguments (JSON)"><textarea required rows={4} value={args} onChange={e => setArgs(e.target.value)} /></FormField>
    <div className="al-form-note"><KeyRound size={17} /><p>The server binds the canonical arguments, tool, scope and mission into an immutable digest. Approval is short-lived and can be consumed once by an enrolled gateway.</p></div><div className="al-modal-actions"><Button type="button" onClick={onClose}>Cancel</Button><Button kind="primary" disabled={pending} type="submit">Request approval</Button></div>
  </form></Modal>;
}

export function BaselineList({ value, empty }: { value: unknown; empty: string }) {
  const entries = parseJson(value, []) as unknown[];
  return entries.length ? <div className="al-allowlist">{entries.map((entry, i) => <code key={i}>{textValue(entry)}</code>)}</div> : <span className="al-muted">{empty}</span>;
}
