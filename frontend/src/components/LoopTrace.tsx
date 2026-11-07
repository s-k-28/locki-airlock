import { useMemo } from "react";
import { Hand, Play, Square, Zap, WifiOff } from "lucide-react";
import type { LockedInEvent, Snapshot } from "../types/api";
import { api } from "../services/api";
import { useToasts } from "./Toasts";

interface Step {
  key: string;
  label: string;
  value: string;
  sub?: string;
  state: "lit" | "blocked" | "skipped" | "pending";
}

function buildTrace(events: LockedInEvent[]): { steps: Step[]; reach: LockedInEvent; simulated: boolean } | null {
  let i = events.length - 1;
  while (i >= 0 && events[i].event_type !== "vision.phone_reach_confirmed") i--;
  if (i < 0) return null;
  const reach = events[i];
  const after = events.slice(i + 1, i + 60);
  const base = reach.mono - (Number(reach.payload.frame_to_event_ms ?? 0) / 1000);
  const t = (e: LockedInEvent) => {
    const v = (e.mono - base) * 1000;
    return `+${v < 100 ? v.toFixed(1) : v.toFixed(0)} ms`;
  };
  const decision = after.find((e) => e.event_type === "coach.decision");
  const rid = decision?.payload.motion?.request_id as string | undefined;
  const find = (type: string) => after.find((e) => e.event_type === type && (!rid || e.payload.request_id === rid));
  const sent = rid ? find("robot.motion_sent") : undefined;
  const blocked = rid ? find("robot.motion_blocked") : undefined;
  const ack = rid ? find("robot.motion_acknowledged") : undefined;
  const done = rid ? find("robot.motion_completed") ?? find("robot.motion_aborted") : undefined;
  const confirmed = rid ? find("robot.motion_confirmed") : undefined;

  const steps: Step[] = [
    { key: "detect", label: "Detected", value: reach.confidence?.toFixed(2) ?? "—", sub: `+${reach.payload.frame_to_event_ms ?? 0} ms`, state: "lit" },
    {
      key: "decide",
      label: "Decided",
      value: decision ? String(decision.payload.action).replace("_", " ") : "…",
      sub: decision ? `+${decision.payload.frame_to_decision_ms ?? "?"} ms` : undefined,
      state: decision ? "lit" : "pending",
    },
  ];
  if (decision && decision.payload.action !== "retreat") {
    steps.push({ key: "safety", label: "Safety", value: "no motion requested", state: "skipped" });
    steps.push({ key: "sent", label: "Sent", value: "—", state: "skipped" });
    steps.push({ key: "ack", label: "Ack", value: "—", state: "skipped" });
    steps.push({ key: "done", label: "Done", value: "—", state: "skipped" });
  } else {
    steps.push({
      key: "safety",
      label: "Safety",
      value: blocked ? String(blocked.payload.reason_code).replace(/_/g, " ") : sent ? "approved" : "…",
      state: blocked ? "blocked" : sent ? "lit" : "pending",
    });
    steps.push({ key: "sent", label: "Sent", value: sent ? `${sent.payload.primitive}` : blocked ? "—" : "…", sub: sent ? t(sent) : undefined, state: sent ? "lit" : blocked ? "skipped" : "pending" });
    steps.push({
      key: "ack",
      label: "Ack",
      value: ack ? (ack.payload.ack === "link_echo" ? "echo" : ack.payload.ack === "simulated" ? "sim" : String(ack.payload.ack)) : blocked ? "—" : "…",
      sub: ack ? t(ack) : undefined,
      state: ack ? "lit" : blocked ? "skipped" : "pending",
    });
    steps.push({
      key: "done",
      label: confirmed ? "Confirmed" : "Done",
      value: done ? (done.event_type.endsWith("aborted") ? "aborted" : confirmed ? "moved" : "window") : blocked ? "—" : "…",
      sub: done ? t(done) : undefined,
      state: done ? (done.event_type.endsWith("aborted") ? "blocked" : "lit") : blocked ? "skipped" : "pending",
    });
  }
  return { steps, reach, simulated: reach.simulated };
}

export function LoopTrace({ snap, events }: { snap: Snapshot; events: LockedInEvent[] }) {
  const { run } = useToasts();
  const trace = useMemo(() => buildTrace(events), [events]);
  const sim = snap.mode.robot_simulated || !snap.mode.camera_live;
  const mockRobot = snap.robot.status?.simulated && snap.robot.status.connected;

  return (
    <div className="panel">
      <div className="flex items-center justify-between gap-3 px-4 pt-3 pb-2">
        <div className="flex items-center gap-2.5">
          <h2 className="font-display text-[13px] font-semibold tracking-[0.18em] text-ink-2 uppercase">Closed loop</h2>
          {trace?.simulated ? <span className="chip chip-sim">Sim input</span> : null}
        </div>
        <span className="label !text-[10px]">last reach · times from frame capture</span>
      </div>
      {trace ? (
        <ol className="grid grid-cols-3 gap-px bg-line-soft sm:grid-cols-6" key={trace.reach.event_id}>
          {trace.steps.map((s, i) => (
            <li
              key={s.key}
              className={`bg-panel px-3 py-2.5 ${s.state === "lit" ? "step-lit" : ""}`}
              style={{ animationDelay: `${i * 90}ms` }}
            >
              <div className={`label !text-[10px] ${s.state === "lit" ? "!text-hit" : s.state === "blocked" ? "!text-sim" : ""}`}>{s.label}</div>
              <div className={`num mt-1 truncate text-[11.5px] ${s.state === "lit" ? "text-ink" : s.state === "blocked" ? "text-sim" : "text-ink-4"}`}>{s.value}</div>
              <div className="num mt-0.5 h-[14px] truncate text-[10.5px] text-ink-3">{s.sub ?? ""}</div>
            </li>
          ))}
        </ol>
      ) : (
        <p className="px-4 pb-3 text-[13px] text-ink-3">No reach yet. When a hand enters the phone zone, each stage of the loop lights up here with its measured latency.</p>
      )}
      {sim ? (
        <div className="flex flex-wrap items-center gap-2 border-t border-line-soft px-4 py-2.5">
          <span className="chip chip-sim">Simulation</span>
          <button className="btn btn-sm btn-sim" onClick={() => run(api.simReach(true))} title="Inject a synthetic approach + confirmed reach (labelled SIMULATED)">
            <Hand size={13} /> Reach
          </button>
          <button className="btn btn-sm btn-ghost" onClick={() => run(api.simReach(false))} title="Approach that leaves without confirming">
            Near miss
          </button>
          <select className="field !h-7 !w-[130px] !text-[12px]" defaultValue="" onChange={(e) => e.target.value && run(api.simFocus(e.target.value)).then(() => (e.target.value = ""))} aria-label="Simulate focus">
            <option value="" disabled>
              Focus state…
            </option>
            <option value="FOCUSED">Focused</option>
            <option value="POSSIBLY_DISTRACTED">Distracted</option>
            <option value="AWAY">Away</option>
          </select>
          {snap.demo_running ? (
            <button className="btn btn-sm btn-ghost" onClick={() => run(api.simDemoStop())}>
              <Square size={12} /> Stop script
            </button>
          ) : (
            <button className="btn btn-sm btn-ghost" onClick={() => run(api.simDemo(), false)} title="Plays the 90-second demo with typed commands and simulated reaches">
              <Play size={13} /> Demo script
            </button>
          )}
          {mockRobot ? (
            <button className="btn btn-sm btn-ghost" onClick={() => run(api.linkLoss(3), false)} title="Mock robot only: frames stop arriving for 3 s">
              <WifiOff size={13} /> Link loss
            </button>
          ) : null}
          <span className="ml-auto hidden items-center gap-1.5 text-[11.5px] text-ink-4 lg:flex">
            <Zap size={12} /> injects labelled synthetic events
          </span>
        </div>
      ) : null}
    </div>
  );
}
