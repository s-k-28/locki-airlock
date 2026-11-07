"""Summarise one run of the backend from its telemetry file (data/telemetry/run-*.jsonl):
what was said, how fast the robot answered, what it decided on its own, what it asked the
robot to do. Use it after the live soak (docs/MILESTONE5_VERIFICATION.md, H8).

    .venv/bin/python scripts/run_report.py [data/telemetry/run-....jsonl]   # default: newest

Only recorded events are counted; nothing is inferred.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def stats(xs: list[float]) -> str:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return "n=0"
    return f"n={len(xs)} p50={statistics.median(xs):.0f} p90={xs[int(0.9 * (len(xs) - 1))]:.0f} max={xs[-1]:.0f} ms"


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else max((ROOT / "data" / "telemetry").glob("run-*.jsonl"), key=lambda p: p.stat().st_mtime)
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not events:
        print("empty run")
        return 1
    span = events[-1]["timestamp"] - events[0]["timestamp"]
    by = defaultdict(list)
    for e in events:
        by[e["event_type"]].append(e)
    print(f"{path.name}: {len(events)} events over {span / 60:.1f} min")
    turns = by["voice.turn"]
    routes = Counter(t["payload"].get("route") or "reflex" for t in turns)
    print(f"\nturns: {len(turns)} ({dict(routes)}), rejected before acting: {len(by['voice.command_rejected'])}")
    first_audio: dict[str, float] = {}
    for e in by["voice.tts_finished"]:
        tr = e["payload"].get("trace") or {}
        if tr.get("kind") == "turn" and tr.get("id") and e["payload"].get("to_audio_ms") is not None:
            first_audio.setdefault(tr["id"], e["payload"]["to_audio_ms"])
    route_of = {t["payload"]["id"]: (t["payload"].get("route") or "reflex") for t in turns}
    for r in ("reflex", "converse"):
        print(f"  speech end -> voice starts, {r:8s}: {stats([v for k, v in first_audio.items() if route_of.get(k) == r])}")
    stt = [t["payload"].get("stt_ms") for t in turns if t["payload"].get("source") == "voice"]
    print(f"  speech-to-text: {stats(stt)}")
    replies = [e["payload"] for e in by["dialogue.reply"]]
    if replies:
        kinds = Counter(r["kind"] for r in replies)
        probs = Counter(p.split(":")[0] for r in replies for p in r.get("problems") or [])
        llm = [r["llm"]["total_ms"] for r in replies if r.get("llm") and r["llm"].get("total_ms")]
        print(f"\nmodel replies: {dict(kinds)}; model time {stats(llm)}; retried {sum(1 for r in replies if r.get('retried'))}")
        if probs:
            print(f"  guard / parse problems: {dict(probs)}")
    dec = [e["payload"] for e in by["behavior.decision"]]
    if dec:
        print("\nunprompted decisions (spoke / silent):")
        for ev, n in Counter(d["event"] for d in dec).most_common():
            spoke = sum(1 for d in dec if d["event"] == ev and d["speak"])
            print(f"  {ev:22s} {spoke:3d} / {n - spoke:3d}")
    acts = [e["payload"] for e in by["action.resolved"]]
    if acts:
        print("\naction requests:")
        for (name, status), n in Counter((a["name"], a["status"]) for a in acts).most_common():
            print(f"  {name:18s} {status:9s} x{n}")
    motion = len(by["robot.motion_sent"])
    print(f"\nrobot motion frames sent: {motion}; motion blocked: {len(by['robot.motion_blocked'])}; e-stops: {len(by['safety.emergency_stop'])}")
    print(f"reaches: confirmed {len(by['vision.phone_reach_confirmed'])}, approaching {len(by['vision.hand_approaching'])}; "
          f"camera drops {len(by['vision.camera_disconnected'])}; robot link drops {len(by['robot.disconnected'])}")
    print(f"errors: voice {len(by['voice.error'])}, system {len(by['system.error'])}, loop stalls {len(by['safety.loop_stall'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
