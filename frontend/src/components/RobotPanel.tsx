import { useEffect, useRef, useState } from "react";
import {
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ArrowUp,
  CornerUpLeft,
  Home,
  Link2,
  Link2Off,
  RotateCcw,
  RotateCw,
  Smartphone,
  Square,
} from "lucide-react";
import type { GateCheck, HardwareGate, RobotAction, Snapshot } from "../types/api";
import { api } from "../services/api";
import { timeOfDay } from "../lib/format";
import { Readout, SectionHeader, SourceTag, Toggle } from "./ui";
import { useToasts } from "./Toasts";

function RobotMap({ pose }: { pose: { x: number; y: number; theta: number } | null }) {
  const trail = useRef<{ x: number; y: number }[]>([]);
  if (pose) {
    const last = trail.current[trail.current.length - 1];
    if (!last || Math.hypot(last.x - pose.x, last.y - pose.y) > 0.004) {
      trail.current = [...trail.current.slice(-80), { x: pose.x, y: pose.y }];
    }
  }
  const W = 300;
  const H = 170;
  const S = 380; // px per metre
  const cx = W / 2;
  const cy = H / 2 + 10;
  const px = (x: number) => cx + x * S;
  const py = (y: number) => cy - y * S;
  const grid = [];
  for (let gx = -0.4; gx <= 0.401; gx += 0.1) grid.push(<line key={`x${gx}`} x1={px(gx)} x2={px(gx)} y1={0} y2={H} stroke="var(--color-line-soft)" strokeWidth={Math.abs(gx) < 1e-6 ? 1 : 0.6} />);
  for (let gy = -0.25; gy <= 0.251; gy += 0.1) grid.push(<line key={`y${gy}`} x1={0} x2={W} y1={py(gy)} y2={py(gy)} stroke="var(--color-line-soft)" strokeWidth={0.6} />);
  const rw = 0.19 * S;
  const rh = 0.21 * S;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="block h-auto w-full" role="img" aria-label="Simulated robot position, top-down">
      <rect width={W} height={H} fill="var(--color-well)" />
      {grid}
      <text x={8} y={14} className="fill-ink-4" fontSize="9" fontFamily="var(--font-sans)" letterSpacing="1.4">
        TOWARD USER ↑
      </text>
      <text x={W - 8} y={H - 8} textAnchor="end" className="fill-ink-4" fontSize="9" fontFamily="var(--font-mono)">
        10 cm grid
      </text>
      <rect x={px(0) - 6} y={py(0) - 6} width={12} height={12} fill="none" stroke="var(--color-live)" strokeDasharray="2 2" />
      <text x={px(0) + 10} y={py(0) - 8} className="fill-live" fontSize="9" fontFamily="var(--font-sans)" letterSpacing="1.2">
        HOME
      </text>
      {trail.current.length > 1 ? (
        <polyline points={trail.current.map((p) => `${px(p.x)},${py(p.y)}`).join(" ")} fill="none" stroke="var(--color-hit)" strokeWidth={1.4} strokeOpacity={0.7} />
      ) : null}
      {pose ? (
        <g transform={`translate(${px(pose.x)} ${py(pose.y)}) rotate(${pose.theta})`} style={{ transition: "transform 120ms linear" }}>
          <rect x={-rw / 2} y={-rh / 2} width={rw} height={rh} rx={6} fill="var(--color-raised)" stroke="var(--color-ink-2)" strokeWidth={1.4} />
          <rect x={-rw / 2 + 6} y={-rh / 2 + 8} width={rw - 12} height={rh * 0.42} rx={2} fill="var(--color-hit-wash)" stroke="var(--color-hit-2)" strokeWidth={1} />
          <line x1={0} y1={-rh / 2} x2={0} y2={-rh / 2 - 9} stroke="var(--color-live)" strokeWidth={2} />
          {[-1, 1].flatMap((sx) => [-1, 1].map((sy) => <rect key={`${sx}${sy}`} x={sx * rw * 0.5 - 4} y={sy * rh * 0.32 - 8} width={8} height={16} rx={2} fill="var(--color-line)" />))}
        </g>
      ) : null}
    </svg>
  );
}

function GateCard({ gate, connected, powerLeftS, onConfirmPower }: { gate: HardwareGate; connected: boolean; powerLeftS: number | null; onConfirmPower: () => void }) {
  const locked = !gate.motion_permitted;
  const title = locked ? "Physical motion locked" : gate.effective === "bench" ? "Bench motion (wheels off the ground)" : "Physical motion permitted";
  const power = gate.checks.find((c) => c.id === "motor_power");
  return (
    <div className={`rounded-[4px] border px-3 py-2.5 ${locked ? "border-danger-2/60 bg-danger/5" : "border-sim/50 bg-sim/5"}`}>
      <div className="flex items-baseline justify-between gap-3">
        <div className={`font-display text-[13px] font-semibold tracking-[0.14em] uppercase ${locked ? "text-danger" : "text-sim"}`}>{title}</div>
        <span className="num text-[11px] text-ink-3">motor commands: {gate.motor_commands}</span>
      </div>
      <ul className="mt-2 space-y-1">
        {gate.checks.map((c) => (
          <GateRow key={c.id} c={c} />
        ))}
      </ul>
      {power && power.ok && powerLeftS != null ? (
        <div className="num mt-1.5 text-[11px] text-ink-3">motor power check expires in {Math.ceil(powerLeftS / 60)} min</div>
      ) : power && connected ? (
        <MotorPowerCheck onConfirm={onConfirmPower} />
      ) : null}
      {gate.autonomy_checks?.length ? (
        <div className="mt-2.5 border-t border-line-soft pt-2">
          <div className="label">Autonomous retreat · {gate.autonomy_permitted ? "permitted" : "locked"}</div>
          <ul className="mt-1.5 space-y-1">
            {gate.autonomy_checks.map((c) => (
              <GateRow key={c.id} c={{ ...c, blocking: c.blocking ?? true }} />
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

/** The firmware cannot measure the battery: the operator checks it before any motion. */
function MotorPowerCheck({ onConfirm }: { onConfirm: () => void }) {
  const [checked, setChecked] = useState(false);
  return (
    <div className="mt-2.5 border-t border-line-soft pt-2">
      <label className="flex items-start gap-2.5 text-[12.5px] leading-snug text-ink-2">
        <input type="checkbox" className="mt-0.5 accent-[var(--color-sim)]" checked={checked} onChange={(e) => setChecked(e.target.checked)} />
        <span>
          I checked motor power: the motor battery LEDs light with the power switch on (USB alone powers only the Arduino), the switch is on, and the wheels are
          clear. <span className="text-ink-3">(valid 30 min, cleared if the link drops)</span>
        </span>
      </label>
      <button className="btn btn-sm mt-2" disabled={!checked} onClick={onConfirm}>
        Confirm motor power
      </button>
    </div>
  );
}

function actionTone(a: RobotAction): string {
  if (a.status === "denied" || a.status === "aborted") return "text-sim";
  if (a.kind === "stop" || a.status.startsWith("e-stop")) return "text-danger";
  return a.physical ? "text-hit" : "text-ink-2";
}

/** What the robot was asked to do and what the safety layer let through. "sent" means frames went
 * over the robot link; nothing here is wheel feedback. */
function ActionLog({ actions }: { actions: RobotAction[] }) {
  return (
    <div>
      <div className="label">Robot actions</div>
      <ul className="num mt-1.5 space-y-1 text-[11px] leading-snug">
        {actions.slice(0, 6).map((a) => (
          <li key={`${a.id}:${a.status}`} className="grid grid-cols-[64px_1fr] gap-2" title={a.ack ? `${a.detail} · ${a.ack}` : a.detail}>
            <span className="text-ink-4">{timeOfDay(a.at)}</span>
            <span className="min-w-0 [overflow-wrap:anywhere]">
              <span className="text-ink-2">{a.what.replace(/_/g, " ")}</span>
              <span className="text-ink-4"> · {a.origin ?? "—"}</span>
              <span className={actionTone(a)}> · {a.status}</span>
              {a.detail && a.status !== "requested" ? <span className="text-ink-4"> · {a.detail}</span> : null}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function GateRow({ c }: { c: GateCheck }) {
  const mark = c.ok === true ? "✓" : c.ok === false ? "✗" : "?";
  const tone = c.ok === true ? "text-live" : c.ok === false ? (c.blocking ? "text-danger" : "text-sim") : "text-ink-3";
  return (
    <li className="grid grid-cols-[14px_1fr] gap-2 text-[12px] leading-snug" title={c.blocking ? "required before any motor command" : "not verifiable; informational"}>
      <span className={`num ${tone}`}>{mark}</span>
      <span className="min-w-0 [overflow-wrap:anywhere]">
        <span className="text-ink-2">{c.label}</span>
        <span className="text-ink-4"> · {c.detail}</span>
      </span>
    </li>
  );
}

const PAD: ({ p: string; icon: JSX.Element; label: string } | null)[] = [
  { p: "rotate_left", icon: <RotateCcw size={16} />, label: "Rotate left" },
  { p: "forward", icon: <ArrowUp size={17} />, label: "Forward" },
  { p: "rotate_right", icon: <RotateCw size={16} />, label: "Rotate right" },
  { p: "strafe_left", icon: <ArrowLeft size={17} />, label: "Strafe left" },
  null,
  { p: "strafe_right", icon: <ArrowRight size={17} />, label: "Strafe right" },
  null,
  { p: "backward", icon: <ArrowDown size={17} />, label: "Backward" },
  null,
];

export function RobotPanel({ snap }: { snap: Snapshot }) {
  const { run, notify } = useToasts();
  const r = snap.robot;
  const st = r.status;
  const sf = snap.safety;
  const connected = !!st?.connected;
  const simulated = !!st?.simulated;
  const [driver, setDriver] = useState<"mock" | "zeus">(r.driver);
  const [host, setHost] = useState(r.host);
  const [clearPath, setClearPath] = useState(false);
  const [testProfile, setTestProfile] = useState(false);
  const [busy, setBusy] = useState(false);
  const canMove = connected && sf.armed && !sf.estop_latched;
  const zeus = r.profile === "zeus";
  const link = r.link;
  const gate = r.gate;
  const motionLocked = zeus && !gate.motion_permitted;
  const lockWhy = motionLocked
    ? gate.checks.filter((c) => c.blocking && !c.ok).map((c) => c.label).join(" · ") || "physical motion is locked"
    : "";
  const lastMsg = st?.last_message ?? null;
  const fw = st?.firmware ?? {};

  useEffect(() => setDriver(r.driver), [r.driver]);

  const connect = async () => {
    setBusy(true);
    const res = await run(api.connect(driver, driver === "zeus" ? host : undefined));
    if (res?.ok) notify(res.message, "ok");
    setBusy(false);
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") void api.stop();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const caps = st?.capabilities;
  const tag = zeus ? (
    connected ? (
      <SourceTag source="live">Live · Zeus Car · {caps?.link_mode ?? "link"}</SourceTag>
    ) : link.state === "connecting" && link.attempt <= 1 ? (
      <SourceTag source="sim">Zeus · connecting</SourceTag>
    ) : (
      <SourceTag source="warn">Zeus disconnected</SourceTag>
    )
  ) : connected ? (
    <SourceTag source="sim">Simulated</SourceTag>
  ) : (
    <SourceTag source="off">Disconnected</SourceTag>
  );

  return (
    <div className="panel">
      <SectionHeader title="Robot" tag={tag}>
        <div className="flex">
          {(["mock", "zeus"] as const).map((d) => (
            <button
              key={d}
              className={`h-7 border px-2.5 text-[11px] font-semibold tracking-[0.1em] uppercase transition-colors [font-stretch:85%] first:rounded-l-[4px] last:rounded-r-[4px] [&+&]:-ml-px ${
                driver === d ? "relative z-10 border-ink-3 bg-raised text-ink" : "border-line-soft text-ink-3 hover:text-ink-2"
              }`}
              onClick={() => setDriver(d)}
            >
              {d === "mock" ? "Sim" : "Zeus"}
            </button>
          ))}
        </div>
      </SectionHeader>

      <div className="flex flex-col gap-4 px-4 pb-4">
        <div className="flex flex-wrap items-end gap-2">
          {driver === "zeus" ? (
            <div className="min-w-[150px] flex-1">
              <label className="label" htmlFor="robothost">
                Robot address
              </label>
              <input id="robothost" className="field num mt-2 !h-9 !text-[13px]" value={host} onChange={(e) => setHost(e.target.value)} placeholder="192.168.4.1" />
            </div>
          ) : null}
          {connected && r.driver === driver ? (
            <button className="btn" onClick={() => run(api.disconnect())}>
              <Link2Off size={15} /> Disconnect
            </button>
          ) : driver === "zeus" && link.supervising && !connected ? (
            <button className="btn" onClick={() => run(api.disconnect())} title="Stop retrying the Zeus link">
              <Link2Off size={15} /> Stop trying
            </button>
          ) : (
            <button className="btn btn-primary" onClick={connect} disabled={busy}>
              <Link2 size={15} /> {busy ? "Connecting…" : connected ? `Switch to ${driver === "mock" ? "sim" : "Zeus"}` : "Connect"}
            </button>
          )}
        </div>
        {driver === "zeus" && !connected ? (
          <div className="flex flex-col gap-1.5">
            {zeus && link.supervising ? (
              <div className="font-display text-[17px] font-bold tracking-[0.14em] text-danger">ZEUS DISCONNECTED</div>
            ) : null}
            <p className="text-[12.5px] leading-relaxed text-ink-3">
              Join the robot's <span className="num text-ink-2">Zeus_Car</span> Wi-Fi first. This laptop loses internet while joined, which is fine: everything runs locally.
              {zeus && link.supervising ? " LOCKED IN keeps retrying on its own and never falls back to the simulator." : ""}
            </p>
            {zeus && link.supervising ? (
              <p className="num text-[11.5px] text-ink-3">
                {link.target} · {link.state === "connecting" ? `connecting (attempt ${link.attempt})` : link.next_retry_in_s != null ? `retry in ${link.next_retry_in_s.toFixed(0)} s (attempt ${link.attempt})` : link.state}
              </p>
            ) : null}
          </div>
        ) : null}
        {(link.last_failure || st?.last_error) && !connected ? <p className="text-[12.5px] text-sim">{link.last_failure ?? st?.last_error}</p> : null}

        {zeus && connected ? (
          <div className="flex flex-col gap-1 rounded-[4px] border border-line-soft px-3 py-2.5 text-[12px]">
            <div className="flex flex-wrap items-baseline justify-between gap-x-3">
              <span className="label">Robot link</span>
              <span className="num text-ink-3">{st?.target}</span>
            </div>
            <div className="text-ink-2">
              {caps?.link_mode === "monitor" ? "Monitor link: receives and measures, sends no motor or stop frames." : "Control link."}{" "}
              <span className="text-ink-3">Camera firmware {fw.camera_firmware_generation ?? "unknown"}{fw.identified ? ` · ${fw.name ?? "Zeus_Car"}` : " · not identified"}.</span>
            </div>
            {lastMsg ? (
              <div className="num truncate text-[11px] text-ink-3" title={lastMsg.text}>
                last from robot ({lastMsg.kind}, {lastMsg.age_s.toFixed(1)} s ago): <span className="text-ink-2">{lastMsg.text}</span>
              </div>
            ) : (
              <div className="text-[11px] text-ink-4">nothing received yet</div>
            )}
          </div>
        ) : null}

        <div className="grid grid-cols-3 gap-x-4 gap-y-3">
          <Readout label="Link" value={connected ? st!.link_state : "—"} tone={st?.link_state === "degraded" ? "sim" : connected ? undefined : "dim"} />
          <Readout
            label={st?.rtt_kind === "ws_ping" ? "WS ping" : simulated ? "Latency" : "Echo"}
            value={st?.rtt_ms != null && connected ? `${st.rtt_ms.toFixed(0)} ms` : "—"}
            title={
              st?.rtt_kind === "ws_ping"
                ? "WebSocket ping round trip to the ESP32 (median); the Arduino is not involved"
                : simulated
                  ? "simulated"
                  : "time from a frame to the Arduino's next relayed reply"
            }
          />
          <Readout label="Last rx" value={connected && st?.last_rx_age_s != null ? `${st.last_rx_age_s.toFixed(1)} s` : "—"} />
          <Readout label="Motion" value={connected ? st!.motion : "—"} tone={st?.motion && st.motion !== "idle" ? "hit" : undefined} />
          {zeus ? (
            <Readout
              label="Arduino replies"
              value={connected ? `${fw.arduino_messages ?? 0}${fw.arduino_last_age_s != null ? ` · ${Number(fw.arduino_last_age_s).toFixed(0)}s ago` : ""}` : "—"}
              title="Replies the Arduino sent through the ESP32 (it answers every serial line). Proof the Arduino runs, not that wheels moved."
            />
          ) : (
            <Readout label="Battery" value="unavailable" tone="dim" title="Stock Zeus firmware does not report battery voltage" />
          )}
          <Readout
            label="Device stop"
            value={caps ? caps.device_watchdog : "—"}
            tone={caps?.device_watchdog === "verified" ? undefined : caps?.device_watchdog === "simulated" ? "sim" : "dim"}
            title="Measured stop-on-link-loss behaviour on the robot itself"
          />
        </div>

        <div className="rule" />

        <div className="flex flex-col gap-3">
          {zeus ? <GateCard gate={gate} connected={connected} powerLeftS={sf.motor_power_expires_in_s} onConfirmPower={() => run(api.motorPower())} /> : null}
          {zeus && connected ? (
            <div className="num text-[11px] text-ink-3">
              sensors: {fw.locked_in_patch ? "from LOCKED IN firmware patch" : "not reported by stock firmware (no ultrasonic, IR or battery values)"}
            </div>
          ) : null}
          <div className="flex flex-wrap items-center gap-x-6 gap-y-3" title={motionLocked ? `Locked: ${lockWhy}` : undefined}>
            <Toggle
              on={sf.armed}
              tone="sim"
              label={sf.armed ? "Armed" : motionLocked ? "Disarmed · locked" : "Disarmed"}
              disabled={!connected || sf.estop_latched || motionLocked}
              onChange={(v) => run(v ? api.arm() : api.disarm())}
            />
            <Toggle
              on={sf.autonomy_enabled}
              tone="sim"
              label="Autonomous retreat"
              disabled={!sf.armed || motionLocked}
              onChange={(v) => run(api.autonomy(v, clearPath))}
            />
          </div>
          {sf.estop_latched && r.hardware_stop ? <p className="text-[12px] leading-relaxed text-danger">{r.hardware_stop.summary}</p> : null}
          {connected && !simulated && !motionLocked ? (
            <label className="flex items-start gap-2.5 text-[12.5px] leading-snug text-ink-2">
              <input type="checkbox" className="mt-0.5 accent-[var(--color-sim)]" checked={clearPath} onChange={(e) => setClearPath(e.target.checked)} />
              <span>
                I checked the retreat path: robot on a clear floor area, nothing within 60 cm behind it, no table edge. <span className="text-ink-3">(valid 15 min)</span>
              </span>
            </label>
          ) : null}
          {sf.motion_suppressed_s != null ? (
            <div className="num text-[11.5px] text-sim">no motion for {Math.ceil(sf.motion_suppressed_s)}s · {sf.motion_suppressed_reason ?? "after a stop"}</div>
          ) : null}
          {sf.autonomous_cooldown_left_s > 0 ? <div className="num text-[11.5px] text-ink-3">autonomy cooldown {sf.autonomous_cooldown_left_s.toFixed(1)}s</div> : null}
          {Object.keys(sf.blocked_counts).length ? (
            <div className="num text-[11px] text-ink-4">
              blocked: {Object.entries(sf.blocked_counts).map(([k, v]) => `${k}×${v}`).join("  ")}
            </div>
          ) : null}
          {r.actions?.length ? <ActionLog actions={r.actions} /> : null}
        </div>

        <div className="grid grid-cols-[auto_1fr] items-start gap-4">
          <div className="grid grid-cols-3 gap-1.5" role="group" aria-label="Manual jog controls">
            {PAD.map((b, i) =>
              b ? (
                <button key={b.p} className="btn !h-11 !w-11 !px-0" disabled={!canMove} aria-label={b.label} title={`${b.label} (one bounded jog)`} onClick={() => run(api.move(b.p, testProfile))}>
                  {b.icon}
                </button>
              ) : i === 4 ? (
                <button key="stop" className="btn !h-11 !w-11 border-danger-2 !px-0 text-danger" onClick={() => run(api.stop())} aria-label="Stop" title="Stop (Esc)">
                  <Square size={15} fill="currentColor" />
                </button>
              ) : (
                <span key={i} />
              ),
            )}
          </div>
          <div className="flex min-w-0 flex-col gap-2">
            {motionLocked ? (
              <p className="text-[11.5px] leading-snug text-ink-3">
                Jog disabled ({gate.link_mode === "monitor" ? "LOCKED IN sends no motor commands on this link" : `needs: ${lockWhy}`}). Stop is always allowed.
              </p>
            ) : null}
            <button className="btn btn-sm justify-start" disabled={!r.displaced || !sf.armed} onClick={() => run(api.returnHome(), false)} title="Reverses the recorded motions. Approximate: no localisation.">
              <CornerUpLeft size={14} /> Return (approx.)
            </button>
            <button className="btn btn-sm justify-start" disabled={!connected} onClick={() => run(api.setHome(), false)}>
              <Home size={14} /> Set home here
            </button>
            <button className="btn btn-sm btn-hit justify-start" onClick={() => run(api.release())}>
              <Smartphone size={14} /> Release phone
            </button>
            <label className="mt-1 flex items-center gap-2 text-[12px] text-ink-3">
              <input type="checkbox" className="accent-[var(--color-sim)]" checked={testProfile} onChange={(e) => setTestProfile(e.target.checked)} />
              Low-speed test profile
            </label>
          </div>
        </div>

        {connected && simulated ? (
          <div className="overflow-hidden rounded-[4px] border border-line-soft">
            <RobotMap pose={r.telemetry?.pose ?? null} />
          </div>
        ) : connected ? (
          <div className="text-[12.5px] leading-relaxed text-ink-3">
            Position is not measured: stock firmware has no odometry.
            {r.displacement.length ? (
              <ul className="num mt-2 space-y-0.5 text-[11px] text-ink-2">
                {r.displacement.map((d, i) => (
                  <li key={i}>
                    {d.primitive} · {d.speed} · {d.ran_s}s ({d.origin})
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}

        {caps?.notes?.length ? (
          <details className="group text-[12px] text-ink-3">
            <summary className="cursor-pointer list-none text-ink-3 hover:text-ink-2">
              <span className="label">Hardware notes</span> <span className="text-ink-4 group-open:hidden">({caps.notes.length})</span>
            </summary>
            <ul className="mt-2 space-y-1.5 leading-snug">
              {caps.notes.map((n, i) => (
                <li key={i}>{n}</li>
              ))}
            </ul>
          </details>
        ) : null}
      </div>
    </div>
  );
}
