import type { CommandResult } from "../types/api";

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    headers: body === undefined ? undefined : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const j = await res.json();
      detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
    } catch {
      /* not json */
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

const post = <T = CommandResult>(path: string, body?: unknown) => request<T>("POST", path, body ?? {});

export const api = {
  startSession: (b: { task?: string; duration_min?: number; target_units?: number | null; unit_label?: string }) => post("/session/start", b),
  pause: () => post("/session/pause"),
  resume: () => post("/session/resume"),
  end: () => post("/session/end", { reason: "user" }),
  progress: (units = 1) => post("/session/progress", { units }),
  release: () => post("/release"),
  estop: () => post("/estop"),
  reset: () => post("/reset"),
  connect: (driver: "mock" | "zeus", host?: string) => post("/robot/connect", { driver, host }),
  disconnect: () => post("/robot/disconnect"),
  arm: () => post("/robot/arm"),
  disarm: () => post("/robot/disarm"),
  motorPower: () => post("/robot/motor-power"),
  autonomy: (enabled: boolean, clear_path_confirmed = false) => post("/robot/autonomy", { enabled, clear_path_confirmed }),
  move: (primitive: string, test_profile = false) => post("/robot/move", { primitive, test_profile }),
  stop: () => post("/robot/stop"),
  returnHome: () => post("/robot/return"),
  setHome: () => post("/robot/home"),
  linkLoss: (duration_s = 3) => post<{ ok: boolean; message: string }>("/robot/simulate-link-loss", { duration_s }),
  linkTest: (b: { setup: "wheels_off_ground" | "on_floor"; primitive: string; speed: number; freeze_s: number }) => post("/robot/link-test", b),
  command: (text: string) => post("/command", { text }),
  visionStart: (source?: string) => post("/vision/start", { source }),
  visionStop: () => post("/vision/stop"),
  cameras: () => request<{ cameras: any[] }>("GET", "/vision/cameras"),
  setZone: (points: [number, number][]) => request<{ ok: boolean }>("PUT", "/vision/zone", { points }),
  attachZone: (points?: [number, number][]) => post("/vision/zone/attach", points ? { points } : {}),
  detachZone: () => post("/vision/zone/detach"),
  voiceStart: () => post("/voice/start"),
  voiceStop: () => post("/voice/stop"),
  voiceDevices: () => request<{ available: boolean; inputs: any[]; outputs: any[]; reason?: string }>("GET", "/voice/devices"),
  testMic: (seconds = 2) => post<{ ok: boolean; message?: string; rms_db?: number; peak_db?: number; speech_detected?: boolean }>("/voice/test-mic", { seconds }),
  ttsTest: () => post<{ ok: boolean; engine: string }>("/tts/test", {}),
  patchSettings: (patch: Record<string, unknown>) => request<{ ok: boolean }>("PATCH", "/settings", patch),
  getSettings: () => request<{ runtime: any; process: Record<string, any> }>("GET", "/settings"),
  simReach: (confirm = true) => post<{ ok: boolean; message: string }>("/sim/reach", { confirm }),
  simFocus: (state: string) => post<{ ok: boolean }>("/sim/focus", { state }),
  simDemo: () => post<{ ok: boolean; message: string }>("/sim/demo", {}),
  simDemoStop: () => request<{ ok: boolean }>("DELETE", "/sim/demo"),
  trialStart: (kind: "reach" | "non_reach") => post<{ ok: boolean }>("/experiments/trial/start", { kind }),
  trialEnd: () => post<{ ok: boolean; trial: any }>("/experiments/trial/end", {}),
  falseTrigger: () => post<{ ok: boolean }>("/experiments/false-trigger", {}),
  perf: () => request<any>("GET", "/metrics/perf"),
  voiceConfig: () => request<import("../types/api").VoiceConfig>("GET", "/voice/config"),
  voiceProfile: (patch: Record<string, unknown>) => request<CommandResult>("PUT", "/voice/profile", patch),
  personality: (b: { mode?: string; profanity?: string; tone?: string; unhinged_opt_in?: boolean }) => post("/voice/personality", b),
  applyPreset: (id: string) => post(`/voice/presets/${encodeURIComponent(id)}/apply`),
  savePreset: (name: string) => post("/voice/presets", { name }),
  deletePreset: (id: string) => request<CommandResult>("DELETE", `/voice/presets/${encodeURIComponent(id)}`),
  cloudCheck: () => post<{ status: import("../types/api").CloudVoiceSnap["status"] }>("/voice/cloud/check"),
  cloudVoices: (mine: boolean, q?: string) =>
    request<{ voices: import("../types/api").CloudVoiceInfo[]; error?: string }>("GET", `/voice/cloud/voices?mine=${mine}${q ? `&q=${encodeURIComponent(q)}` : ""}`),
  voiceTest: (live: boolean) => post("/voice/test", { live }),
  voiceSample: (category: string) => post("/voice/sample", { category }),
  cachePlan: (scope: "pack" | "pack_chill" | "current" | "all_modes") => post("/voice/cache/plan", { scope }),
  cacheGenerate: (plan_id: string, max_bytes: number) => post("/voice/cache/generate", { plan_id, confirm: true, max_bytes }),
  cacheCancel: () => request<CommandResult>("DELETE", "/voice/cache/generate"),
  voicePrompts: () => request<{ prompts: import("../types/api").VoicePromptDef[] }>("GET", "/experiments/voice/prompts"),
  voiceBlock: (speaker: string, condition: string, execute: boolean) => post<{ ok: boolean }>("/experiments/voice/block", { speaker, condition, execute }),
  voicePrompt: (p: { expected: string; phrase: string; slots?: Record<string, number | string> }) => post<{ ok: boolean }>("/experiments/voice/prompt", { slots: {}, ...p }),
  voiceMiss: () => post<{ ok: boolean }>("/experiments/voice/miss"),
  voiceQuiet: (seconds: number) => post<{ ok: boolean }>("/experiments/voice/quiet", { seconds }),
  voiceEnd: () => post<{ ok: boolean; block: any }>("/experiments/voice/end"),
};
