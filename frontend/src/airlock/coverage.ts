import { milliseconds, type Row } from "./format";

/** Device-health views may omit other devices' enrollment rows for Mac clients. */
export function latestHealth(health: Row[], devices: Row[], component: string): Row | undefined {
  return health.filter(h => h.component === component && h.deviceId !== "fixture" && !h.deviceId.endsWith(":fixture")
    && !devices.some(d => d.id === h.deviceId && !d.enabled))
    .sort((a, b) => milliseconds(b.lastSeenMs) - milliseconds(a.lastSeenMs))[0];
}

export function healthStatus(health: Row | undefined, now: number, connected: boolean, enrolled = true): string {
  if (!connected) return "unverified";
  if (!health) return enrolled ? "no heartbeat" : "not enrolled";
  const observed = milliseconds(health.lastSeenMs);
  if (!Number.isFinite(observed) || observed <= 0 || observed > now + 5000) return "unverified";
  if (["online", "degraded"].includes(health.status) && now - observed > 15000) return "stale";
  return ["online", "degraded", "offline", "unavailable"].includes(health.status) ? health.status : "unverified";
}

/** Model readiness is separate from deterministic camera/USB/app coverage. */
export function detectionCoverage(health: Row[], devices: Row[], now: number, connected: boolean): "available" | "degraded" | "unverified" {
  if (!connected) return "unverified";
  if (health.some(h => h.deviceId === "database" && h.component === "capacity" && h.status === "degraded")) return "degraded";
  return ["camera", "usb", "apps"].every(component => healthStatus(latestHealth(health, devices, component), now, connected) === "online") ? "available" : "degraded";
}
