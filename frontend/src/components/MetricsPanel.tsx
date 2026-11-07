import { useMemo } from "react";
import type { FocusState, LockedInEvent, Snapshot } from "../types/api";
import { minutes, pct } from "../lib/format";

const FOCUS_COLOR: Record<FocusState, string> = {
  FOCUSED: "var(--color-live-2)",
  POSSIBLY_DISTRACTED: "var(--color-sim)",
  AWAY: "var(--color-ink-4)",
  UNKNOWN: "transparent",
};

function FocusRibbon({ snap, events }: { snap: Snapshot; events: LockedInEvent[] }) {
  const s = snap.session;
  const data = useMemo(() => {
    if (!s) return null;
    const start = s.started_at_wall;
    const end = snap.server_time;
    const span = Math.max(1, end - start);
    const mine = events.filter((e) => e.session_id === s.session_id);
    const segs: { from: number; to: number; state: FocusState }[] = [];
    let cur: FocusState = "UNKNOWN";
    let since = start;
    const pauses: { from: number; to: number }[] = [];
    let pausedAt: number | null = null;
    const reaches: { at: number; sim: boolean }[] = [];
    for (const e of mine) {
      if (e.event_type === "vision.focus_changed") {
        segs.push({ from: since, to: e.timestamp, state: cur });
        cur = e.payload.to;
        since = e.timestamp;
      } else if (e.event_type === "session.paused") pausedAt = e.timestamp;
      else if (e.event_type === "session.resumed" && pausedAt != null) {
        pauses.push({ from: pausedAt, to: e.timestamp });
        pausedAt = null;
      } else if (e.event_type === "vision.phone_reach_confirmed") reaches.push({ at: e.timestamp, sim: e.simulated });
    }
    segs.push({ from: since, to: end, state: cur });
    if (pausedAt != null) pauses.push({ from: pausedAt, to: end });
    const x = (t: number) => ((t - start) / span) * 100;
    return { segs, pauses, reaches, x };
  }, [s, events, snap.server_time]);

  if (!data) return null;
  return (
    <div>
      <div className="relative h-7 overflow-hidden rounded-[3px] border border-line-soft bg-[repeating-linear-gradient(135deg,transparent_0_5px,oklch(0.31_0.014_230/0.35)_5px_6px)]">
        {data.segs.map((g, i) => (
          <div key={i} className="absolute top-0 bottom-0" style={{ left: `${data.x(g.from)}%`, width: `${Math.max(0, data.x(g.to) - data.x(g.from))}%`, background: FOCUS_COLOR[g.state], opacity: g.state === "AWAY" ? 0.55 : 0.75 }} />
        ))}
        {data.pauses.map((p, i) => (
          <div key={`p${i}`} className="absolute top-0 bottom-0 bg-void/70" style={{ left: `${data.x(p.from)}%`, width: `${Math.max(0.4, data.x(p.to) - data.x(p.from))}%` }} title="paused" />
        ))}
        {data.reaches.map((r, i) => (
          <div key={`r${i}`} className={`absolute top-0 bottom-0 w-[3px] -translate-x-1/2 ${r.sim ? "bg-hit/60" : "bg-hit"}`} style={{ left: `${data.x(r.at)}%` }} title="phone reach" />
        ))}
      </div>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-ink-3">
        <Legend color="var(--color-live-2)" label="Focused" />
        <Legend color="var(--color-sim)" label="Possibly distracted" />
        <Legend color="var(--color-ink-4)" label="Away" />
        <Legend hatch label="Unknown (camera off)" />
        <Legend color="var(--color-hit)" label="Phone reach" thin />
      </div>
    </div>
  );
}

function Legend({ color, label, hatch, thin }: { color?: string; label: string; hatch?: boolean; thin?: boolean }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className={`inline-block h-2.5 ${thin ? "w-[3px]" : "w-3"} rounded-[1px] border border-line-soft`}
        style={hatch ? { background: "repeating-linear-gradient(135deg, transparent 0 2px, var(--color-line) 2px 3px)" } : { background: color }}
      />
      {label}
    </span>
  );
}

export function MetricsPanel({ snap, events }: { snap: Snapshot; events: LockedInEvent[] }) {
  const m = snap.metrics;
  const has = !!snap.session && m.elapsed_active_s != null;
  const observed = (m.observed_fraction ?? 0) >= 0.5;
  const cells: { label: string; value: string; note?: string; tone?: string }[] = has
    ? [
        { label: "Active time", value: minutes(m.elapsed_active_s) },
        { label: "Presence", value: m.presence_ratio != null ? pct(m.presence_ratio) : "—", note: m.presence_ratio != null ? (observed ? `observed ${pct(m.observed_fraction)}` : `low coverage ${pct(m.observed_fraction)}`) : "unavailable · camera off", tone: m.presence_ratio == null ? "text-ink-4" : undefined },
        { label: "Focused", value: minutes(m.focused_s) },
        { label: "Away", value: minutes(m.away_s) },
        { label: "Phone reaches", value: String(m.reach_confirmed ?? 0), tone: (m.reach_confirmed ?? 0) > 0 ? "text-hit" : undefined, note: m.reach_suppressed ? `${m.reach_suppressed} in cooldown` : undefined },
        { label: "Interventions", value: String(m.interventions ?? 0) },
        { label: "Robot moves", value: String(m.robot_moves ?? 0), note: m.motions_blocked ? `${m.motions_blocked} blocked` : undefined },
        { label: "Completed", value: `${m.completed_units ?? 0}${m.target_units ? ` / ${m.target_units}` : ""}` },
      ]
    : [];

  return (
    <div className="panel px-5 pt-4 pb-5">
      <div className="flex items-center justify-between">
        <h2 className="font-display text-[13px] font-semibold tracking-[0.18em] text-ink-2 uppercase">Session metrics</h2>
        <span className="label !text-[10px]">measured or user-reported only</span>
      </div>
      {!has ? (
        <p className="mt-3 text-[13px] text-ink-3">Metrics appear during a session. Presence and away time come from the camera; while it is off they are counted as unknown, never guessed.</p>
      ) : (
        <>
          <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-4">
            {cells.map((c) => (
              <div key={c.label} className="min-w-0">
                <dt className="label">{c.label}</dt>
                <dd className={`num mt-1.5 text-[19px] leading-none ${c.tone ?? "text-ink"}`}>{c.value}</dd>
                {c.note ? <dd className="mt-1 truncate text-[11px] text-ink-4">{c.note}</dd> : null}
              </div>
            ))}
          </dl>
          <div className="mt-5">
            <FocusRibbon snap={snap} events={events} />
          </div>
        </>
      )}
    </div>
  );
}
