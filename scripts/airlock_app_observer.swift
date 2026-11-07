#!/usr/bin/env swift
// Native visible-application observations only. No app launching, screen lock,
// microphone/camera access, command arguments, or arbitrary shell execution.
import AppKit
import Foundation
import Security
import CryptoKit
import Darwin

let arguments = Array(CommandLine.arguments.dropFirst())
func value(_ name: String) -> String? {
    guard let index = arguments.firstIndex(of: name), index + 1 < arguments.count else { return nil }
    return arguments[index + 1]
}
func output(_ value: [String: Any]) throws {
    let bytes = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    FileHandle.standardOutput.write(bytes)
    FileHandle.standardOutput.write(Data([10]))
}
func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}
func signature(_ url: URL?) -> String {
    guard let url else { return "unavailable" }
    var code: SecStaticCode?
    let created = SecStaticCodeCreateWithPath(url as CFURL, SecCSFlags(rawValue: 0), &code)
    guard created == errSecSuccess, let code else { return "unavailable:\(created)" }
    let checked = SecStaticCodeCheckValidity(code, SecCSFlags(rawValue: kSecCSBasicValidateOnly), nil)
    return checked == errSecSuccess ? "basic_signature_valid" : "validation_failed:\(checked)"
}
func metadata(_ app: NSRunningApplication, includePath: Bool = false) -> [String: String] {
    let fallback = app.bundleURL?.path ?? app.executableURL?.path ?? "pid:\(app.processIdentifier)"
    let digest = SHA256.hash(data: Data(fallback.utf8)).map { String(format: "%02x", $0) }.joined()
    var result = [
        "appId": app.bundleIdentifier ?? "app:sha256:\(digest)",
        "label": String((app.localizedName ?? "Visible application").prefix(128)),
        "captureMode": "NSWorkspace.didLaunchApplicationNotification",
        "detail": "signing=\(signature(app.bundleURL)); executable=\(app.executableURL?.lastPathComponent ?? "unavailable")"
    ]
    result["detail"] = String(result["detail"]!.prefix(256))
    if includePath { result["executablePath"] = app.executableURL?.path ?? "unavailable" }
    return result
}

if arguments.contains("--help") || arguments.isEmpty {
    print("""
    AIRLOCK native macOS visible-application observer
      swift scripts/airlock_app_observer.swift --snapshot
      swift scripts/airlock_app_observer.swift --workstation ID --session ID \
        --device ID --state-file data/airlock/app-seq.json --duration 30

    Emits actual SecurityEventV1 JSON lines; no publishing by itself.
    Native NSWorkspace launch notifications cover visible apps, not every daemon.
    Pipe into airlock_publish_stream.mjs --real-observation for explicit publication.
    Use a dedicated enrolled Mac device ID/sequence file for this observer.
    """)
    exit(0)
}

let workspace = NSWorkspace.shared
if arguments.contains("--snapshot") {
    let apps = workspace.runningApplications.filter { $0.activationPolicy == .regular }.map { metadata($0, includePath: true) }
    do {
        try output([
            "observer": "NSWorkspace visible application snapshot",
            "simulated": false,
            "observed_at": Int64(Date().timeIntervalSince1970 * 1000),
            "baseline": apps.compactMap { $0["appId"] },
            "frontmost_app_id": workspace.frontmostApplication?.bundleIdentifier ?? "unavailable",
            "apps": apps
        ])
    } catch { fail("Snapshot unavailable: \(error)") }
    exit(0)
}

guard let workstation = value("--workstation"), let session = value("--session"), let device = value("--device"), let statePath = value("--state-file") else {
    fail("Observation requires --workstation, --session, --device and --state-file")
}
for (name, id) in [("workstation", workstation), ("session", session), ("device", device)] {
    if id.range(of: "^[a-zA-Z0-9_.:-]{1,128}$", options: .regularExpression) == nil { fail("Invalid \(name) ID") }
}
let duration = Double(value("--duration") ?? "0") ?? -1
if duration < 0 { fail("Duration must be nonnegative; zero runs until interrupted") }
let stateURL = URL(fileURLWithPath: statePath)
do { try FileManager.default.createDirectory(at: stateURL.deletingLastPathComponent(), withIntermediateDirectories: true) }
catch { fail("Cannot create sequence directory: \(error)") }
let lockDescriptor = open(statePath + ".lock", O_CREAT | O_RDWR, S_IRUSR | S_IWUSR)
if lockDescriptor < 0 || flock(lockDescriptor, LOCK_EX | LOCK_NB) != 0 { fail("Sequence state is already in use or unavailable") }
var sequence: Int64 = 0
if FileManager.default.fileExists(atPath: statePath) {
    do {
        let state = try JSONSerialization.jsonObject(with: Data(contentsOf: stateURL)) as? [String: Any]
        guard state?["workstation"] as? String == workstation, state?["device"] as? String == device, let saved = state?["seq"] as? Int64 else { fail("Sequence file belongs to another workstation/device or is malformed") }
        sequence = saved
    } catch { fail("Cannot read sequence state: \(error)") }
}

func emit(_ kind: String, _ meta: [String: String]) {
    sequence += 1
    do {
        let state = try JSONSerialization.data(withJSONObject: ["seq": sequence, "workstation": workstation, "device": device])
        try state.write(to: stateURL, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: statePath)
        try output([
            "schema_version": 1, "workstation_id": workstation, "session_id": session,
            "device_id": device, "event_id": "app-native-\(UUID().uuidString.lowercased())",
            "source": "mac", "kind": kind,
            "observed_at": Int64(Date().timeIntervalSince1970 * 1000),
            "seq": sequence, "confidence": 1.0, "metadata": meta, "simulated": false
        ])
    } catch { fail("Observation/sequence persistence failed: \(error)") }
}

let observer = workspace.notificationCenter.addObserver(forName: NSWorkspace.didLaunchApplicationNotification, object: nil, queue: .main) { notification in
    guard let app = notification.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication, app.activationPolicy == .regular else { return }
    emit("app_launched", metadata(app))
}
let timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { _ in
    emit("heartbeat", ["detail": "Native NSWorkspace visible-app observer healthy", "captureMode": "NSWorkspace.didLaunchApplicationNotification"])
}
emit("heartbeat", ["detail": "Native NSWorkspace visible-app observer healthy", "captureMode": "NSWorkspace.didLaunchApplicationNotification"])
FileHandle.standardError.write(Data("Native NSWorkspace observer active; actual visible-app notifications only\n".utf8))
if duration > 0 { RunLoop.main.run(until: Date(timeIntervalSinceNow: duration)) }
else { RunLoop.main.run() }
timer.invalidate()
workspace.notificationCenter.removeObserver(observer)
flock(lockDescriptor, LOCK_UN)
close(lockDescriptor)
