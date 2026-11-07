"""Zeus transport vs. a firmware model built from the upstream source (tests/fake_esp32.py).

These verify protocol conformance. They do not prove anything about physical motion;
hardware tests R1-R6 in docs/TEST_PLAN.md do that."""

import asyncio
import json

import pytest

from app.robot.commands import MotionPrimitive
from app.robot.zeus import (
    STOP_FIRST_GAP_S,
    ZeusRobotTransport,
    arduino_joystick,
    encode_motion,
    encode_stop,
    parse_incoming,
)

from .fake_esp32 import MODE_APP, MODE_NONE, FakeESP32, esp32_convert, get_joystick, get_str_of, to_int


# ------------------------------------------------------------------ pure encoding
@pytest.mark.parametrize(
    "primitive,angle",
    [
        (MotionPrimitive.FORWARD, 0),
        (MotionPrimitive.STRAFE_RIGHT, 90),
        (MotionPrimitive.STRAFE_LEFT, -90),
        (MotionPrimitive.BACKWARD, 180),
    ],
)
def test_translation_frames_decode_to_the_intended_direction(primitive, angle):
    frame = encode_motion(primitive, 40)
    line = esp32_convert(frame)
    assert line.startswith("WS+") and line.count(";") == 25
    a, r = get_joystick(line[3:], 10)  # REGION_K
    assert (a, r) == (angle, 40)
    assert len(line) < 64  # fits the Uno's 64-byte serial RX buffer


def test_rotation_uses_heading_joystick_q():
    left = esp32_convert(encode_motion(MotionPrimitive.ROTATE_LEFT, 50))[3:]
    right = esp32_convert(encode_motion(MotionPrimitive.ROTATE_RIGHT, 50))[3:]
    assert get_joystick(left, 16)[0] == -90  # REGION_Q
    assert get_joystick(right, 16)[0] == 90
    assert get_joystick(left, 10) == (0, 0)  # no translation


def test_stop_frame_presses_region_f():
    line = esp32_convert(encode_stop())[3:]
    assert to_int(get_str_of(line, 5)) == 1  # REGION_F
    assert get_joystick(line, 10) == (0, 0)


def test_speed_is_clamped_to_joystick_range():
    assert json.loads(encode_motion(MotionPrimitive.FORWARD, 250)) == {"K": [0, 100]}
    assert json.loads(encode_motion(MotionPrimitive.BACKWARD, -5)) == {"K": [0, 0]}


def test_arduino_joystick_math_matches_firmware():
    assert arduino_joystick(0, 50) == (0, 50)
    assert arduino_joystick(-50, 0) == (-90, 50)


def test_parse_incoming_variants():
    modern = parse_incoming(json.dumps({"Name": "Zeus_Car", "Type": "Zeus_Car", "Check": "SC", "video": "", "StaIp": "", "VideoTemplate": ""}))
    assert modern.kind == "check_info"
    assert parse_incoming("pong 12345").data == {"esp_ms": 12345}
    assert parse_incoming("null").kind == "arduino"
    assert parse_incoming('{"I":0}').kind == "arduino"
    assert parse_incoming('{"state":"OK","errors":[]}').kind == "config_reply"
    assert parse_incoming("garbage").kind == "text"


# ------------------------------------------------------------------ against the firmware model
async def connected(variant="modern", **server_kw):
    server = await FakeESP32(variant, **server_kw).start()
    t = ZeusRobotTransport("127.0.0.1", server.port, link_mode="control")
    events = {"connected": [], "disconnected": [], "acks": [], "ended": []}
    t.callbacks.on_connected = lambda info: events["connected"].append(info)
    t.callbacks.on_disconnected = lambda reason: events["disconnected"].append(reason)
    t.callbacks.on_ack = lambda info: events["acks"].append(info)
    t.callbacks.on_motion_ended = lambda why, info: events["ended"].append(why)
    await t.connect()
    return server, t, events


async def test_connect_identifies_modern_firmware_and_parks_the_car():
    server, t, ev = await connected("modern")
    try:
        assert t.connected
        assert t.firmware["identified"] and t.firmware["camera_firmware_family"] == "multi_client"
        assert t.firmware["video_url"] == "http://192.168.4.1:9000/mjpg"
        await asyncio.sleep(0.25)
        assert server.arduino.mode == MODE_NONE
        assert server.received and json.loads(server.received[0][1]) == {"F": True}
        assert ev["connected"][0]["simulated"] is False
    finally:
        await t.disconnect()
        await server.close()


async def test_move_then_stop_drives_the_model_correctly_without_dropped_frames():
    server, t, ev = await connected("modern")
    try:
        await asyncio.sleep(0.2)
        receipt = await t.move(MotionPrimitive.STRAFE_LEFT, 45, 2.0)
        assert receipt.sent
        await asyncio.sleep(0.35)
        a = server.arduino
        assert a.mode == MODE_APP and a.remote_angle == -90 and a.remote_power == 45
        assert a.motors_active
        assert ev["acks"] and ev["acks"][0]["ack"] == "link_echo"
        stop = await t.stop("test")
        assert stop.sent
        await asyncio.sleep(0.2)
        assert server.arduino.mode == MODE_NONE and not server.arduino.motors_active
        assert server.dropped == 0
        gaps = [b[0] - a_[0] for a_, b in zip(server.received, server.received[1:], strict=False)]
        assert min(gaps) >= STOP_FIRST_GAP_S * 0.8  # never inside the ESP32's 20 ms drop window
        assert server.pings == 0
        assert server.arduino.forbidden_regions_seen == []
    finally:
        await t.disconnect()
        await server.close()


async def test_motion_refresh_rate_is_about_10hz():
    server, t, _ = await connected("modern")
    try:
        await asyncio.sleep(0.2)
        n0 = len(server.received)
        await t.move(MotionPrimitive.FORWARD, 40, 2.0)
        await asyncio.sleep(1.0)
        motion_frames = [r for _, r in server.received[n0:] if r.startswith('{"K"')]
        assert 8 <= len(motion_frames) <= 12
        await t.stop()
    finally:
        await t.disconnect()
        await server.close()


async def test_transport_stops_itself_at_the_hard_cap():
    server, t, ev = await connected("modern")
    try:
        await asyncio.sleep(0.15)
        await t.move(MotionPrimitive.BACKWARD, 40, 0.3)  # nobody calls stop()
        await asyncio.sleep(0.6)
        assert "deadline" in ev["ended"]
        assert server.arduino.mode == MODE_NONE
    finally:
        await t.disconnect()
        await server.close()


async def test_legacy_firmware_is_detected_and_never_pinged():
    server, t, _ = await connected("legacy")
    try:
        assert t.firmware["camera_firmware_family"] == "single_client"
        await asyncio.sleep(1.2)  # idle keepalives
        assert server.pings == 0 and server.appstops == []
        assert server.arduino.ws_connected
    finally:
        await t.disconnect()
        await server.close()


async def test_robot_side_disconnect_is_detected_and_motion_ended():
    server, t, ev = await connected("modern")
    try:
        await asyncio.sleep(0.15)
        await t.move(MotionPrimitive.FORWARD, 40, 2.0)
        await asyncio.sleep(0.2)
        await server.drop_all_clients()
        await asyncio.sleep(0.3)
        assert not t.connected
        assert ev["disconnected"]
        assert "link_lost" in ev["ended"]
        # Stock behaviour: the ESP32 reports [DISCONNECTED] and the Arduino parks itself.
        assert server.arduino.mode == MODE_NONE
    finally:
        await server.close()


async def test_stock_firmware_failsafe_when_laptop_goes_silent():
    """Models the R5 hardware test: the laptop stops sending mid-motion. With stock
    firmware only the ESP32's data/ping timeout stops the car (3-4 s on 1.5.x; scaled
    down to 0.5 s here). This is why autonomy on real hardware stays locked until the
    device-side stop is measured (docs/SAFETY.md)."""
    server, t, _ = await connected("modern", data_timeout_s=0.5, pingpong_timeout_s=5.0, check_period_s=0.1)
    try:
        await asyncio.sleep(0.15)
        await t.move(MotionPrimitive.FORWARD, 40, 5.0)
        await asyncio.sleep(0.3)
        assert server.arduino.motors_active
        t._tx_task.cancel()  # laptop freezes: frames stop, socket stays open
        await asyncio.sleep(0.25)
        assert server.arduino.motors_active  # still moving: there is no Arduino-side command timeout
        await asyncio.sleep(0.6)
        assert server.appstops
        assert not server.arduino.motors_active
    finally:
        await t._teardown("test end", notify=False)
        await server.close()


async def test_connect_to_nothing_fails_cleanly():
    t = ZeusRobotTransport("127.0.0.1", 9, connect_timeout_s=0.5)
    with pytest.raises(ConnectionError):
        await t.connect()
    assert not t.connected
    assert t.get_status().last_error


async def test_status_is_honest_about_capabilities():
    server, t, _ = await connected("modern")
    try:
        st = t.get_status()
        assert st.simulated is False
        assert st.capabilities.obstacle_sensing is False
        assert st.capabilities.device_watchdog == "unverified"
        assert st.battery_v is None
        assert any("No sensor telemetry" in n for n in st.capabilities.notes)
    finally:
        await t.disconnect()
        await server.close()
