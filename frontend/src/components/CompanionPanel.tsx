import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import type { ActionRequestLog, BehaviorDecision, DialogueReplyLog, MindSnap, Snapshot } from "../types/api";
import { SectionHeader, SourceTag } from "./ui";

/** Milestone 5: what the companion knows, why it spoke or stayed quiet, what it asked the
 *  robot to do and what happened. Everything here comes from recorded events. */

function ago(s: number | null | undefined): string {
  if (s == null) return "";
  if (s < 45) return "now";
  const m = Math.round(s / 60);
  return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h${m % 60 ? ` ${m % 60}m` : ""}`;
}

const STATUS_CHIP: Record<ActionRequestLog["status"], string> = {
  requested: "chip-dim",
  executed: "chip-live",
  refused: "chip-sim",
  ignored: "chip-dim",
  failed: "chip-danger",
};

const KIND_LABEL: Record<DialogueReplyLog["kind"], string> = {
  generated: "written now",
  prepared: "prepared ahead",
  fallback: "fixed line",
  fallback_parser: "parser fallback",
};

function ModelLine({ mind }: { mind: MindSnap }) {
  const llm = mind.llm;
  if (!llm) {
    return (
      <p className="px-4 pb-2.5 text-[12px] leading-snug text-ink-3">
        No language model in this process ({mind.provider === "local" ? "weights missing or disabled" : `provider: ${mind.provider}`}). Replies come
        from the line library.
      </p>
    );
  }
  const r = llm.ready ?? {};
  const pf = llm.prefill;
  return (
    <div className="num flex flex-wrap items-center gap-x-3 gap-y-1 px-4 pb-2.5 text-[11px] text-ink-3">
      <span className="text-ink-2">{llm.model}</span>
      <span>on this Mac · offline</span>
      {r.mlx_active_gb != null ? <span>{r.mlx_active_gb.toFixed(2)} GB</span> : null}
      {llm.p50_ms != null ? <span>reply p50 {Math.round(llm.p50_ms)} ms</span> : null}
      {pf && pf.sent ? <span title="context read while you were still talking">prefill {pf.hits}/{pf.sent}</span> : null}
      {llm.failures ? <span className="text-sim">{llm.failures} failed</span> : null}
      {llm.busy ? <span className="text-live">thinking…</span> : null}
      {llm.error ? <span className="text-danger">{llm.error}</span> : null}
    </div>
  );
}

function Context({ mind }: { mind: MindSnap }) {
  const c = mind.context;
  return (
    <div className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5 px-4 pb-3 text-[12.5px] leading-snug">
      <span className="label !leading-[1.6]">goal</span>
      <span className="min-w-0 [overflow-wrap:anywhere] text-ink">
        {c.goal ? (
          <>
            {c.goal.text}
            <span className="num text-[10.5px] text-ink-4"> · {c.goal.source === "user_said" ? "you said" : "dashboard"}</span>
          </>
        ) : (
          <span className="text-ink-4">not stated yet</span>
        )}
      </span>
      <span className="label !leading-[1.6]">bounds</span>
      <span className="min-w-0 text-ink-2">
        {c.quiet ? (
          <span className="text-sim">quiet mode{c.quiet_left_s != null ? ` · ${Math.ceil(c.quiet_left_s / 60)} min left` : ""}</span>
        ) : (
          <span className="text-ink-4">none</span>
        )}
        {c.complaints_10min ? <span className="text-sim"> · you objected {c.complaints_10min}× · remarks halved</span> : null}
      </span>
      {mind.prepared ? (
        <>
          <span className="label !leading-[1.6]">ready</span>
          <span className="min-w-0 [overflow-wrap:anywhere] text-ink-3" title="written in the background for the next confirmed reach">
            “{mind.prepared.text}”<span className="num text-[10.5px] text-ink-4"> · for reach #{mind.prepared.n} · {ago(mind.prepared.age_s)}</span>
          </span>
        </>
      ) : null}
    </div>
  );
}

function Memory({ mind }: { mind: MindSnap }) {
  const obs = mind.context.observations.slice(0, 6);
  const said = mind.context.statements.slice(0, 6);
  return (
    <div className="grid grid-cols-2 gap-x-4 px-4 pb-3">
      <div className="min-w-0">
        <div className="label mb-1.5">observed</div>
        {obs.length ? (
          <ul className="space-y-1">
            {obs.map((o, i) => (
              <li key={`${o.at}-${i}`} className="text-[12px] leading-snug text-ink-2">
                <span className="num text-[10.5px] text-ink-4">{ago(o.age_s).padEnd(3)} </span>
                <span className={o.text.includes("SIMULATED") ? "text-sim" : o.kind === "reach_confirmed" ? "text-hit" : ""}>{o.text}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-[12px] text-ink-4">nothing recorded this session</p>
        )}
      </div>
      <div className="min-w-0">
        <div className="label mb-1.5">you said</div>
        {said.length ? (
          <ul className="space-y-1">
            {said.map((s, i) => (
              <li key={`${s.at}-${i}`} className="text-[12px] leading-snug [overflow-wrap:anywhere] text-ink-2">
                <span className="num text-[10.5px] text-ink-4">{ago(s.age_s).padEnd(3)} </span>“{s.text}”
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-[12px] text-ink-4">nothing yet</p>
        )}
      </div>
    </div>
  );
}

function Decision({ d }: { d: BehaviorDecision }) {
  const [open, setOpen] = useState(false);
  const tone = d.speak ? (d.event === "confirmed_reach" ? "text-hit" : "text-live") : "text-ink-4";
  return (
    <li className="text-[12px] leading-snug">
      <button className="flex w-full items-start gap-1.5 text-left" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        {open ? <ChevronDown size={13} className="mt-[2px] shrink-0 text-ink-4" /> : <ChevronRight size={13} className="mt-[2px] shrink-0 text-ink-4" />}
        <span className={`num w-[52px] shrink-0 text-[10.5px] tracking-[0.08em] uppercase ${tone}`}>{d.speak ? "spoke" : "silent"}</span>
        <span className="min-w-0 flex-1 [overflow-wrap:anywhere] text-ink-2">
          <span className="text-ink">{d.event.replace(/_/g, " ")}</span> · {d.reason}
          {d.said ? (
            <span className="text-ink-3">
              {" "}
              → “{d.said}” <span className="num text-[10.5px] text-ink-4">{d.source}</span>
            </span>
          ) : null}
        </span>
      </button>
      {open ? (
        <ul className="num mt-1 ml-[66px] space-y-0.5 text-[10.5px] leading-snug text-ink-3">
          {d.rules.map((r, i) => (
            <li key={i}>{r}</li>
          ))}
          {d.physical ? <li className="text-sim">physical: {d.physical}</li> : null}
        </ul>
      ) : null}
    </li>
  );
}

function Requests({ items }: { items: ActionRequestLog[] }) {
  if (!items.length) return <p className="px-4 pb-3 text-[12px] text-ink-4">No action requested yet.</p>;
  return (
    <ul className="space-y-1.5 px-4 pb-3">
      {items.slice(0, 6).map((r) => (
        <li key={r.id} className="flex items-start gap-2 text-[12px] leading-snug">
          <span className={`chip ${STATUS_CHIP[r.status]} shrink-0`}>{r.status}</span>
          <span className="min-w-0 [overflow-wrap:anywhere] text-ink-2">
            <span className="num text-ink">{r.name}</span>
            {Object.keys(r.args ?? {}).length ? <span className="num text-[10.5px] text-ink-3"> {JSON.stringify(r.args)}</span> : null}
            <span className="num text-[10.5px] text-ink-4">
              {" "}
              · {r.origin}
              {r.physical ? " · physical" : ""}
            </span>
            {r.reason && r.status !== "executed" ? <span className="block text-[11.5px] text-ink-3">{r.reason}</span> : null}
          </span>
        </li>
      ))}
    </ul>
  );
}

function Replies({ items }: { items: DialogueReplyLog[] }) {
  if (!items.length) return <p className="px-4 pb-3 text-[12px] text-ink-4">Talk to it: replies the model writes show up here with their timings.</p>;
  return (
    <ul className="space-y-2 px-4 pb-3">
      {items.slice(0, 5).map((r) => (
        <li key={r.id} className="text-[12px] leading-snug">
          <div className="[overflow-wrap:anywhere] text-ink-3">{r.user ? `“${r.user}”` : <span className="text-ink-4">event: {r.event?.replace(/_/g, " ")}</span>}</div>
          <div className="[overflow-wrap:anywhere] text-ink">
            {r.spoken ? `→ ${r.spoken}` : <span className="text-ink-4">→ (fixed line)</span>}
          </div>
          <div className="num text-[10.5px] text-ink-4">
            {KIND_LABEL[r.kind] ?? r.kind}
            {r.llm?.first_token_ms != null ? ` · first token ${Math.round(r.llm.first_token_ms)} ms` : ""}
            {r.llm?.total_ms != null ? ` · ${Math.round(r.llm.total_ms)} ms` : ""}
            {r.streamed ? ` · ${r.streamed} sentence${r.streamed > 1 ? "s" : ""} streamed` : ""}
            {r.action ? ` · ${r.action.name} ${r.action.status}` : ""}
          </div>
          {r.problems?.length ? <div className="text-[11px] text-sim">{r.problems.join(" · ")}</div> : null}
          {r.rejected_say ? <div className="text-[11px] text-ink-4">not spoken: “{r.rejected_say}”</div> : null}
        </li>
      ))}
    </ul>
  );
}

export function CompanionPanel({ snap }: { snap: Snapshot }) {
  const mind = snap.mind;
  if (!mind) return null;
  const source = mind.available ? "live" : "off";
  return (
    <div className="panel flex flex-col">
      <SectionHeader title="Companion" tag={<SourceTag source={source}>{mind.available ? "Local model" : "Line library only"}</SourceTag>} />
      <ModelLine mind={mind} />
      <div className="rule" />
      <div className="pt-2.5">
        <Context mind={mind} />
        <Memory mind={mind} />
      </div>
      <div className="rule" />
      <div className="px-4 pt-2.5 pb-1.5">
        <div className="label">why it spoke or stayed quiet</div>
      </div>
      {mind.behavior.decisions.length ? (
        <ul className="space-y-1 px-4 pb-3">
          {mind.behavior.decisions.slice(0, 7).map((d) => (
            <Decision key={d.id} d={d} />
          ))}
        </ul>
      ) : (
        <p className="px-4 pb-3 text-[12px] text-ink-4">No unprompted moment yet. Reaches, streaks and camera drops are decided here.</p>
      )}
      <div className="rule" />
      <div className="px-4 pt-2.5 pb-1.5">
        <div className="label">actions it asked for</div>
      </div>
      <Requests items={mind.requests} />
      <div className="rule" />
      <div className="px-4 pt-2.5 pb-1.5">
        <div className="label">replies</div>
      </div>
      <Replies items={mind.replies} />
    </div>
  );
}
