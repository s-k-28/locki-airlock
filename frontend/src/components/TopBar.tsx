import { Bug, Settings2, OctagonX, RotateCcw, AudioLines } from "lucide-react";
import type { Snapshot, StateName } from "../types/api";
import type { LinkStatus } from "../hooks/useLockedIn";
import { api } from "../services/api";
import { useToasts } from "./Toasts";

export function stateTone(s: StateName | undefined): "live" | "hit" | "sim" | "danger" | "dim" {
  switch (s) {
    case "SESSION_ACTIVE":
      return "live";
    case "POSSIBLE_PHONE_REACH":
    case "PHONE_REACH_CONFIRMED":
    case "RETREATING":
    case "HOLDING":
    case "RETURNING":
      return "hit";
    case "PAUSED":
    case "RELEASED":
      return "sim";
    case "EMERGENCY_STOP":
      return "danger";
    default:
      return "dim";
  }
}

function Wordmark() {
  return (
    <div className="flex shrink-0 items-center gap-2.5 whitespace-nowrap">
      <svg width="22" height="26" viewBox="0 0 22 26" aria-hidden>
        <rect x="2" y="1.5" width="18" height="23" rx="3.5" fill="none" stroke="var(--color-live)" strokeWidth="2" />
        <path d="M7 13.5V11a4 4 0 0 1 8 0v2.5" fill="none" stroke="var(--color-hit)" strokeWidth="1.8" />
        <rect x="5.5" y="13.5" width="11" height="7" rx="1.2" fill="var(--color-hit)" />
      </svg>
      <span className="font-display text-[19px] leading-none font-bold tracking-[0.16em] text-ink">LOCKED IN</span>
    </div>
  );
}

function modeChip(mode: Snapshot["mode"]): string {
  if (mode.robot_profile !== "zeus") return mode.robot_simulated ? "chip-sim" : "chip-live";
  if (mode.robot_state === "connected") return "chip-live";
  return mode.robot_state === "connecting" ? "chip-sim" : "chip-danger";
}

function modeTitle(mode: Snapshot["mode"]): string {
  if (mode.robot_profile === "zeus") {
    const motion = mode.physical_motion === "locked" ? "Physical motion locked: LOCKED IN sends no motor commands." : `Physical motion ${mode.physical_motion}.`;
    return `Real Zeus Car profile. ${motion} Camera: ${mode.camera_label ?? "offline"}.`;
  }
  return "A: full physical · B: live perception, simulated robot · C: full simulation";
}

function Health({ label, ok, value, warn }: { label: string; ok: boolean; value: string; warn?: boolean }) {
  return (
    <div className="hidden items-center gap-1.5 xl:flex" title={`${label}: ${value}`}>
      <span className={`dot ${ok ? (warn ? "dot-sim" : "dot-live") : ""}`} />
      <span className="label !text-[10px]">{label}</span>
      <span className="num text-[11px] text-ink-2">{value}</span>
    </div>
  );
}

export function TopBar({
  snap,
  link,
  wsLatencyMs,
  onSettings,
  onDebug,
  onVoice,
}: {
  snap: Snapshot | null;
  link: LinkStatus;
  wsLatencyMs: number | null;
  onSettings: () => void;
  onDebug: () => void;
  onVoice: () => void;
}) {
  const { run } = useToasts();
  const state = snap?.state.name;
  const tone = stateTone(state);
  const mode = snap?.mode;
  const robot = snap?.robot.status;
  const estop = snap?.safety.estop_latched;
  const vision = snap?.vision;
  const mic = snap?.voice.input;
  const expr = snap?.expression;
  const pers = snap?.voice.personality;

  return (
    <header className="sticky top-0 z-40 border-b border-line-soft bg-void/95 backdrop-blur-[2px]">
      <div className="flex h-14 items-center gap-5 px-5">
        <Wordmark />
        <div className="h-6 w-px bg-line-soft" />
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="label">State</span>
          <span className={`dot ${tone === "live" ? "dot-live" : tone === "hit" ? "dot-hit" : tone === "sim" ? "dot-sim" : tone === "danger" ? "dot-danger blink" : ""}`} />
          <span
            className={`num truncate text-[13px] ${
              tone === "live" ? "text-live" : tone === "hit" ? "text-hit" : tone === "sim" ? "text-sim" : tone === "danger" ? "text-danger" : "text-ink-2"
            }`}
          >
            {link === "open" ? (state ?? "…") : "BACKEND OFFLINE"}
          </span>
          {snap ? <span className="num hidden text-[11px] text-ink-4 lg:inline">{snap.state.since_s.toFixed(0)}s</span> : null}
        </div>
        {mode ? (
          <span className={`chip ${modeChip(mode)} hidden md:inline-flex ${mode.robot_state === "connecting" ? "blink" : ""}`} title={modeTitle(mode)}>
            {mode.robot_profile === "zeus" ? mode.label : `Mode ${mode.code} · ${mode.label}`}
          </span>
        ) : null}
        {mode?.offline ? (
          <span className="chip chip-dim hidden lg:inline-flex" title="No internet needed: cached Fish Audio clips, the local macOS voice for the rest. Fish Audio and the LLM are never contacted.">
            Offline
          </span>
        ) : null}

        {expr ? (
          <span
            className={`chip hidden xl:inline-flex ${expr.color === "danger" ? "chip-danger" : expr.color === "hit" ? "chip-hit" : expr.color === "party" ? "chip-hit" : expr.color === "calm" || expr.color === "dim" ? "chip-dim" : "chip-live"} ${expr.pattern === "pulse" || expr.pattern === "sparkle" ? "blink" : ""}`}
            title={`Robot expression (${expr.hardware})`}
          >
            {expr.label}
          </span>
        ) : null}

        <div className="ml-auto flex items-center gap-4">
          <Health label="API" ok={link === "open"} value={link === "open" ? (wsLatencyMs != null ? `${wsLatencyMs.toFixed(0)}ms` : "ok") : link} />
          <Health
            label="Robot"
            ok={!!robot?.connected}
            warn={robot?.simulated || robot?.link_state === "degraded"}
            value={
              robot?.connected
                ? robot.simulated
                  ? "sim"
                  : robot.rtt_ms != null
                    ? `${robot.rtt_ms.toFixed(0)}ms`
                    : robot.link_state
                : "off"
            }
          />
          <Health
            label="Camera"
            ok={vision?.camera === "live"}
            warn={vision?.camera === "reconnecting" || vision?.camera === "opening"}
            value={
              vision?.camera === "live"
                ? `${Math.round((vision.stats?.capture_fps as number) ?? 0)}fps`
                : vision?.running
                  ? vision.camera === "opening" ? "opening" : "retrying"
                  : "off"
            }
          />
          <Health label="Mic" ok={!!mic?.running} value={mic?.running ? (mic.mode === "push_to_talk" ? "ptt" : mic.mode ?? "on") : "off"} />
          <div className="flex items-center gap-1">
            <button className="btn btn-ghost btn-sm gap-1.5 !px-2" onClick={onVoice} aria-label="Personality and voice" title="Personality + Voice">
              <AudioLines size={16} />
              {pers ? <span className="num hidden text-[11px] text-ink-3 lg:inline">{pers.label}</span> : null}
            </button>
            <button className="btn btn-ghost btn-sm !px-2" onClick={onDebug} aria-label="Debug and experiments">
              <Bug size={16} />
            </button>
            <button className="btn btn-ghost btn-sm !px-2" onClick={onSettings} aria-label="Settings">
              <Settings2 size={16} />
            </button>
          </div>
          {estop ? (
            <div className="flex items-center gap-2">
              {snap?.robot.hardware_stop?.kind === "software_only" ? (
                <span className="chip chip-danger hidden lg:inline-flex" title={snap.robot.hardware_stop.summary}>
                  Software stop · no motor command sent
                </span>
              ) : null}
              <button className="btn btn-sm !h-10 border-danger bg-danger/15 !px-4 text-[oklch(0.85_0.1_27)]" onClick={() => run(api.reset(), false)}>
                <RotateCcw size={15} /> Reset E-stop
              </button>
            </div>
          ) : (
            <button
              className="btn !h-10 border-danger-2 bg-danger !px-5 text-[14px] font-bold tracking-[0.12em] text-[oklch(0.98_0.01_27)] hover:!border-danger hover:!bg-[oklch(0.66_0.23_27)]"
              onClick={() => run(api.estop())}
              title={
                robot && !robot.simulated && robot.capabilities.link_mode === "monitor"
                  ? "Latches a software stop and disarms. This monitor link sends no motor commands, so nothing is sent to the robot: its power switch is the hardware stop."
                  : "Latching emergency stop: stops motion, disarms, requires reset"
              }
            >
              <OctagonX size={17} /> E-STOP
            </button>
          )}
        </div>
      </div>
    </header>
  );
}
