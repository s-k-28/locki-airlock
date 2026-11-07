import { describe, expect, it } from "vitest";
import { armConsent, maySubmitLiveArm } from "./armConsent";

const station = { id: "desk", owner: "owner-a", ownerPolicyHash: "policy-a", policyVersion: 1, autoLockEnabled: true, lockCooldownMs: 30000n };
const capture = { deviceId: "mac-a", publisher: { toHexString: () => "publisher-a" }, baselineJson: '["usb-b","usb-a"]', observedAtMs: 100000n };
const reviewed = () => armConsent(station, capture, ["app-a"], "60")!;

describe("live arm consent", () => {
  it("submits the exact canonical reviewed USB set, including an explicitly empty inventory", () => {
    expect(reviewed().baselineJson).toBe('["usb-a","usb-b"]');
    expect(armConsent(station, { ...capture, baselineJson: "[]" }, [], "60")?.baselineJson).toBe("[]");
  });

  it("blocks changed inventory synchronously before any UI reset effect", () => {
    const old = reviewed();
    const changed = armConsent(station, { ...capture, baselineJson: '["new-usb"]' }, ["app-a"], "60");
    expect(maySubmitLiveArm(old.fingerprint, changed, capture.observedAtMs, 101000)).toBe(false);
    expect(maySubmitLiveArm(changed!.fingerprint, changed, capture.observedAtMs, 101000)).toBe(true);
  });

  it.each([
    { id: "other-desk" }, { owner: "owner-b" }, { ownerPolicyHash: "policy-b" },
    { policyVersion: 2 }, { autoLockEnabled: false }, { lockCooldownMs: 60000n },
  ])("invalidates consent when workstation authority or policy changes (%#)", change => {
    const current = armConsent({ ...station, ...change }, capture, ["app-a"], "60");
    expect(maySubmitLiveArm(reviewed().fingerprint, current, capture.observedAtMs, 101000)).toBe(false);
  });

  it("invalidates changes to publisher, device, app allowlist and duration", () => {
    const variants = [
      armConsent(station, { ...capture, publisher: "publisher-b" }, ["app-a"], "60"),
      armConsent(station, { ...capture, deviceId: "mac-b" }, ["app-a"], "60"),
      armConsent(station, capture, ["app-b"], "60"),
      armConsent(station, capture, ["app-a"], "15"),
    ];
    for (const current of variants) expect(maySubmitLiveArm(reviewed().fingerprint, current, capture.observedAtMs, 101000)).toBe(false);
  });

  it("preserves consent across timestamp-only refresh and equivalent canonical inventories", () => {
    const refreshed = armConsent(station, { ...capture, observedAtMs: 101000n, baselineJson: '["usb-a","usb-b","usb-a"]' }, ["app-a"], "60");
    expect(refreshed?.fingerprint).toBe(reviewed().fingerprint);
    expect(maySubmitLiveArm(reviewed().fingerprint, refreshed, 101000n, 102000)).toBe(true);
  });

  it("checks consent and capture freshness again at submission time", () => {
    expect(maySubmitLiveArm(null, reviewed(), 100000n, 100000)).toBe(false);
    for (const now of [94999, 130001, NaN]) expect(maySubmitLiveArm(reviewed().fingerprint, reviewed(), 100000n, now)).toBe(false);
    for (const now of [95000, 130000]) expect(maySubmitLiveArm(reviewed().fingerprint, reviewed(), 100000n, now)).toBe(true);
  });

  it("fails closed on missing publisher or malformed baseline instead of consenting to an invented empty inventory", () => {
    for (const baselineJson of ["not-json", "null", "{}", '[42]', '[""]', JSON.stringify(["x".repeat(257)])]) {
      expect(armConsent(station, { ...capture, baselineJson }, [], "60")).toBeNull();
    }
    expect(armConsent(station, { ...capture, publisher: "" }, [], "60")).toBeNull();
    expect(armConsent(station, undefined, [], "60")).toBeNull();
  });
});
