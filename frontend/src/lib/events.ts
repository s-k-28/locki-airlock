import type { LockedInEvent } from "../types/api";

export type Tone = "live" | "hit" | "sim" | "danger" | "dim" | "ink";
export type Group = "session" | "vision" | "robot" | "voice" | "safety" | "coach" | "system";

export interface Described {
  title: string;
  detail?: string;
  tone: Tone;
  group: Group;
  hidden?: boolean;
}

const conf = (e: LockedInEvent) => (e.confidence == null ? "" : ` · ${e.confidence.toFixed(2)}`);

export function describe(e: LockedInEvent): Described {
  const p = e.payload ?? {};
  switch (e.event_type) {
    case "session.started":
      return { title: `Session started · ${p.task}`, detail: `${Math.round(p.planned_s / 60)} min${p.target_units ? ` · target ${p.target_units} ${p.unit_label}` : ""} · via ${p.origin}`, tone: "live", group: "session" };
    case "session.paused":
      return { title: "Session paused", detail: `via ${p.origin}`, tone: "sim", group: "session" };
    case "session.resumed":
      return { title: "Session resumed", detail: `via ${p.origin}`, tone: "live", group: "session" };
    case "session.ended":
      return { title: `Session ended · ${p.reason}`, detail: p.metrics ? `${Math.round((p.metrics.elapsed_active_s ?? 0) / 60)} min · ${p.metrics.reach_confirmed ?? 0} reaches` : undefined, tone: "ink", group: "session" };
    case "session.progress":
      return { title: `Progress +${p.units}`, detail: `${p.total}${p.target ? ` / ${p.target}` : ""} ${p.unit_label}`, tone: "live", group: "session" };
    case "session.milestone":
      return { title: `Milestone · ${String(p.milestone).replace("_", " ")}`, tone: "dim", group: "session" };
    case "vision.hand_approaching":
      return { title: `Hand approaching phone zone${conf(e)}`, detail: `distance ${p.distance} · speed ${p.speed}`, tone: "sim", group: "vision" };
    case "vision.phone_reach_confirmed":
      return { title: `PHONE REACH CONFIRMED${conf(e)}`, detail: `dwell ${p.dwell_ms} ms${p.approach_seen ? " · approach seen" : " · static"}${p.frame_to_event_ms != null ? ` · frame→event ${p.frame_to_event_ms} ms` : ""}`, tone: "hit", group: "vision" };
    case "vision.reach_cleared":
      return { title: "Possible reach cleared", detail: p.reason ? String(p.reason).replace(/_/g, " ") : undefined, tone: "dim", group: "vision" };
    case "vision.reach_suppressed":
      return { title: `Reach suppressed (cooldown)${conf(e)}`, detail: p.cooldown_left_s != null ? `${p.cooldown_left_s}s left` : undefined, tone: "dim", group: "vision" };
    case "vision.focus_changed":
      return { title: `Focus · ${String(p.to).replace(/_/g, " ").toLowerCase()}`, detail: p.reason, tone: p.to === "FOCUSED" ? "live" : p.to === "AWAY" ? "dim" : "sim", group: "vision" };
    case "vision.zone_tracking":
      return {
        title: `Robot marker · ${String(p.to).toLowerCase()}`,
        detail: p.reason,
        tone: p.to === "LOST" ? "danger" : p.to === "HELD" ? "sim" : p.to === "TRACKING" ? "live" : "dim",
        group: "vision",
      };
    case "vision.zone_attached":
      return { title: "Phone zone now follows the robot", detail: `marker${p.markers?.length > 1 ? "s" : ""} ${(p.markers ?? []).join(", ")}`, tone: "live", group: "vision" };
    case "vision.camera_connected":
      return { title: "Camera live", detail: p.source ?? p.reason, tone: "live", group: "vision" };
    case "vision.camera_disconnected":
      return { title: "CAMERA DISCONNECTED", detail: p.reason, tone: "danger", group: "vision" };
    case "vision.started":
      return { title: `Vision started · ${p.label ?? p.source}`, detail: p.label ? `${p.source}${p.mount === "robot" ? " · robot-mounted" : ""}` : undefined, tone: "live", group: "vision" };
    case "vision.stopped":
      return { title: "Vision stopped", tone: "dim", group: "vision" };
    case "vision.hand_detected":
    case "vision.hand_lost":
    case "vision.face_present":
    case "vision.face_absent":
      return { title: e.event_type, tone: "dim", group: "vision", hidden: true };
    case "robot.connecting":
      return { title: `Connecting to ${p.driver}`, detail: p.target, tone: "dim", group: "robot" };
    case "robot.connected":
      return { title: `Robot connected · ${p.driver}`, detail: p.firmware?.camera_firmware_generation ? `ESP32 firmware ${p.firmware.camera_firmware_generation}` : p.target, tone: "live", group: "robot" };
    case "robot.disconnected":
      return { title: "Robot disconnected", detail: p.reason, tone: "danger", group: "robot" };
    case "robot.connection_failed":
      return { title: p.retrying ? "Zeus not reachable · retrying" : "Robot connection failed", detail: `${p.target ?? ""} ${p.reason ?? ""}`.trim(), tone: "danger", group: "robot" };
    case "robot.link_degraded":
      return { title: "Robot link degraded", detail: `no data for ${p.rx_gap_s}s`, tone: "sim", group: "robot" };
    case "robot.motion_requested":
      return { title: `Motion requested · ${p.primitive}`, detail: `${p.origin}${p.speed != null ? ` · speed ${p.speed}` : ""}${p.duration_s != null ? ` · ${p.duration_s}s` : ""} · ${p.profile}`, tone: "ink", group: "robot" };
    case "robot.motion_blocked":
      return { title: `Motion blocked · ${p.reason_code}`, detail: p.reason, tone: "sim", group: "safety" };
    case "robot.motion_sent":
      return { title: `Motion sent · ${p.primitive} ${p.speed}`, detail: `${p.duration_s}s · ${p.detail ?? ""}`, tone: "hit", group: "robot" };
    case "robot.motion_acknowledged":
      return { title: `Acknowledged · ${p.ack}`, detail: p.latency_ms != null ? `${p.latency_ms} ms after send` : p.note, tone: "ink", group: "robot" };
    case "robot.motion_completed":
      return { title: "Motion window complete", detail: "host-side: stop sent after the commanded window", tone: "ink", group: "robot" };
    case "robot.motion_confirmed":
      return { title: "Pose change confirmed", detail: p.note, tone: "ink", group: "robot" };
    case "robot.motion_aborted":
      return { title: `Motion aborted · ${p.outcome}`, detail: p.detail ?? (p.ran_s != null ? `ran ${p.ran_s}s` : undefined), tone: "sim", group: "robot" };
    case "robot.stop_requested":
      return { title: "Stop requested", detail: p.reason, tone: "ink", group: "safety" };
    case "robot.stop_sent":
      return {
        title: p.sent ? "Stop sent" : String(p.detail ?? "").startsWith("monitor link") ? "Software stop · no frame sent" : "Stop not sent",
        detail: p.detail,
        tone: p.sent ? "ink" : "sim",
        group: "safety",
      };
    case "robot.device_watchdog_tripped":
      return { title: "Robot firmware stopped the motors", detail: p.during_link_test ? "watchdog trip during the link test" : p.during_motion ? "link went silent mid-motion; host ended the motion" : "watchdog trip reported", tone: "sim", group: "safety" };
    case "robot.link_test_started":
      return { title: `R5 link test · ${String(p.setup).replace(/_/g, " ")}`, detail: `${p.primitive} ${p.speed} · freeze ${p.freeze_s}s after ${p.lead_s}s`, tone: "sim", group: "safety" };
    case "robot.link_frozen":
      return { title: "LINK FROZEN · transmit paused", detail: `${p.freeze_s}s with the socket open`, tone: "danger", group: "safety" };
    case "robot.link_test_finished":
      return { title: `R5 link test ${p.outcome}`, detail: p.device_watchdog?.summary, tone: p.outcome === "completed" ? "ink" : "sim", group: "safety" };
    case "robot.firmware_detected":
      return { title: "Firmware detected", detail: JSON.stringify(p.firmware).slice(0, 120), tone: "dim", group: "robot", hidden: true };
    case "safety.armed":
      return { title: "ARMED", detail: `by ${p.source}`, tone: "sim", group: "safety" };
    case "safety.disarmed":
      return { title: "Disarmed", detail: p.reason, tone: "ink", group: "safety" };
    case "safety.autonomy_changed":
      return { title: p.enabled ? "Autonomous retreat enabled" : "Autonomous retreat disabled", detail: p.clear_path_confirmed ? "operator confirmed clear path" : undefined, tone: p.enabled ? "sim" : "ink", group: "safety" };
    case "safety.emergency_stop":
      return { title: "EMERGENCY STOP", detail: p.reason, tone: "danger", group: "safety" };
    case "safety.reset":
      return { title: "E-stop reset", detail: "robot remains disarmed", tone: "ink", group: "safety" };
    case "safety.watchdog_fired":
      return { title: "Watchdog stopped the robot", detail: p.why, tone: "danger", group: "safety" };
    case "safety.loop_stall":
      return { title: "Event loop stall", detail: `${p.lag_ms} ms`, tone: "sim", group: "safety" };
    case "voice.transcribed":
      return { title: `${p.source === "typed" ? "Typed" : "Heard"}: “${p.text}”`, detail: p.latency_ms != null ? `STT ${Math.round(p.latency_ms)} ms` : undefined, tone: "ink", group: "voice" };
    case "voice.command_recognized":
      return { title: `Command · ${String(p.intent).replace(/_/g, " ")}${conf(e)}`, detail: p.slots && Object.keys(p.slots).length ? JSON.stringify(p.slots) : undefined, tone: "live", group: "voice" };
    case "voice.command_rejected":
      return { title: "Command rejected", detail: p.reason, tone: "dim", group: "voice" };
    case "voice.response_generated":
      return { title: `LOCKED IN: “${p.text}”`, tone: "ink", group: "voice" };
    case "coach.intervention_generated":
      return { title: `LOCKED IN: “${p.text}”`, detail: `intervention · ${p.category}`, tone: "hit", group: "coach" };
    case "coach.decision":
      return { title: `Decision · ${String(p.action).replace("_", " ")}`, detail: p.explanation, tone: p.action === "retreat" ? "hit" : "ink", group: "coach" };
    case "coach.adaptation":
      return { title: `Adapted ${p.param}`, detail: `${p.old} → ${p.new} · ${p.rule} ${p.reason}`, tone: "sim", group: "coach" };
    case "voice.tts_started":
    case "voice.tts_finished":
      return { title: e.event_type, tone: "dim", group: "voice", hidden: true };
    case "robot.expression":
      return { title: `Expression · ${p.label}`, detail: p.hardware, tone: p.color === "danger" ? "danger" : p.color === "hit" ? "hit" : "dim", group: "robot", hidden: true };
    case "voice.listening":
      return { title: "Microphone listening", detail: p.mode, tone: "live", group: "voice" };
    case "voice.stopped":
      return { title: "Microphone off", tone: "dim", group: "voice" };
    case "voice.error":
      return { title: "Voice error", detail: p.error ?? p.message, tone: "danger", group: "voice" };
    case "voice.speech_started":
    case "voice.speech_ended":
      return { title: e.event_type, tone: "dim", group: "voice", hidden: true };
    case "state.changed":
      return { title: `${p.from} → ${p.to}`, detail: p.trigger, tone: "dim", group: "system" };
    case "state.transition_rejected":
      return { title: `Rejected ${p.trigger} in ${p.state}`, detail: p.reason, tone: "dim", group: "system", hidden: true };
    case "system.mode_changed":
      return {
        title: p.robot_profile === "zeus" ? p.label : `Mode ${p.code} · ${p.label}`,
        detail: p.camera_label ? `camera ${p.camera_label}` : undefined,
        tone: p.robot_profile === "zeus" ? (p.robot_state === "connected" ? "live" : "danger") : p.robot_simulated ? "sim" : "live",
        group: "system",
      };
    case "system.started":
      return { title: "Backend started", detail: `v${p.version} · driver ${p.driver}`, tone: "dim", group: "system" };
    case "settings.changed":
      return { title: "Settings changed", detail: Object.keys(p.patch ?? {}).join(", "), tone: "dim", group: "system" };
    case "demo.step":
      return { title: `Demo · ${p.step}`, detail: p.detail || undefined, tone: "sim", group: "system" };
    case "experiment.trial_started":
      return { title: `Trial started · ${p.kind}`, tone: "sim", group: "system" };
    case "experiment.trial_ended":
      return { title: `Trial ${p.outcome}`, detail: `${p.kind} · ${p.duration_s}s`, tone: "sim", group: "system" };
    case "experiment.voice_trial":
      return {
        title: `Voice trial · ${String(p.outcome).replace(/_/g, " ")}`,
        detail: `${p.window === "quiet" ? "quiet window" : `“${p.phrase}”`} → heard “${p.heard_text ?? ""}” (${p.heard_intent})`,
        tone: p.outcome === "correct" || p.outcome === "correct_reject" ? "live" : p.outcome === "rejected" ? "sim" : "danger",
        group: "system",
      };
    case "experiment.label":
      return { title: `Labelled ${p.label}`, tone: "sim", group: "system" };
    default:
      return { title: e.event_type, tone: "dim", group: "system" };
  }
}
