"""Soak the software loop against the simulated robot: the scripted demo back to back,
then mixed sessions (reaches, holding, return, pause, progress, robot link drops), with
invariants checked on every event. Prints markdown; exit code 1 if an invariant fails.

    uv run python scripts/soak_demo.py                 # 10 demos + 10 sessions
    uv run python scripts/soak_demo.py --demos 30 --sessions 30 --time-scale 0.05

Simulated perception and a simulated robot only: this measures the software, never the
hardware. Uses a temporary data directory unless --data-dir is given.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.config import RuntimeSettings, Settings  # noqa: E402
from app.intelligence.state_machine import State  # noqa: E402
from app.runtime import LockedInRuntime  # noqa: E402
from app.simulation import scenarios  # noqa: E402
from app.simulation.scenarios import simulate_reach  # noqa: E402
from app.telemetry.events import EventType  # noqa: E402
from app.voice.tts import NullTTS  # noqa: E402


async def wait_for(pred, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


class Checker:
    def __init__(self) -> None:
        self.events: list = []
        self.failures: list[str] = []

    def on_event(self, e) -> None:
        if self.events and e.seq <= self.events[-1].seq:
            self.failures.append(f"seq not increasing: {self.events[-1].seq} -> {e.seq} ({e.event_type.value})")
        self.events.append(e)

    def expect(self, ok: bool, what: str) -> None:
        if not ok:
            self.failures.append(what)

    def final(self) -> None:
        ids = [e.event_id for e in self.events]
        self.expect(len(ids) == len(set(ids)), f"duplicate event ids: {len(ids) - len(set(ids))}")
        sent = {e.payload["request_id"] for e in self.events if e.event_type == EventType.ROBOT_MOTION_SENT}
        ended = {
            e.payload["request_id"]
            for e in self.events
            if e.event_type in (EventType.ROBOT_MOTION_COMPLETED, EventType.ROBOT_MOTION_ABORTED)
        }
        self.expect(sent <= ended, f"motions sent without an ending: {sorted(sent - ended)}")
        fired = [e for e in self.events if e.event_type == EventType.SAFETY_WATCHDOG_FIRED]
        self.expect(not fired, f"safety watchdog fired {len(fired)}x: {[e.payload for e in fired][:3]}")

    def count(self, et: EventType, **match) -> int:
        return sum(1 for e in self.events if e.event_type == et and all(e.payload.get(k) == v for k, v in match.items()))


async def soak(args) -> int:
    data_dir = Path(args.data_dir) if args.data_dir else Path(tempfile.mkdtemp(prefix="lockedin-soak-"))
    rs = RuntimeSettings()
    rs.coach.min_seconds_between_interventions = 0.0
    rt = LockedInRuntime(
        Settings(_env_file=None, data_dir=data_dir, robot_driver="mock", mock_autoconnect=True),  # no secrets needed
        runtime_settings=rs,
        persist_settings=False,
        tts_engine=NullTTS(),
        enable_vision=False,
        enable_voice=False,
    )
    chk = Checker()
    rt.bus.add_listener(chk.on_event)
    await rt.start()
    await wait_for(lambda: rt.transport is not None and rt.transport.connected and rt.sm.state == State.IDLE)
    rt.safety.config.autonomous_cooldown_s = args.cooldown
    demo_s: list[float] = []
    session_s: list[float] = []
    t_start = time.monotonic()
    try:
        for i in range(args.demos):
            t0 = time.monotonic()
            await scenarios.run_demo(rt, time_scale=args.time_scale)
            chk.expect(await wait_for(lambda: rt.safety.active is None), f"demo {i}: motion still active after the demo")
            chk.expect(rt.sm.state == State.RELEASED, f"demo {i}: ended in {rt.sm.state.value}, expected RELEASED")
            demo_s.append(time.monotonic() - t0)
        retreats = chk.count(EventType.ROBOT_MOTION_SENT, origin="policy")
        chk.expect(retreats == args.demos, f"demos produced {retreats} retreats, expected {args.demos}")

        if args.sessions:
            ok, why = await scenarios.prepare_demo(rt)  # session ended, robot home, armed
            chk.expect(ok, f"prepare before sessions: {why}")
        for i in range(args.sessions):
            t0 = time.monotonic()
            await rt.start_session(task=f"soak {i}", duration_min=25)
            await simulate_reach(rt, approach_ms=10)
            chk.expect(await wait_for(lambda: rt.sm.state == State.HOLDING), f"session {i}: no HOLDING after the reach")
            await simulate_reach(rt, approach_ms=10)
            await rt.return_home()
            chk.expect(await wait_for(lambda: rt.sm.state == State.SESSION_ACTIVE), f"session {i}: return did not finish")
            await rt.pause_session(speak=False)
            await simulate_reach(rt, approach_ms=10)
            await rt.resume_session(speak=False)
            await rt.add_progress(2, speak=False)
            offline = i % 3 == 2
            if offline:
                await rt.disconnect_robot()
                await simulate_reach(rt, approach_ms=10)
                await rt.connect_robot("mock")
                chk.expect(await wait_for(lambda: rt.sm.state == State.SESSION_ACTIVE), f"session {i}: no recovery after reconnect")
                chk.expect(not rt.safety.armed, f"session {i}: reconnect re-armed by itself")
                await rt.arm()
                await rt.set_autonomy(True)
            m = (await rt.end_session(reason="user", speak=False)).data["summary"]["metrics"]
            want = 2 if offline else 1
            chk.expect(m["reach_confirmed"] == want, f"session {i}: reach_confirmed {m['reach_confirmed']} != {want}")
            chk.expect(m["reach_while_holding"] == 1, f"session {i}: reach_while_holding {m['reach_while_holding']} != 1")
            chk.expect(m["completed_units"] == 2 and m["pauses"] == 1, f"session {i}: progress/pauses {m['completed_units']}/{m['pauses']}")
            session_s.append(time.monotonic() - t0)
        chk.expect(await wait_for(lambda: rt.safety.active is None and not rt.transport.is_moving), "motion still active at the end")
        chk.final()
    finally:
        lag_ms = round(rt.watchdog.max_lag_s * 1000, 1)
        await rt.shutdown()

    def fmt(xs: list[float]) -> str:
        return f"{statistics.median(xs):.2f} s median, {max(xs):.2f} s max" if xs else "—"

    print("## Soak (simulated robot, simulated perception)\n")
    print("| | |\n|---|---|")
    print(f"| demos | {args.demos} at time scale {args.time_scale} ({fmt(demo_s)}) |")
    print(f"| sessions | {args.sessions} ({fmt(session_s)}) |")
    print(f"| events | {len(chk.events)} |")
    print(f"| motions sent / blocked | {chk.count(EventType.ROBOT_MOTION_SENT)} / {chk.count(EventType.ROBOT_MOTION_BLOCKED)} |")
    print(f"| event-loop max lag | {lag_ms} ms |")
    print(f"| wall time | {time.monotonic() - t_start:.1f} s |")
    print(f"| invariant failures | {len(chk.failures)} |")
    for f in chk.failures[:20]:
        print(f"- {f}")
    return 1 if chk.failures else 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--demos", type=int, default=10)
    p.add_argument("--sessions", type=int, default=10)
    p.add_argument("--time-scale", type=float, default=0.05, help="pause scale for the demo script (1.0 = real time)")
    p.add_argument("--cooldown", type=float, default=1.0, help="autonomous cooldown in seconds (default 10 s in the app)")
    p.add_argument("--data-dir", default=None)
    sys.exit(asyncio.run(soak(p.parse_args())))


if __name__ == "__main__":
    main()
