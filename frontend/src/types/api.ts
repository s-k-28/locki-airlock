// Mirrors backend/app/runtime.py snapshot() and telemetry/events.py. Keep in sync.

export type StateName =
  | "DISCONNECTED"
  | "IDLE"
  | "SESSION_ACTIVE"
  | "POSSIBLE_PHONE_REACH"
  | "PHONE_REACH_CONFIRMED"
  | "RETREATING"
  | "HOLDING"
  | "RETURNING"
  | "PAUSED"
  | "RELEASED"
  | "EMERGENCY_STOP";

export type FocusState = "FOCUSED" | "POSSIBLY_DISTRACTED" | "AWAY" | "UNKNOWN";
export type Primitive = "forward" | "backward" | "strafe_left" | "strafe_right" | "rotate_left" | "rotate_right" | "stop";

export interface LockedInEvent {
  event_id: string;
  seq: number;
  timestamp: number;
  mono: number;
  event_type: string;
  source: string;
  session_id: string | null;
  payload: Record<string, any>;
  confidence: number | null;
  simulated: boolean;
}

export interface SessionSnap {
  session_id: string;
  task: string;
  planned_duration_s: number;
  target_units: number | null;
  unit_label: string;
  completed_units: number;
  pause_count: number;
  paused: boolean;
  elapsed_active_s: number;
  remaining_s: number;
  started_at_wall: number;
  progress_log: { at_active_s: number; units: number; total: number }[];
}

export interface Metrics {
  elapsed_active_s: number;
  remaining_s: number;
  planned_s: number;
  progress_fraction: number;
  completed_units: number;
  target_units: number | null;
  presence_s: number;
  focused_s: number;
  possibly_distracted_s: number;
  away_s: number;
  unknown_s: number;
  observed_fraction: number;
  presence_ratio: number | null;
  vision_available: boolean;
  reach_suspected: number;
  reach_confirmed: number;
  reach_suppressed: number;
  interventions: number;
  voice_commands: number;
  robot_moves: number;
  motions_blocked: number;
  pauses: number;
}

export interface Capabilities {
  simulated: boolean;
  obstacle_sensing: boolean;
  battery_sensing: boolean;
  device_watchdog: "verified" | "unverified" | "simulated" | "none";
  device_watchdog_timeout_s: number | null;
  motion_ack: string;
  camera_stream_url: string | null;
  /** False on the real robot until the hardware motion gate opens. */
  motion_permitted: boolean;
  motion_lock_reasons: string[];
  link_mode: "simulated" | "monitor" | "control";
  notes: string[];
}

export interface RobotStatus {
  transport: string;
  simulated: boolean;
  connected: boolean;
  link_state: string;
  target: string | null;
  last_error: string | null;
  connected_since: number | null;
  last_rx_age_s: number | null;
  last_tx_age_s: number | null;
  rtt_ms: number | null;
  rtt_kind: "arduino_echo" | "ws_ping" | "simulated" | null;
  frames_sent: number;
  frames_received: number;
  motion: string;
  battery_v: number | null;
  firmware: Record<string, any>;
  last_message: { kind: string; text: string; at: number; age_s: number } | null;
  message_counts: Record<string, number>;
  capabilities: Capabilities;
}

export interface RobotLink {
  state: "idle" | "connecting" | "connected" | "retry_wait" | "error";
  attempt: number;
  target: string | null;
  last_failure: string | null;
  last_failure_at?: number;
  connected_at?: number;
  next_retry_in_s: number | null;
  profile: "sim" | "zeus";
  supervising: boolean;
}

export interface GateCheck {
  id: string;
  ok: boolean | null;
  label: string;
  detail: string;
  blocking: boolean;
}

export interface HardwareGate {
  robot: "simulated" | "zeus";
  motion_permitted: boolean;
  motor_commands: string;
  effective?: "disabled" | "bench" | "enabled";
  link_mode?: string | null;
  allowed_profiles?: string[] | null;
  checks: GateCheck[];
  /** Real robot: what autonomous retreat needs on top of the motion gate. */
  autonomy_permitted?: boolean;
  autonomy_checks?: (Omit<GateCheck, "blocking"> & { blocking?: boolean })[];
}

/** One robot action as it went through the safety layer (newest first in the snapshot). */
export interface RobotAction {
  id: string;
  at: number;
  kind: "motion" | "stop";
  what: string;
  origin: string | null;
  profile?: string | null;
  status: string;
  /** True only when frames went to the real robot; never wheel feedback. */
  physical: boolean;
  detail: string;
  reason_code?: string;
  ack?: string;
}

/** One utterance end to end: heard -> recognised -> acted on -> answered -> audible. */
export interface VoiceTurn {
  id: string;
  at: number;
  source: string;
  text: string;
  stt_confidence: number;
  stt_ms: number | null;
  during_playback: boolean;
  intent: { intent: string; confidence: number; rule: string | null; conflict?: string | null; moves_robot?: boolean; safety_critical?: boolean } | null;
  replies: { category: string; text: string; cue: string | null }[];
  reply_source: string | null;
  /** End of the user's speech -> the reply started playing (backend clock). */
  reply_to_audio_ms: number | null;
  output_latency_ms?: number | null;
  echo_removed?: number;
  ok?: boolean;
  action?: string;
  handled_ms?: number;
}

export interface AudioOutputStatus {
  device: string | null;
  label: string;
  listed: boolean | null;
  devices: string[] | null;
  system_volume: { volume: number | null; muted: boolean; checked_at: number; device: string | null; applies: boolean } | null;
  warning: string | null;
  error: string | null;
  last_source: string | null;
  last_outcome: string | null;
  last_at: number | null;
  last_output_latency_ms: number | null;
}

export interface CameraLink {
  connects: number;
  reconnects: number;
  attempts: number;
  next_retry_in_s: number | null;
  last_error: string | null;
  frame_age_ms: number | null;
  device_fps: number | null;
  missed: number;
  decode_errors: number;
  kbps: number | null;
  frame_kb: number | null;
  delay_above_best_ms_p50: number | null;
  delay_above_best_ms_p95: number | null;
}

export interface Telemetry {
  timestamp: number;
  ultrasonic_cm: number | null;
  ir_left_blocked: boolean | null;
  ir_right_blocked: boolean | null;
  battery_v: number | null;
  pose: { x: number; y: number; theta: number } | null;
  // Only from the optional firmware patch (firmware/optional):
  firmware_patch: number | null;
  device_mode: number | null;
  device_watchdog_trips: number | null;
  device_watchdog_latched: boolean | null;
  device_free_ram: number | null;
}

export type PersonalityMode = "chill" | "roast" | "unhinged" | "judge_demo";
export type Profanity = "off" | "mild" | "strong";

export interface PersonalitySnap {
  mode: PersonalityMode;
  effective_mode: PersonalityMode;
  label: string;
  unhinged_opt_in: boolean;
  profanity: Profanity;
  max_profanity: Profanity;
  tone: "normal" | "gentle" | "serious";
  supportive_s_left: number;
  pool: string;
  insults_allowed: boolean;
}

export interface VoiceProfile {
  provider: "system" | "fish_audio" | "none";
  reference_id: string;
  model: string;
  fish: { latency: "low" | "normal" | "balanced"; speed: number; temperature: number; top_p: number; normalize: boolean; cues: boolean };
  system_voice: string | null;
  system_rate_wpm: number;
  volume: number;
  cache_behavior: "cache_only" | "cache_then_live";
  daily_live_bytes: number;
}

export interface VoicePreset {
  id: string;
  name: string;
  profile: VoiceProfile;
  personality: PersonalityMode;
  profanity: Profanity;
  output_device: string | null;
  builtin: boolean;
}

export interface CloudVoiceInfo {
  id: string;
  title: string;
  description: string;
  languages: string[];
  tags: string[];
  visibility: string | null;
  author: string | null;
  sample_url: string | null;
}

export interface CloudVoiceSnap {
  provider: string;
  configured: boolean;
  status: { state: string; detail: string; checked_at: number | null; voice: CloudVoiceInfo | null };
  generated: number;
  failures: number;
  last_latency_ms: number | null;
  usage_today_bytes: Record<string, number>;
  cache_hits: number;
  cache_files: number;
}

export interface CacheJobState {
  state: "running" | "finished" | "stopped" | "cancelled";
  total: number;
  done: number;
  failed: number;
  bytes: number;
  error: string | null;
}

export interface SpeechHistoryItem {
  at: number;
  text: string;
  category: string;
  priority: string;
  source: string;
  source_label: string;
  outcome: string;
  detail?: string;
  voice?: string;
  queued_ms?: number;
  first_audio_ms?: number;
  duration_ms?: number;
}

export interface ExpressionSnap {
  name: string;
  label: string;
  color: string;
  pattern: string;
  until: number | null;
  hardware: string;
}

export interface VoiceConfig {
  profile: VoiceProfile;
  active_preset: string | null;
  presets: VoicePreset[];
  personality: PersonalitySnap;
  fish: CloudVoiceSnap & {
    models: { id: string; label: string; usd_per_million_bytes: number; bracket_cues: boolean; note: string }[];
    daily_live_bytes: number;
    live_bytes_today: number;
  };
  cache: { lines: number; cached: number; missing: number; pack: { lines: number; cached: number }; job: CacheJobState | null };
}

export interface VoicePromptDef {
  phrase: string;
  expected: string;
  slots?: Record<string, number | string>;
}

export interface VoiceTrialRecord {
  block_id: string;
  speaker: string;
  condition: string;
  at: number;
  window: "prompt" | "quiet";
  phrase: string | null;
  expected: string;
  heard_text: string | null;
  heard_intent: string | null;
  outcome: "correct" | "wrong_intent" | "wrong_slots" | "rejected" | "no_response" | "false_activation" | "correct_reject";
  rejected_reason?: string | null;
  stt_ms: number | null;
}

export interface VoiceTrialGroup {
  speaker: string;
  condition: string;
  prompts: number;
  correct: number;
  accuracy: number | null;
  wrong_intent_rate: number | null;
  rejection_rate: number | null;
  stop_prompts: number;
  stop_recognised: number | null;
  quiet_minutes: number;
  quiet_false_activations: number;
  false_activations_per_10_min: number | null;
  near_miss_false_activations: number;
  risky_errors: number;
  stt_ms_p50: number | null;
}

export interface VoiceTrialsSnap {
  active: boolean;
  block: { block_id: string; speaker: string; condition: string; execute: boolean; prompt: VoicePromptDef | null; quiet_left_s: number | null } | null;
  recent: VoiceTrialRecord[];
  groups: VoiceTrialGroup[];
}

export interface LinkTestResult {
  test_id: string;
  at: number;
  simulated: boolean;
  transport: string;
  setup: "wheels_off_ground" | "on_floor";
  primitive: string;
  speed: number;
  lead_s: number;
  freeze_s: number;
  outcome: "completed" | "interrupted" | "link_closed" | "not_sent" | "unsupported";
  frozen_for_s: number | null;
  frames_written_during_freeze: number | null;
  link_closed_after_s: number | null;
  stop_sent_after_freeze_s: number | null;
  device_watchdog: { source: string; tripped: boolean | null; summary: string; wheels_stopped_after_s?: number; trips_before?: number | null; trips_after?: number | null };
  notes: string[];
  summary: string;
}

export interface Snapshot {
  server_time: number;
  run_id: string;
  version: string;
  uptime_s: number;
  mode: {
    code: string;
    label: string;
    robot_profile: "sim" | "zeus";
    robot_state: "connected" | "connecting" | "disconnected";
    robot_simulated: boolean;
    robot_connected: boolean;
    camera_live: boolean;
    camera_label: string | null;
    physical_motion: "simulated" | "locked" | "permitted";
    offline: boolean;
  };
  state: { name: StateName; since_s: number; history: { at: number; trigger: string; from_state: string; to_state: string; reason: string }[] };
  session: SessionSnap | null;
  last_session: { session_id: string; task: string; end_reason: string } | null;
  metrics: Partial<Metrics>;
  focus: { state: FocusState; since_s: number; durations: Record<FocusState, number> };
  robot: {
    driver: "mock" | "zeus";
    profile: "sim" | "zeus";
    link: RobotLink;
    gate: HardwareGate;
    hardware_stop: { kind: "no_robot" | "simulated" | "software_only" | "stop_frame_sent"; summary: string } | null;
    status: RobotStatus | null;
    telemetry: Telemetry | null;
    displacement: { primitive: string; speed: number; ran_s: number; origin: string }[];
    displaced: boolean;
    host: string;
    link_test_running: boolean;
    link_tests: LinkTestResult[];
    actions: RobotAction[];
  };
  safety: {
    armed: boolean;
    autonomy_enabled: boolean;
    clear_path_confirmed: boolean;
    clear_path_expires_in_s: number | null;
    motor_power_checked: boolean;
    motor_power_expires_in_s: number | null;
    motion_suppressed_s: number | null;
    motion_suppressed_reason?: string | null;
    estop_latched: boolean;
    estop_reason: string | null;
    autonomous_cooldown_left_s: number;
    active_motion: (Record<string, any> & { elapsed_s: number }) | null;
    approved_count: number;
    blocked_counts: Record<string, number>;
    watchdog_fired: number;
    loop_max_lag_ms: number;
  };
  vision: {
    running: boolean;
    camera: string;
    reason: string | null;
    source?: string | null;
    source_label?: string | null;
    source_kind?: string | null;
    backend?: string | null;
    mount?: "fixed" | "robot";
    mirror?: boolean | null;
    flip?: string | null;
    frame_size?: [number, number] | null;
    link?: CameraLink | null;
    choice?: string;
    zeus_camera_url?: string;
    ego_motion_paused?: boolean;
    zone_mode_setting?: "static" | "marker";
    stats?: Record<string, number | null>;
    hands?: number;
    detector?: { tracks: any[]; stats: Record<string, number>; cooldown_left_s: number | null };
    focus?: { state: FocusState; presence_p: number; face_present: boolean; yaw_ratio: number | null };
    last_reach?: Record<string, any> | null;
    models?: { hands: boolean; face: boolean; error: string | null };
    zone?: [number, number][] | null;
    zone_mode?: "static" | "marker";
    tracking?: {
      state: "DISABLED" | "SEARCHING" | "TRACKING" | "HELD" | "LOST";
      markers: number[];
      visible_markers: number[];
      anchored_markers: number[];
      last_fix_age_s: number | null;
      moved_since_fix: boolean;
      marker_ms_p50: number | null;
      dictionary: string;
      ids: number[];
      stats: Record<string, number>;
    };
    tuning: Record<string, any>;
  };
  voice: {
    tts: {
      engine: string;
      available: boolean;
      muted: boolean;
      speaking: boolean;
      spoken: number;
      voice?: string | null;
      provider?: string;
      current?: { text: string; priority: string } | null;
      audio_error?: string | null;
      last_fallback?: string | null;
      history?: SpeechHistoryItem[];
    };
    personality: PersonalitySnap;
    profile: VoiceProfile;
    active_preset: string | null;
    cloud: CloudVoiceSnap;
    cache_job: CacheJobState | null;
    input: { running: boolean; reason?: string; mode?: string; device?: string | null; stt?: Record<string, any>; ptt_active?: boolean; listening?: boolean };
    last_utterance: { text: string; source: string; confidence: number; at: number; intent: string } | null;
    last_reply: { text: string; category: string; at: number } | null;
    llm: Record<string, any> | null;
    state: "off" | "ready" | "hearing" | "transcribing" | "thinking" | "speaking";
    turns: VoiceTurn[];
    output: AudioOutputStatus;
  };
  expression: ExpressionSnap | null;
  mind?: MindSnap;
  coach: {
    personality: PersonalityMode;
    intensity: "gentle" | "standard" | "strict";
    muted: boolean;
    adaptation: Record<string, any>;
    last_decision: (Record<string, any> & { action: string; category: string | null; rules: string[]; explanation: string; at: number }) | null;
    cooldown_s: number;
  };
  experiment: { active: boolean; summary: Record<string, any>; voice: VoiceTrialsSnap };
  settings: RuntimeSettings;
  perf: Record<string, { n: number; mean: number; p50: number; p95: number; max: number; last: number }>;
  demo_running: boolean;
  offline: { enabled: boolean; fish_audio: string; llm: string; fish_refusals: number };
}

export interface RuntimeSettings {
  camera_source: string;
  camera_mount: "auto" | "fixed" | "robot";
  zeus_camera_flip: "none" | "horizontal" | "vertical" | "both";
  robot_camera_zone: { points: [number, number][]; mode: "static" | "marker"; anchor: Record<string, any> | null };
  vision_enabled: boolean;
  show_video: boolean;
  phone_zone: { points: [number, number][]; mode: "static" | "marker"; anchor: Record<string, any> | null };
  vision: Record<string, number>;
  motion: {
    retreat_primitive: "backward" | "forward" | "strafe_left" | "strafe_right";
    retreat_speed: number;
    retreat_duration_s: number;
    manual_speed: number;
    manual_duration_s: number;
    autonomous_cooldown_s: number;
  };
  voice: {
    listen_mode: "push_to_talk" | "wake_word" | "always";
    input_device: string | number | null;
    output_device: string | null;
    min_command_confidence: number;
    min_motion_command_confidence: number;
    muted: boolean;
  };
  coach: {
    personality: PersonalityMode;
    profanity: Profanity;
    tone: "normal" | "gentle" | "serious";
    unhinged_opt_in: boolean;
    intensity: "gentle" | "standard" | "strict";
    min_seconds_between_interventions: number;
    announce_milestones: boolean;
  };
  conversation?: { generated_replies: boolean; proactive_generated: boolean; anticipate: boolean };
  voice_profile: VoiceProfile;
  active_preset: string | null;
  robot_host: string | null;
  default_session_minutes: number;
}

export interface CommandResult {
  ok: boolean;
  message: string;
  state: string;
  reply: string | null;
  data: Record<string, any>;
}

/** Milestone 5: the companion layer (backend app/intelligence/companion.py). */
export interface MindObservation {
  kind: string;
  text: string;
  at: number;
  age_s: number;
  data?: Record<string, unknown>;
}

export interface MindTurn {
  role: "user" | "robot";
  text: string;
  source: string;
  proactive: boolean;
  at: number;
  age_s: number;
}

export interface BehaviorDecision {
  id: string;
  event: string;
  speak: boolean;
  urgency: string;
  reason: string;
  rules: string[];
  category: string | null;
  generate: boolean;
  escalation: number;
  physical: string | null;
  at: number;
  said: string | null;
  source: string | null;
}

export interface ActionRequestLog {
  id: string;
  at: number;
  origin: string;
  name: string;
  args: Record<string, unknown>;
  physical: boolean;
  status: "requested" | "executed" | "refused" | "ignored" | "failed";
  reason: string | null;
}

export interface DialogueReplyLog {
  id: string;
  at: number;
  user: string;
  event: string | null;
  kind: "generated" | "prepared" | "fallback" | "fallback_parser";
  spoken: string | null;
  action: { name: string; args: Record<string, unknown>; status: string; reason: string | null } | null;
  problems: string[];
  rejected_say: string | null;
  requested: string | null;
  streamed: number;
  ms: number | null;
  llm: { finish: string; first_token_ms: number | null; total_ms: number | null; gen_tokens: number | null; prompt_tokens: number | null; gen_tps: number | null; error: string | null } | null;
}

export interface MindSnap {
  provider: string;
  available: boolean;
  llm: {
    model: string;
    state: string;
    error: string | null;
    ready: { load_ms?: number; prefix_tokens?: number; rss_mb?: number; mlx_active_gb?: number };
    calls: number;
    failures: number;
    cancelled: number;
    p50_ms: number | null;
    busy: boolean;
    last: Record<string, unknown> | null;
    prefill?: { sent: number; hits: number; misses: number };
  } | null;
  conversation: { generated_replies: boolean; proactive_generated: boolean; anticipate: boolean };
  context: {
    goal: { text: string; source: string; at: number } | null;
    quiet: boolean;
    quiet_left_s: number | null;
    quiet_reason: string | null;
    complaints_10min: number;
    observations: MindObservation[];
    statements: { text: string; at: number; age_s: number }[];
    turns: MindTurn[];
    limits: { turns: number; observations: number; statements: number };
  };
  behavior: {
    decisions: BehaviorDecision[];
    confirmed_recent: number;
    last_unprompted_s: number | null;
    gap_multiplier: number;
    camera_lost_for_s: number | null;
  };
  requests: ActionRequestLog[];
  replies: DialogueReplyLog[];
  prepared: { text: string; n: number; age_s: number; ms: number } | null;
  allowed_actions: string[];
}
