import { useMemo } from "react";
import type { LockedInEvent, Snapshot } from "../types/api";
import { timeOfDay } from "../lib/format";

export function DecisionCard({ snap, events }: { snap: Snapshot; events: LockedInEvent[] }) {
  const d = snap.coach.last_decision;
  const verdict = useMemo(() => {
    const rid = d?.motion?.request_id;
    if (!rid) return null;
    for (let i = events.length - 1; i >= 0; i--) {
      const e = events[i];
      if (e.payload?.request_id !== rid) continue;
      if (e.event_type === "robot.motion_blocked") return { ok: false, text: `${e.payload.reason_code}: ${e.payload.reason}`, checks: e.payload.checks as any[] };
      if (e.event_type === "robot.motion_sent") return { ok: true, text: `approved · sent ${e.payload.primitive} ${e.payload.speed} for ${e.payload.duration_s}s` };
    }
    return null;
  }, [d, events]);

  return (
    <div className="px-5 pt-4 pb-5">
      <div className="flex items-center justify-between">
        <h3 className="font-display text-[13px] font-semibold tracking-[0.18em] text-ink-2 uppercase">Why it acted</h3>
        {d ? <span className="num text-[11px] text-ink-4">{timeOfDay(d.at)}</span> : null}
      </div>
      {!d ? (
        <p className="mt-3 max-w-[48ch] text-[13px] leading-relaxed text-ink-3">
          Decisions appear here with every rule that fired. The policy only ever <em>requests</em> motion; the safety layer decides.
        </p>
      ) : (
        <div className="mt-3">
          <div className="flex items-baseline gap-3">
            <span className={`font-display text-[20px] font-semibold tracking-[0.06em] uppercase ${d.action === "retreat" ? "text-hit" : d.action === "voice_only" ? "text-ink" : "text-ink-3"}`}>
              {d.action.replace("_", " ")}
            </span>
            {d.category ? <span className="num text-[11px] text-ink-3">{d.category}</span> : null}
          </div>
          <p className="mt-1 text-[13px] text-ink-2">{d.explanation}</p>
          <ul className="mt-3 space-y-1 border-l border-line pl-3">
            {d.rules.map((r: string, i: number) => (
              <li key={i} className="num text-[11px] leading-snug text-ink-3">
                {r}
              </li>
            ))}
          </ul>
          {verdict ? (
            <div className={`mt-3 text-[12.5px] ${verdict.ok ? "text-ink-2" : "text-sim"}`}>
              <span className="label mr-2">Safety</span>
              {verdict.text}
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}
