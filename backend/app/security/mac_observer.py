"""Read-only macOS USB snapshot poller for AIRLOCK SecurityEventV1.

This is a labeled polling fallback, not an IOKit notification subscription. It
never opens a mounted volume, reads peripheral files, locks the screen, launches
an application, or sends a robot command. No third-party Python packages needed.
Use --snapshot to capture a redacted arm baseline. Publishing is explicit and
requires the token of an enrolled Mac identity and a real armed session.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

MAX_EVENT_AGE_MS = 120_000
IOREG = "/usr/sbin/ioreg"


def usb_devices(rows: list[dict[str, Any]] | dict[str, Any]) -> dict[str, dict[str, str]]:
    """Extract only USB device nodes; raw serials never leave this function."""
    devices: dict[str, dict[str, str]] = {}

    def visit(nodes: list[dict[str, Any]]) -> None:
        for row in nodes:
            if not isinstance(row, dict):
                continue
            if "idVendor" in row and "idProduct" in row:
                vendor, product = int(row["idVendor"]), int(row["idProduct"])
                # A location identifies a port, not an authenticated accessory.
                # Serial values are observational too and can be spoofed.
                material = json.dumps(
                    [vendor, product, row.get("locationID", ""), str(row.get("USB Serial Number", ""))],
                    separators=(",", ":"),
                ).encode()
                key = "usb:sha256:" + hashlib.sha256(material).hexdigest()
                devices[key] = {"deviceKey": key, "label": f"USB {vendor:04x}:{product:04x}"}
            children = row.get("IORegistryEntryChildren", [])
            if isinstance(children, list):
                visit(children)

    visit([rows] if isinstance(rows, dict) else rows)
    return devices


def snapshot() -> tuple[dict[str, dict[str, str]], float]:
    if platform.system() != "Darwin":
        raise RuntimeError("The real USB observer requires macOS; fixtures use airlock_publisher.mjs")
    started = time.perf_counter()
    result = subprocess.run(
        [IOREG, "-a", "-l", "-p", "IOUSB"],
        check=True, capture_output=True, timeout=5,
    )
    rows = plistlib.loads(result.stdout)
    if not isinstance(rows, (list, dict)):
        raise RuntimeError("Unexpected IORegistry archive; USB status is unavailable")
    return usb_devices(rows), round((time.perf_counter() - started) * 1000, 3)


def event(kind: str, metadata: dict[str, str], args: argparse.Namespace, seq: int) -> dict[str, Any]:
    return {
        "schema_version": 1, "workstation_id": args.workstation,
        "session_id": args.session, "device_id": args.device,
        "event_id": "mac-poll-" + str(uuid.uuid4()), "source": "mac", "kind": kind,
        "observed_at": time.time_ns() // 1_000_000, "seq": seq,
        "confidence": 1.0, "metadata": metadata, "simulated": False,
    }


def reducer_args(observation: dict[str, Any]) -> list[Any]:
    return [
        observation["workstation_id"], observation["session_id"], observation["device_id"],
        observation["event_id"], observation["kind"], observation["observed_at"],
        observation["seq"], observation["confidence"],
        json.dumps(observation["metadata"], separators=(",", ":")), False, 1,
    ]


def read_token(path: Path) -> str:
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("Token file must be owner-only; set its permissions to 600")
    saved = json.loads(path.read_text())
    token = saved.get("token")
    if not isinstance(token, str) or not token:
        raise ValueError("Token file must contain a token string")
    return token


def publish(observation: dict[str, Any], args: argparse.Namespace, token: str) -> None:
    base = urllib.parse.urlsplit(args.uri)
    if base.scheme not in {"http", "https"} or not base.netloc or base.username or base.password:
        raise ValueError("Database URI must be HTTP(S) without embedded credentials")
    url = (
        f"{base.scheme}://{base.netloc}/v1/database/"
        f"{urllib.parse.quote(args.database, safe='')}/call/ingest_device_event?confirmed=true"
    )
    request = urllib.request.Request(
        url, data=json.dumps(reducer_args(observation)).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        response.read()


def save_seq(path: Path, seq: int, workstation: str, device: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"seq": seq, "workstation": workstation, "device": device}, stream)
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", action="store_true", help="Print redacted USB baseline and exit")
    parser.add_argument("--workstation")
    parser.add_argument("--session")
    parser.add_argument("--device")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--duration", type=float, default=0, help="Seconds to observe; 0 runs until interrupted")
    parser.add_argument("--publish", action="store_true", help="Publish real observations to the enrolled session")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--uri", default=os.environ.get("AIRLOCK_SPACETIME_URI", "http://127.0.0.1:3000"))
    parser.add_argument("--database", default=os.environ.get("AIRLOCK_DATABASE", "locki-airlock-sid"))
    args = parser.parse_args()
    current, elapsed = snapshot()
    if args.snapshot:
        print(json.dumps({
            "observer": "macOS IORegistry snapshot poll (fallback)", "simulated": False,
            "observed_at": time.time_ns() // 1_000_000,
            "baseline": sorted(current), "devices": list(current.values()),
            "snapshot_duration_ms": elapsed,
        }, indent=2))
        return
    if not all([args.workstation, args.session, args.device]):
        parser.error("Observation requires --workstation, --session and --device")
    if not 0.25 <= args.interval <= 10 or args.duration < 0:
        parser.error("--interval must be 0.25–10 seconds and --duration nonnegative")
    if args.publish and (not args.token_file or not args.state_file):
        parser.error("Publishing requires --token-file and persistent --state-file")
    token = read_token(args.token_file) if args.publish else ""
    seq = 0
    if args.state_file and args.state_file.exists():
        state = json.loads(args.state_file.read_text())
        if state.get("workstation") != args.workstation or state.get("device") != args.device:
            parser.error("Sequence file belongs to another workstation/device")
        seq = int(state["seq"])
    previous: dict[str, dict[str, str]] = {}
    pending: list[dict[str, Any]] = []
    started, heartbeat_at = time.monotonic(), 0.0
    print(json.dumps({"observer": "macOS USB snapshot poll (fallback)", "interval_seconds": args.interval,
                      "publishing": args.publish, "simulated": False}), file=sys.stderr)
    while True:
        cycle = time.monotonic()
        updates = [("usb_added", current[key]) for key in sorted(current.keys() - previous.keys())]
        updates += [("usb_removed", previous[key]) for key in sorted(previous.keys() - current.keys())]
        if cycle - heartbeat_at >= 3:
            updates.append(("heartbeat", {"detail": "USB snapshot poll healthy; no app-launch coverage"}))
            heartbeat_at = cycle
        for kind, metadata in updates:
            seq += 1
            observation = event(kind, metadata, args, seq)
            if args.state_file:
                save_seq(args.state_file, seq, args.workstation, args.device)
            if args.publish:
                if len(pending) >= 256:
                    raise RuntimeError("Publisher queue full; observation delivery is degraded")
                pending.append(observation)
            else:
                print(json.dumps(observation), flush=True)
        while pending:
            if time.time_ns() // 1_000_000 - pending[0]["observed_at"] > MAX_EVENT_AGE_MS:
                pending.pop(0)
                print("Expired undelivered observation; publisher is degraded", file=sys.stderr)
                continue
            try:
                publish(pending[0], args, token)
            except urllib.error.HTTPError as error:
                if error.code < 500:
                    raise RuntimeError(
                        f"Database rejected observation ({error.code}); check enrollment, session and sequence"
                    ) from None
                print(f"Database unavailable ({error.code}); retrying", file=sys.stderr)
                break
            except (urllib.error.URLError, TimeoutError) as error:
                print(f"Observation not delivered ({type(error).__name__}); retrying", file=sys.stderr)
                break
            delivered = pending.pop(0)
            print(json.dumps({"published": True, "event_id": delivered["event_id"],
                              "kind": delivered["kind"], "simulated": False}), flush=True)
        previous = current
        if args.duration and time.monotonic() - started >= args.duration:
            break
        time.sleep(max(0, args.interval - (time.monotonic() - cycle)))
        current, _ = snapshot()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print(f"USB observer unavailable: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1) from None
