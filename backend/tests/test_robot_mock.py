import asyncio

import pytest

from app.robot.commands import MotionPrimitive
from app.robot.mock import MockRobotTransport


async def test_connect_disconnect_callbacks():
    t = MockRobotTransport(latency_s=0.001)
    events = []
    t.callbacks.on_connected = lambda info: events.append(("connected", info["simulated"]))
    t.callbacks.on_disconnected = lambda reason: events.append(("disconnected", reason))
    await t.connect()
    assert t.connected and t.simulated
    await t.disconnect()
    assert events == [("connected", True), ("disconnected", "operator disconnect")]


async def test_move_requires_connection():
    t = MockRobotTransport()
    r = await t.move(MotionPrimitive.FORWARD, 50, 0.5)
    assert not r.sent


@pytest.mark.parametrize(
    "primitive,axis,sign",
    [
        (MotionPrimitive.FORWARD, "y", 1),
        (MotionPrimitive.BACKWARD, "y", -1),
        (MotionPrimitive.STRAFE_RIGHT, "x", 1),
        (MotionPrimitive.STRAFE_LEFT, "x", -1),
    ],
)
async def test_translation_directions(primitive, axis, sign):
    t = MockRobotTransport(latency_s=0.001)
    await t.connect()
    await t.move(primitive, 100, 0.2)
    await asyncio.sleep(0.3)
    pose = t.get_telemetry().pose
    assert pose[axis] * sign > 0.03
    other = "x" if axis == "y" else "y"
    assert abs(pose[other]) < 1e-6
    await t.disconnect()


async def test_rotation_changes_heading_only():
    t = MockRobotTransport(latency_s=0.001)
    await t.connect()
    await t.move(MotionPrimitive.ROTATE_RIGHT, 50, 0.2)
    await asyncio.sleep(0.3)
    pose = t.get_telemetry().pose
    assert 5 < pose["theta"] < 40
    assert abs(pose["x"]) < 1e-6 and abs(pose["y"]) < 1e-6
    await t.disconnect()


async def test_transport_enforces_its_own_deadline():
    t = MockRobotTransport(latency_s=0.001)
    ended = []
    t.callbacks.on_motion_ended = lambda why, info: ended.append((why, info["ran_s"]))
    await t.connect()
    await t.move(MotionPrimitive.FORWARD, 50, 0.15)  # nobody calls stop()
    await asyncio.sleep(0.3)
    assert not t.is_moving
    assert ended[0][0] == "deadline" and ended[0][1] < 0.25
    await t.disconnect()


async def test_stop_halts_and_logs_travel():
    t = MockRobotTransport(latency_s=0.001)
    await t.connect()
    await t.move(MotionPrimitive.STRAFE_LEFT, 60, 2.0)
    await asyncio.sleep(0.1)
    await t.stop("test")
    await asyncio.sleep(0.02)
    assert not t.is_moving
    assert t.travel_log[-1]["why"] == "test"
    await t.disconnect()


async def test_frames_lost_while_link_down():
    t = MockRobotTransport(latency_s=0.001, host_link_timeout_s=5.0)
    await t.connect()
    t.simulate_link_loss(0.5)
    r = await t.move(MotionPrimitive.FORWARD, 50, 0.3)
    assert r.sent and "lost" in r.detail
    await asyncio.sleep(0.05)
    assert not t.is_moving  # the robot never received it
    await t.disconnect()
