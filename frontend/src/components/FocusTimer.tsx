import { useMemo, useState } from "react";
import { Minus, Pause, Play, Plus, Square, Smartphone } from "lucide-react";
import type { LockedInEvent, Snapshot } from "../types/api";
import { useTicker } from "../hooks/useLockedIn";
import { api } from "../services/api";
import { clock } from "../lib/format";
import { useToasts } from "./Toasts";

const PRESETS = [15, 25, 45, 60];
const UNITS = ["problems", "pages", "questions", "tasks", "flashcards"];

function StartForm({ defaultMinutes }: { defaultMinutes: number }) {
  const { run } = useToasts();
  const [task, setTask] = useState("");
  const [mins, setMins] = useState(defaultMinutes);
  const [target, setTarget] = useState<string>("");
  const [unit, setUnit] = useState("problems");
  const [busy, setBusy] = useState(false);

  const start = async () => {
    setBusy(true);
    await run(
      api.startSession({
        task: task.trim() || undefined,
        duration_min: mins,
        target_units: target ? Number(target) : null,
        unit_label: target ? unit : undefined,
      }),
    );
    setBusy(false);
  };

  return (
    <form
      className="flex flex-col gap-4"
      onSubmit={(e) => {
        e.preventDefault();
        void start();
      }}
    >
      <div>
        <label className="label" htmlFor="task">
          What are you locking in on
        </label>
        <input id="task" className="field mt-2 !h-11 !text-[16px]" placeholder="Calculus problem set" value={task} onChange={(e) => setTask(e.target.value)} maxLength={120} />
      </div>
      <div className="flex flex-wrap items-end gap-x-5 gap-y-3">
        <div>
          <div className="label">Minutes</div>
          <div className="mt-2 flex">
            {PRESETS.map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => setMins(m)}
                className={`num h-9 w-12 border text-[13px] transition-colors first:rounded-l-[4px] last:rounded-r-[4px] [&+&]:-ml-px ${
                  mins === m ? "relative z-10 border-live bg-live-wash text-live" : "border-line bg-well text-ink-2 hover:text-ink"
                }`}
              >
                {m}
              </button>
            ))}
          </div>
        </div>
        <div>
          <label className="label" htmlFor="target">
            Target (optional)
          </label>
          <div className="mt-2 flex gap-2">
            <input
              id="target"
              className="field num !w-16"
              inputMode="numeric"
              placeholder="5"
              value={target}
              onChange={(e) => setTarget(e.target.value.replace(/[^0-9]/g, "").slice(0, 3))}
            />
            <select className="field !w-[130px]" value={unit} onChange={(e) => setUnit(e.target.value)} aria-label="Unit">
              {UNITS.map((u) => (
                <option key={u}>{u}</option>
              ))}
            </select>
          </div>
        </div>
      </div>
      <div className="flex items-center gap-4">
        <button type="submit" className="btn btn-primary !h-11 !px-6 !text-[14px]" disabled={busy}>
          <Play size={16} /> Lock in
        </button>
        <span className="text-[13px] text-ink-3">
          or say <span className="text-ink-2">“Lock in, 25 minutes for calculus”</span>
        </span>
      </div>
    </form>
  );
}

export function FocusTimer({ snap, snapshotAt, events }: { snap: Snapshot; snapshotAt: number; events: LockedInEvent[] }) {
  const now = useTicker(8);
  const { run } = useToasts();
  const s = snap.session;
  const state = snap.state.name;

  const live = useMemo(() => {
    if (!s) return null;
    const dt = s.paused ? 0 : Math.max(0, (now - snapshotAt) / 1000);
    return { elapsed: s.elapsed_active_s + dt, remaining: Math.max(0, s.remaining_s - dt) };
  }, [s, now, snapshotAt]);

  const reachMarks = useMemo(() => {
    if (!s) return [];
    return events
      .filter((e) => e.event_type === "vision.phone_reach_confirmed" && e.session_id === s.session_id && typeof e.payload.session_active_s === "number")
      .map((e) => ({ id: e.event_id, at: e.payload.session_active_s as number, sim: e.simulated }));
  }, [events, s]);

  const status = !s ? (state === "RELEASED" ? "Released" : "Idle") : s.paused ? "Paused" : "Running";
  const statusClass = !s ? (state === "RELEASED" ? "chip-sim" : "chip-dim") : s.paused ? "chip-sim" : "chip-live";
  const planned = s?.planned_duration_s ?? 0;
  const progress = live && planned ? Math.min(1, live.elapsed / planned) : 0;

  return (
    <div className="flex flex-col gap-6 px-5 pt-4 pb-5">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <h2 className="font-display text-[13px] font-semibold tracking-[0.18em] text-ink-2 uppercase">Focus session</h2>
          <span className={`chip ${statusClass}`}>{status}</span>
        </div>
        {s ? <span className="num text-[11px] text-ink-4">{s.session_id.slice(-6)}</span> : null}
      </div>

      {!s || !live ? (
        <>
          {snap.last_session ? (
            <div className="text-[13px] text-ink-3">
              Last: <span className="text-ink-2">{snap.last_session.task}</span> · ended {snap.last_session.end_reason}
            </div>
          ) : null}
          <StartForm defaultMinutes={snap.settings.default_session_minutes} />
        </>
      ) : (
        <>
          <div>
            <div className="font-display text-[22px] leading-tight font-semibold text-ink">{s.task}</div>
            <div
              className={`num mt-3 leading-[0.9] font-[350] tracking-[-0.04em] ${s.paused ? "text-ink-3" : "text-ink"}`}
              style={{ fontSize: "clamp(76px, 8.6vw, 132px)" }}
              aria-label={`${clock(live.remaining)} remaining`}
            >
              {clock(live.remaining)}
            </div>
            <div className="mt-3 flex items-baseline gap-3 text-[13px] text-ink-3">
              <span className="label">Elapsed</span>
              <span className="num text-ink-2">{clock(live.elapsed)}</span>
              <span className="text-ink-4">of</span>
              <span className="num text-ink-2">{clock(planned)}</span>
              {s.pause_count ? <span className="text-ink-4">· {s.pause_count} pause{s.pause_count > 1 ? "s" : ""}</span> : null}
            </div>
          </div>

          <div className="relative h-5" aria-hidden>
            <div className="absolute top-2 right-0 left-0 h-px bg-line" />
            <div className="absolute top-[7px] left-0 h-[3px] bg-live transition-[width] duration-700 ease-[cubic-bezier(0.25,1,0.5,1)]" style={{ width: `${progress * 100}%` }} />
            {Array.from({ length: Math.max(0, Math.floor(planned / 300) - 1) }, (_, i) => (
              <div key={i} className="absolute top-[5px] h-[7px] w-px bg-line" style={{ left: `${((i + 1) * 300 * 100) / planned}%` }} />
            ))}
            {reachMarks.map((m) => (
              <div
                key={m.id}
                title={`phone reach at ${clock(m.at)}${m.sim ? " (simulated)" : ""}`}
                className={`absolute top-0 h-[17px] w-[3px] -translate-x-1/2 ${m.sim ? "bg-hit/60" : "bg-hit"}`}
                style={{ left: `${Math.min(100, (m.at * 100) / planned)}%` }}
              />
            ))}
          </div>

          <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
            <div className="flex items-center gap-3">
              <div>
                <div className="label">Done</div>
                <div className="num mt-1 text-[20px] text-ink">
                  {s.completed_units}
                  {s.target_units ? <span className="text-ink-3"> / {s.target_units}</span> : null}
                  <span className="ml-2 font-sans text-[13px] text-ink-3">{s.unit_label}</span>
                </div>
              </div>
              <div className="flex gap-1">
                <button className="btn btn-sm !w-8 !px-0" onClick={() => run(api.progress(-1))} aria-label="Remove one" disabled={s.completed_units === 0}>
                  <Minus size={14} />
                </button>
                <button className="btn btn-sm !px-2.5" onClick={() => run(api.progress(1))}>
                  <Plus size={14} /> 1
                </button>
              </div>
            </div>
            <div className="ml-auto flex flex-wrap gap-2">
              {s.paused ? (
                <button className="btn btn-primary" onClick={() => run(api.resume())}>
                  <Play size={15} /> Resume
                </button>
              ) : (
                <button className="btn" onClick={() => run(api.pause())}>
                  <Pause size={15} /> Pause
                </button>
              )}
              <button className="btn" onClick={() => run(api.end())}>
                <Square size={13} /> Finish
              </button>
              <button className="btn btn-hit" onClick={() => run(api.release())} title="Stop the robot and end the session: the phone is yours">
                <Smartphone size={15} /> Release phone
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
