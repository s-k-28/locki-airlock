import type { Snapshot, StateName } from "../types/api";

const MAIN: { s: StateName; short: string }[] = [
  { s: "IDLE", short: "Idle" },
  { s: "SESSION_ACTIVE", short: "Active" },
  { s: "POSSIBLE_PHONE_REACH", short: "Possible" },
  { s: "PHONE_REACH_CONFIRMED", short: "Confirmed" },
  { s: "RETREATING", short: "Retreat" },
  { s: "HOLDING", short: "Hold" },
  { s: "RETURNING", short: "Return" },
];
const SIDE: { s: StateName; short: string; tone: string }[] = [
  { s: "PAUSED", short: "Paused", tone: "text-sim border-sim" },
  { s: "RELEASED", short: "Released", tone: "text-sim border-sim" },
  { s: "DISCONNECTED", short: "Disconnected", tone: "text-ink-2 border-ink-3" },
  { s: "EMERGENCY_STOP", short: "E-Stop", tone: "text-danger border-danger" },
];
const INTERVENTION: StateName[] = ["POSSIBLE_PHONE_REACH", "PHONE_REACH_CONFIRMED", "RETREATING", "HOLDING", "RETURNING"];

export function StateRail({ snap }: { snap: Snapshot }) {
  const current = snap.state.name;
  const idx = MAIN.findIndex((m) => m.s === current);
  const history = snap.state.history.slice(-5).reverse();

  return (
    <div className="px-5 pt-4 pb-5">
      <div className="flex items-center justify-between">
        <h3 className="font-display text-[13px] font-semibold tracking-[0.18em] text-ink-2 uppercase">State machine</h3>
        <span className="label !text-[10px]">11 explicit states</span>
      </div>
      <ol className="mt-4 grid grid-cols-7" aria-label="Main behaviour path">
        {MAIN.map((m, i) => {
          const active = m.s === current;
          const passed = idx >= 0 && i < idx;
          const hot = INTERVENTION.includes(m.s);
          return (
            <li key={m.s} className="relative flex flex-col items-center" title={m.s}>
              {i > 0 ? (
                <span className={`absolute top-[7px] right-1/2 h-px w-full transition-colors duration-300 ${passed || active ? (hot ? "bg-hit" : "bg-live") : "bg-line"}`} aria-hidden />
              ) : null}
              <span
                className={`relative z-10 h-[15px] w-[15px] rounded-[3px] border transition-[background-color,border-color,transform] duration-300 ease-[cubic-bezier(0.25,1,0.5,1)] ${
                  active
                    ? hot
                      ? "scale-110 border-hit bg-hit"
                      : "scale-110 border-live bg-live"
                    : passed
                      ? hot
                        ? "border-hit bg-hit-wash"
                        : "border-live bg-live-wash"
                      : "border-line bg-well"
                }`}
              />
              <span className={`mt-2 text-center text-[10.5px] leading-tight font-semibold tracking-[0.06em] uppercase [font-stretch:80%] ${active ? (hot ? "text-hit" : "text-live") : "text-ink-4"}`}>
                {m.short}
              </span>
            </li>
          );
        })}
      </ol>
      <div className="mt-4 flex flex-wrap gap-1.5">
        {SIDE.map((x) => (
          <span key={x.s} className={`chip ${x.s === current ? x.tone + " bg-raised" : "chip-dim"}`}>
            {x.short}
          </span>
        ))}
      </div>
      {history.length ? (
        <ul className="mt-4 space-y-1">
          {history.map((h, i) => (
            <li key={`${h.at}-${i}`} className={`num flex items-baseline gap-2 text-[11px] ${i === 0 ? "text-ink-2" : "text-ink-4"}`}>
              <span className="w-[13ch] shrink-0 truncate">{h.trigger}</span>
              <span className="text-ink-4">→</span>
              <span className="truncate">{h.to_state}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
