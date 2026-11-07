"""Milestone 3: the Zeus monitor link (no motor commands) against models of every ESP32 camera
firmware family, written from ai-camera-firmware 1.3.0 / 1.4.3 / 1.5.2 / 1.5.4 (see
fake_esp32.py), and the runtime's link supervisor. Protocol conformance with the published
source, not proof about the physical robot."""

from __future__ import annotations

import asyncio
import json
import socket

import pytest
import websockets

from app.robot.commands import MotionPrimitive
from app.robot.zeus import MONITOR_NO_MOTION, MONITOR_NO_STOP, ZeusRobotTransport, classify_camera_firmware

from .conftest import make_runtime, wait_for
from .fake_esp32 import FakeESP32


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_camera_firmware_families_from_the_handshake():
    assert classify_camera_firmware(True, {"Check": "SC"})[0] == "single_client"  # 1.1-1.4.x
    assert classify_camera_firmware(True, {"Check": "SC", "StaIp": ""}) == ("single_client", "1.5.2 (single-client)")
    assert classify_camera_firmware(False, {"Check": "SC", "StaIp": ""}) == ("multi_client", "1.5.3+ (multi-client)")
    assert classify_camera_firmware(False, {})[0] == "unknown"


@pytest.mark.parametrize(
    ("variant", "family"), [("legacy", "single_client"), ("legacy152", "single_client"), ("modern", "multi_client")]
)
async def test_monitor_link_never_sends_a_frame_the_arduino_acts_on(variant, family):
    server = await FakeESP32(variant).start()
    t = ZeusRobotTransport("127.0.0.1", server.port)  # monitor is the default
    try:
        await t.connect()
        assert t.firmware["camera_firmware_family"] == family and t.firmware["identified"]
        await asyncio.sleep(3.6)  # past the 2 s (single-client) and 3 s (1.5.3+) silence timeouts
        st = t.get_status()
        assert st.connected and st.link_state == "connected"  # 1.5.3+ would have closed a silent client
        assert server.arduino.region_lines == []  # not one WS+ line: no Stop, no motion
        assert not server.arduino.ir_or_app and server.arduino.forbidden_regions_seen == []
        if family == "single_client":
            assert server.pings == 0 and server.appstops == []  # "ping" would become [APPSTOP]
            assert any("[DISCONNECTED] timeout" in ln for ln in server.arduino.lines)  # the ESP32's own notice
        else:
            assert server.pings >= 3 and st.firmware["text_ping_rtt_ms"] is not None
        assert st.rtt_kind == "ws_ping" and st.rtt_ms is not None and st.rtt_ms < 100
        assert st.firmware["arduino_messages"] >= 1  # relayed replies prove the Arduino runs
        assert st.capabilities.link_mode == "monitor" and not st.capabilities.motion_permitted
        assert st.capabilities.motion_ack == "none"
        assert all(f["frame"] == "ping" for f in st.firmware["recent_frames"] if f["dir"] == "tx")
    finally:
        await t.disconnect()
        await server.close()


async def test_monitor_link_move_and_stop_write_nothing():
    server = await FakeESP32("modern").start()
    t = ZeusRobotTransport("127.0.0.1", server.port)
    try:
        await t.connect()
        sent_before = list(server.received)
        r = await t.move(MotionPrimitive.FORWARD, 40, 0.5)
        assert not r.sent and r.detail == MONITOR_NO_MOTION
        r = await t.stop("test")
        assert not r.sent and r.detail == MONITOR_NO_STOP
        assert t.freeze_tx(1.0) is False
        await asyncio.sleep(0.2)
        assert [m for _, m in server.received if m != "ping"] == [m for _, m in sent_before if m != "ping"] == []
        assert server.arduino.region_lines == [] and not t.is_moving
    finally:
        await t.disconnect()
        await server.close()


async def test_unexpected_packets_are_counted_and_never_read_as_telemetry():
    async def handler(ws):
        await ws.send(json.dumps({"Name": "Zeus_Car", "Type": "Zeus_Car", "Check": "SC", "StaIp": "", "VideoTemplate": ""}))
        await ws.send(b"\x00\x01binary")
        await ws.send("garbage {not json")
        await ws.send("[1, 2, 3]")
        await ws.send(json.dumps({"us": 12, "ir": 0}))  # sensor-looking keys without the patch marker
        await asyncio.sleep(0.3)
        await ws.send("x" * 70000)  # larger than the client accepts
        await asyncio.sleep(1.0)

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    t = ZeusRobotTransport("127.0.0.1", port)
    try:
        await t.connect()
        await asyncio.sleep(0.2)
        st = t.get_status()
        counts = st.message_counts
        assert counts.get("binary") == 1 and counts.get("text", 0) >= 2
        assert not st.firmware["locked_in_patch"] and not st.capabilities.obstacle_sensing
        assert t.get_telemetry().ultrasonic_cm is None  # never trusted without the patch marker
        await wait_for(lambda: not t.connected, timeout=2.0)
        st = t.get_status()
        assert not st.connected and st.link_state == "error" and st.last_error
    finally:
        await t.disconnect()
        server.close()
        await server.wait_closed()


async def test_frozen_link_is_declared_lost_without_a_tcp_close():
    server = await FakeESP32("modern").start()
    t = ZeusRobotTransport("127.0.0.1", server.port)
    try:
        await t.connect()
        await asyncio.sleep(0.5)
        server.freeze()
        assert await wait_for(lambda: not t.connected, timeout=4.0)
        assert "nothing from the robot" in (t.get_status().last_error or "")
    finally:
        server.freeze(False)
        await t.disconnect()
        await server.close()


async def test_connecting_to_nothing_fails_fast_and_honestly():
    t = ZeusRobotTransport("127.0.0.1", free_port(), connect_timeout_s=0.5)
    with pytest.raises(ConnectionError):
        await t.connect()
    st = t.get_status()
    assert not st.connected and st.link_state == "error" and "cannot open" in st.last_error
    assert not st.simulated


# ------------------------------------------------------------------ runtime supervisor
async def zeus_profile_runtime(settings, tts, port: int):
    settings = settings.model_copy(update={"zeus_port": port, "robot_driver": "zeus", "zeus_host": "127.0.0.1"})
    rt = await make_runtime(settings, tts)
    rt.ZEUS_RETRY_S = (0.1, 0.2, 0.3)
    return rt


async def test_robot_unavailable_shows_zeus_disconnected_never_simulation(settings, tts):
    rt = await zeus_profile_runtime(settings, tts, free_port())
    try:
        r = await rt.connect_robot("zeus")
        assert not r.ok and "keeps retrying" in r.message
        snap = rt.snapshot()
        assert snap["mode"]["code"] == "Z" and snap["mode"]["label"] == "ZEUS DISCONNECTED"
        assert snap["mode"]["robot_profile"] == "zeus" and not snap["mode"]["robot_simulated"]
        assert snap["robot"]["status"]["simulated"] is False and snap["robot"]["status"]["connected"] is False
        assert snap["robot"]["link"]["supervising"] and snap["robot"]["link"]["last_failure"]
        assert not snap["robot"]["gate"]["motion_permitted"]
        armed = await rt.arm()
        assert not armed.ok
    finally:
        await rt.shutdown()


async def test_network_switch_connects_on_its_own_and_drops_are_survived(settings, tts):
    port = free_port()
    rt = await zeus_profile_runtime(settings, tts, port)
    server = None
    try:
        assert not (await rt.connect_robot("zeus")).ok  # Mac still on home Wi-Fi
        server = await FakeESP32("legacy").start(port)  # ... now joined Zeus_Car
        assert await wait_for(lambda: rt.transport is not None and rt.transport.connected, timeout=3.0)
        mode = rt.mode()
        assert mode["code"] in ("A", "A-") and mode["label"] == "ZEUS · MOTION LOCKED"
        assert not rt.safety.armed
        await server.close()  # robot switched off
        assert await wait_for(lambda: rt.mode()["label"] == "ZEUS DISCONNECTED", timeout=3.0)
        server = await FakeESP32("legacy").start(port)  # back on
        assert await wait_for(lambda: rt.transport.connected, timeout=4.0)
        assert not rt.safety.armed  # a reconnect never arms
        assert server.arduino.region_lines == []  # and never sends anything on its own
    finally:
        await rt.shutdown()
        if server is not None:
            await server.close()


async def test_switching_to_sim_stops_the_zeus_supervisor(settings, tts):
    rt = await zeus_profile_runtime(settings, tts, free_port())
    try:
        await rt.connect_robot("zeus")
        assert rt.robot_link_snapshot()["supervising"]
        r = await rt.connect_robot("mock")
        assert r.ok and rt.transport.simulated and not rt.robot_link_snapshot()["supervising"]
        assert rt.mode()["robot_profile"] == "sim" and rt.mode()["robot_simulated"]
        assert rt.snapshot()["robot"]["gate"]["robot"] == "simulated"
    finally:
        await rt.shutdown()


async def test_emergency_stop_on_the_monitor_link_is_reported_as_software_only(settings, tts):
    server = await FakeESP32("modern").start()
    rt = await zeus_profile_runtime(settings, tts, server.port)
    try:
        assert (await rt.connect_robot("zeus")).ok
        r = await rt.emergency_stop(speak=False)
        hw = r.data["hardware_stop"]
        assert hw["kind"] == "software_only" and "no stop frame" in hw["summary"]
        assert rt.safety.estop_latched and rt.snapshot()["robot"]["hardware_stop"]["kind"] == "software_only"
        assert server.arduino.region_lines == []
        stop_sent = [e for e in rt.bus.history() if e.event_type.value == "robot.stop_sent"]
        assert stop_sent and stop_sent[-1].payload["sent"] is False and "monitor link" in stop_sent[-1].payload["detail"]
    finally:
        await rt.shutdown()
        await server.close()


async def test_monitor_link_recognises_the_firmware_patch_without_sending_a_frame():
    """After flashing patch v2, the Arduino's replies to the ESP32's own notices carry the
    patch marker, so the gate can show device-side expiry before any motor command exists."""
    server = await FakeESP32("legacy", patched=True).start()
    t = ZeusRobotTransport("127.0.0.1", server.port)
    try:
        await t.connect()
        assert await wait_for(lambda: t.get_status().firmware["locked_in_patch"], timeout=3.0)
        st = t.get_status()
        assert st.firmware["locked_in_patch_version"] == 2
        assert not st.capabilities.motion_permitted  # still a monitor link
        assert server.arduino.region_lines == []
    finally:
        await t.disconnect()
        await server.close()
