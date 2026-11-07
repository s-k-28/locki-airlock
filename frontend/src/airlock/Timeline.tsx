import { useState } from "react";
import { Activity, Cable, Camera, Check, ChevronDown, FileClock, Fingerprint, Laptop, ShieldAlert, UserRound } from "lucide-react";
import { type Row, clock, isSynthetic, parseJson, shortId, textValue, words } from "./format";
import { Badge, Empty, JsonDetails, Panel } from "./ui";

const EVENT_LABELS: Record<string, string> = {
  usb_added: "USB device connected", usb_removed: "USB device removed", app_launched: "Application launched",
  person_in_zone: "Person entered the desk zone", person_cleared: "Desk zone cleared", camera_offline: "Camera became unavailable",
  camera_online: "Camera returned online", heartbeat: "Publisher heartbeat",
};
function EventIcon({ kind }: { kind: string }) {
  const Icon = kind.startsWith("usb") ? Cable : kind.startsWith("person") ? UserRound : kind.startsWith("camera") ? Camera : kind === "app_launched" ? Laptop : Activity;
  return <Icon size={16} />;
}

export function IncidentSummary({ incident, events }: { incident: Row; events: Row[] }) {
  const physical = events.find(e => e.id === incident.physicalEventId);
  const digital = events.find(e => e.id === incident.digitalEventId);
  const delta = physical && digital ? Math.abs(Number(physical.observedAtMs) - Number(digital.observedAtMs)) / 1000 : null;
  return <div className={`al-incident-summary ${incident.state === "resolved" ? "is-resolved" : ""}`}>
    <div className="al-incident-title"><span className="al-incident-icon">{incident.state === "resolved" ? <Check size={20} /> : <ShieldAlert size={20} />}</span><div><div className="al-inline"><Badge tone={incident.state === "resolved" ? "quiet" : "magenta"}>{words(incident.severity)} priority</Badge>{isSynthetic(incident) && <Badge tone="amber">Synthetic</Badge>}</div><h3>{textValue(incident.summary, "Unapproved device activity needs review")}</h3></div></div>
    {physical && digital ? <div className="al-correlation"><span><UserRound size={14} />Physical observation</span><b>{delta?.toFixed(1)}s apart</b><span><Cable size={14} />Digital observation</span></div> : <p className="al-incident-context">{incident.ruleId === "owner_panic" ? "The authenticated owner requested this alert. No hostile activity is inferred." : physical ? "Physical evidence is available." : "No fresh physical evidence is linked. The digital observation independently triggered this incident."}</p>}
    <p className="al-uncertainty">An unapproved device or app is an observation, not proof of malicious activity.</p>
    <div className="al-incident-meta"><code>{shortId(incident.id)}</code><span>{words(incident.ruleId)}</span><Badge tone="quiet">{words(incident.state)}</Badge></div>
  </div>;
}

export function Timeline({ events, audit, incidents, selectedIncidentId, onSelect }: { events: Row[]; audit: Row[]; incidents: Row[]; selectedIncidentId: string; onSelect: (id: string) => void }) {
  const [tab, setTab] = useState("observations");
  const [source, setSource] = useState("all");
  const [expanded, setExpanded] = useState(false);
  const selected = incidents.find(i => i.id === selectedIncidentId);
  const linkedIds = selected ? parseJson(selected.eventIdsJson, []) as string[] : [];
  const sortedEvents = [...events].sort((a, b) => Number(b.receivedAtMs) - Number(a.receivedAtMs));
  const filtered = sortedEvents.filter(e => source === "all" || (source === "synthetic" ? isSynthetic(e) : !isSynthetic(e)));
  const visible = expanded ? filtered : filtered.slice(0, 16);
  return <Panel title="Security timeline" icon={<Activity size={16} />} aside={<span className="al-small al-muted">Committed by SpacetimeDB</span>} className="al-timeline-panel">
    <div className="al-tabs"><button onClick={() => setTab("observations")} aria-selected={tab === "observations"}>Observations <span>{events.length}</span></button><button onClick={() => setTab("audit")} aria-selected={tab === "audit"}>Audit trail <span>{audit.length}</span></button><div className="al-tabs-spacer" /><span className="al-small al-muted">Newest first</span></div>
    {incidents.length > 0 && <div className="al-case-picker"><label htmlFor="al-case">Review incident</label><select id="al-case" value={selectedIncidentId} onChange={e => onSelect(e.target.value)}>{incidents.map(i => <option key={i.id} value={i.id}>{words(i.severity)} · {words(i.state)} · {shortId(i.id)}</option>)}</select></div>}
    {selected && <IncidentSummary incident={selected} events={events} />}
    {tab === "observations" ? <>
      <div className="al-feed-toolbar"><div className="al-segments" aria-label="Observation provenance">{["all", "live", "synthetic"].map(s => <button key={s} aria-pressed={source === s} onClick={() => setSource(s)}>{s === "all" ? "All sources" : s === "live" ? "Live" : "Synthetic"}</button>)}</div><span className="al-small al-muted">{filtered.length} events</span></div>
      {visible.length ? <ol className="al-event-list">{visible.map(event => {
        const meta = parseJson(event.metadataJson, {});
        const linked = linkedIds.includes(event.id);
        const unapproved = /unapproved|unknown|suspicious/.test(event.classification);
        return <li key={event.id} className={`al-event ${linked ? "is-linked" : ""}`}><div className="al-event-marker"><EventIcon kind={event.kind} /></div><div className="al-event-body"><div className="al-event-top"><span className="al-event-source">{isSynthetic(event) ? "Fixture publisher" : words(event.source || event.deviceId)}</span><time>{clock(event.receivedAtMs)}</time></div><div className="al-event-label"><h3>{EVENT_LABELS[event.kind] || words(event.kind)}</h3>{isSynthetic(event) && <Badge tone="amber">Synthetic</Badge>}</div><p>{textValue(meta.label || meta.deviceKey || meta.appId || meta.detail, event.kind === "person_in_zone" ? "Zone presence reported; identity is not inferred." : event.kind === "heartbeat" ? "Publisher reported it is available." : "Observation received from the enrolled publisher.")}</p><div className="al-event-meta"><code title={event.id}>{shortId(event.id)}</code><span className={unapproved ? "al-text-magenta" : ""}>{words(event.classification)}</span>{linked && <span className="al-evidence-tag"><Fingerprint size={11} />Linked evidence</span>}</div><JsonDetails label="Observation details" value={{ event_id: event.id, publisher: event.deviceId, observed_at: clock(event.observedAtMs), received_at: clock(event.receivedAtMs), confidence: event.confidence, metadata: meta, provenance: isSynthetic(event) ? "synthetic fixture" : "enrolled device" }} /></div></li>;
      })}</ol> : <Empty icon={<FileClock size={25} />} title={source === "all" ? "No observations yet" : `No ${source} observations`} >{source === "all" ? "Arm a session, then connect an enrolled publisher or send a clearly labeled synthetic fixture below." : "Change the source filter to see other observations."}</Empty>}
      {filtered.length > 16 && <button className="al-load-more" onClick={() => setExpanded(!expanded)}>{expanded ? "Show recent events" : `Show ${filtered.length - 16} more events`}<ChevronDown size={14} /></button>}
    </> : audit.length ? <ol className="al-audit-list">{[...audit].sort((a, b) => Number(b.id) - Number(a.id)).slice(0, 40).map(entry => <li key={String(entry.id)}><span className="al-audit-number">#{String(entry.id)}</span><div><strong>{words(entry.kind)}</strong><p>{entry.detail || shortId(entry.entityId)}</p><code title={textValue(entry.actor)}>Actor {shortId(entry.actor)}</code></div><time>{clock(entry.createdAtMs)}</time></li>)}</ol> : <Empty title="No audit records visible">Authenticated changes to this workstation appear here in server order.</Empty>}
    <footer className="al-timeline-footer"><span><i className="al-small-dot" />Server receipt order</span><span>Raw camera frames stay off this database</span></footer>
  </Panel>;
}
