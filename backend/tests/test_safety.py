import asyncio

import pytest

from app.robot.base import TransportCapabilities
from app.robot.commands import MotionOrigin, MotionPrimitive, MotionProfileName, MotionRequest
from app.robot.mock import Box, MockRobotTransport
from app.safety.controller import SafetyConfig, SafetyController
from app.safety.watchdog import MotionWatchdog
from app.telemetry.events import EventBus, EventType


class FakeRealTransport(MockRobotTransport):
    """Behaves like the mock but reports itself as physical hardware."""

    name = "fake-real"

    def __init__(self, caps: TransportCapabilities, **kw):
        super().__init__(**kw)
        self._caps = caps

    def get_status(self):
        st = super().get_status()
        return st.model_copy(update={"simulated": False, "transport": "fake-real", "capabilities": self._caps})


async def setup(transport=None, **cfg):
    bus = EventBus()
    t = transport or MockRobotTransport(latency_s=0.005)
    await t.connect()
    safety = SafetyController(bus=bus, get_transport=lambda: t, config=SafetyConfig(**cfg))
    return bus, t, safety


def req(primitive=MotionPrimitive.BACKWARD, origin=MotionOrigin.OPERATOR, profile=MotionProfileName.NORMAL_CONTROL, **kw):
    return MotionRequest(primitive=primitive, origin=origin, profile=profile, **kw)


def evaluate(safety, r, state_allows=True):
    return safety.evaluate(r, state_allows=state_allows, state_name="TEST")


async def test_disarmed_by_default_denies_motion():
    _, t, s = await setup()
    d = evaluate(s, req(speed=40, duration_s=0.3))
    assert not d.approved and d.reason_code == "disarmed"
    await t.disconnect()


async def test_not_connected_denies():
    bus = EventBus()
    t = MockRobotTransport()
    s = SafetyController(bus=bus, get_transport=lambda: t)
    s.armed = True
    d = evaluate(s, req(speed=40, duration_s=0.3))
    assert d.reason_code == "not_connected"


async def test_state_must_allow_motion():
    _, t, s = await setup()
    s.arm()
    d = evaluate(s, req(speed=40, duration_s=0.3), state_allows=False)
    assert d.reason_code == "state"
    await t.disconnect()


async def test_speed_and_duration_are_clamped_to_profile():
    _, t, s = await setup()
    s.arm()
    d = evaluate(s, req(speed=100, duration_s=5.0, profile=MotionProfileName.SHORT_RETREAT))
    assert d.approved
    assert d.motion.speed == 60
    assert d.motion.duration_s == 0.8
    assert len(d.motion.clamped) == 2
    await t.disconnect()


async def test_zero_speed_and_bad_values_rejected():
    _, t, s = await setup()
    s.arm()
    assert evaluate(s, req(speed=0, duration_s=0.3)).reason_code == "bounds"
    with pytest.raises(ValueError):
        req(speed=float("nan"))
    await t.disconnect()


async def test_profile_restricts_primitives_and_stop_is_not_a_motion():
    _, t, s = await setup()
    s.arm()
    d = evaluate(s, req(MotionPrimitive.ROTATE_LEFT, profile=MotionProfileName.SHORT_RETREAT, speed=40, duration_s=0.3))
    assert d.reason_code == "profile"
    assert evaluate(s, req(MotionPrimitive.STOP, speed=40, duration_s=0.3)).reason_code == "invalid_primitive"
    await t.disconnect()


async def test_autonomous_motion_needs_autonomy_and_respects_cooldown():
    _, t, s = await setup(autonomous_cooldown_s=10.0)
    s.arm()
    r = req(origin=MotionOrigin.POLICY, profile=MotionProfileName.SHORT_RETREAT, speed=50, duration_s=0.05)
    assert evaluate(s, r).reason_code == "autonomy_disabled"
    s.set_autonomy(True)
    d = await s.request(r, state_allows=True, state_name="PHONE_REACH_CONFIRMED")
    assert d.approved
    await asyncio.sleep(0.15)
    r2 = req(origin=MotionOrigin.POLICY, profile=MotionProfileName.SHORT_RETREAT, speed=50, duration_s=0.05)
    assert evaluate(s, r2).reason_code == "cooldown"
    await t.disconnect()


async def test_real_hardware_autonomy_requires_verified_watchdog_and_clear_path():
    caps = TransportCapabilities(simulated=False, device_watchdog="unverified", obstacle_sensing=False)
    t = FakeRealTransport(caps, latency_s=0.005)
    # Models LOCKEDIN_ZEUS_MOTOR_COMMANDS=enabled; the defaults allow no real motion at all.
    _, t, s = await setup(t, real_profiles=None, real_autonomy_allowed=True, real_motion_mode="enabled")
    s.confirm_motor_power()
    s.arm()
    s.set_autonomy(True)
    r = req(origin=MotionOrigin.POLICY, profile=MotionProfileName.SHORT_RETREAT, speed=50, duration_s=0.3)
    assert evaluate(s, r).reason_code == "device_watchdog_unverified"

    t._caps = TransportCapabilities(simulated=False, device_watchdog="verified", obstacle_sensing=False)
    assert evaluate(s, r).reason_code == "path_unverified"

    s.set_autonomy(True, clear_path_confirmed=True)
    assert evaluate(s, r).approved
    # Manual operator moves do not need the autonomy gates.
    assert evaluate(s, req(speed=40, duration_s=0.3)).approved
    await t.disconnect()


async def test_obstacle_blocks_only_the_covered_direction():
    t = MockRobotTransport(latency_s=0.005, obstacles=[Box(-0.2, 0.05, 0.2, 0.3)])  # 5 cm ahead
    _, t, s = await setup(t)
    s.arm()
    await asyncio.sleep(0.15)  # let telemetry publish
    assert evaluate(s, req(MotionPrimitive.FORWARD, speed=40, duration_s=0.3)).reason_code == "obstacle"
    assert evaluate(s, req(MotionPrimitive.BACKWARD, speed=40, duration_s=0.3)).approved
    await t.disconnect()


async def test_motion_lifecycle_events_are_distinct_and_ordered():
    bus, t, s = await setup()
    s.arm()
    d = await s.request(req(speed=50, duration_s=0.15), state_allows=True, state_name="IDLE")
    assert d.approved
    await asyncio.wait_for(s.active.task, 2)
    await asyncio.sleep(0.03)  # stop frame reaches the simulator after its latency
    types = [e.event_type for e in bus.history()]
    i_req = types.index(EventType.ROBOT_MOTION_REQUESTED)
    i_sent = types.index(EventType.ROBOT_MOTION_SENT)
    i_done = types.index(EventType.ROBOT_MOTION_COMPLETED)
    assert i_req < i_sent < i_done
    done = [e for e in bus.history() if e.event_type == EventType.ROBOT_MOTION_COMPLETED][0]
    assert "not measured" in done.payload["note"]  # completion is host-side, not physical
    assert t.travel_log and t.travel_log[-1]["primitive"] == "backward"
    await t.disconnect()


async def test_stop_preempts_running_motion_immediately():
    bus, t, s = await setup()
    s.arm()
    await s.request(req(speed=50, duration_s=0.9), state_allows=True, state_name="IDLE")
    await asyncio.sleep(0.1)
    assert t.is_moving
    await s.stop("test stop")
    await asyncio.sleep(0.03)
    assert not t.is_moving
    assert s.active is None
    aborted = [e for e in bus.history() if e.event_type == EventType.ROBOT_MOTION_ABORTED]
    assert aborted and aborted[-1].payload["outcome"] == "stopped"
    assert aborted[-1].payload["ran_s"] < 0.3
    await t.disconnect()


async def test_emergency_stop_latches_and_disarms():
    bus, t, s = await setup()
    s.arm()
    await s.request(req(speed=50, duration_s=0.9), state_allows=True, state_name="IDLE")
    await asyncio.sleep(0.05)
    await s.emergency_stop("test")
    await asyncio.sleep(0.03)
    assert not t.is_moving and s.estop_latched and not s.armed
    ok, _ = s.arm()
    assert not ok
    assert evaluate(s, req(speed=40, duration_s=0.3)).reason_code == "estop_latched"
    ok, _ = s.reset_estop()
    assert ok and not s.estop_latched and not s.armed  # reset never re-arms
    await t.disconnect()


async def test_link_loss_mid_motion_device_watchdog_stops_wheels():
    t = MockRobotTransport(latency_s=0.005, device_watchdog_s=0.2, host_link_timeout_s=0.6)
    bus, t, s = await setup(t)
    ended = []
    disconnected = []
    t.callbacks.on_motion_ended = lambda why, info: ended.append(why)
    t.callbacks.on_disconnected = lambda reason: disconnected.append(reason)
    s.arm()
    await s.request(req(speed=50, duration_s=1.0), state_allows=True, state_name="IDLE")
    await asyncio.sleep(0.05)
    t.simulate_link_loss(5.0)
    await asyncio.sleep(0.4)
    assert "device_watchdog" in ended  # simulated firmware stopped on its own
    await asyncio.sleep(0.5)
    assert disconnected and not t.connected


async def test_watchdog_stops_orphan_motion():
    bus, t, s = await setup()
    wd = MotionWatchdog(s, bus, overrun_grace_s=0.05)
    await t.move(MotionPrimitive.FORWARD, 40, 2.0)  # bypasses the safety layer on purpose
    await asyncio.sleep(0.02)
    assert t.is_moving
    await wd.check()
    await asyncio.sleep(0.07)
    await wd.check()
    await asyncio.sleep(0.03)
    assert not t.is_moving
    assert wd.fired == 1
    assert any(e.event_type == EventType.SAFETY_WATCHDOG_FIRED for e in bus.history())
    await t.disconnect()


async def test_watchdog_stops_after_loop_stall():
    bus, t, s = await setup()
    s.arm()
    wd = MotionWatchdog(s, bus)
    await s.request(req(speed=50, duration_s=0.9), state_allows=True, state_name="IDLE")
    await asyncio.sleep(0.05)
    await wd.check(lag_s=0.5)
    await asyncio.sleep(0.03)
    assert not t.is_moving
    assert any(e.event_type == EventType.SAFETY_LOOP_STALL for e in bus.history())
    await t.disconnect()


async def test_blocked_requests_publish_reasons():
    bus, t, s = await setup()
    d = await s.request(req(speed=50, duration_s=0.3), state_allows=True, state_name="IDLE")
    assert not d.approved
    blocked = [e for e in bus.history() if e.event_type == EventType.ROBOT_MOTION_BLOCKED]
    assert blocked and blocked[0].payload["reason_code"] == "disarmed"
    assert s.blocked_counts == {"disarmed": 1}
    await t.disconnect()


async def test_real_robot_with_motion_locked_cannot_arm_or_move():
    caps = TransportCapabilities(
        simulated=False, motion_permitted=False, link_mode="monitor",
        motion_lock_reasons=["monitor link: this connection never sends motor commands"],
    )
    t = FakeRealTransport(caps, latency_s=0.005)
    _, t, s = await setup(t, real_profiles=None, real_autonomy_allowed=True)  # even with permissive profiles
    ok, msg = s.arm()
    assert not ok and "physical motion is locked" in msg and "monitor link" in msg
    s.armed = True  # even if something forced the flag, evaluate() still refuses
    d = evaluate(s, req(speed=40, duration_s=0.3, profile=MotionProfileName.LOW_SPEED_TEST))
    assert d.reason_code == "hardware_motion_locked"
    assert not t.travel_log
    await t.disconnect()


async def test_bench_mode_allows_only_wheels_off_ground_profiles_and_no_autonomy():
    caps = TransportCapabilities(simulated=False, motion_permitted=True, link_mode="control", device_watchdog="unverified")
    t = FakeRealTransport(caps, latency_s=0.005)
    bench = frozenset({MotionProfileName.LOW_SPEED_TEST, MotionProfileName.LINK_TEST})
    _, t, s = await setup(t, real_profiles=bench, real_autonomy_allowed=False, real_motion_mode="bench")
    s.confirm_motor_power()
    assert s.arm()[0]
    assert evaluate(s, req(speed=40, duration_s=0.3)).reason_code == "real_profile"  # NORMAL_CONTROL
    assert evaluate(s, req(speed=40, duration_s=0.3, profile=MotionProfileName.LOW_SPEED_TEST)).approved
    ok, msg = s.set_autonomy(True)
    assert not ok and "autonomous motion is off" in msg
    s.autonomy_enabled = True
    r = req(origin=MotionOrigin.POLICY, profile=MotionProfileName.SHORT_RETREAT, speed=50, duration_s=0.3)
    assert evaluate(s, r).reason_code in ("real_profile", "real_autonomy_off")
    await t.disconnect()


async def test_real_robot_needs_the_operator_motor_power_check_to_arm_or_move():
    """The firmware cannot measure the battery (USB alone powers only the Arduino), so a real
    robot never arms or moves until the operator confirms motor power; the check expires."""
    caps = TransportCapabilities(simulated=False, motion_permitted=True, link_mode="control", device_watchdog="verified")
    t = FakeRealTransport(caps, latency_s=0.005)
    bench = frozenset({MotionProfileName.LOW_SPEED_TEST, MotionProfileName.LINK_TEST})
    bus, t, s = await setup(t, real_profiles=bench, real_autonomy_allowed=False, real_motion_mode="bench")
    ok, msg = s.arm()
    assert not ok and "motor power not checked" in msg and "USB alone powers only the Arduino" in msg
    s.armed = True  # even if something forced the flag, evaluate() still refuses
    test_move = req(speed=40, duration_s=0.3, profile=MotionProfileName.LOW_SPEED_TEST)
    assert evaluate(s, test_move).reason_code == "motor_power_unchecked"
    s.armed = False

    s.confirm_motor_power(source="test")
    assert s.snapshot()["motor_power_checked"]
    assert s.arm()[0]
    assert evaluate(s, test_move).approved
    confirmed = [e for e in bus.history() if e.event_type == EventType.SAFETY_MOTOR_POWER]
    assert confirmed and confirmed[-1].payload["confirmed"] is True

    s.motor_power_confirmed_at -= s.config.motor_power_ttl_s + 1  # 30 minutes later
    assert evaluate(s, test_move).reason_code == "motor_power_unchecked"
    assert not s.snapshot()["motor_power_checked"]

    s.confirm_motor_power()
    s.clear_motor_power("robot link lost")
    assert evaluate(s, test_move).reason_code == "motor_power_unchecked"
    assert bus.history()[-1].payload == {"confirmed": False, "reason": "robot link lost"}
    assert not t.travel_log
    await t.disconnect()


async def test_simulated_robot_does_not_need_a_motor_power_check():
    t = MockRobotTransport(latency_s=0.005)
    _, t, s = await setup(t)
    assert s.arm()[0]
    assert evaluate(s, req(speed=40, duration_s=0.3)).approved
    await t.disconnect()

