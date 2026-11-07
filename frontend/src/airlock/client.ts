import { useCallback, useEffect, useRef, useState } from "react";
import { DbConnection } from "../airlock-bindings";
import type { Row } from "./format";

export interface ConnectionConfig { uri: string; database: string; profile: string }
export const DEFAULT_CONFIG: ConnectionConfig = {
  uri: import.meta.env.VITE_SPACETIMEDB_URI || import.meta.env.VITE_SPACETIME_URI || "http://127.0.0.1:3000",
  database: import.meta.env.VITE_SPACETIMEDB_DATABASE || import.meta.env.VITE_SPACETIME_DATABASE || "locki-airlock-sid",
  profile: new URLSearchParams(location.search).get("client") || "operator",
};

export function readConfig(): ConnectionConfig {
  try {
    const saved = JSON.parse(localStorage.getItem("airlock.connection.v1") || "{}");
    return { ...DEFAULT_CONFIG, ...saved, profile: DEFAULT_CONFIG.profile };
  } catch { return DEFAULT_CONFIG; }
}

const VIEWS = {
  workstations: "myWorkstations", memberships: "myMemberships", devices: "myDevices",
  sessions: "mySessions", events: "myDeviceEvents", incidents: "myIncidents",
  health: "myDeviceHealth", locks: "myLockRequests", actions: "myProtectedActions",
  approvals: "myApprovals", runs: "myAgentRuns", steps: "myAgentSteps",
  acknowledgements: "myAcknowledgements", audit: "myAuditEvents", baselines: "myBaselineSnapshots",
} as const;

export type TableName = keyof typeof VIEWS;
export type AirlockData = Record<TableName, Row[]>;
const emptyData = (): AirlockData => Object.fromEntries(Object.keys(VIEWS).map(k => [k, [] as Row[]])) as unknown as AirlockData;
const sqlName = (s: string) => s.replace(/[A-Z]/g, c => `_${c.toLowerCase()}`);
const tokenKey = (c: ConnectionConfig) => `airlock.token.v1:${c.uri}:${c.database}:${c.profile}`;

export type ConnectionStatus = "connecting" | "subscribing" | "connected" | "reconnecting" | "offline";

export function useAirlock(config: ConnectionConfig) {
  const [data, setData] = useState<AirlockData>(emptyData);
  const [status, setStatus] = useState<ConnectionStatus>("connecting");
  const [identity, setIdentity] = useState("");
  const [error, setError] = useState("");
  const [lastSyncedAt, setLastSyncedAt] = useState(0);
  const [retry, setRetry] = useState(0);
  const [pending, setPending] = useState("");
  const connection = useRef<DbConnection | null>(null);
  const configKey = `${config.uri}|${config.database}|${config.profile}`;

  useEffect(() => {
    let stopped = false;
    let retryTimer: number | undefined;
    let refreshTimer: number | undefined;
    let syncTimer: number | undefined;
    setData(emptyData());
    setIdentity("");
    setError("");
    setStatus(retry ? "reconnecting" : "connecting");

    const reschedule = (message: string) => {
      if (stopped) return;
      setStatus("offline");
      setError(message);
      window.clearTimeout(retryTimer);
      retryTimer = window.setTimeout(() => setRetry(v => v + 1), Math.min(15000, 1500 + retry * 1000));
    };
    const sync = (ctx: DbConnection) => {
      if (stopped) return;
      const next = emptyData();
      for (const [key, accessor] of Object.entries(VIEWS)) {
        const table = (ctx.db as any)[accessor];
        if (table) next[key as TableName] = Array.from(table.iter());
      }
      setData(next);
      setLastSyncedAt(Date.now());
    };
    const scheduleSync = (ctx: DbConnection) => {
      window.clearTimeout(syncTimer);
      syncTimer = window.setTimeout(() => sync(ctx), 0);
    };

    try {
      const conn = DbConnection.builder()
        .withUri(config.uri)
        .withDatabaseName(config.database)
        .withToken(localStorage.getItem(tokenKey(config)) || undefined)
        .onConnect((ctx, id, token) => {
          if (stopped) { ctx.disconnect(); return; }
          localStorage.setItem(tokenKey(config), token);
          setIdentity(id.toHexString());
          setStatus("subscribing");
          setError("");
          for (const accessor of Object.values(VIEWS)) {
            const table = (ctx.db as any)[accessor];
            if (!table) continue;
            table.onInsert(() => scheduleSync(ctx));
            table.onDelete(() => scheduleSync(ctx));
            table.onUpdate?.(() => scheduleSync(ctx));
          }
          ctx.subscriptionBuilder()
            .onApplied(() => {
              if (stopped) return;
              sync(ctx);
              setStatus("connected");
              refreshTimer = window.setInterval(() => {
                for (const station of ctx.db.myWorkstations.iter()) {
                  void ctx.reducers.refreshWorkstation({ workstationId: station.id }).catch(() => {});
                }
              }, 5000);
            })
            .onError(ctx => reschedule(`Subscription rejected: ${ctx.event?.message || "check the database name and published module"}`))
            .subscribe(Object.values(VIEWS).map(name => `SELECT * FROM ${sqlName(name)}`));
        })
        .onConnectError((_ctx, err) => reschedule(err.message || "Could not reach SpacetimeDB"))
        .onDisconnect((_ctx, err) => reschedule(err?.message || "SpacetimeDB connection closed"))
        .build();
      connection.current = conn;
    } catch (err) { reschedule(err instanceof Error ? err.message : String(err)); }

    return () => {
      stopped = true;
      window.clearTimeout(retryTimer);
      window.clearTimeout(syncTimer);
      window.clearInterval(refreshTimer);
      connection.current?.disconnect();
      connection.current = null;
    };
  // Each retry opens a fresh connection with the persisted identity.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [configKey, retry]);

  const call = useCallback(async (reducer: string, args: Row) => {
    const conn = connection.current;
    if (!conn || status !== "connected") throw new Error("Connect to SpacetimeDB before changing security state.");
    const method = (conn.reducers as any)[reducer];
    if (typeof method !== "function") throw new Error(`Reducer ${reducer} is unavailable. Check the module version.`);
    setPending(reducer);
    try { await method(args); }
    finally { setPending(""); }
  }, [status]);

  return { data, status, identity, error, lastSyncedAt, pending, call, reconnect: () => setRetry(v => v + 1) };
}
