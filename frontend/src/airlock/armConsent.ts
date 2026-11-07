import { type Row, textValue } from "./format";

export interface ArmConsent { baselineJson: string; fingerprint: string }

/** Bind consent to reviewed contents/authority, not periodic capture timestamps. */
export function armConsent(station: Row, capture: Row | undefined, apps: string[], duration: string): ArmConsent | null {
  if (!capture?.deviceId || !capture.publisher) return null;
  let keys: unknown;
  try { keys = JSON.parse(capture.baselineJson); } catch { return null; }
  if (!Array.isArray(keys) || keys.length > 256 || keys.some(key => typeof key !== "string" || !key.length || key.length > 256)) return null;
  const baselineJson = JSON.stringify([...new Set(keys)].sort());
  const fingerprint = JSON.stringify([
    station.id, textValue(station.owner, ""), capture.deviceId, textValue(capture.publisher, ""), baselineJson,
    station.ownerPolicyHash, String(station.policyVersion), Boolean(station.autoLockEnabled), String(station.lockCooldownMs),
    [...new Set(apps)].sort(), duration,
  ]);
  return { baselineJson, fingerprint };
}

export function maySubmitLiveArm(consented: string | null, current: ArmConsent | null, observedAtMs: unknown, now: number): boolean {
  const age = now - Number(observedAtMs);
  return Boolean(current && consented === current.fingerprint && Number.isFinite(age) && age >= -5000 && age <= 30000);
}
