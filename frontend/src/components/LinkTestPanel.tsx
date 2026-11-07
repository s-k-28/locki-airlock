import { useEffect, useRef, useState } from "react";
import { Unplug } from "lucide-react";
import type { LockedInEvent, Snapshot } from "../types/api";
import { api } from "../services/api";
import { timeOfDay } from "../lib/format";
import { useToasts } from "./Toasts";

type Setup = "wheels_off_ground" | "on_floor";
const MAX_FREEZE: Record<Setup, number> = { wheels_off_ground: 5, on_floor: 1.5 };

/** R5: drive slowly, stop transmitting with the link open, film the wheels next to the timer. */
export function LinkTestPanel({ snap, events }: { snap: Snapshot; events: LockedInEvent[] }) {
  const { run } = useToasts();
  const [setup, setSetup] = useState<Setup | null>(null);
  const [primitive, setPrimitive] = useState("forward");
  const [speed, setSpeed] = useState(30);
  const [freeze, setFreeze] = useState(2);

  const running = snap.robot.link_test_running;
  const results = snap.robot.link_tests ?? [];
  const blockers = [
    !snap.robot.status?.connected && "robot not connected",
    !snap.safety.armed && "not armed",
    snap.session && "a session is running",
    snap.state.name !== "IDLE" && `state ${snap.state.name}`,
    !setup && "choose the setup",
  ].filter(Boolean) as string[];
  const limit = setup ? MAX_FREEZE[setup] : 5;

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)]">
      <div className="flex flex-col gap-3">
        <p className="max-w-[62ch] text-[13px] leading-relaxed text-ink-3">
          Test R5 (docs/HARDWARE_TESTS.md). The robot drives slowly, then the laptop stops transmitting with the connection left open, as a hung laptop or stalled Wi-Fi would. Film the wheels with this
          timer in frame. Pass: wheels stop within 0.5 s, every run. E-STOP ends the freeze at once.
        </p>
        <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="Robot setup">
          {(["wheels_off_ground", "on_floor"] as Setup[]).map((s) => (
            <button
              key={s}
              role="radio"
              aria-checked={setup === s}
              className={`btn btn-sm ${setup === s ? "btn-primary" : ""}`}
              onClick={() => {
                setSetup(s);
                setFreeze((f) => Math.min(f, MAX_FREEZE[s]));
              }}
            >
              {s === "wheels_off_ground" ? "Wheels off the ground" : "On the floor"}
            </button>
          ))}
        </div>
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1">
            <span className="label">Direction</span>
            <select className="field !h-8 !w-[150px] !text-[12px]" value={primitive} onChange={(e) => setPrimitive(e.target.value)}>
              {["forward", "backward", "strafe_left", "strafe_right"].map((p) => (
                <option key={p} value={p}>
                  {p.replace("_", " ")}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            <span className="label">Speed (≤ 40)</span>
            <input className="field num !h-8 !w-[80px] !text-[12px]" type="number" min={1} max={40} value={speed} onChange={(e) => setSpeed(Number(e.target.value))} />
          </label>
          <label className="flex flex-col gap-1">
            <span className="label">Freeze s (≤ {limit})</span>
            <input className="field num !h-8 !w-[80px] !text-[12px]" type="number" min={0.5} max={limit} step={0.5} value={freeze} onChange={(e) => setFreeze(Number(e.target.value))} />
          </label>
          <button
            className="btn btn-hit"
            disabled={running || blockers.length > 0}
            title={blockers.join(" · ")}
            onClick={() => setup && run(api.linkTest({ setup, primitive, speed, freeze_s: freeze }))}
          >
            <Unplug size={14} /> Run link test
          </button>
        </div>
        {blockers.length && !running ? <span className="num text-[11.5px] text-ink-4">needs: {blockers.join(" · ")}</span> : null}
        <FreezeTimer events={events} />
      </div>

      <div className="min-w-0">
        {results.length ? (
          <table className="num w-full text-[11.5px]">
            <thead>
              <tr className="text-left text-ink-4">
                <th className="py-1 pr-3 font-normal">time</th>
                <th className="pr-3 font-normal">setup</th>
                <th className="pr-3 font-normal">outcome</th>
                <th className="pr-3 font-normal">frozen</th>
                <th className="pr-3 font-normal">frames</th>
                <th className="font-normal">device</th>
              </tr>
            </thead>
            <tbody>
              {results
                .slice()
                .reverse()
                .map((r) => (
                  <tr key={r.test_id} className="border-t border-line-soft/60 align-top">
                    <td className="py-1 pr-3 text-ink-4">
                      {timeOfDay(r.at)} {r.simulated ? <span className="text-sim">SIM</span> : null}
                    </td>
                    <td className="pr-3 text-ink-3">{r.setup === "on_floor" ? "floor" : "off ground"}</td>
                    <td className={`pr-3 ${r.outcome === "completed" ? "text-ink" : "text-sim"}`}>{r.outcome}</td>
                    <td className="pr-3 text-ink">{r.frozen_for_s != null ? `${r.frozen_for_s.toFixed(2)}s` : "—"}</td>
                    <td className="pr-3 text-ink-3" title="frames written during the freeze (must be 0)">
                      {r.frames_written_during_freeze ?? "—"}
                    </td>
                    <td className={r.device_watchdog.tripped ? "text-live" : r.device_watchdog.tripped === false ? "text-danger" : "text-ink-3"}>{r.device_watchdog.summary}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        ) : (
          <p className="max-w-[52ch] text-[13px] text-ink-3">No link tests yet. Results also go to the timeline and the run's JSONL log. The laptop never sees the wheels: the stop time comes from the video.</p>
        )}
      </div>
    </div>
  );
}

/** Counts from the moment the backend paused transmission (robot.link_frozen) until the test ends. */
function FreezeTimer({ events }: { events: LockedInEvent[] }) {
  const [view, setView] = useState<{ id: string; startMs: number; endS: number | null } | null>(null);
  const [now, setNow] = useState(Date.now());
  const raf = useRef<number>(0);

  useEffect(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      const e = events[i];
      if (e.event_type === "robot.link_test_finished" && view && e.payload?.test_id === view.id && view.endS == null) {
        setView({ ...view, endS: e.payload.frozen_for_s ?? (Date.now() - view.startMs) / 1000 });
        return;
      }
      if (e.event_type === "robot.link_frozen") {
        if (!view || view.id !== e.payload?.test_id) setView({ id: e.payload?.test_id, startMs: e.timestamp * 1000, endS: null });
        return;
      }
    }
  }, [events, view]);

  useEffect(() => {
    if (!view || view.endS != null) return;
    const tick = () => {
      setNow(Date.now());
      raf.current = requestAnimationFrame(tick);
    };
    raf.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf.current);
  }, [view]);

  if (!view) return null;
  const live = view.endS == null;
  const secs = live ? Math.max(0, (now - view.startMs) / 1000) : view.endS!;
  return (
    <div className={`mt-1 w-fit border px-4 py-2 ${live ? "border-danger bg-danger/10" : "border-line"}`} aria-live="polite">
      <div className="label">{live ? "Link frozen · transmit paused" : "Freeze ended · stop sent"}</div>
      <div className={`num text-[44px] leading-none tabular-nums ${live ? "text-danger" : "text-ink-2"}`}>{secs.toFixed(2)}s</div>
    </div>
  );
}
