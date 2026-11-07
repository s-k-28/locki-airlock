import { createContext, useCallback, useContext, useState, type ReactNode } from "react";

interface Toast {
  id: number;
  text: string;
  tone: "ok" | "err" | "info";
}

interface ToastApi {
  notify: (text: string, tone?: Toast["tone"]) => void;
  run: (p: Promise<any>, quietOnSuccess?: boolean) => Promise<any>;
}

const Ctx = createContext<ToastApi | null>(null);
let seq = 0;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const notify = useCallback((text: string, tone: Toast["tone"] = "info") => {
    const id = ++seq;
    setToasts((t) => [...t.slice(-3), { id, text, tone }]);
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), tone === "err" ? 6000 : 3200);
  }, []);

  const run = useCallback(
    async (p: Promise<any>, quietOnSuccess = true) => {
      try {
        const r = await p;
        if (r && r.ok === false) notify(r.message ?? "Request refused", "err");
        else if (!quietOnSuccess && r?.message) notify(r.message, "ok");
        return r;
      } catch (e: any) {
        notify(e?.message ?? String(e), "err");
        return null;
      }
    },
    [notify],
  );

  return (
    <Ctx.Provider value={{ notify, run }}>
      {children}
      <div className="pointer-events-none fixed bottom-5 left-5 z-50 flex w-[min(420px,calc(100vw-40px))] flex-col gap-2" aria-live="polite">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={`pop-in pointer-events-auto rounded-[4px] border px-3.5 py-2.5 text-[13px] leading-snug ${
              t.tone === "err" ? "border-danger/60 bg-[oklch(0.2_0.04_27)] text-ink" : t.tone === "ok" ? "border-live/40 bg-raised text-ink" : "border-line bg-raised text-ink-2"
            }`}
          >
            {t.text}
          </div>
        ))}
      </div>
    </Ctx.Provider>
  );
}

export function useToasts(): ToastApi {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("ToastProvider missing");
  return ctx;
}
