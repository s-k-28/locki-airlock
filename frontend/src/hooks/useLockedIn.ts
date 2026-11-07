import { useCallback, useEffect, useRef, useState } from "react";
import type { LockedInEvent, Snapshot } from "../types/api";

export type LinkStatus = "connecting" | "open" | "closed";

export interface LiveState {
  snapshot: Snapshot | null;
  snapshotAt: number; // performance.now() when the snapshot arrived
  events: LockedInEvent[];
  link: LinkStatus;
  wsLatencyMs: number | null;
  audioDb: number | null;
  lastLiveEvent: LockedInEvent | null;
  send: (msg: object) => void;
}

const MAX_EVENTS = 400;

export function useLockedIn(): LiveState {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [snapshotAt, setSnapshotAt] = useState(0);
  const [events, setEvents] = useState<LockedInEvent[]>([]);
  const [link, setLink] = useState<LinkStatus>("connecting");
  const [wsLatencyMs, setLatency] = useState<number | null>(null);
  const [audioDb, setAudioDb] = useState<number | null>(null);
  const [lastLiveEvent, setLastLiveEvent] = useState<LockedInEvent | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const retryRef = useRef(0);
  const seenRef = useRef<Set<string>>(new Set());
  const runRef = useRef<string | null>(null);

  useEffect(() => {
    let closed = false;
    let pingTimer: number | undefined;
    let reconnectTimer: number | undefined;

    const connect = () => {
      setLink("connecting");
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${proto}://${location.host}/ws`);
      wsRef.current = ws;

      ws.onopen = () => {
        retryRef.current = 0;
        setLink("open");
        pingTimer = window.setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "ping", t: performance.now() }));
        }, 2000);
      };

      ws.onmessage = (msg) => {
        let data: any;
        try {
          data = JSON.parse(msg.data);
        } catch {
          return;
        }
        if (data.type === "snapshot") {
          const snap = data.data as Snapshot;
          if (runRef.current !== snap.run_id) {
            // The backend restarted: events from the previous run must not look current.
            if (runRef.current !== null) {
              seenRef.current = new Set();
              setEvents([]);
              setLastLiveEvent(null);
            }
            runRef.current = snap.run_id;
          }
          setSnapshot(snap);
          setSnapshotAt(performance.now());
        } else if (data.type === "event") {
          const ev = data.data as LockedInEvent;
          if (seenRef.current.has(ev.event_id)) return;
          seenRef.current.add(ev.event_id);
          if (seenRef.current.size > MAX_EVENTS * 3) seenRef.current = new Set(Array.from(seenRef.current).slice(-MAX_EVENTS));
          setEvents((prev) => {
            const next = prev.length >= MAX_EVENTS ? prev.slice(prev.length - MAX_EVENTS + 1) : prev.slice();
            next.push(ev);
            return next;
          });
          if (!data.replay) setLastLiveEvent(ev);
        } else if (data.type === "pong" && typeof data.t === "number") {
          setLatency(Math.round((performance.now() - data.t) * 10) / 10);
        } else if (data.type === "audio") {
          setAudioDb(typeof data.data?.rms_db === "number" ? data.data.rms_db : null);
        }
      };

      ws.onclose = () => {
        window.clearInterval(pingTimer);
        setLink("closed");
        if (closed) return;
        const delay = Math.min(5000, 400 * 2 ** retryRef.current);
        retryRef.current += 1;
        reconnectTimer = window.setTimeout(connect, delay);
      };
      ws.onerror = () => ws.close();
    };

    connect();
    return () => {
      closed = true;
      window.clearInterval(pingTimer);
      window.clearTimeout(reconnectTimer);
      wsRef.current?.close();
    };
  }, []);

  const send = useCallback((msg: object) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
  }, []);

  return { snapshot, snapshotAt, events, link, wsLatencyMs, audioDb, lastLiveEvent, send };
}

/** Re-renders at `hz` so timers can interpolate between snapshots. */
export function useTicker(hz = 10): number {
  const [now, setNow] = useState(() => performance.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(performance.now()), 1000 / hz);
    return () => window.clearInterval(id);
  }, [hz]);
  return now;
}
