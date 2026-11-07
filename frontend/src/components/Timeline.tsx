import { useMemo, useState } from "react";
import { Bot, Brain, Cpu, Eye, MessageSquare, ShieldAlert, Timer, Flag } from "lucide-react";
import type { LockedInEvent } from "../types/api";
import { describe, type Group } from "../lib/events";
import { timeOfDay } from "../lib/format";
import { api } from "../services/api";
import { useToasts } from "./Toasts";

const ICON: Record<Group, JSX.Element> = {
  session: <Timer size={14} />,
  vision: <Eye size={14} />,
  robot: <Bot size={14} />,
  safety: <ShieldAlert size={14} />,
  voice: <MessageSquare size={14} />,
  coach: <Brain size={14} />,
  system: <Cpu size={14} />,
};

const FILTERS: { key: "all" | "loop" | Group; label: string }[] = [
  { key: "loop", label: "Interventions" },
  { key: "all", label: "All" },
  { key: "vision", label: "Vision" },
  { key: "robot", label: "Robot" },
  { key: "voice", label: "Voice" },
  { key: "session", label: "Session" },
];

const LOOP_TYPES = new Set([
  "session.started",
  "session.ended",
  "session.paused",
  "session.resumed",
  "vision.phone_reach_confirmed",
  "vision.hand_approaching",
  "coach.decision",
  "coach.intervention_generated",
  "robot.motion_sent",
  "robot.motion_blocked",
  "robot.motion_completed",
  "robot.motion_aborted",
  "safety.emergency_stop",
  "safety.watchdog_fired",
  "robot.disconnected",
  "voice.transcribed",
  "voice.response_generated",
  "vision.camera_disconnected",
]);

const TONE: Record<string, string> = {
  live: "text-live",
  hit: "text-hit",
  sim: "text-sim",
  danger: "text-danger",
  dim: "text-ink-3",
  ink: "text-ink",
};

export function Timeline({ events }: { events: LockedInEvent[] }) {
  const [filter, setFilter] = useState<(typeof FILTERS)[number]["key"]>("loop");
  const { run, notify } = useToasts();

  const rows = useMemo(() => {
    const out: { e: LockedInEvent; d: ReturnType<typeof describe> }[] = [];
    for (let i = events.length - 1; i >= 0 && out.length < 220; i--) {
      const e = events[i];
      const d = describe(e);
      if (d.hidden) continue;
      if (filter === "loop" && !LOOP_TYPES.has(e.event_type)) continue;
      if (filter !== "all" && filter !== "loop" && d.group !== filter && !(filter === "robot" && d.group === "safety")) continue;
      out.push({ e, d });
    }
    return out;
  }, [events, filter]);

  return (
    <div className="panel flex h-full min-h-[360px] flex-col">
      <div className="flex flex-wrap items-center justify-between gap-2 px-4 pt-3 pb-2.5">
        <h2 className="font-display text-[13px] font-semibold tracking-[0.18em] text-ink-2 uppercase">Timeline</h2>
        <div className="flex flex-wrap gap-1">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              onClick={() => setFilter(f.key)}
              className={`chip cursor-pointer transition-colors ${filter === f.key ? "border-ink-3 bg-raised text-ink" : "chip-dim hover:text-ink-2"}`}
            >
              {f.label}
            </button>
          ))}
        </div>
      </div>
      <div className="rule" />
      {rows.length === 0 ? (
        <p className="px-4 py-6 text-[13px] text-ink-3">Nothing yet. Start a session, reach for the phone, or talk to it. Every step lands here as it happens.</p>
      ) : (
        <ol className="scroll-y max-h-[620px] min-h-0 flex-1">
          {rows.map(({ e, d }) => (
            <li key={e.event_id} className="group grid grid-cols-[64px_18px_1fr] items-start gap-x-2.5 border-b border-line-soft/60 px-4 py-2 last:border-b-0">
              <span className="num pt-[3px] text-[10.5px] text-ink-4">{timeOfDay(e.timestamp)}</span>
              <span className={`pt-[3px] ${TONE[d.tone]}`}>{ICON[d.group]}</span>
              <div className="min-w-0">
                <div className="flex items-start gap-2">
                  <span className={`min-w-0 text-[13px] leading-snug ${d.tone === "hit" || d.tone === "danger" ? `font-semibold ${TONE[d.tone]}` : d.tone === "dim" ? "text-ink-3" : "text-ink"}`}>{d.title}</span>
                  {e.simulated ? <span className="chip chip-sim !h-[18px] !px-1.5 !text-[9.5px]">Sim</span> : null}
                  {e.event_type === "vision.phone_reach_confirmed" ? (
                    <button
                      className="ml-auto hidden shrink-0 items-center gap-1 text-[11px] text-ink-4 group-hover:inline-flex hover:text-sim"
                      title="Label this as a false trigger (feeds adaptation and the experiment log)"
                      onClick={async () => {
                        const r = await run(api.falseTrigger());
                        if (r) notify("Labelled as false trigger", "ok");
                      }}
                    >
                      <Flag size={11} /> false trigger
                    </button>
                  ) : null}
                </div>
                {d.detail ? <div className="mt-0.5 truncate text-[11.5px] text-ink-3">{d.detail}</div> : null}
              </div>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
