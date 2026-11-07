import { useCallback, useEffect, useState } from "react";
import { X, Play, Radio, Search, ShieldCheck, Save, Trash2, DownloadCloud, Square, Sparkles } from "lucide-react";
import type { CloudVoiceInfo, PersonalityMode, Profanity, Snapshot, SpeechHistoryItem, VoiceConfig, VoiceProfile } from "../types/api";
import { api } from "../services/api";
import { timeOfDay } from "../lib/format";
import { Section, Segmented, Slider, Toggle } from "./ui";
import { useToasts } from "./Toasts";

const MODES: { v: PersonalityMode; name: string; blurb: string; sample: string }[] = [
  { v: "chill", name: "Chill", blurb: "Friendly, relaxed, lightly sarcastic", sample: "Already? We just started." },
  { v: "roast", name: "Roast", blurb: "Trash talk like an actual friend", sample: "Bro, we literally just started." },
  { v: "unhinged", name: "Unhinged", blurb: "Opt-in: strong roasting, insults at you", sample: "Dumbass, that's your third attempt." },
  { v: "judge_demo", name: "Judge demo", blurb: "Energetic, presentation-safe, no swearing", sample: "Whoa, already? We just started!" },
];

const CLOUD_STATE: Record<string, { label: string; tone: string; hint?: string }> = {
  no_key: { label: "No API key", tone: "text-ink-3", hint: "Add FISH_AUDIO_API_KEY to .env and restart the backend. Until then the local voice speaks." },
  unchecked: { label: "Not checked", tone: "text-ink-3" },
  online: { label: "Online", tone: "text-live" },
  offline: { label: "Offline", tone: "text-sim", hint: "No internet (robot Wi-Fi?). Cached clips still play; the rest uses the local voice." },
  auth_error: { label: "Key rejected", tone: "text-danger", hint: "Fish Audio refused the key in .env." },
  no_credit: { label: "No credit", tone: "text-danger", hint: "No API credit left for this model. The free s2.1-pro-free model needs none." },
  voice_not_found: { label: "Voice not found", tone: "text-danger", hint: "Check the voice id." },
  no_voice: { label: "No voice id", tone: "text-sim" },
  error: { label: "Error", tone: "text-danger" },
};

const SOURCE_TONE: Record<string, string> = {
  fish_cache: "border-live/40 text-live",
  fish_live: "border-hit/50 text-hit",
  system_cache: "border-line text-ink-2",
  system_live: "border-line text-ink-2",
  text_only: "border-line-soft text-ink-4",
  none: "border-line-soft text-ink-4",
};

function bytes(n: number) {
  return n >= 10000 ? `${(n / 1000).toFixed(1)} kB` : `${n} B`;
}

export function VoiceStudio({ open, onClose, snap }: { open: boolean; onClose: () => void; snap: Snapshot }) {
  const { run, notify } = useToasts();
  const [cfg, setCfg] = useState<VoiceConfig | null>(null);
  const [voiceId, setVoiceId] = useState("");
  const [devices, setDevices] = useState<{ outputs: any[]; available: boolean } | null>(null);
  const [voices, setVoices] = useState<CloudVoiceInfo[] | null>(null);
  const [voiceQuery, setVoiceQuery] = useState("");
  const [confirmLive, setConfirmLive] = useState(false);
  const [plan, setPlan] = useState<any | null>(null);
  const [presetName, setPresetName] = useState("");

  const refresh = useCallback(() => {
    api.voiceConfig().then((c) => {
      setCfg(c);
      setVoiceId(c.profile.reference_id);
    }).catch(() => setCfg(null));
  }, []);

  useEffect(() => {
    if (!open) return;
    refresh();
    api.voiceDevices().then(setDevices).catch(() => setDevices(null));
  }, [open, refresh]);

  const act = async (p: Promise<any>, ok?: string) => {
    const r = await run(p, false);
    if (r && r.ok === false) notify(r.message, "err");
    else if (ok) notify(ok, "ok");
    refresh();
    return r;
  };
  const profile = (patch: Partial<VoiceProfile> | Record<string, unknown>) => act(api.voiceProfile(patch as Record<string, unknown>));

  const v = snap.voice;
  const pers = v.personality;
  const prof: VoiceProfile = v.profile;
  const cloud = v.cloud;
  const state = CLOUD_STATE[cloud.status.state] ?? { label: cloud.status.state, tone: "text-ink-3" };
  const fish = prof.provider === "fish_audio";
  const history: SpeechHistoryItem[] = (v.tts.history ?? []).slice().reverse();
  const model = cfg?.fish.models.find((m) => m.id === prof.model);
  const job = v.cache_job;

  return (
    <>
      <div className={`fixed inset-0 z-40 bg-void/50 transition-opacity duration-300 ${open ? "opacity-100" : "pointer-events-none opacity-0"}`} onClick={onClose} aria-hidden />
      <aside
        className={`fixed top-0 right-0 bottom-0 z-50 flex w-[min(600px,100vw)] flex-col border-l border-line bg-panel transition-transform duration-[420ms] ease-[cubic-bezier(0.16,1,0.3,1)] ${open ? "translate-x-0" : "translate-x-full"}`}
        aria-hidden={!open}
        aria-label="Personality and voice"
      >
        <div className="flex h-14 shrink-0 items-center gap-3 border-b border-line-soft px-5">
          <h2 className="font-display text-[15px] font-semibold tracking-[0.16em] text-ink uppercase">Personality + Voice</h2>
          <span className={`num ml-1 text-[11.5px] ${fish ? state.tone : "text-ink-3"}`}>{fish ? `Fish Audio · ${state.label}` : prof.provider === "system" ? "Local voice" : "Text only"}</span>
          <button className="btn btn-ghost btn-sm ml-auto !px-2" onClick={onClose} aria-label="Close personality and voice">
            <X size={16} />
          </button>
        </div>

        <div className="scroll-y min-h-0 flex-1">
          {v.tts.current ? (
            <div className="num border-b border-line-soft bg-live-wash px-5 py-2 text-[12px] text-ink-2">
              Speaking ({v.tts.current.priority}): “{v.tts.current.text}”
            </div>
          ) : null}

          <Section title="Personality" note="Changes how it says things, never what happened. Safety and mute commands always win.">
            <div className="grid grid-cols-2 gap-2" role="radiogroup" aria-label="Personality mode">
              {MODES.map((m) => {
                const selected = pers.mode === m.v;
                return (
                  <button
                    key={m.v}
                    role="radio"
                    aria-checked={selected}
                    onClick={() => act(api.personality({ mode: m.v }))}
                    className={`flex flex-col items-start gap-1 rounded-[4px] border px-3 py-2.5 text-left transition-colors ${
                      selected ? (m.v === "unhinged" ? "border-hit bg-hit-wash" : "border-live bg-live-wash") : "border-line bg-well hover:border-ink-4"
                    }`}
                  >
                    <span className={`font-display text-[13px] font-semibold tracking-[0.12em] uppercase ${selected ? (m.v === "unhinged" ? "text-hit" : "text-live") : "text-ink-2"}`}>{m.name}</span>
                    <span className="text-[12px] leading-snug text-ink-3">{m.blurb}</span>
                    <span className="text-[12px] leading-snug text-ink-2">“{m.sample}”</span>
                  </button>
                );
              })}
            </div>
            {pers.mode === "unhinged" ? (
              <Toggle
                on={pers.unhinged_opt_in}
                onChange={(on) => act(api.personality({ unhinged_opt_in: on }))}
                tone="sim"
                label={pers.unhinged_opt_in ? "Opted in: strong roasting and playful insults aimed at me" : "Opt in to Unhinged (until then it talks like Roast)"}
              />
            ) : null}
            <div>
              <div className="mb-1.5 flex items-baseline justify-between">
                <span className="text-[13px] text-ink-2">Profanity</span>
                <span className="num text-[11.5px] text-ink-3">
                  {pers.max_profanity !== pers.profanity ? `capped at ${pers.max_profanity}` : ""}
                  {pers.effective_mode === "judge_demo" ? " · judge demo is always clean" : pers.effective_mode === "chill" ? " · chill caps at mild" : ""}
                </span>
              </div>
              <Segmented<Profanity>
                value={pers.profanity}
                options={[
                  { v: "off", label: "Off" },
                  { v: "mild", label: "Mild" },
                  { v: "strong", label: "Strong" },
                ]}
                onChange={(p) => act(api.personality({ profanity: p }))}
              />
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-[13px] text-ink-2">Tone</span>
              {(["normal", "gentle", "serious"] as const).map((t) => (
                <button key={t} className={`btn btn-sm ${pers.tone === t ? "btn-primary" : ""}`} onClick={() => act(api.personality({ tone: t }))}>
                  {t === "normal" ? "Normal" : t === "gentle" ? "No roasting" : "Serious"}
                </button>
              ))}
              {pers.supportive_s_left > 0 ? <span className="chip chip-live">helping · no insults {Math.ceil(pers.supportive_s_left / 60)} min</span> : null}
            </div>
            <div className="flex flex-wrap gap-2">
              {[
                ["reach_first", "Phone grab"],
                ["progress_ack", "Progress"],
                ["target_reached", "Celebration"],
                ["session_start", "Session start"],
              ].map(([cat, label]) => (
                <button key={cat} className="btn btn-sm" onClick={() => act(api.voiceSample(cat))}>
                  <Play size={12} /> {label}
                </button>
              ))}
            </div>
          </Section>

          <Section title="Presets" note="A preset sets voice, model, settings, personality, profanity and output device at once. New lines use it immediately.">
            <div className="flex flex-col gap-1.5">
              {(cfg?.presets ?? []).map((p) => (
                <div key={p.id} className={`flex items-center gap-2 rounded-[4px] border px-3 py-2 ${v.active_preset === p.id ? "border-live bg-live-wash" : "border-line-soft"}`}>
                  <div className="min-w-0 flex-1">
                    <div className="text-[13px] text-ink">{p.name}</div>
                    <div className="num truncate text-[11px] text-ink-3">
                      {p.profile.provider === "fish_audio" ? `Fish ${p.profile.reference_id ? p.profile.reference_id.slice(0, 8) + "…" : "· no voice yet"} · ${p.profile.model}` : p.profile.provider === "system" ? "local voice" : "text only"} · {p.personality} · {p.profanity}
                    </div>
                  </div>
                  <button className="btn btn-sm" onClick={() => act(api.applyPreset(p.id))} disabled={v.active_preset === p.id}>
                    {v.active_preset === p.id ? "Active" : "Use"}
                  </button>
                  {!p.builtin ? (
                    <button className="btn btn-ghost btn-sm !px-2" onClick={() => act(api.deletePreset(p.id))} aria-label={`Delete preset ${p.name}`}>
                      <Trash2 size={13} />
                    </button>
                  ) : null}
                </div>
              ))}
            </div>
            <div className="flex gap-2">
              <input className="field !h-8 flex-1 !text-[12px]" placeholder="Save current voice + personality as…" maxLength={40} value={presetName} onChange={(e) => setPresetName(e.target.value)} />
              <button className="btn btn-sm" disabled={!presetName.trim()} onClick={() => act(api.savePreset(presetName.trim()), "Preset saved").then(() => setPresetName(""))}>
                <Save size={13} /> Save
              </button>
            </div>
          </Section>

          <Section title="Voice">
            <Segmented<VoiceProfile["provider"]>
              value={prof.provider}
              options={[
                { v: "fish_audio", label: "Fish Audio" },
                { v: "system", label: "Local voice" },
                { v: "none", label: "Text only" },
              ]}
              onChange={(p) => profile({ provider: p })}
            />
            {fish ? (
              <>
                {state.hint ? <p className="text-[12px] leading-relaxed text-ink-3">{state.hint}</p> : null}
                <label className="block">
                  <span className="text-[13px] text-ink-2">Voice id (paste the id or the fish.audio/m/… link)</span>
                  <div className="mt-1.5 flex gap-2">
                    <input className="field num !h-8 flex-1 !text-[12px]" value={voiceId} spellCheck={false} onChange={(e) => setVoiceId(e.target.value)} placeholder="32 hex characters" />
                    <button className="btn btn-sm" disabled={voiceId === prof.reference_id} onClick={() => profile({ reference_id: voiceId })}>
                      Set
                    </button>
                    <button className="btn btn-sm" disabled={!cloud.configured || !prof.reference_id} onClick={() => act(api.cloudCheck())} title="Free lookup: checks the key, the network and the voice">
                      <ShieldCheck size={13} /> Check
                    </button>
                  </div>
                </label>
                {cloud.status.voice ? (
                  <div className="flex flex-col gap-1.5 rounded-[4px] border border-line-soft px-3 py-2">
                    <div className="text-[13px] text-ink">
                      {cloud.status.voice.title} <span className="text-ink-3">by {cloud.status.voice.author ?? "unknown"} · {cloud.status.voice.languages.join(", ")}</span>
                    </div>
                    {cloud.status.voice.sample_url ? <audio controls preload="none" src={cloud.status.voice.sample_url} className="h-8 w-full" aria-label="Fish Audio sample of this voice (free)" /> : null}
                  </div>
                ) : null}
                <div className="flex gap-2">
                  <input className="field !h-8 flex-1 !text-[12px]" placeholder="Search public voices by name" value={voiceQuery} onChange={(e) => setVoiceQuery(e.target.value)} />
                  <button className="btn btn-sm" disabled={!cloud.configured} onClick={() => api.cloudVoices(false, voiceQuery || undefined).then((r) => (r.error ? notify(r.error, "err") : setVoices(r.voices)))}>
                    <Search size={13} /> Search
                  </button>
                  <button className="btn btn-sm" disabled={!cloud.configured} onClick={() => api.cloudVoices(true).then((r) => (r.error ? notify(r.error, "err") : setVoices(r.voices)))}>
                    My voices
                  </button>
                </div>
                {voices ? (
                  <ul className="flex max-h-56 flex-col gap-1 overflow-y-auto">
                    {voices.length === 0 ? <li className="text-[12px] text-ink-3">No voices found.</li> : null}
                    {voices.map((vc) => (
                      <li key={vc.id} className="flex items-center gap-2 border-b border-line-soft/60 py-1.5">
                        <div className="min-w-0 flex-1">
                          <div className="truncate text-[12.5px] text-ink">{vc.title}</div>
                          <div className="num truncate text-[11px] text-ink-3">
                            {vc.author ?? "?"} · {vc.languages.join(", ")} · {vc.id}
                          </div>
                        </div>
                        {vc.sample_url ? <audio controls preload="none" src={vc.sample_url} className="h-7 w-36" aria-label={`Sample of ${vc.title}`} /> : null}
                        <button className="btn btn-sm" onClick={() => profile({ reference_id: vc.id })}>
                          Use
                        </button>
                      </li>
                    ))}
                  </ul>
                ) : null}
                <label className="block">
                  <span className="text-[13px] text-ink-2">Model</span>
                  <select className="field mt-1.5" value={prof.model} onChange={(e) => profile({ model: e.target.value })}>
                    {(cfg?.fish.models ?? []).map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.label} · ${m.usd_per_million_bytes}/M bytes
                      </option>
                    ))}
                  </select>
                  {model?.note ? <span className="mt-1 block text-[11.5px] text-ink-4">{model.note}</span> : null}
                </label>
                <Slider label="Speed" value={prof.fish.speed} min={0.5} max={2} step={0.05} onCommit={(x) => profile({ fish: { speed: x } })} fmt={(x) => `${x.toFixed(2)}×`} />
                <Slider label="Expressiveness (temperature)" value={prof.fish.temperature} min={0} max={1} step={0.05} onCommit={(x) => profile({ fish: { temperature: x } })} fmt={(x) => x.toFixed(2)} />
                <Slider label="Variety (top-p)" value={prof.fish.top_p} min={0} max={1} step={0.05} onCommit={(x) => profile({ fish: { top_p: x } })} fmt={(x) => x.toFixed(2)} />
                <div>
                  <span className="mb-1.5 block text-[13px] text-ink-2">Latency mode</span>
                  <Segmented<VoiceProfile["fish"]["latency"]>
                    value={prof.fish.latency}
                    options={[
                      { v: "low", label: "Low" },
                      { v: "balanced", label: "Balanced" },
                      { v: "normal", label: "Quality" },
                    ]}
                    onChange={(x) => profile({ fish: { latency: x } })}
                  />
                </div>
                <Toggle on={prof.fish.cues} onChange={(x) => profile({ fish: { cues: x } })} label="Delivery cues on S2 models ([excited], [laughing]…)" />
                <div>
                  <span className="mb-1.5 block text-[13px] text-ink-2">When a line is not cached</span>
                  <Segmented<VoiceProfile["cache_behavior"]>
                    value={prof.cache_behavior}
                    options={[
                      { v: "cache_only", label: "Local voice" },
                      { v: "cache_then_live", label: "Generate live" },
                    ]}
                    onChange={(x) => (x === "cache_then_live" ? setConfirmLive(true) : profile({ cache_behavior: x }))}
                  />
                  {confirmLive ? (
                    <div className="mt-2 flex flex-col gap-2 rounded-[4px] border border-sim/50 bg-sim-wash px-3 py-2.5">
                      <p className="text-[12px] leading-relaxed text-ink-2">
                        Uncached lines will be sent to Fish Audio ({prof.model}, ${model?.usd_per_million_bytes ?? "?"} per million bytes) up to {bytes(prof.daily_live_bytes)} a day. Safety lines never wait for it.
                      </p>
                      <div className="flex gap-2">
                        <button className="btn btn-sm btn-primary" onClick={() => profile({ cache_behavior: "cache_then_live" }).then(() => setConfirmLive(false))}>
                          Allow live generation
                        </button>
                        <button className="btn btn-sm" onClick={() => setConfirmLive(false)}>
                          Cancel
                        </button>
                      </div>
                    </div>
                  ) : null}
                </div>
                <Slider label="Daily live budget" value={prof.daily_live_bytes} min={0} max={50000} step={500} onCommit={(x) => profile({ daily_live_bytes: x })} fmt={(x) => `${bytes(cfg?.fish.live_bytes_today ?? 0)} / ${bytes(x)}`} />
              </>
            ) : null}
            {prof.provider === "system" ? (
              <>
                <label className="block">
                  <span className="text-[13px] text-ink-2">Local voice name (macOS `say -v ?` lists them; empty = system default)</span>
                  <input className="field mt-1.5 !text-[12px]" defaultValue={prof.system_voice ?? ""} onBlur={(e) => profile({ system_voice: e.target.value || null })} />
                </label>
                <Slider label="Rate" value={prof.system_rate_wpm} min={120} max={300} step={5} unit="wpm" onCommit={(x) => profile({ system_rate_wpm: x })} />
              </>
            ) : null}
            <Slider label="Volume" value={prof.volume} min={0} max={1.5} step={0.05} onCommit={(x) => profile({ volume: x })} fmt={(x) => `${Math.round(x * 100)}%`} />
            {devices?.available ? (
              <label className="block">
                <span className="text-[13px] text-ink-2">Output device</span>
                <select className="field mt-1.5" value={snap.settings.voice.output_device ?? ""} onChange={(e) => act(api.patchSettings({ voice: { output_device: e.target.value || null } }))}>
                  <option value="">System default</option>
                  {devices.outputs.map((d: any) => (
                    <option key={d.index} value={d.name}>
                      {d.name}
                    </option>
                  ))}
                </select>
              </label>
            ) : null}
            {v.tts.audio_error ? <p className="text-[12px] text-danger">{v.tts.audio_error} · speaking as text for now</p> : null}
            <div className="flex flex-wrap items-center gap-2">
              <Toggle on={v.tts.muted} onChange={(m) => act(api.patchSettings({ voice: { muted: m } }))} label="Mute" />
              <button className="btn btn-sm" onClick={() => act(api.voiceTest(false))} title="Cached clip or local voice: free">
                <Play size={12} /> Test voice
              </button>
              {fish ? (
                <button className="btn btn-sm" disabled={!cloud.configured || !prof.reference_id} onClick={() => act(api.voiceTest(true))} title="Asks Fish Audio for a fresh clip of the test line (a few dozen bytes of text)">
                  <Radio size={12} /> Test live (Fish)
                </button>
              ) : null}
            </div>
          </Section>

          {fish ? (
            <Section title="Offline cache" note="The robot's Wi-Fi has no internet. Pre-generate the fixed lines while online; cached clips play in ~10 ms with no network.">
              <div className="num flex items-baseline justify-between text-[12px]">
                <span className="text-ink-2">
                  {cfg ? `Voice pack ${cfg.cache.pack.cached}/${cfg.cache.pack.lines} · all lines for this personality ${cfg.cache.cached}/${cfg.cache.lines}` : "…"}
                </span>
                <span className="text-ink-3">{cloud.cache_files} clips on disk</span>
              </div>
              <div className="flex flex-wrap gap-2">
                <button className="btn btn-sm btn-primary" onClick={() => api.cachePlan("pack").then((r) => (r.ok ? setPlan(r.data.plan) : notify(r.message, "err")))}>
                  Plan: voice pack (~30 lines)
                </button>
                <button className="btn btn-sm" onClick={() => api.cachePlan("pack_chill").then((r) => (r.ok ? setPlan(r.data.plan) : notify(r.message, "err")))}>
                  Pack + Chill
                </button>
                <button className="btn btn-sm" onClick={() => api.cachePlan("current").then((r) => (r.ok ? setPlan(r.data.plan) : notify(r.message, "err")))}>
                  Plan: this personality
                </button>
                <button className="btn btn-sm" onClick={() => api.cachePlan("all_modes").then((r) => (r.ok ? setPlan(r.data.plan) : notify(r.message, "err")))}>
                  Plan: all modes
                </button>
              </div>
              {plan ? (
                <div className="flex flex-col gap-2 rounded-[4px] border border-line px-3 py-2.5">
                  <div className="num text-[12px] text-ink-2">
                    {plan.missing} of {plan.lines} lines to generate · {bytes(plan.bytes)} of text · ~${plan.estimated_usd ?? "?"} on {plan.model}
                  </div>
                  <ul className="num list-inside list-disc text-[11.5px] text-ink-3">
                    {plan.sample.map((t: string) => (
                      <li key={t} className="truncate">
                        {t}
                      </li>
                    ))}
                  </ul>
                  {plan.missing > 0 ? (
                    <button
                      className="btn btn-sm btn-primary w-fit"
                      disabled={!plan.configured || job?.state === "running"}
                      title={plan.configured ? "" : "Add FISH_AUDIO_API_KEY to .env first"}
                      onClick={() => act(api.cacheGenerate(plan.plan_id, plan.bytes), "Generating clips").then(() => setPlan(null))}
                    >
                      <DownloadCloud size={13} /> Generate {plan.missing} clips · approve {bytes(plan.bytes)}
                    </button>
                  ) : (
                    <span className="text-[12px] text-live">Everything is cached.</span>
                  )}
                </div>
              ) : null}
              {job ? (
                <div className="flex items-center gap-3">
                  <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-well">
                    <div className="h-full bg-live transition-[width] duration-300" style={{ width: `${job.total ? (100 * job.done) / job.total : 0}%` }} />
                  </div>
                  <span className="num text-[11.5px] text-ink-2">
                    {job.done}/{job.total} {job.state !== "running" ? `· ${job.state}` : ""} {job.error ? `· ${job.error}` : ""}
                  </span>
                  {job.state === "running" ? (
                    <button className="btn btn-ghost btn-sm !px-2" onClick={() => act(api.cacheCancel())} aria-label="Cancel cache generation">
                      <Square size={12} />
                    </button>
                  ) : null}
                </div>
              ) : null}
            </Section>
          ) : null}

          <Section title="Speech history" note="Where each line actually came from. Lines that were cut, replaced or muted are kept here too.">
            {history.length ? (
              <ul className="flex flex-col">
                {history.map((h, i) => (
                  <li key={`${h.at}-${i}`} className="flex items-start gap-2 border-b border-line-soft/60 py-1.5">
                    <span className="num w-[58px] shrink-0 text-[11px] text-ink-4">{timeOfDay(h.at)}</span>
                    <div className="min-w-0 flex-1">
                      <div className={`text-[12.5px] leading-snug ${h.outcome === "finished" ? "text-ink" : "text-ink-3"}`}>{h.text}</div>
                      <div className="num mt-0.5 flex flex-wrap items-center gap-1.5 text-[10.5px] text-ink-4">
                        <span className={`rounded-[3px] border px-1.5 py-px ${SOURCE_TONE[h.source] ?? "border-line text-ink-3"}`}>{h.source_label}</span>
                        <span>{h.priority}</span>
                        <span>{h.category}</span>
                        {h.outcome !== "finished" ? <span className="text-sim">{h.outcome}</span> : null}
                        {h.first_audio_ms != null ? <span>gen {Math.round(h.first_audio_ms)} ms</span> : null}
                        {h.detail ? <span title={h.detail} className="max-w-[260px] truncate">{h.detail}</span> : null}
                      </div>
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="flex items-center gap-2 text-[12px] text-ink-3">
                <Sparkles size={13} /> Nothing said yet. Try a sample above.
              </p>
            )}
          </Section>
        </div>
      </aside>
    </>
  );
}
