import { useEffect, useState } from "react";
import { Mic, SkipForward, Timer } from "lucide-react";
import type { Snapshot, VoicePromptDef, VoiceTrialRecord } from "../types/api";
import { api } from "../services/api";
import { pct } from "../lib/format";
import { Toggle } from "./ui";
import { useToasts } from "./Toasts";

const CONDITIONS = ["quiet room", "hackathon noise", "music playing", "robot speaker talking", "2 m from the mic"];

const OUTCOME_TONE: Record<VoiceTrialRecord["outcome"], string> = {
  correct: "text-live",
  correct_reject: "text-live",
  rejected: "text-sim",
  no_response: "text-sim",
  wrong_intent: "text-danger",
  wrong_slots: "text-danger",
  false_activation: "text-danger",
};

/** Real-speech trials (docs/VOICE_TESTS.md): prompt, speak, compare with what was heard. */
export function VoiceTrialPanel({ snap }: { snap: Snapshot }) {
  const { run } = useToasts();
  const vt = snap.experiment.voice;
  const [prompts, setPrompts] = useState<VoicePromptDef[]>([]);
  const [next, setNext] = useState(0);
  const [speaker, setSpeaker] = useState("A");
  const [condition, setCondition] = useState(CONDITIONS[0]);
  const [execute, setExecute] = useState(false);

  useEffect(() => {
    api.voicePrompts().then((r) => setPrompts(r.prompts)).catch(() => setPrompts([]));
  }, []);

  const showNext = () => {
    if (!prompts.length) return;
    const p = prompts[next % prompts.length];
    setNext((i) => i + 1);
    run(api.voicePrompt({ expected: p.expected, phrase: p.phrase, slots: p.slots }));
  };

  if (!vt.active || !vt.block) {
    return (
      <div className="flex flex-col gap-3">
        <p className="max-w-[60ch] text-[13px] leading-relaxed text-ink-3">
          Voice trials for docs/VOICE_TESTS.md. One block = one speaker in one condition. Use a label, not a name. Only text is kept (data/experiments/voice_trials.jsonl), never audio. Start the
          microphone first.
        </p>
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1">
            <span className="label">Speaker label</span>
            <input className="field num !h-8 !w-[90px] !text-[12px]" maxLength={24} value={speaker} onChange={(e) => setSpeaker(e.target.value)} />
          </label>
          <label className="flex flex-col gap-1">
            <span className="label">Condition</span>
            <input className="field !h-8 !w-[200px] !text-[12px]" list="voice-conditions" maxLength={60} value={condition} onChange={(e) => setCondition(e.target.value)} />
            <datalist id="voice-conditions">
              {CONDITIONS.map((c) => (
                <option key={c} value={c} />
              ))}
            </datalist>
          </label>
          <Toggle on={execute} onChange={setExecute} label={execute ? "Commands run" : "Dry run (score only)"} tone="sim" />
          <button className="btn btn-primary" disabled={!speaker.trim() || !condition.trim()} onClick={() => run(api.voiceBlock(speaker, condition, execute))}>
            <Mic size={14} /> Start block
          </button>
        </div>
        <Groups snap={snap} />
      </div>
    );
  }

  const b = vt.block;
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="chip chip-sim">
          Speaker {b.speaker} · {b.condition} · {b.execute ? "commands run" : "dry run"}
        </span>
        <button className="btn btn-sm ml-auto" onClick={() => run(api.voiceEnd())}>
          End block
        </button>
      </div>
      <div className="flex flex-wrap items-stretch gap-3">
        <div className="min-w-[260px] flex-1 border border-line px-4 py-3" aria-live="polite">
          {b.quiet_left_s != null && b.quiet_left_s > 0 ? (
            <>
              <div className="label">Quiet window: talk normally, no commands</div>
              <div className="num mt-1 text-[28px] text-ink">{Math.ceil(b.quiet_left_s)}s</div>
            </>
          ) : b.prompt ? (
            <>
              <div className="label">Say this · expect {b.prompt.expected.replace(/_/g, " ")}</div>
              <div className="mt-1 font-display text-[22px] leading-snug text-ink">“{b.prompt.phrase}”</div>
            </>
          ) : (
            <div className="text-[13px] text-ink-3">Press Next prompt, then the speaker says it once (push-to-talk or the configured mode).</div>
          )}
        </div>
        <div className="flex flex-col gap-2">
          <button className="btn btn-hit" disabled={!prompts.length} onClick={showNext}>
            <SkipForward size={14} /> Next prompt {prompts.length ? `(${(next % prompts.length) + 1}/${prompts.length})` : ""}
          </button>
          <button className="btn btn-sm" disabled={!b.prompt} onClick={() => run(api.voiceMiss())}>
            Nothing heard
          </button>
          <button className="btn btn-sm" onClick={() => run(api.voiceQuiet(120))}>
            <Timer size={12} /> Quiet window 2 min
          </button>
        </div>
      </div>
      {vt.recent.length ? (
        <table className="num w-full text-[11.5px]">
          <thead>
            <tr className="text-left text-ink-4">
              <th className="py-1 pr-3 font-normal">asked</th>
              <th className="pr-3 font-normal">heard</th>
              <th className="pr-3 font-normal">intent</th>
              <th className="pr-3 font-normal">STT</th>
              <th className="font-normal">outcome</th>
            </tr>
          </thead>
          <tbody>
            {vt.recent
              .slice()
              .reverse()
              .map((r, i) => (
                <tr key={`${r.at}-${i}`} className="border-t border-line-soft/60 align-top">
                  <td className="py-1 pr-3 text-ink-3">{r.window === "quiet" ? "(quiet window)" : r.phrase}</td>
                  <td className="pr-3 break-all text-ink">{r.heard_text ?? "—"}</td>
                  <td className="pr-3 text-ink-2">{r.heard_intent ?? "—"}</td>
                  <td className="pr-3 text-ink-3">{r.stt_ms != null ? `${Math.round(r.stt_ms)} ms` : "—"}</td>
                  <td className={OUTCOME_TONE[r.outcome]} title={r.rejected_reason ?? undefined}>
                    {r.outcome.replace(/_/g, " ")}
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
      ) : null}
      <Groups snap={snap} />
    </div>
  );
}

function Groups({ snap }: { snap: Snapshot }) {
  const groups = snap.experiment.voice.groups;
  if (!groups.length) return null;
  return (
    <table className="num w-full text-[11.5px]">
      <thead>
        <tr className="text-left text-ink-4">
          <th className="py-1 pr-3 font-normal">speaker · condition</th>
          <th className="pr-3 font-normal">n</th>
          <th className="pr-3 font-normal">accuracy</th>
          <th className="pr-3 font-normal">wrong</th>
          <th className="pr-3 font-normal">rejected</th>
          <th className="pr-3 font-normal">stop ok</th>
          <th className="pr-3 font-normal">FA /10 min</th>
          <th className="pr-3 font-normal">risky</th>
          <th className="font-normal">STT p50</th>
        </tr>
      </thead>
      <tbody>
        {groups.map((g) => (
          <tr key={`${g.speaker}|${g.condition}`} className="border-t border-line-soft/60">
            <td className="py-1 pr-3 text-ink-2">
              {g.speaker} · {g.condition}
            </td>
            <td className="pr-3 text-ink-3">{g.prompts}</td>
            <td className="pr-3 text-ink">{pct(g.accuracy)}</td>
            <td className={`pr-3 ${g.wrong_intent_rate ? "text-danger" : "text-ink-3"}`}>{pct(g.wrong_intent_rate)}</td>
            <td className="pr-3 text-ink-3">{pct(g.rejection_rate)}</td>
            <td className="pr-3 text-ink">{g.stop_prompts ? pct(g.stop_recognised) : "—"}</td>
            <td className="pr-3 text-ink-3">{g.false_activations_per_10_min ?? "—"}</td>
            <td className={`pr-3 ${g.risky_errors ? "text-danger" : "text-ink-3"}`}>{g.risky_errors}</td>
            <td className="text-ink-3">{g.stt_ms_p50 != null ? `${Math.round(g.stt_ms_p50)} ms` : "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
