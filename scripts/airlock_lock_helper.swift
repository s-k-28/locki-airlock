// AIRLOCK's only OS-action boundary. Building this file never sends a key event.
// Execution requires explicit mode, operator flag, private local consent and fresh request.
import Foundation
import ApplicationServices
import CoreGraphics
import Carbon.HIToolbox
import Darwin

func emit(_ value: [String: Any], code: Int32 = 0) -> Never {
    if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]),
       let output = String(data: data, encoding: .utf8) { print(output) }
    exit(code)
}
func deny(_ message: String) -> Never {
    emit(["shortcutPosted": false, "screenLocked": NSNull(), "confirmationSupported": false,
          "status": "failed", "reason": message], code: 2)
}
func sessionObservation() -> [String: Any] {
    let available = CGSessionCopyCurrentDictionary() != nil
    // Public Quartz session information does not document a reliable screen-lock
    // Boolean. Do not promote a private dictionary key or process exit to proof.
    return ["guiSessionAvailable": available, "screenLocked": NSNull(),
            "confirmationSupported": false,
            "reason": "No independently validated public screen-lock observation is configured."]
}
func trustedWithoutPrompt() -> Bool {
    let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
    return AXIsProcessTrustedWithOptions([key: false] as CFDictionary)
}
func privateJSON(_ path: String) -> [String: Any] {
    let fd = open(path, O_RDONLY | O_NOFOLLOW)
    guard fd >= 0 else { deny("Private consent file is unavailable or a symlink.") }
    defer { close(fd) }
    var info = stat()
    guard fstat(fd, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG,
          info.st_uid == geteuid(), (info.st_mode & 0o077) == 0,
          info.st_size > 0, info.st_size <= 16_384 else {
        deny("Consent file must be a small regular owner-only file owned by this operator.")
    }
    let stream = FileHandle(fileDescriptor: fd, closeOnDealloc: false)
    guard let data = try? stream.readToEnd(), let parsed = try? JSONSerialization.jsonObject(with: data),
          let json = parsed as? [String: Any] else { deny("Consent JSON is invalid.") }
    return json
}
func number(_ value: Any?) -> Double? {
    guard let n = value as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID() else { return nil }
    let result = n.doubleValue
    return result.isFinite ? result : nil
}

let args = Array(CommandLine.arguments.dropFirst())
if args == ["--observe"] { emit(sessionObservation()) }
if args == ["--preflight"] {
    var value = sessionObservation()
    value["accessibilityTrusted"] = trustedWithoutPrompt()
    value["promptRequested"] = false
    value["shortcutPosted"] = false
    emit(value)
}
guard args.count == 4, args[0] == "--post-control-command-q",
      args[1] == "--operator-consented", args[2] == "--consent-file" else {
    deny("Only --observe, --preflight, or explicit consented Control-Command-Q execution is supported.")
}
guard ProcessInfo.processInfo.environment["AIRLOCK_LOCAL_LOCK_EXECUTION"] == "explicit-operator-consent" else {
    deny("Execution requires the local operator consent environment marker.")
}
let consent = privateJSON(args[3])
let input = FileHandle.standardInput.readData(ofLength: 16_385)
guard input.count <= 16_384,
      let object = try? JSONSerialization.jsonObject(with: input),
      let action = object as? [String: Any] else { deny("Bound request input is invalid.") }
let now = Date().timeIntervalSince1970 * 1000
let allowed = Set(["schemaVersion", "enabled", "workstationId", "deviceId", "ownerPolicyHash", "expiresAtMs"])
guard Set(consent.keys).isSubset(of: allowed), number(consent["schemaVersion"]) == 1,
      let enabled = consent["enabled"] as? NSNumber, CFGetTypeID(enabled) == CFBooleanGetTypeID(), enabled.boolValue == true,
      let consentExpiry = number(consent["expiresAtMs"]), consentExpiry > now,
      consentExpiry <= now + 86_400_000,
      let requestExpiry = number(action["expiresAtMs"]), requestExpiry > now,
      requestExpiry <= now + 15_000 else { deny("Local consent or request is disabled, expired, or outside its narrow deadline.") }
for key in ["workstationId", "deviceId", "ownerPolicyHash"] {
    guard let bound = consent[key] as? String, !bound.isEmpty,
          let requested = action[key] as? String, requested == bound else {
        deny("Local consent does not match workstation, Mac device and frozen owner policy.")
    }
}
guard let actionID = action["actionId"] as? String, actionID.hasPrefix("lock:incident:"), actionID.count <= 300,
      CGSessionCopyCurrentDictionary() != nil else { deny("No bound action or GUI session is available.") }
guard trustedWithoutPrompt() else { deny("Accessibility permission is absent; no prompt or keyboard event was sent.") }
guard let source = CGEventSource(stateID: .privateState),
      let down = CGEvent(keyboardEventSource: source, virtualKey: CGKeyCode(kVK_ANSI_Q), keyDown: true),
      let up = CGEvent(keyboardEventSource: source, virtualKey: CGKeyCode(kVK_ANSI_Q), keyDown: false) else {
    deny("Core Graphics could not allocate the fixed shortcut events.")
}
// The only key and modifiers this program can post are Apple's screen-lock shortcut.
down.flags = [.maskControl, .maskCommand]
up.flags = [.maskControl, .maskCommand]
down.post(tap: .cghidEventTap)
up.post(tap: .cghidEventTap)
emit(["shortcutPosted": true, "status": "executed", "screenLocked": NSNull(),
      "confirmationSupported": false, "actionId": actionID,
      "reason": "Control-Command-Q events posted. OS lock confirmation remains unavailable."])
