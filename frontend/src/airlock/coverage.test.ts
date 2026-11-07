import { describe, expect, it } from "vitest";
import { detectionCoverage, healthStatus, latestHealth } from "./coverage";

const now = 100_000;
const row = (component: string, status = "online", lastSeenMs = now) => ({ deviceId: component, component, status, lastSeenMs });
const healthy = [row("camera"), row("usb"), row("apps")];
const devices = healthy.map(h => ({ id: h.deviceId, enabled: true }));

describe("truthful detection coverage", () => {
  it("does not report cached online sensors as available after disconnect", () => {
    expect(detectionCoverage(healthy, devices, now, false)).toBe("unverified");
    expect(healthStatus(healthy[0], now, false)).toBe("unverified");
  });
  it("requires app coverage as well as camera and USB", () => {
    expect(detectionCoverage(healthy, devices, now, true)).toBe("available");
    expect(detectionCoverage([row("camera"), row("usb"), row("apps", "offline")], devices, now, true)).toBe("degraded");
  });
  it("keeps model availability distinct from deterministic protection", () => {
    expect(detectionCoverage([...healthy, row("model", "offline")], devices, now, true)).toBe("available");
  });
  it("excludes a revoked publisher even when its latest report is online", () => {
    const revoked = devices.map(d => ({ ...d, enabled: d.id !== "camera" }));
    expect(latestHealth(healthy, revoked, "camera")).toBeUndefined();
    expect(detectionCoverage(healthy, revoked, now, true)).toBe("degraded");
  });
  it("accepts server-authorized health when a Mac cannot see another device's enrollment", () => {
    expect(latestHealth(healthy, [], "camera")?.status).toBe("online");
  });
  it.each(["online", "degraded"])("expires stale %s reports at the server's boundary", status => {
    expect(healthStatus(row("camera", status, now - 15000), now, true)).toBe(status);
    expect(healthStatus(row("camera", status, now - 15001), now, true)).toBe("stale");
  });
  it("cannot hide sticky server capacity loss behind fresh heartbeats", () => {
    expect(detectionCoverage([...healthy, { deviceId: "database", component: "capacity", status: "degraded", lastSeenMs: 1 }], devices, now, true)).toBe("degraded");
  });
  it("does not treat fixture health or generic watcher liveness as physical coverage", () => {
    expect(latestHealth([{ ...row("camera"), deviceId: "desk:fixture" }], [], "camera")).toBeUndefined();
    expect(detectionCoverage([row("watcher"), row("camera"), row("apps")], devices, now, true)).toBe("degraded");
  });
  it("reports missing, invalid, and future reports without claiming online coverage", () => {
    expect(healthStatus(undefined, now, true, false)).toBe("not enrolled");
    expect(healthStatus(undefined, now, true, true)).toBe("no heartbeat");
    expect(healthStatus(row("usb", "online", 0), now, true)).toBe("unverified");
    expect(healthStatus(row("usb", "online", now + 5001), now, true)).toBe("unverified");
  });
});
