export type Row = Record<string, any>;

export function field(row: Row | null | undefined, ...keys: string[]): any {
  for (const key of keys) if (row?.[key] !== undefined && row?.[key] !== null) return row[key];
  return undefined;
}

export function textValue(value: unknown, fallback = "—"): string {
  if (value === null || value === undefined || value === "") return fallback;
  if (typeof value === "object" && "toHexString" in value) return (value as { toHexString(): string }).toHexString();
  return String(value);
}

export function shortId(value: unknown): string {
  const str = textValue(value);
  return str.length > 20 ? `${str.slice(0, 10)}…${str.slice(-6)}` : str;
}

export function milliseconds(value: any): number {
  if (!value) return 0;
  if (typeof value === "number") return value > 1e14 ? value / 1000 : value;
  if (typeof value === "bigint") return value > 100000000000000n ? Number(value) / 1000 : Number(value);
  if (value.microsSinceUnixEpoch !== undefined) return Number(value.microsSinceUnixEpoch) / 1000;
  if (typeof value.toDate === "function") return value.toDate().getTime();
  const num = Number(value);
  if (Number.isFinite(num)) return num > 1e14 ? num / 1000 : num;
  return new Date(value).getTime() || 0;
}

export function clock(value: unknown): string {
  const at = milliseconds(value);
  return at ? new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }) : "—";
}

export function ago(value: unknown, now = Date.now()): string {
  const at = milliseconds(value);
  if (!at) return "No heartbeat";
  const seconds = Math.max(0, Math.floor((now - at) / 1000));
  return seconds < 60 ? `${seconds}s ago` : seconds < 3600 ? `${Math.floor(seconds / 60)}m ago` : `${Math.floor(seconds / 3600)}h ago`;
}

export function words(value: unknown): string {
  return textValue(value, "Unknown").replace(/[_-]/g, " ");
}

export function parseJson(value: any, fallback: any = {}): any {
  if (value && typeof value === "object") return value;
  try { return JSON.parse(value); } catch { return fallback; }
}

export function pretty(value: unknown): string {
  if (value === undefined || value === null || value === "") return "No details provided";
  const parsed = typeof value === "string" ? parseJson(value, value) : value;
  return typeof parsed === "string" ? parsed : JSON.stringify(parsed, (_, v) => typeof v === "bigint" ? String(v) : v, 2);
}

export const isSynthetic = (row: Row): boolean => Boolean(field(row, "synthetic", "simulated", "isSynthetic", "is_synthetic")) || textValue(field(row, "source", "provenance", "deviceId", "device_id"), "").includes("fixture");

export function rowId(row: Row): string {
  return textValue(field(row, "eventId", "event_id", "incidentId", "incident_id", "actionId", "action_id", "runId", "run_id", "id"));
}
