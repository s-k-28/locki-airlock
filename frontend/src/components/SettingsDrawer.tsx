import { useEffect, useState } from "react";
import { X, Mic, Volume2, ScanSearch } from "lucide-react";
import type { Snapshot } from "../types/api";
import { api } from "../services/api";
import { Section, Segmented, Slider, Toggle } from "./ui";
import { useToasts } from "./Toasts";

export function SettingsDrawer({ open, onClose, snap, onOpenVoice }: { open: boolean; onClose: () => void; snap: Snapshot; onOpenVoice: () => void }) {
  const { run, notify } = useToasts();
  const s = snap.settings;
  const [proc, setProc] = useState<Record<string, any> | null>(null);
  const [devices, setDevices] = useState<{ inputs: any[]; outputs: any[]; available: boolean; reason?: string } | null>(null);
  const [cams, setCams] = useState<any[] | null>(null);
  const [customSrc, setCustomSrc] = useState("");
  const [mic, setMic] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    api.getSettings().then((r) => setProc(r.process)).catch(() => setProc(null));
    api.voiceDevices().then(setDevices).catch(() => setDevices(null));
  }, [open]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && open && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const patch = (p: Record<string, unknown>) => run(api.patchSettings(p));

  return (
    <>
      <div className={`fixed inset-0 z-40 bg-void/50 transition-opacity duration-300 ${open ? "opacity-100" : "pointer-events-none opacity-0"}`} onClick={onClose} aria-hidden />
      <aside
        className={`fixed top-0 right-0 bottom-0 z-50 flex w-[min(440px,100vw)] flex-col border-l border-line bg-panel transition-transform duration-[420ms] ease-[cubic-bezier(0.16,1,0.3,1)] ${open ? "translate-x-0" : "translate-x-full"}`}
        aria-hidden={!open}
        aria-label="Settings"
      >
        <div className="flex h-14 shrink-0 items-center justify-between border-b border-line-soft px-5">
          <h2 className="font-display text-[15px] font-bold tracking-[0.16em] uppercase">Settings</h2>
          <button className="btn btn-ghost btn-sm !px-2" onClick={onClose} aria-label="Close settings">
            <X size={16} />
          </button>
        </div>
        <div className="scroll-y flex-1">
          <Section title="Camera" note="Frames stay in memory; nothing is recorded or uploaded.">
            <div className="flex gap-2">
              <select className="field" value={s.camera_source} onChange={(e) => patch({ camera_source: e.target.value })} aria-label="Camera source">
                {["0", "1", "2"].map((i) => (
                  <option key={i} value={i}>
                    Camera index {i}
                  </option>
                ))}
                {snap.robot.status?.capabilities.camera_stream_url ? <option value={snap.robot.status.capabilities.camera_stream_url}>Zeus ESP32-CAM stream</option> : null}
                <option value="http://192.168.4.1:9000/mjpg">ESP32-CAM 192.168.4.1</option>
                {!["0", "1", "2", "http://192.168.4.1:9000/mjpg", snap.robot.status?.capabilities.camera_stream_url].includes(s.camera_source) ? <option value={s.camera_source}>{s.camera_source}</option> : null}
              </select>
              <button
                className="btn btn-sm !h-9 shrink-0"
                onClick={async () => {
                  notify("Scanning cameras… (a few seconds)");
                  const r = await run(api.cameras());
                  if (r) setCams(r.cameras);
                }}
              >
                <ScanSearch size={14} /> Scan
              </button>
            </div>
            {cams ? (
              <ul className="num space-y-1 text-[11.5px] text-ink-3">
                {cams.map((c, i) => (
                  <li key={i}>
                    index {c.index}: {c.in_use ? "in use by LOCKED IN" : c.available ? `available ${c.size ? c.size.join("×") : ""}` : `unavailable${c.reason ? ` (${c.reason.slice(0, 60)}…)` : ""}`}
                  </li>
                ))}
              </ul>
            ) : null}
            <form
              className="flex gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                if (customSrc.trim()) patch({ camera_source: customSrc.trim() });
              }}
            >
              <input className="field num !text-[12px]" placeholder="custom: http://…/mjpg or /path/clip.mp4" value={customSrc} onChange={(e) => setCustomSrc(e.target.value)} />
              <button className="btn btn-sm !h-9" type="submit">
                Use
              </button>
            </form>
            <Toggle on={s.show_video} onChange={(v) => patch({ show_video: v })} label="Show video (off = landmarks only)" />
            <div>
              <div className="mb-2 text-[13px] text-ink-2">Camera position</div>
              <Segmented
                value={s.camera_mount}
                options={[
                  { v: "auto", label: "Auto" },
                  { v: "fixed", label: "Fixed" },
                  { v: "robot", label: "On robot" },
                ]}
                onChange={(v) => patch({ camera_mount: v })}
              />
              <p className="mt-1.5 text-[12px] text-ink-4">Auto: the Zeus ESP32-CAM rides on the robot, every other camera is fixed. A camera on the robot uses its own reach zone and pauses reach detection while the robot moves.</p>
            </div>
            <div>
              <div className="mb-2 text-[13px] text-ink-2">Zeus camera orientation</div>
              <Segmented
                value={s.zeus_camera_flip}
                options={[
                  { v: "none", label: "As sent" },
                  { v: "horizontal", label: "Mirror" },
                  { v: "vertical", label: "Flip" },
                  { v: "both", label: "Rotate 180" },
                ]}
                onChange={(v) => patch({ zeus_camera_flip: v })}
              />
            </div>
            <p className="text-[12px] text-ink-4">Pick the camera in the Vision panel; it switches straight away. Position and orientation apply immediately to a running camera.</p>
          </Section>

          <Section title="Reach sensitivity" note="Adaptation may add strictness on top of these (shown in the snapshot as effective values).">
            <Slider label="Confirm dwell after approach" value={s.vision.confirm_dwell_ms} min={60} max={800} step={20} unit="ms" onCommit={(v) => patch({ vision: { confirm_dwell_ms: v } })} />
            <Slider label="Static dwell (no approach seen)" value={s.vision.static_dwell_ms} min={300} max={2500} step={50} unit="ms" onCommit={(v) => patch({ vision: { static_dwell_ms: v } })} />
            <Slider label="Approach margin" value={s.vision.approach_margin} min={0.02} max={0.3} step={0.01} onCommit={(v) => patch({ vision: { approach_margin: v } })} fmt={(v) => v.toFixed(2)} />
            <Slider label="Minimum hand confidence" value={s.vision.min_hand_confidence} min={0.3} max={0.95} step={0.05} onCommit={(v) => patch({ vision: { min_hand_confidence: v } })} fmt={(v) => v.toFixed(2)} />
            <Slider label="Reach cooldown" value={s.vision.reach_cooldown_s} min={0} max={30} step={1} unit="s" onCommit={(v) => patch({ vision: { reach_cooldown_s: v } })} />
          </Section>

          {s.conversation ? (
            <Section
              title="Conversation"
              note="The local language model runs on this Mac and never touches the motors. Off = the fixed line library (Milestone 4 behaviour)."
            >
              <Toggle on={s.conversation.generated_replies} onChange={(v) => patch({ conversation: { generated_replies: v } })} label="Write replies with the local model" />
              <Toggle
                on={s.conversation.proactive_generated}
                onChange={(v) => patch({ conversation: { proactive_generated: v } })}
                label="Unprompted remarks written for the moment"
              />
              <Toggle on={s.conversation.anticipate} onChange={(v) => patch({ conversation: { anticipate: v } })} label="Prepare the next reaction in the background" />
            </Section>
          ) : null}
          <Section title="Robot motion" note="Speeds are Zeus joystick units (0-100). Profiles clamp everything: retreat ≤ 60 for ≤ 0.8 s.">
            <div>
              <div className="mb-2 text-[13px] text-ink-2">Retreat direction</div>
              <Segmented
                value={s.motion.retreat_primitive}
                options={[
                  { v: "backward", label: "Back" },
                  { v: "strafe_left", label: "Left" },
                  { v: "strafe_right", label: "Right" },
                  { v: "forward", label: "Fwd" },
                ]}
                onChange={(v) => patch({ motion: { retreat_primitive: v } })}
              />
            </div>
            <Slider label="Retreat speed" value={s.motion.retreat_speed} min={10} max={60} step={1} onCommit={(v) => patch({ motion: { retreat_speed: v } })} />
            <Slider label="Retreat duration" value={s.motion.retreat_duration_s} min={0.1} max={0.8} step={0.05} unit="s" onCommit={(v) => patch({ motion: { retreat_duration_s: v } })} fmt={(v) => v.toFixed(2)} />
            <Slider label="Manual jog speed" value={s.motion.manual_speed} min={10} max={70} step={1} onCommit={(v) => patch({ motion: { manual_speed: v } })} />
            <Slider label="Manual jog duration" value={s.motion.manual_duration_s} min={0.1} max={1.0} step={0.05} unit="s" onCommit={(v) => patch({ motion: { manual_duration_s: v } })} fmt={(v) => v.toFixed(2)} />
            <Slider label="Autonomous cooldown" value={s.motion.autonomous_cooldown_s} min={0} max={60} step={1} unit="s" onCommit={(v) => patch({ motion: { autonomous_cooldown_s: v } })} />
          </Section>

          <Section title="Voice" note="Bluetooth speakers add latency and often disable their microphone in stereo mode; use the laptop mic.">
            <Segmented
              value={s.voice.listen_mode}
              options={[
                { v: "push_to_talk", label: "Push to talk" },
                { v: "wake_word", label: "Wake word" },
                { v: "always", label: "Always" },
              ]}
              onChange={(v) => patch({ voice: { listen_mode: v } })}
            />
            {devices?.available ? (
              <>
                <label className="block">
                  <span className="text-[13px] text-ink-2">Microphone</span>
                  <select className="field mt-1.5" value={s.voice.input_device == null ? "" : String(s.voice.input_device)} onChange={(e) => patch({ voice: { input_device: e.target.value === "" ? null : Number(e.target.value) } })}>
                    <option value="">System default</option>
                    {devices.inputs.map((d) => (
                      <option key={d.index} value={d.index}>
                        {d.name}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="block">
                  <span className="text-[13px] text-ink-2">Speaker (robot voice)</span>
                  <select className="field mt-1.5" value={s.voice.output_device ?? ""} onChange={(e) => patch({ voice: { output_device: e.target.value || null } })}>
                    <option value="">System default</option>
                    {devices.outputs.map((d) => (
                      <option key={d.index} value={d.name}>
                        {d.name}
                      </option>
                    ))}
                  </select>
                </label>
              </>
            ) : (
              <p className="text-[12px] text-ink-4">Audio devices unavailable{devices?.reason ? `: ${devices.reason}` : ""}.</p>
            )}
            <div className="flex flex-wrap items-center gap-2">
              <button
                className="btn btn-sm"
                onClick={async () => {
                  setMic("listening for 2 s…");
                  const r = await run(api.testMic(2));
                  setMic(r?.ok ? `level ${r.rms_db?.toFixed(1)} dBFS · peak ${r.peak_db?.toFixed(1)} · ${r.speech_detected ? "speech detected" : "no speech"}` : r?.message ?? "failed");
                }}
              >
                <Mic size={13} /> Test mic
              </button>
              <button className="btn btn-sm" onClick={() => run(api.ttsTest())}>
                <Volume2 size={13} /> Test voice
              </button>
            </div>
            {mic ? <div className="num text-[11.5px] text-ink-3">{mic}</div> : null}
            <Slider label="Min command confidence" value={s.voice.min_command_confidence} min={0.2} max={0.9} step={0.05} onCommit={(v) => patch({ voice: { min_command_confidence: v } })} fmt={(v) => v.toFixed(2)} />
          </Section>

          <Section title="Personality" note="Mode, profanity, voice and presets live in the Personality + Voice drawer.">
            <button className="btn btn-sm w-fit" onClick={onOpenVoice}>
              {snap.voice.personality.label} · profanity {snap.voice.personality.max_profanity} · open Personality + Voice
            </button>
            <Segmented
              value={s.coach.intensity}
              options={[
                { v: "gentle", label: "Gentle" },
                { v: "standard", label: "Standard" },
                { v: "strict", label: "Strict" },
              ]}
              onChange={(v) => patch({ coach: { intensity: v } })}
            />
            <Slider label="Min gap between interventions" value={s.coach.min_seconds_between_interventions} min={0} max={180} step={5} unit="s" onCommit={(v) => patch({ coach: { min_seconds_between_interventions: v } })} />
            <Toggle on={s.coach.announce_milestones} onChange={(v) => patch({ coach: { announce_milestones: v } })} label="Announce halfway / five minutes left" />
            <Toggle on={s.voice.muted} onChange={(v) => patch({ voice: { muted: v } })} label="Mute robot voice (safety lines still play)" />
          </Section>

          <Section title="Services" note="Keys live in .env. Paid services stay off unless a key is configured there.">
            {proc ? (
              <dl className="num grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-[12px]">
                <dt className="text-ink-3">speech-to-text</dt>
                <dd className="text-ink-2">
                  {proc.stt_engine} {proc.stt_engine !== "none" ? `(${proc.whisper_model}, local)` : ""}
                </dd>
                <dt className="text-ink-3">voice</dt>
                <dd className="text-ink-2">{proc.tts_engine}</dd>
                <dt className="text-ink-3">Fish Audio</dt>
                <dd className={proc.fish_audio_configured ? "text-live" : "text-ink-4"}>{proc.fish_audio_configured ? "key configured" : "no key (local voice)"}</dd>
                <dt className="text-ink-3">LLM</dt>
                <dd className={proc.llm_configured ? "text-live" : "text-ink-4"}>{proc.llm_provider === "none" ? "off (templates)" : `${proc.llm_provider}${proc.llm_configured ? "" : " (not ready)"}`}</dd>
                <dt className="text-ink-3">robot driver</dt>
                <dd className="text-ink-2">{proc.robot_driver}</dd>
                <dt className="text-ink-3">device stop verified</dt>
                <dd className={proc.zeus_device_watchdog_verified ? "text-live" : "text-sim"}>{proc.zeus_device_watchdog_verified ? "yes" : "no (autonomy locked on hardware)"}</dd>
                <dt className="text-ink-3">raw video storage</dt>
                <dd className="text-ink-2">{proc.store_raw_video ? "ON" : "off"}</dd>
              </dl>
            ) : (
              <p className="text-[12px] text-ink-4">Loading…</p>
            )}
          </Section>
        </div>
      </aside>
    </>
  );
}
