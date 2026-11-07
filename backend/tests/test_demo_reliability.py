"""Demo reliability: confirmed reaches counted once, a repeatable scripted demo, and a
multi-session soak against the simulated robot. Simulated perception throughout."""

from __future__ import annotations

import asyncio
import time

from app.intelligence.state_machine import State
from app.simulation import scenarios
from app.simulation.scenarios import simulate_reach
from app.telemetry.events import EventType

from .conftest import make_runtime


def of_type(events, et: EventType):
    return [e for e in events if e.event_type == et]


async def wait_for(pred, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


async def armed_session(rt):
    assert (await rt.arm()).ok
    assert (await rt.set_autonomy(True)).ok
    assert (await rt.start_session(task="essay", duration_min=25)).ok


# ------------------------------------------------------------------ reach accounting
async def test_reach_during_a_retreat_is_recorded_but_not_counted_or_acted_on(rt):
    seen: list = []
    rt.bus.add_listener(seen.append)
    await armed_session(rt)
    await simulate_reach(rt, approach_ms=20)
    assert await wait_for(lambda: rt.sm.state == State.RETREATING, 1.0)
    decisions, blocked = len(of_type(seen, EventType.COACH_DECISION)), rt.counters.motions_blocked
    await simulate_reach(rt, approach_ms=10)  # the hand chases the retreating phone
    await asyncio.sleep(1.0)  # past any comedic beat
    assert rt.counters.reach_confirmed == 1
    assert len(of_type(seen, EventType.COACH_DECISION)) == decisions
    assert rt.counters.motions_blocked == blocked
    assert len(of_type(seen, EventType.COACH_INTERVENTION_GENERATED)) == 1  # one remark, for the first reach
    assert of_type(seen, EventType.VISION_PHONE_REACH_CONFIRMED)[-1].payload["state"] == "RETREATING"


async def test_reach_while_the_robot_is_offline_still_counts_and_falls_back_to_voice(rt):
    await armed_session(rt)
    await rt.disconnect_robot()
    assert rt.sm.state == State.DISCONNECTED and rt.sessions.active
    await simulate_reach(rt, approach_ms=10)
    assert rt.counters.reach_confirmed == 1
    assert rt.last_decision["action"] == "voice_only"


async def test_reach_with_no_session_or_while_paused_does_nothing(rt):
    await simulate_reach(rt, approach_ms=10)
    assert rt.last_decision is None and rt.counters.reach_confirmed == 0
    await armed_session(rt)
    await rt.pause_session(speak=False)
    await simulate_reach(rt, approach_ms=10)
    assert rt.last_decision is None and rt.counters.reach_confirmed == 0
    assert rt.sm.state == State.PAUSED


# ------------------------------------------------------------------ repeatable demo
async def test_demo_runs_twice_in_a_row_with_a_retreat_each_time(rt):
    rt.safety.config.autonomous_cooldown_s = 1.0
    seen: list = []
    rt.bus.add_listener(seen.append)
    for _ in range(2):
        await scenarios.run_demo(rt, time_scale=0.02)
        assert await wait_for(lambda: rt.safety.active is None, 2.0)
    retreats = [e for e in of_type(seen, EventType.ROBOT_MOTION_SENT) if e.payload["origin"] == "policy"]
    assert len(retreats) == 2
    steps = [e.payload["step"] for e in of_type(seen, EventType.DEMO_STEP)]
    assert steps.count("demo finished") == 2
    assert "simulated robot reset to home" in steps  # run 2 would otherwise start in HOLDING
    assert "waiting for the cooldown" in steps
    assert all(e.simulated for e in of_type(seen, EventType.DEMO_STEP))
    assert rt.sm.state == State.RELEASED


async def test_demo_ends_a_running_session_first(rt):
    await rt.start_session(task="leftover", duration_min=25)
    ok, _ = await scenarios.prepare_demo(rt)
    assert ok and rt.sessions.current is None


async def test_demo_refuses_to_start_during_an_emergency_stop(rt):
    await rt.emergency_stop(speak=False)
    ok, why = await scenarios.prepare_demo(rt)
    assert not ok and "emergency stop" in why


# ------------------------------------------------------------------ soak
async def test_soak_many_sessions_keep_events_counters_and_motion_consistent(settings, tts):
    rt = await make_runtime(settings, tts)
    rt.safety.config.autonomous_cooldown_s = 0.0
    seen: list = []
    rt.bus.add_listener(seen.append)
    try:
        await rt.arm()
        await rt.set_autonomy(True)
        for i in range(5):
            assert (await rt.start_session(task=f"soak {i}", duration_min=25)).ok
            assert rt.sm.state == State.SESSION_ACTIVE

            await simulate_reach(rt, approach_ms=10)  # retreat
            assert await wait_for(lambda: rt.sm.state == State.HOLDING)
            await simulate_reach(rt, approach_ms=10)  # phone already away
            assert (await rt.return_home()).ok
            assert await wait_for(lambda: rt.sm.state == State.SESSION_ACTIVE)

            await rt.pause_session(speak=False)
            await simulate_reach(rt, approach_ms=10)  # paused: ignored
            await rt.resume_session(speak=False)
            await rt.add_progress(1, speak=False)
            await rt.add_progress(2, speak=False)

            if i % 2:  # robot link drops mid-session and comes back
                await rt.disconnect_robot()
                await simulate_reach(rt, approach_ms=10)  # offline: counted, voice only
                assert (await rt.connect_robot("mock")).ok
                assert await wait_for(lambda: rt.sm.state == State.SESSION_ACTIVE)
                assert not rt.safety.armed  # reconnect never re-arms on its own
                await rt.arm()
                await rt.set_autonomy(True)

            r = await rt.end_session(reason="user", speak=False)
            m = r.data["summary"]["metrics"]
            assert m["reach_confirmed"] == (2 if i % 2 else 1), (i, m)
            assert m["reach_while_holding"] == 1
            assert m["completed_units"] == 3 and m["pauses"] == 1
            assert rt.sm.state == State.IDLE

        assert await wait_for(lambda: rt.safety.active is None and not rt.transport.is_moving)
        ids = [e.event_id for e in seen]
        assert len(ids) == len(set(ids))
        seqs = [e.seq for e in seen]
        assert all(b > a for a, b in zip(seqs, seqs[1:], strict=False))
        sent = {e.payload["request_id"] for e in of_type(seen, EventType.ROBOT_MOTION_SENT)}
        ended = {e.payload["request_id"] for e in seen if e.event_type in (EventType.ROBOT_MOTION_COMPLETED, EventType.ROBOT_MOTION_ABORTED)}
        assert sent and sent <= ended  # every motion that went out has an ending
        assert len(of_type(seen, EventType.SESSION_ENDED)) == 5
        assert not of_type(seen, EventType.SAFETY_WATCHDOG_FIRED)
    finally:
        await rt.shutdown()
