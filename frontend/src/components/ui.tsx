import { useEffect, useState, type ReactNode } from "react";

export function SectionHeader({ title, children, tag }: { title: string; children?: ReactNode; tag?: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 px-4 pt-3 pb-2.5">
      <div className="flex min-w-0 items-center gap-2.5">
        <h2 className="font-display text-[13px] font-semibold tracking-[0.18em] text-ink-2 uppercase">{title}</h2>
        {tag}
      </div>
      <div className="flex shrink-0 items-center gap-1.5">{children}</div>
    </div>
  );
}

export type Source = "live" | "sim" | "off" | "warn";

export function SourceTag({ source, children }: { source: Source; children?: ReactNode }) {
  const cls = source === "live" ? "chip-live" : source === "sim" ? "chip-sim" : source === "warn" ? "chip-danger" : "chip-dim";
  const text = children ?? (source === "live" ? "Live" : source === "sim" ? "Simulated" : "Unavailable");
  return <span className={`chip ${cls}`}>{text}</span>;
}

export function Readout({ label, value, unit, tone, title }: { label: string; value: ReactNode; unit?: string; tone?: "live" | "hit" | "sim" | "danger" | "dim"; title?: string }) {
  const color =
    tone === "live" ? "text-live" : tone === "hit" ? "text-hit" : tone === "sim" ? "text-sim" : tone === "danger" ? "text-danger" : tone === "dim" ? "text-ink-3" : "text-ink";
  return (
    <div className="min-w-0" title={title}>
      <div className="label">{label}</div>
      <div className={`num mt-1 truncate text-[13px] ${color}`}>
        {value}
        {unit ? <span className="ml-1 text-ink-3">{unit}</span> : null}
      </div>
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="flex flex-col items-start gap-2 px-4 py-6">
      <div className="font-display text-[15px] font-semibold tracking-[0.12em] text-ink-2 uppercase">{title}</div>
      {children ? <div className="max-w-[46ch] text-[13px] leading-relaxed text-ink-3">{children}</div> : null}
    </div>
  );
}

export function Toggle({ on, onChange, label, disabled, tone = "live" }: { on: boolean; onChange: (v: boolean) => void; label: string; disabled?: boolean; tone?: "live" | "sim" }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      disabled={disabled}
      onClick={() => onChange(!on)}
      className="group inline-flex items-center gap-2.5 disabled:cursor-not-allowed disabled:opacity-40"
    >
      <span
        className={`relative inline-block h-[18px] w-[32px] shrink-0 rounded-[3px] border transition-colors duration-200 ${
          on ? (tone === "sim" ? "border-sim bg-sim/25" : "border-live bg-live/20") : "border-line bg-well"
        }`}
      >
        <span
          className={`absolute top-[2px] left-0 h-[12px] w-[12px] rounded-[2px] transition-transform duration-200 ease-[cubic-bezier(0.25,1,0.5,1)] ${
            on ? `translate-x-[16px] ${tone === "sim" ? "bg-sim" : "bg-live"}` : "translate-x-[2px] bg-ink-3"
          }`}
        />
      </span>
      <span className="text-[13px] font-medium text-ink-2 group-hover:text-ink">{label}</span>
    </button>
  );
}

export function Section({ title, children, note }: { title: string; children: ReactNode; note?: string }) {
  return (
    <section className="flex flex-col gap-4 border-b border-line-soft px-5 py-5">
      <div>
        <h3 className="font-display text-[13px] font-semibold tracking-[0.18em] text-ink-2 uppercase">{title}</h3>
        {note ? <p className="mt-1 text-[12px] leading-relaxed text-ink-4">{note}</p> : null}
      </div>
      {children}
    </section>
  );
}

export function Slider({ label, value, min, max, step, unit, onCommit, fmt }: { label: string; value: number; min: number; max: number; step: number; unit?: string; onCommit: (v: number) => void; fmt?: (v: number) => string }) {
  const [v, setV] = useState(value);
  useEffect(() => setV(value), [value]);
  return (
    <label className="block">
      <div className="flex items-baseline justify-between">
        <span className="text-[13px] text-ink-2">{label}</span>
        <span className="num text-[12px] text-ink">
          {fmt ? fmt(v) : v}
          {unit ? <span className="text-ink-3"> {unit}</span> : null}
        </span>
      </div>
      <input type="range" min={min} max={max} step={step} value={v} onChange={(e) => setV(Number(e.target.value))} onPointerUp={() => v !== value && onCommit(v)} onKeyUp={() => v !== value && onCommit(v)} />
    </label>
  );
}

export function Segmented<T extends string>({ value, options, onChange }: { value: T; options: { v: T; label: string }[]; onChange: (v: T) => void }) {
  return (
    <div className="flex">
      {options.map((o) => (
        <button
          key={o.v}
          className={`h-8 flex-1 border px-3 text-[12px] font-semibold tracking-[0.08em] uppercase transition-colors [font-stretch:85%] first:rounded-l-[4px] last:rounded-r-[4px] [&+&]:-ml-px ${
            value === o.v ? "relative z-10 border-live bg-live-wash text-live" : "border-line bg-well text-ink-3 hover:text-ink-2"
          }`}
          onClick={() => onChange(o.v)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}
