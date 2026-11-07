import { useEffect, useMemo, useRef, useState } from "react";
import { Mic, MicOff, Send, Volume2, VolumeX } from "lucide-react";
import type { LockedInEvent, Snapshot } from "../types/api";
import { api } from "../services/api";
import { SectionHeader, SourceTag } from "./ui";
import { useToasts } from "./Toasts";

const SUGGESTIONS = ["Lock in, 25 minutes for calculus", "How am I doing?", "I finished a problem", "How much time is left?", "Give me my phone"];

interface Line {
  id: string;
  who: "you" | "bot" | "note";
  text: string;
  meta?: string;
  hit?: boolean;
}

function transcript(events: LockedInEvent[]): Line[] {
  const out: Line[] = [];
  for (const e of events) {
    const p = e.payload;
    if (e.event_type === "voice.transcribed") out.push({ id: e.event_id, who: "you", text: p.text, meta: p.source === "typed" ? "typed" : p.latency_ms != null ? `heard · STT ${Math.round(p.latency_ms)} ms` : "heard" });
    else if (e.event_type === "voice.command_recognized" && out.length && out[out.length - 1].who === "you" && !out[out.length - 1].meta?.includes("·intent"))
      out[out.length - 1].meta = `${out[out.length - 1].meta ?? ""} · ${String(p.intent).replace(/_/g, " ")} ${e.confidence?.toFixed(2) ?? ""}`;
    else if (e.event_type === "voice.response_generated") out.push({ id: e.event_id, who: "bot", text: p.text, meta: `${p.category}${p.audible === false ? " · muted" : ""}` });
    else if (e.event_type === "coach.intervention_generated") out.push({ id: e.event_id, who: "bot", text: p.text, meta: `intervention · ${p.category}${p.audible === false ? " · muted" : ""}`, hit: true });
    else if (e.event_type === "voice.command_rejected" && p.reason && p.reason !== "not addressed to LOCKED IN") out.push({ id: e.event_id, who: "note", text: `not acted on: ${p.reason}` });
  }
  return out.slice(-40);
}

const STATE_TONE: Record<Snapshot["voice"]["state"], string> = {
  off: "text-ink-4",
  ready: "text-ink-3",
  hearing: "text-live",
  transcribing: "text-sim",
  thinking: "text-sim",
  speaking: "text-hit",
};

/** The last utterance through the whole loop, and where the robot's voice is going. */
function VoiceLoop({ voice }: { voice: Snapshot["voice"] }) {
  const out = voice.output;
  const turn = voice.turns?.[0] ?? null;
  const intent = turn?.intent ?? null;
  const reply = turn?.replies[0] ?? null;
  const state = voice.state ?? "off";
  const vol = out?.system_volume;
  return (
    <div className="space-y-2 px-4 pb-2.5">
      <div className="flex items-center justify-between gap-3">
        <span className={`num inline-flex items-center gap-1.5 text-[11px] tracking-[0.12em] uppercase ${STATE_TONE[state]}`}>
          <span className={`h-1.5 w-1.5 rounded-full bg-current ${state === "hearing" || state === "speaking" ? "animate-pulse" : ""}`} />
          {state}
        </span>
        <span className="num min-w-0 truncate text-[11px] text-ink-3" title={out?.listed === false ? "not in the audio device list taken at startup" : undefined}>
          out · {out?.label ?? "—"}
          {vol?.applies && vol.volume != null ? ` · vol ${vol.volume}%` : ""}
          {out?.last_output_latency_ms != null ? ` · device ${Math.round(out.last_output_latency_ms)} ms` : ""}
        </span>
      </div>
      {out?.warning || out?.error || out?.listed === false ? (
        <p className="text-[12px] leading-snug text-danger">
          Speaker: {out.warning ?? out.error ?? `${out.label} is not connected (not in the device list)`}
        </p>
      ) : null}
      {turn ? (
        <dl className="grid grid-cols-[48px_1fr] gap-x-2 gap-y-1 text-[12px] leading-snug">
          <dt className="label !text-[10px] !leading-[1.5]">heard</dt>
          <dd className="min-w-0 [overflow-wrap:anywhere] text-ink-2">
            “{turn.text || "…"}”
            <span className="num text-[10.5px] text-ink-4">
              {" "}
              · {turn.source === "voice" ? (turn.stt_ms != null ? `STT ${Math.round(turn.stt_ms)} ms` : "voice") : turn.source}
              {turn.echo_removed != null ? ` · echo removed ${turn.echo_removed.toFixed(2)}` : ""}
            </span>
          </dd>
          <dt className="label !text-[10px] !leading-[1.5]">intent</dt>
          <dd className="num min-w-0 text-[11.5px] text-ink-2">
            {intent ? `${intent.intent.replace(/_/g, " ")} · ${intent.confidence.toFixed(2)}` : "—"}
            {intent?.conflict ? <span className="text-sim"> · conflict: {intent.conflict}</span> : null}
          </dd>
          <dt className="label !text-[10px] !leading-[1.5]">action</dt>
          <dd className={`min-w-0 [overflow-wrap:anywhere] ${turn.ok === false ? "text-sim" : "text-ink-2"}`}>{turn.action ?? "…"}</dd>
          <dt className="label !text-[10px] !leading-[1.5]">reply</dt>
          <dd className="min-w-0 [overflow-wrap:anywhere] text-ink-2">
            {reply ? `“${reply.text}”` : <span className="text-ink-4">none</span>}
            {turn.reply_source ? (
              <span className="num text-[10.5px] text-ink-4" title="end of your speech (or the typed command) to the reply starting to play, on the backend's clock">
                {" "}
                · {turn.reply_source}
                {turn.reply_to_audio_ms != null
                  ? ` · playing ${Math.round(turn.reply_to_audio_ms)} ms after ${turn.source === "voice" ? "you stopped talking" : "the command"}`
                  : ""}
              </span>
            ) : null}
          </dd>
        </dl>
      ) : null}
    </div>
  );
}

function Meter({ db, active }: { db: number | null; active: boolean }) {
  const level = db == null ? 0 : Math.max(0, Math.min(1, (db + 60) / 60));
  const bars = 18;
  return (
    <div className="flex h-4 items-end gap-[2px]" aria-label="Microphone level">
      {Array.from({ length: bars }, (_, i) => {
        const on = active && i / bars < level;
        return <span key={i} className={`w-[3px] rounded-[1px] transition-colors duration-75 ${on ? (i > bars * 0.8 ? "bg-hit" : "bg-live") : "bg-line-soft"}`} style={{ height: `${30 + (i / bars) * 70}%` }} />;
      })}
    </div>
  );
}

export function VoicePanel({ snap, events, audioDb, send }: { snap: Snapshot; events: LockedInEvent[]; audioDb: number | null; send: (m: object) => void }) {
  const { run } = useToasts();
  const lines = useMemo(() => transcript(events), [events]);
  const [text, setText] = useState("");
  const [talking, setTalking] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  const input = snap.voice.input;
  const tts = snap.voice.tts;
  const micOn = !!input.running;
  const ptt = snap.settings.voice.listen_mode === "push_to_talk";

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [lines.length]);

  useEffect(() => {
    if (!micOn || !ptt) return;
    const isTyping = (t: EventTarget | null) => t instanceof HTMLInputElement || t instanceof HTMLTextAreaElement || t instanceof HTMLSelectElement;
    const down = (e: KeyboardEvent) => {
      if (e.code !== "Space" || e.repeat || isTyping(e.target)) return;
      e.preventDefault();
      setTalking(true);
      send({ type: "ptt", action: "start" });
    };
    const up = (e: KeyboardEvent) => {
      if (e.code !== "Space" || isTyping(e.target)) return;
      e.preventDefault();
      setTalking(false);
      send({ type: "ptt", action: "stop" });
    };
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
    };
  }, [micOn, ptt, send]);

  const submit = async (t: string) => {
    if (!t.trim()) return;
    setText("");
    await run(api.command(t.trim()));
  };

  const pttDown = () => {
    if (!micOn) return;
    setTalking(true);
    send({ type: "ptt", action: "start" });
  };
  const pttUp = () => {
    if (!talking) return;
    setTalking(false);
    send({ type: "ptt", action: "stop" });
  };

  return (
    <div className="panel flex h-full min-h-[420px] flex-col">
      <SectionHeader
        title="Voice"
        tag={micOn ? <SourceTag source="live">{input.mode === "push_to_talk" ? "Push to talk" : input.mode === "wake_word" ? "Wake word" : "Always on"}</SourceTag> : <SourceTag source="off">Mic off</SourceTag>}
      >
        <button className="btn btn-sm btn-ghost !px-2" onClick={() => run(api.patchSettings({ voice: { muted: !tts.muted } }))} aria-label={tts.muted ? "Unmute robot voice" : "Mute robot voice"} title={`${tts.engine} voice${tts.muted ? " (muted)" : ""}`}>
          {tts.muted ? <VolumeX size={15} /> : <Volume2 size={15} />}
        </button>
        {micOn ? (
          <button className="btn btn-sm" onClick={() => run(api.voiceStop())}>
            <MicOff size={13} /> Off
          </button>
        ) : (
          <button className="btn btn-sm btn-primary" onClick={() => run(api.voiceStart(), false)}>
            <Mic size={13} /> Start mic
          </button>
        )}
      </SectionHeader>

      <VoiceLoop voice={snap.voice} />

      <div ref={listRef} className="scroll-y min-h-[160px] flex-1 space-y-2.5 border-y border-line-soft px-4 py-3">
        {lines.length === 0 ? (
          <p className="text-[13px] leading-relaxed text-ink-3">Talk to it, or type below. Stop, pause and “give me my phone” are matched by fixed rules, never a model, so they work offline.</p>
        ) : (
          lines.map((l) =>
            l.who === "note" ? (
              <div key={l.id} className="num text-[11px] text-ink-4">
                {l.text}
              </div>
            ) : (
              <div key={l.id} className={`flex flex-col ${l.who === "you" ? "items-end" : "items-start"}`}>
                <div
                  className={`max-w-[88%] rounded-[4px] px-3 py-2 text-[13.5px] leading-snug ${
                    l.who === "you" ? "bg-raised text-ink" : l.hit ? "border border-hit/50 bg-hit-wash text-ink" : "border border-line-soft text-ink"
                  }`}
                >
                  {l.text}
                </div>
                {l.meta ? <div className="num mt-1 text-[10.5px] text-ink-4">{l.meta}</div> : null}
              </div>
            ),
          )
        )}
      </div>

      <div className="flex flex-col gap-3 px-4 py-3">
        {micOn && ptt ? (
          <div className="flex items-center gap-3">
            <button
              className={`btn !h-11 flex-1 select-none ${talking ? "border-live bg-live-wash text-live" : ""}`}
              onPointerDown={pttDown}
              onPointerUp={pttUp}
              onPointerLeave={pttUp}
              aria-pressed={talking}
            >
              <Mic size={16} /> {talking ? "Listening…" : "Hold to talk (Space)"}
            </button>
            <Meter db={audioDb} active={micOn} />
          </div>
        ) : micOn ? (
          <div className="flex items-center gap-3 text-[12.5px] text-ink-3">
            <Meter db={audioDb} active /> {input.mode === "wake_word" ? "Say “Locked in, …”" : "Listening continuously"}
          </div>
        ) : input.reason ? (
          <p className="text-[12px] text-ink-4">{input.reason}</p>
        ) : null}
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            void submit(text);
          }}
        >
          <input className="field" placeholder="Type a command…" value={text} onChange={(e) => setText(e.target.value)} maxLength={300} aria-label="Typed command" />
          <button className="btn !w-10 !px-0" type="submit" aria-label="Send">
            <Send size={15} />
          </button>
        </form>
        <div className="flex flex-wrap gap-1.5">
          {SUGGESTIONS.map((s) => (
            <button key={s} className="chip cursor-pointer chip-dim hover:text-ink-2" onClick={() => submit(s)}>
              {s}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
