"""End-to-end software loop: perception event -> state machine -> policy -> safety -> mock robot -> events."""

import asyncio

from fastapi.testclient import TestClient

from app.intelligence.state_machine import State
from app.main import create_app
from app.runtime import LockedInRuntime, VisionEvent
from app.simulation.scenarios import simulate_reach
from app.telemetry.events import EventType

from .conftest import RecordingTTS, make_runtime, wait_for


async def armed_session(rt: LockedInRuntime, elapsed_s: float = 400.0) -> None:
    r = await rt.handle_utterance("Lock in. I have 25 minutes to finish calculus.")
    assert r.ok
    assert (await rt.arm()).ok
    assert (await rt.set_autonomy(True)).ok
    rt.sessions.current.started_at_mono -= elapsed_s


async def test_camera_event_drives_safe_retreat_on_mock(rt):
    await armed_session(rt)
    await simulate_reach(rt)
    assert rt.sm.state == State.RETREATING
    assert await wait_for(lambda: rt.sm.state == State.HOLDING, timeout=2)
    types = [e.event_type for e in rt.bus.history(500)]
    order = [
        EventType.VISION_HAND_APPROACHING,
        EventType.VISION_PHONE_REACH_CONFIRMED,
        EventType.COACH_DECISION,
        EventType.ROBOT_MOTION_REQUESTED,
        EventType.ROBOT_MOTION_SENT,
        EventType.ROBOT_MOTION_ACKNOWLEDGED,
        EventType.ROBOT_MOTION_COMPLETED,
    ]
    idx = [types.index(t) for t in order]
    assert idx == sorted(idx), [t.value for t in order]
    pose = rt.transport.get_telemetry().pose
    assert pose["y"] < -0.05  # simulated robot backed away
    assert rt.counters.reach_confirmed == 1 and rt.counters.robot_moves == 1
    assert rt.displacement and rt.displacement[0]["primitive"] == "backward"
    assert rt.perf.summary()["frame_to_motion_sent_ms"]["n"] == 1


async def test_disarmed_reach_is_voice_only(rt, tts):
    r = await rt.handle_utterance("lock in 25 minutes")
    assert r.ok
    rt.sessions.current.started_at_mono -= 400
    await simulate_reach(rt)
    await asyncio.sleep(0.05)
    assert rt.sm.state == State.SESSION_ACTIVE
    assert rt.last_decision["action"] == "voice_only"
    assert not any(e.event_type == EventType.ROBOT_MOTION_SENT for e in rt.bus.history(500))
    for _ in range(100):  # the remark comes after a short comedic beat (0.25-0.7 s)
        if len(tts.spoken) >= 2:
            break
        await asyncio.sleep(0.02)
    assert len(tts.spoken) >= 2  # start line + reach remark


async def test_reach_cooldown_prevents_repeated_motion(rt):
    await armed_session(rt)
    await simulate_reach(rt)
    assert await wait_for(lambda: rt.sm.state == State.HOLDING)
    await rt.set_home()
    assert rt.sm.state == State.SESSION_ACTIVE
    await simulate_reach(rt)
    await asyncio.sleep(0.05)
    sent = [e for e in rt.bus.history(500) if e.event_type == EventType.ROBOT_MOTION_SENT]
    blocked = [e for e in rt.bus.history(500) if e.event_type == EventType.ROBOT_MOTION_BLOCKED]
    assert len(sent) == 1
    assert blocked and blocked[-1].payload["reason_code"] == "cooldown"


async def test_release_during_retreat_stops_immediately(rt):
    await armed_session(rt)
    await simulate_reach(rt)
    assert rt.sm.state == State.RETREATING
    await asyncio.sleep(0.1)
    r = await rt.handle_utterance("give me my phone")
    assert r.ok
    await asyncio.sleep(0.05)
    assert rt.sm.state == State.RELEASED
    assert not rt.transport.is_moving
    assert not rt.sessions.active
    assert rt.sessions.last.end_reason == "released"


async def test_low_confidence_voice_never_moves_robot(rt):
    await armed_session(rt)
    rt.displacement.append({"primitive": "backward", "speed": 50, "ran_s": 0.5, "origin": "policy"})
    r = await rt.handle_utterance("come back", source="voice", stt_confidence=0.6)
    assert not r.ok
    assert not any(e.event_type == EventType.ROBOT_MOTION_SENT for e in rt.bus.history(500))
    rejected = [e for e in rt.bus.history(500) if e.event_type == EventType.VOICE_COMMAND_REJECTED]
    assert rejected


async def test_stop_word_halts_motion_even_inside_other_intent(rt):
    await rt.arm()
    r = await rt.manual_move("forward", 40, 0.9)
    assert r.ok
    await asyncio.sleep(0.1)
    assert rt.transport.is_moving
    await rt.handle_utterance("stop roasting me", source="voice", stt_confidence=0.9)
    await asyncio.sleep(0.05)
    assert not rt.transport.is_moving


async def test_unrecognised_command_does_nothing_physical(rt):
    await rt.arm()
    r = await rt.handle_utterance("drive in circles really fast")
    assert not r.ok
    assert not any(e.event_type == EventType.ROBOT_MOTION_REQUESTED for e in rt.bus.history(500))


async def test_status_answer_is_grounded(rt, tts):
    await armed_session(rt, elapsed_s=12 * 60 + 5)
    await rt.add_progress(2, speak=False)
    await simulate_reach(rt)
    await asyncio.sleep(0.05)
    r = await rt.handle_utterance("how am I doing?")
    # The grounded sentence first; roast mode may add a short tag with no numbers in it.
    assert r.reply.startswith("You're 12 minutes in, you've finished 2 tasks, and I've caught 1 phone reach.")
    assert not any(ch.isdigit() for ch in r.reply.split("reach.", 1)[1])


async def test_robot_disconnect_mid_session_keeps_session_and_reconnects(settings, tts):
    rt = await make_runtime(settings, tts, device_watchdog_s=0.1, host_link_timeout_s=0.3)
    try:
        await armed_session(rt)
        rt.transport.simulate_link_loss(10)
        assert await wait_for(lambda: rt.sm.state == State.DISCONNECTED, timeout=2)
        assert rt.sessions.active and not rt.safety.armed
        assert any(e.event_type == EventType.ROBOT_DISCONNECTED for e in rt.bus.history(500))
        r = await rt.connect_robot("mock")
        assert r.ok
        assert await wait_for(lambda: rt.sm.state == State.SESSION_ACTIVE)
    finally:
        await rt.shutdown()


async def test_emergency_stop_blocks_until_reset(rt):
    await armed_session(rt)
    await rt.emergency_stop()
    assert rt.sm.state == State.EMERGENCY_STOP
    r = await rt.manual_move("forward", 40, 0.3)
    assert not r.ok
    assert (await rt.reset()).ok
    assert rt.sm.state == State.SESSION_ACTIVE
    assert not rt.safety.armed


async def test_return_home_reverses_displacement(rt):
    await armed_session(rt)
    await simulate_reach(rt)
    assert await wait_for(lambda: rt.sm.state == State.HOLDING)
    y_away = rt.transport.get_telemetry().pose["y"]
    r = await rt.return_home()
    assert r.ok and rt.sm.state == State.RETURNING
    assert await wait_for(lambda: rt.sm.state == State.SESSION_ACTIVE, timeout=3)
    assert abs(rt.transport.get_telemetry().pose["y"]) < abs(y_away) * 0.3
    assert rt.displacement == []


async def test_session_completes_on_timer(rt, tts):
    await rt.start_session("quick", duration_min=1)
    rt.sessions.current.started_at_mono -= 61
    assert await wait_for(lambda: not rt.sessions.active, timeout=2)
    assert rt.sessions.last.end_reason == "completed"
    assert rt.sm.state == State.IDLE


async def test_focus_event_feeds_presence_metrics(rt):
    await rt.start_session("x", duration_min=10)
    await rt.handle_vision_event(VisionEvent("focus", {"state": "FOCUSED"}, confidence=0.9, simulated=True))
    await asyncio.sleep(0.2)
    await rt.handle_vision_event(VisionEvent("focus", {"state": "AWAY"}, confidence=0.9, simulated=True))
    m = rt.metrics()
    assert m["focused_s"] >= 0.15


def test_rest_and_websocket_deliver_live_events(settings):
    runtime = LockedInRuntime(settings, persist_settings=False, tts_engine=RecordingTTS(), enable_vision=False, enable_voice=False)
    app = create_app(settings, runtime)
    with TestClient(app) as client:
        assert client.get("/api/health").json()["ok"]
        with client.websocket_connect("/ws") as ws:
            first = ws.receive_json()
            assert first["type"] == "snapshot"
            r = client.post("/api/session/start", json={"task": "calculus", "duration_min": 25}).json()
            assert r["ok"] and r["state"] == "SESSION_ACTIVE"
            seen = set()
            for _ in range(200):
                msg = ws.receive_json()
                if msg["type"] == "event" and not msg.get("replay"):
                    seen.add(msg["data"]["event_type"])
                if "session.started" in seen:
                    break
            assert "session.started" in seen
            ws.send_json({"type": "ping", "t": 123})
            for _ in range(200):
                msg = ws.receive_json()
                if msg["type"] == "pong":
                    assert msg["t"] == 123
                    break
            else:
                raise AssertionError("no pong")


async def test_spoken_unit_is_adopted_when_session_has_none(rt):
    await rt.handle_utterance("lock in for 25 minutes")
    r = await rt.handle_utterance("I finished two problems")
    assert r.ok and rt.sessions.current.completed_units == 2
    assert rt.sessions.current.unit_label == "problems"
    if any(ch.isdigit() for ch in r.reply):  # some roast lines say no number at all
        assert "2 problems" in r.reply


async def test_release_line_respects_mute_but_estop_ack_does_not(rt, tts):
    rt.set_muted(True)
    await rt.start_session("x", duration_min=10, speak=False)
    await rt.release()
    await asyncio.sleep(0.05)
    assert tts.spoken == []
    await rt.emergency_stop()
    await asyncio.sleep(0.05)
    assert tts.spoken == ["Emergency stop. I'm not moving."]
