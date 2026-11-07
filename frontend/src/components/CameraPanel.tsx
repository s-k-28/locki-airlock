import { useEffect, useMemo, useRef, useState } from "react";
import { Camera, CameraOff, EyeOff, Eye, Crosshair, Check, X, RotateCcw } from "lucide-react";
import type { LockedInEvent, Snapshot } from "../types/api";
import { api } from "../services/api";
import { SectionHeader, SourceTag } from "./ui";
import { useToasts } from "./Toasts";

type Pt = [number, number];

const SOURCES: { v: string; label: string }[] = [
  { v: "zeus", label: "Zeus ESP32-CAM" },
  { v: "0", label: "Mac webcam" },
  { v: "1", label: "Camera 1" },
];

function sourceLabel(choice: string): string {
  const known = SOURCES.find((s) => s.v === choice);
  if (known) return known.v === "zeus" ? "ZEUS ESP32-CAM" : known.label.toUpperCase();
  return choice.startsWith("http") ? `STREAM ${choice.replace(/^https?:\/\//, "").split("/")[0]}` : choice;
}
const DEFAULT_ZONE: Pt[] = [
  [0.62, 0.55],
  [0.92, 0.55],
  [0.92, 0.95],
  [0.62, 0.95],
];

function ZoneEditor({ points, editing, visible, onChange }: { points: Pt[]; editing: boolean; visible: boolean; onChange: (p: Pt[]) => void }) {
  const svgRef = useRef<SVGSVGElement>(null);
  const drag = useRef<number | null>(null);

  const toNorm = (e: { clientX: number; clientY: number }): Pt => {
    const r = svgRef.current!.getBoundingClientRect();
    return [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))];
  };

  if (!visible && !editing) return null;
  const poly = points.map((p) => p.join(",")).join(" ");
  return (
    <svg
      ref={svgRef}
      className={`absolute inset-0 h-full w-full ${editing ? "cursor-crosshair" : "pointer-events-none"}`}
      viewBox="0 0 1 1"
      preserveAspectRatio="none"
      onPointerMove={(e) => {
        if (drag.current == null) return;
        const next = points.slice();
        next[drag.current] = toNorm(e);
        onChange(next);
      }}
      onPointerUp={() => (drag.current = null)}
      onDoubleClick={(e) => {
        if (!editing || points.length >= 16) return;
        const p = toNorm(e);
        // insert after the nearest vertex
        let best = 0;
        let bestD = Infinity;
        points.forEach((q, i) => {
          const d = (q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2;
          if (d < bestD) {
            bestD = d;
            best = i;
          }
        });
        const next = points.slice();
        next.splice(best + 1, 0, p);
        onChange(next);
      }}
    >
      <polygon points={poly} fill={editing ? "oklch(0.86 0.145 200 / 0.14)" : "oklch(0.86 0.145 200 / 0.06)"} stroke="var(--color-live)" strokeWidth={editing ? 2 : 1.5} vectorEffect="non-scaling-stroke" strokeDasharray={editing ? "0" : "6 5"} />
      {editing
        ? points.map((p, i) => (
            <rect
              key={i}
              x={p[0] - 0.013}
              y={p[1] - 0.0175}
              width={0.026}
              height={0.035}
              fill="var(--color-void)"
              stroke="var(--color-live)"
              strokeWidth={2}
              vectorEffect="non-scaling-stroke"
              className="cursor-grab"
              onPointerDown={(e) => {
                e.stopPropagation();
                (e.target as Element).setPointerCapture(e.pointerId);
                drag.current = i;
              }}
              onContextMenu={(e) => {
                e.preventDefault();
                if (points.length > 3) onChange(points.filter((_, j) => j !== i));
              }}
            />
          ))
        : null}
    </svg>
  );
}

function useReachBanner(events: LockedInEvent[], lastLive: LockedInEvent | null) {
  const [bannerFor, setBannerFor] = useState<LockedInEvent | null>(null);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (lastLive?.event_type !== "vision.phone_reach_confirmed") return;
    setBannerFor(lastLive);
    // Own timer: later events (decision, motion) must not cancel the hide.
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setBannerFor(null), 3200);
  }, [lastLive]);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const outcome = useMemo(() => {
    if (!bannerFor) return null;
    const after = events.filter((e) => e.seq > bannerFor.seq).slice(0, 40);
    const decision = after.find((e) => e.event_type === "coach.decision");
    if (!decision) return { text: "Deciding…", tone: "dim" as const };
    const rid = decision.payload.motion?.request_id;
    if (decision.payload.action === "retreat" && rid) {
      const sent = after.find((e) => e.event_type === "robot.motion_sent" && e.payload.request_id === rid);
      if (sent) return { text: `Retreat executed${sent.simulated ? " · simulated" : ""}`, tone: "hit" as const };
      const blocked = after.find((e) => e.event_type === "robot.motion_blocked" && e.payload.request_id === rid);
      if (blocked) return { text: `Motion blocked · ${String(blocked.payload.reason_code).replace(/_/g, " ")}`, tone: "sim" as const };
      return { text: "Retreat requested…", tone: "dim" as const };
    }
    if (decision.payload.action === "voice_only") return { text: "Voice only · robot not moving", tone: "ink" as const };
    return { text: "No intervention · " + (decision.payload.rules?.slice(-1)[0] ?? ""), tone: "dim" as const };
  }, [bannerFor, events]);

  return { bannerFor, outcome };
}

export function CameraPanel({ snap, events, lastLiveEvent }: { snap: Snapshot; events: LockedInEvent[]; lastLiveEvent: LockedInEvent | null }) {
  const { run, notify } = useToasts();
  const v = snap.vision;
  const live = v.running && v.camera === "live";
  const opening = v.running && v.camera === "opening";
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Pt[] | null>(null);
  const [streamKey, setStreamKey] = useState(0);
  const [source, setSource] = useState(v.choice ?? snap.settings.camera_source);
  useEffect(() => {
    if (v.choice) setSource(v.choice);
  }, [v.choice]);
  const boxRef = useRef<HTMLDivElement>(null);
  const followMode = snap.settings.phone_zone.mode === "marker";
  const tracking = v.tracking;
  // In follow mode the editor starts from where the zone is now, not where it was saved. The
  // robot-mounted camera has its own zone (its view of the space in front of the robot).
  const savedZone = v.mount === "robot" ? snap.settings.robot_camera_zone.points : snap.settings.phone_zone.points;
  const zone = (draft ?? ((followMode || v.mount === "robot") && v.zone ? v.zone : savedZone)) as Pt[];
  const { bannerFor, outcome } = useReachBanner(events, lastLiveEvent);
  const simulated = snap.mode.robot_simulated && !live;
  const label = v.source_label ?? sourceLabel(source);
  const robotCam = v.mount === "robot";
  const retrying = v.running && !live && !opening;
  const link = v.link ?? null;
  const size = v.frame_size ?? [640, 480];
  const stats = v.stats ?? {};

  useEffect(() => {
    if (live) setStreamKey((k) => k + 1);
  }, [live]);

  const start = async () => {
    const r = await run(api.visionStart(source));
    if (r?.ok) notify(r.message, "ok");
  };

  // Switching while running keeps the models loaded and never falls back to another camera.
  const pick = async (next: string) => {
    setSource(next);
    if (v.running) {
      const r = await run(api.visionStart(next));
      if (r?.ok) notify(r.message, "ok");
    }
  };
  const options = SOURCES.some((o) => o.v === source) ? SOURCES : [...SOURCES, { v: source, label: sourceLabel(source) }];

  const save = async () => {
    if (!draft) return setEditing(false);
    const r = followMode ? await run(api.attachZone(draft)) : await run(api.setZone(draft));
    if (r && r.ok !== false) {
      notify(followMode ? r.message : "Phone zone saved", "ok");
      setDraft(null);
      setEditing(false);
    }
  };

  const setMode = async (mode: "static" | "marker") => {
    if (mode === "static") {
      const r = await run(api.detachZone());
      if (r?.ok) notify(r.message, "ok");
      return;
    }
    // Attach the current zone to whichever robot markers the camera sees right now.
    const r = await run(api.attachZone());
    if (r?.ok) notify(r.message, "ok");
  };

  return (
    <div className="panel overflow-hidden">
      <SectionHeader
        title="Vision"
        tag={
          live ? (
            <SourceTag source="live">Live</SourceTag>
          ) : v.running ? (
            <SourceTag source="warn">{opening ? "Opening" : "Reconnecting"}</SourceTag>
          ) : simulated ? (
            <SourceTag source="sim">Simulated perception</SourceTag>
          ) : (
            <SourceTag source="off">Camera offline</SourceTag>
          )
        }
      >
        {editing ? (
          <>
            <button className="btn btn-sm btn-ghost" onClick={() => setDraft(DEFAULT_ZONE)} title="Reset to default rectangle">
              <RotateCcw size={13} />
            </button>
            <button className="btn btn-sm" onClick={() => (setDraft(null), setEditing(false))}>
              <X size={13} /> Cancel
            </button>
            <button className="btn btn-sm btn-primary" onClick={save}>
              <Check size={13} /> Save zone
            </button>
          </>
        ) : (
          <>
            <select
              aria-label="Camera source"
              className="field !h-7 !w-[150px] !py-0 !text-[12px]"
              value={source}
              onChange={(e) => void pick(e.target.value)}
              title={v.running ? "Switch camera now (models stay loaded)" : "Camera to start"}
            >
              {options.map((o) => (
                <option key={o.v} value={o.v}>
                  {o.label}
                </option>
              ))}
            </select>
            <button
              className="btn btn-sm btn-ghost"
              onClick={() => setEditing(true)}
              title={robotCam ? "Calibrate the robot camera's reach zone: the space in front of the robot where a reaching hand appears" : "Calibrate the phone zone: drag corners, double-click to add a point, right-click to remove"}
            >
              <Crosshair size={14} /> Zone
            </button>
            <button
              className="btn btn-sm btn-ghost !px-2"
              onClick={() => run(api.patchSettings({ show_video: !snap.settings.show_video }))}
              title={snap.settings.show_video ? "Hide video (landmarks only)" : "Show video"}
              aria-label="Toggle video privacy"
            >
              {snap.settings.show_video ? <Eye size={14} /> : <EyeOff size={14} />}
            </button>
            {v.running ? (
              <button className="btn btn-sm" onClick={() => run(api.visionStop())}>
                <CameraOff size={14} /> Stop
              </button>
            ) : (
              <button className="btn btn-sm btn-primary" onClick={start}>
                <Camera size={14} /> Start
              </button>
            )}
          </>
        )}
      </SectionHeader>

      <div ref={boxRef} className={`relative overflow-hidden border-y border-line-soft bg-well`} style={{ aspectRatio: `${size[0]} / ${size[1]}` }}>
        {live ? <img key={streamKey} src={`/api/vision/stream.mjpg?k=${streamKey}`} alt="Annotated camera view" className="absolute inset-0 h-full w-full object-contain" draggable={false} /> : null}
        {!live ? (
          <div className="absolute inset-0 flex flex-col justify-between p-5">
            <div>
              <div className={`font-display text-[22px] font-bold tracking-[0.14em] ${retrying ? "text-danger" : "text-ink-2"}`}>
                {!v.running ? "CAMERA OFFLINE" : opening && !link?.attempts ? `OPENING ${label}…` : `${label} DISCONNECTED`}
              </div>
              <p className="mt-2 max-w-[52ch] text-[13px] leading-relaxed text-ink-3">
                {sentence(link?.last_error ?? v.reason ?? "Vision is not running.")}
                {v.running && link?.next_retry_in_s != null ? ` Retrying in ${link.next_retry_in_s.toFixed(0)} s (attempt ${link.attempts + 1}).` : ""}
                {v.running && robotCam ? " Join the robot's Zeus_Car Wi-Fi; LOCKED IN keeps trying and never switches to another camera." : ""}
                {simulated ? " Simulation mode can still inject labelled reach events below." : ""}
              </p>
            </div>
            {!v.running ? (
              <div className="flex flex-wrap items-end gap-2">
                <div>
                  <label className="label" htmlFor="camsrc">
                    Source
                  </label>
                  <select id="camsrc" className="field mt-2 !h-9 !w-[230px]" value={source} onChange={(e) => setSource(e.target.value)}>
                    {options.map((o) => (
                      <option key={o.v} value={o.v}>
                        {o.label}
                        {o.v === "zeus" && v.zeus_camera_url ? ` (${v.zeus_camera_url.replace(/^http:\/\//, "")})` : ""}
                      </option>
                    ))}
                  </select>
                </div>
                <button className="btn btn-primary" onClick={start}>
                  <Camera size={15} /> Start camera
                </button>
              </div>
            ) : null}
          </div>
        ) : null}
        <ZoneEditor points={zone} editing={editing} visible={!live} onChange={setDraft} />
        {live && !editing ? (
          <div className="pointer-events-none absolute top-3 left-3 flex flex-col items-start gap-1">
            <span className="chip chip-live bg-void/80">
              {label}
              {v.frame_size ? ` · ${v.frame_size[0]}×${v.frame_size[1]}` : ""}
              {stats.capture_fps != null ? ` · ${Number(stats.capture_fps).toFixed(1)} fps` : ""}
            </span>
            {v.ego_motion_paused ? <span className="chip chip-sim bg-void/80">Reach paused · camera moving with the robot</span> : null}
          </div>
        ) : null}
        {live && (followMode || (tracking?.visible_markers?.length ?? 0) > 0) ? (
          <div className="pointer-events-none absolute top-3 right-3 flex flex-col items-end gap-1">
            {followMode ? <TrackingChip state={tracking?.state} markers={tracking?.markers ?? []} /> : (
              <span className="chip chip-dim bg-void/80">marker {tracking?.visible_markers?.join(", ")} in view</span>
            )}
          </div>
        ) : null}
        {bannerFor ? (
          <>
            <div className="flash-frame pointer-events-none absolute inset-0 border-2" />
            <div className="sweep pointer-events-none absolute right-0 left-0 h-[2px] bg-hit" style={{ ["--h" as any]: `${boxRef.current?.clientHeight ?? 400}px` }} />
            <div className="pop-in pointer-events-none absolute bottom-4 left-4 rounded-[4px] border border-hit/60 bg-void/90 px-4 py-3">
              <div className="font-display text-[19px] leading-none font-bold tracking-[0.12em] text-hit">PHONE REACH DETECTED</div>
              <div className="num mt-2 text-[12px] text-ink-2">
                confidence {bannerFor.confidence?.toFixed(2)}
                {bannerFor.simulated ? " · SIMULATED" : ""}
              </div>
              {outcome ? (
                <div
                  className={`mt-1.5 font-display text-[14px] font-semibold tracking-[0.1em] uppercase ${
                    outcome.tone === "hit" ? "text-ink" : outcome.tone === "sim" ? "text-sim" : "text-ink-3"
                  }`}
                >
                  {outcome.text}
                </div>
              ) : null}
            </div>
          </>
        ) : null}
        {editing ? (
          <div className="pointer-events-none absolute top-3 left-3 rounded-[3px] bg-void/85 px-2.5 py-1.5 text-[11.5px] text-ink-2">
            Drag corners · double-click adds a point · right-click removes
          </div>
        ) : null}
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-line-soft px-4 py-2.5">
        <span className="label">Phone zone</span>
        <div className="flex">
          {([
            ["static", "Fixed"],
            ["marker", "Follows robot"],
          ] as const).map(([m, label]) => (
            <button
              key={m}
              className={`h-7 border px-2.5 text-[11px] font-semibold tracking-[0.08em] uppercase transition-colors [font-stretch:85%] first:rounded-l-[4px] last:rounded-r-[4px] [&+&]:-ml-px ${
                (m === "marker") === followMode ? "relative z-10 border-ink-3 bg-raised text-ink" : "border-line-soft text-ink-3 hover:text-ink-2"
              }`}
              onClick={() => setMode(m)}
              disabled={(m === "marker") === followMode || (robotCam && m === "marker")}
              title={
                robotCam && m === "marker"
                  ? "This camera rides on the robot, so it cannot see the robot's markers; its zone is already robot-relative"
                  : m === "marker"
                    ? "Anchor the zone to the ArUco markers on the robot (marker must be in view)"
                    : "Keep the zone fixed in the camera image"
              }
            >
              {label}
            </button>
          ))}
        </div>
        {robotCam ? (
          <span className="text-[11px] text-ink-3">Robot-mounted camera: the zone is the space in front of the robot; markers are not used.</span>
        ) : followMode && tracking ? (
          <span className="num text-[11px] text-ink-3">
            {tracking.state === "HELD" && tracking.last_fix_age_s != null ? `holding last pose ${tracking.last_fix_age_s.toFixed(1)}s` : null}
            {tracking.state === "TRACKING" ? `marker ${tracking.markers.join(", ")}` : null}
            {tracking.state === "LOST" ? "zone unknown: reach detection paused" : null}
            {tracking.state === "SEARCHING" ? "looking for the robot's marker" : null}
          </span>
        ) : null}
        {robotCam ? null : (
          <>
            <a className="ml-auto text-[11.5px] text-ink-3 underline decoration-line underline-offset-4 hover:text-ink-2" href="/api/vision/marker.png?marker_id=7" target="_blank" rel="noreferrer">
              Print marker 7
            </a>
            <a className="text-[11.5px] text-ink-3 underline decoration-line underline-offset-4 hover:text-ink-2" href="/api/vision/marker.png?marker_id=8" target="_blank" rel="noreferrer">
              8
            </a>
          </>
        )}
      </div>

      <div className="grid grid-cols-3 gap-x-4 gap-y-3 px-4 py-3 sm:grid-cols-6">
        <Stat label="Camera FPS" value={live && stats.capture_fps != null ? Number(stats.capture_fps).toFixed(1) : "—"} />
        <Stat label="Processed" value={live ? `${Math.round(Number(stats.processing_fps ?? 0))} fps` : "—"} />
        <Stat label="Resolution" value={live && v.frame_size ? `${v.frame_size[0]}×${v.frame_size[1]}` : "—"} />
        <Stat label="Cap→result p95" value={live && stats.capture_to_result_ms_p95 != null ? `${Number(stats.capture_to_result_ms_p95).toFixed(0)} ms` : "—"} />
        <Stat label="Hands" value={live ? String(v.hands ?? 0) : "—"} />
        <Stat label="Face" value={live ? (v.focus?.face_present ? (v.focus.state ?? "").toLowerCase().replace("_", " ") : v.models?.face ? "none" : "no model") : "—"} />
      </div>
      {v.running && v.source_kind === "url" && link ? (
        <div className="num flex flex-wrap gap-x-4 gap-y-1 border-t border-line-soft px-4 py-2 text-[11px] text-ink-3" title="Measured from the stream itself. 'Delay above best' compares each frame's arrival with the camera's own timestamp (the clocks are not synchronised, so this is extra delay, not absolute latency).">
          <span>{v.backend === "http" ? "MJPEG reader" : "OpenCV FFmpeg"}</span>
          <span>camera clock {link.device_fps != null ? `${link.device_fps.toFixed(1)} fps` : "—"}</span>
          <span>{link.kbps != null ? `${link.kbps} kbps` : "— kbps"}{link.frame_kb != null ? ` · ${link.frame_kb} KB/frame` : ""}</span>
          <span>delay above best p50/p95 {link.delay_above_best_ms_p50 ?? "—"}/{link.delay_above_best_ms_p95 ?? "—"} ms</span>
          <span>lost in transit {link.missed}</span>
          <span>skipped (busy) {stats.frames_dropped ?? 0}</span>
          <span>corrupt {link.decode_errors}</span>
          <span>reconnects {link.reconnects}</span>
          {link.frame_age_ms != null ? <span>last frame {link.frame_age_ms} ms ago</span> : null}
        </div>
      ) : null}
    </div>
  );
}

function TrackingChip({ state, markers }: { state?: string; markers: number[] }) {
  const cls = state === "TRACKING" ? "chip-live" : state === "HELD" ? "chip-sim" : state === "LOST" ? "chip-danger" : "chip-dim";
  const text =
    state === "TRACKING" ? `Robot tracked · ${markers.join(", ")}` : state === "HELD" ? "Marker hidden · holding" : state === "LOST" ? "Robot marker lost" : "Searching for robot";
  return <span className={`chip ${cls} bg-void/80`}>{text}</span>;
}

function sentence(s: string): string {
  const t = s.trim();
  return t ? t.charAt(0).toUpperCase() + t.slice(1) + (/[.!?]$/.test(t) ? "" : ".") : t;
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <div className="label !text-[10px]">{label}</div>
      <div className="num mt-1 truncate text-[12.5px] text-ink-2">{value}</div>
    </div>
  );
}
