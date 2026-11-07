import { useEffect, useMemo, useState } from "react";
import { X, FlaskConical, RefreshCw } from "lucide-react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { LockedInEvent, Snapshot } from "../types/api";
import { api } from "../services/api";
import { pct, timeOfDay } from "../lib/format";
import { useToasts } from "./Toasts";
import { LinkTestPanel } from "./LinkTestPanel";
import { VoiceTrialPanel } from "./VoiceTrialPanel";

type Tab = "events" | "frames" | "perf" | "experiments" | "linktest";

export function DebugDrawer({ open, onClose, snap, events }: { open: boolean; onClose: () => void; snap: Snapshot; events: LockedInEvent[] }) {
  const [tab, setTab] = useState<Tab>("events");
  const [q, setQ] = useState("");
  const [proc, setProc] = useState<any>(null);
  const { run, notify } = useToasts();

  useEffect(() => {
    if (open && tab === "perf") api.perf().then(setProc).catch(() => setProc(null));
  }, [open, tab]);

  const shown = useMemo(() => {
    const out: LockedInEvent[] = [];
    for (let i = events.length - 1; i >= 0 && out.length < 120; i--) {
      const e = events[i];
      if (!q || e.event_type.includes(q) || JSON.stringify(e.payload).includes(q)) out.push(e);
    }
    return out;
  }, [events, q]);

  const perfRows = Object.entries(snap.perf).sort((a, b) => a[0].localeCompare(b[0]));
  const chart = perfRows.filter(([k]) => k !== "backend_startup_ms").map(([k, v]) => ({ name: k.replace(/_ms$/, "").replace(/_/g, " "), p50: v.p50, p95: v.p95 }));
  const frames: any[] = snap.robot.status?.firmware?.recent_frames ?? [];
  const exp = snap.experiment.summary;

  return (
    <div
      className={`fixed right-0 bottom-0 left-0 z-50 flex h-[min(52vh,560px)] flex-col border-t border-line bg-panel transition-transform duration-[420ms] ease-[cubic-bezier(0.16,1,0.3,1)] ${open ? "translate-y-0" : "translate-y-full"}`}
      aria-hidden={!open}
    >
      <div className="flex h-11 shrink-0 items-center gap-1 border-b border-line-soft px-4">
        {(["events", "frames", "perf", "experiments", "linktest"] as Tab[]).map((t) => (
          <button key={t} onClick={() => setTab(t)} className={`h-11 border-b-2 px-3 text-[12px] font-semibold tracking-[0.12em] uppercase [font-stretch:85%] ${tab === t ? "border-live text-ink" : "border-transparent text-ink-3 hover:text-ink-2"}`}>
            {t === "frames" ? "Robot wire" : t === "linktest" ? "R5 link test" : t}
          </button>
        ))}
        <button className="btn btn-ghost btn-sm ml-auto !px-2" onClick={onClose} aria-label="Close debug">
          <X size={16} />
        </button>
      </div>
      <div className="scroll-y min-h-0 flex-1 px-4 py-3">
        {tab === "events" ? (
          <>
            <input className="field num mb-3 !h-8 !text-[12px]" placeholder="filter: event type or payload text" value={q} onChange={(e) => setQ(e.target.value)} />
            <ul className="space-y-1">
              {shown.map((e) => (
                <li key={e.event_id} className="num text-[11px] leading-snug">
                  <span className="text-ink-4">{timeOfDay(e.timestamp)} #{e.seq}</span> <span className={e.simulated ? "text-sim" : "text-live"}>{e.event_type}</span> <span className="text-ink-3">{e.source}</span>{" "}
                  {e.confidence != null ? <span className="text-ink-2">c={e.confidence}</span> : null} <span className="break-all text-ink-2">{JSON.stringify(e.payload)}</span>
                </li>
              ))}
            </ul>
          </>
        ) : null}

        {tab === "frames" ? (
          snap.robot.status?.simulated ? (
            <p className="text-[13px] text-ink-3">The simulated robot has no wire protocol. Connect the Zeus Car to see the exact JSON frames sent over the WebSocket and the replies relayed from the Arduino.</p>
          ) : frames.length ? (
            <table className="num w-full text-[11.5px]">
              <thead>
                <tr className="text-left text-ink-4">
                  <th className="py-1 pr-4 font-normal">time</th>
                  <th className="pr-4 font-normal">dir</th>
                  <th className="pr-4 font-normal">kind</th>
                  <th className="font-normal">frame</th>
                </tr>
              </thead>
              <tbody>
                {frames
                  .slice()
                  .reverse()
                  .map((f, i) => (
                    <tr key={i} className="border-t border-line-soft/60">
                      <td className="py-1 pr-4 text-ink-4">{timeOfDay(f.t)}</td>
                      <td className={`pr-4 ${f.dir === "tx" ? "text-live" : "text-ink-2"}`}>{f.dir}</td>
                      <td className="pr-4 text-ink-3">{f.kind}</td>
                      <td className="break-all text-ink">{f.frame}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          ) : (
            <p className="text-[13px] text-ink-3">No frames yet.</p>
          )
        ) : null}

        {tab === "perf" ? (
          <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
            <div>
              <div className="mb-2 flex items-center justify-between">
                <span className="label">Latency samples (ms)</span>
                <button className="btn btn-ghost btn-sm" onClick={() => api.perf().then(setProc)}>
                  <RefreshCw size={12} /> Refresh
                </button>
              </div>
              <table className="num w-full text-[11.5px]">
                <thead>
                  <tr className="text-left text-ink-4">
                    <th className="py-1 font-normal">metric</th>
                    <th className="font-normal">n</th>
                    <th className="font-normal">p50</th>
                    <th className="font-normal">p95</th>
                    <th className="font-normal">max</th>
                  </tr>
                </thead>
                <tbody>
                  {perfRows.map(([k, v]) => (
                    <tr key={k} className="border-t border-line-soft/60">
                      <td className="py-1 pr-3 text-ink-2">{k}</td>
                      <td className="text-ink-3">{v.n}</td>
                      <td className="text-ink">{v.p50}</td>
                      <td className="text-ink">{v.p95}</td>
                      <td className="text-ink-3">{v.max}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {proc ? (
                <div className="num mt-3 text-[11.5px] text-ink-3">
                  process: cpu {proc.process.cpu_percent}% · rss {proc.process.rss_mb} MB · {proc.process.threads} threads · loop max lag {proc.watchdog.loop_max_lag_ms} ms
                </div>
              ) : null}
            </div>
            <div className="h-[300px]">
              {chart.length ? (
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={chart} layout="vertical" margin={{ left: 40, right: 16 }}>
                    <CartesianGrid stroke="var(--color-line-soft)" horizontal={false} />
                    <XAxis type="number" stroke="var(--color-ink-4)" fontSize={10} />
                    <YAxis type="category" dataKey="name" stroke="var(--color-ink-4)" fontSize={10} width={130} />
                    <Tooltip contentStyle={{ background: "var(--color-raised)", border: "1px solid var(--color-line)", fontSize: 12 }} cursor={{ fill: "var(--color-live-wash)" }} />
                    <Bar dataKey="p50" fill="var(--color-live-2)" barSize={7} />
                    <Bar dataKey="p95" fill="var(--color-hit-2)" barSize={7} />
                  </BarChart>
                </ResponsiveContainer>
              ) : (
                <p className="text-[13px] text-ink-3">No samples yet.</p>
              )}
            </div>
          </div>
        ) : null}

        {tab === "linktest" ? <LinkTestPanel snap={snap} events={events} /> : null}

        {tab === "experiments" ? (
          <div className="grid gap-6 lg:grid-cols-2">
            <div className="flex flex-col gap-3">
              <p className="max-w-[60ch] text-[13px] leading-relaxed text-ink-3">
                Reach trials for docs/PERFORMANCE.md. Start a trial, perform the action (a deliberate reach, or a non-reach movement near the phone), then end it. A confirmation during a reach trial is a true positive; during a non-reach trial, a false positive.
              </p>
              <div className="flex flex-wrap gap-2">
                <button className="btn btn-hit" disabled={snap.experiment.active} onClick={() => run(api.trialStart("reach"))}>
                  <FlaskConical size={14} /> Reach trial
                </button>
                <button className="btn" disabled={snap.experiment.active} onClick={() => run(api.trialStart("non_reach"))}>
                  <FlaskConical size={14} /> Non-reach trial
                </button>
                <button
                  className="btn btn-primary"
                  disabled={!snap.experiment.active}
                  onClick={async () => {
                    const r = await run(api.trialEnd());
                    if (r?.trial) notify(`Trial: ${r.trial.outcome}`, "ok");
                  }}
                >
                  End trial
                </button>
              </div>
              {snap.experiment.active ? <span className="chip chip-sim blink w-fit">Trial running</span> : null}
            </div>
            <dl className="num grid grid-cols-4 gap-4 text-[12px]">
              {[
                ["trials", exp.trials],
                ["TP", exp.tp],
                ["FP", exp.fp],
                ["FN", exp.fn],
                ["TN", exp.tn],
                ["precision", pct(exp.precision)],
                ["recall", pct(exp.recall)],
                ["FPR", pct(exp.false_positive_rate)],
              ].map(([k, v]) => (
                <div key={String(k)}>
                  <dt className="label">{k}</dt>
                  <dd className="mt-1 text-[18px] text-ink">{v ?? "—"}</dd>
                </div>
              ))}
            </dl>
            <div className="border-t border-line-soft pt-4 lg:col-span-2">
              <VoiceTrialPanel snap={snap} />
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
