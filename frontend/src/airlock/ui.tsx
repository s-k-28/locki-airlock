import { useEffect, useId, useRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { X, Terminal, ArrowUpRight } from "lucide-react";
import { pretty } from "./format";

export type Tone = "cyan" | "amber" | "magenta" | "quiet";
export function Badge({ children, tone = "quiet", dot = false }: { children: ReactNode; tone?: Tone; dot?: boolean }) {
  return <span className={`al-badge al-${tone}`}>{dot && <i />}{children}</span>;
}
export function Button({ children, kind = "default", ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { kind?: "default" | "primary" | "warning" | "ghost" }) {
  return <button className={`al-button al-button-${kind}`} {...props}>{children}</button>;
}
export function Panel({ id, title, icon, aside, children, className = "" }: { id?: string; title: string; icon?: ReactNode; aside?: ReactNode; children: ReactNode; className?: string }) {
  return <section id={id} className={`al-panel ${className}`}><header className="al-panel-heading"><h2>{icon}{title}</h2>{aside}</header>{children}</section>;
}
export function Empty({ icon, title, children }: { icon?: ReactNode; title: string; children: ReactNode }) {
  return <div className="al-empty">{icon && <span className="al-empty-icon">{icon}</span>}<strong>{title}</strong><p>{children}</p></div>;
}
export function JsonDetails({ label = "Evidence details", value }: { label?: string; value: unknown }) {
  return <details className="al-json"><summary><Terminal size={12} />{label}</summary><pre>{pretty(value)}</pre></details>;
}
export function Modal({ title, subtitle, children, onClose }: { title: string; subtitle?: string; children: ReactNode; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const subtitleId = useId();
  useEffect(() => { const dialog = ref.current; dialog?.showModal(); return () => dialog?.close(); }, []);
  return <dialog ref={ref} className="al-modal" aria-labelledby={titleId} aria-describedby={subtitle ? subtitleId : undefined} onCancel={e => { e.preventDefault(); onClose(); }} onClick={e => { if (e.target === e.currentTarget) onClose(); }}><div className="al-modal-inner"><header><div><h2 id={titleId}>{title}</h2>{subtitle && <p id={subtitleId}>{subtitle}</p>}</div><button type="button" className="al-icon-button" aria-label="Close dialog" onClick={onClose}><X size={19} /></button></header>{children}</div></dialog>;
}
export function FormField({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return <label className="al-form-field"><span>{label}</span>{children}{hint && <small>{hint}</small>}</label>;
}
export function ExternalLink({ href, children }: { href: string; children: ReactNode }) {
  return <a className="al-link" href={href} target="_blank" rel="noreferrer">{children}<ArrowUpRight size={13} /></a>;
}
