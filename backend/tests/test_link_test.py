"""R5 link-freeze test and the optional firmware patch, against the simulator and the
firmware model in fake_esp32.py. Passing these says the host side behaves as designed
against models written from the sources; it says nothing about the physical car."""

from __future__ import annotations

import asyncio
import json
import time

from app.robot.commands import MotionPrimitive
from app.robot.zeus import ZeusRobotTransport
from app.telemetry.events import EventType

from .conftest import make_runtime
from .fake_esp32 import MODE_APP, MODE_NONE, FakeESP32


async def connected(patched: bool, **server_kw):
    server = await FakeESP32("modern", patched=patched, **server_kw).start()
    t = ZeusRobotTransport("127.0.0.1", server.port, link_mode="control")
    ev = {"ended": [], "watchdog": [], "disconnected": []}
    t.callbacks.on_motion_ended = lambda why, info: ev["ended"].append(why)
    t.callbacks.on_device_watchdog = lambda info: ev["watchdog"].append(info)
    t.callbacks.on_disconnected = lambda reason: ev["disconnected"].append(reason)
    await t.connect()
    await asyncio.sleep(0.15)
    return server, t, ev


def frames_between(server: FakeESP32, t0: float, t1: float) -> list[str]:
    return [raw for ts, raw in server.received if t0 <= ts < t1]


# ------------------------------------------------------------------ transport + firmware model
async def test_patched_firmware_telemetry_is_parsed():
    server, t, _ = await connected(patched=True)
    try:
        await asyncio.sleep(0.6)  # at least one keepalive and its reply
        tel = t.get_telemetry()
        assert tel.firmware_patch == 2
        assert tel.ultrasonic_cm == 87.5
        assert tel.ir_left_blocked is False and tel.ir_right_blocked is False
        assert tel.device_mode == MODE_NONE and tel.device_watchdog_trips == 0
        assert tel.device_watchdog_latched is False and tel.device_free_ram == 487
        st = t.get_status()
        assert st.capabilities.obstacle_sensing is True
        assert st.firmware["locked_in_patch_version"] == 2
        assert st.capabilities.device_watchdog == "unverified"  # only R5 on the real car changes this
    finally:
        await t.disconnect()
        await server.close()


async def test_ultrasonic_not_measured_reads_as_unknown():
    server, t, _ = await connected(patched=True)
    try:
        server.arduino.us_cm = -2  # patch: not measured in this mode
        await asyncio.sleep(0.6)
        assert t.get_telemetry().ultrasonic_cm is None
        assert t.get_status().capabilities.obstacle_sensing is False
    finally:
        await t.disconnect()
        await server.close()


async def test_freeze_writes_nothing_keeps_the_socket_open_and_the_patched_watchdog_stops():
    server, t, ev = await connected(patched=True)
    try:
        assert (await t.move(MotionPrimitive.FORWARD, 30, 5.0)).sent
        await asyncio.sleep(0.3)
        assert server.arduino.motors_active
        assert t.freeze_tx(3.0)
        freeze_at = time.monotonic()
        await asyncio.sleep(1.2)
        assert frames_between(server, freeze_at + 0.005, time.monotonic()) == []
        assert t.connected and ev["disconnected"] == []  # silent, not closed
        a = server.arduino
        assert a.trips == 1 and a.latched
        # The watchdog counts from the last line the Arduino got, which the 10 Hz refresh
        # put up to 100 ms before the freeze began.
        last_frame = max(ts for ts, _ in server.received if ts < freeze_at)
        assert freeze_at - last_frame <= 0.11
        stopped = a.motors_stopped_at(freeze_at)
        assert stopped is not None and 0.39 <= stopped - last_frame <= 0.48, stopped - last_frame
        stop_at = time.monotonic()
        assert (await t.stop("freeze over")).sent
        assert not t.tx_frozen
        await asyncio.sleep(0.05)
        first = frames_between(server, stop_at, time.monotonic() + 1)[0]
        assert json.loads(first) == {"F": True}
        await asyncio.sleep(0.3)
        assert not a.latched and a.mode == MODE_NONE  # Stop cleared the latch
        assert t.get_telemetry().device_watchdog_trips == 1
        assert ev["watchdog"] and ev["watchdog"][0]["during_motion"] is False  # host had already stopped
    finally:
        await t.disconnect()
        await server.close()


async def test_stock_firmware_keeps_driving_through_a_freeze():
    server, t, _ = await connected(patched=False)
    try:
        await t.move(MotionPrimitive.FORWARD, 30, 5.0)
        await asyncio.sleep(0.3)
        t.freeze_tx(3.0)
        await asyncio.sleep(1.0)
        assert server.arduino.motors_active  # nothing on the Arduino stops it
        await t.stop("freeze over")
        await asyncio.sleep(0.15)
        assert not server.arduino.motors_active
        assert t.get_telemetry().device_watchdog_trips is None
    finally:
        await t.disconnect()
        await server.close()


async def test_stop_during_a_freeze_goes_out_at_once():
    server, t, _ = await connected(patched=False)
    try:
        await t.move(MotionPrimitive.BACKWARD, 30, 5.0)
        await asyncio.sleep(0.3)
        t.freeze_tx(3.0)
        await asyncio.sleep(0.2)
        stop_at = time.monotonic()
        receipt = await t.stop("operator")
        assert receipt.sent and time.monotonic() - stop_at < 0.1
        await asyncio.sleep(0.1)
        assert not server.arduino.motors_active
    finally:
        await t.disconnect()
        await server.close()


async def test_freeze_cap_ends_the_motion_and_never_resumes_it():
    server, t, ev = await connected(patched=False)
    try:
        await t.move(MotionPrimitive.FORWARD, 30, 5.0)
        await asyncio.sleep(0.3)
        t.freeze_tx(0.5)
        freeze_at = time.monotonic()
        await asyncio.sleep(0.9)
        assert "freeze_timeout" in ev["ended"]
        after = [json.loads(f) for f in frames_between(server, freeze_at + 0.005, time.monotonic())]
        assert after and all(f == {"F": True} for f in after)  # stop frames only, no motion refresh
        assert not server.arduino.motors_active and not t.is_moving
    finally:
        await t.disconnect()
        await server.close()


async def test_move_is_refused_while_frozen():
    server, t, _ = await connected(patched=False)
    try:
        t.freeze_tx(1.0)
        r = await t.move(MotionPrimitive.FORWARD, 30, 1.0)
        assert not r.sent and "link test" in r.detail
    finally:
        await t.disconnect()
        await server.close()


async def test_wifi_stall_trips_the_patched_watchdog_and_stale_frames_do_not_restart_the_wheels():
    server, t, ev = await connected(patched=True)
    try:
        await t.move(MotionPrimitive.FORWARD, 30, 3.0)
        await asyncio.sleep(0.3)
        server.stall(0.6)
        stall_at = time.monotonic()
        await asyncio.sleep(1.0)
        a = server.arduino
        assert a.trips == 1 and a.ignored_while_latched >= 1
        trip = a.trip_times[0]
        last_frame = max(ts for ts, _ in server.received if ts < stall_at)
        assert 0.39 <= trip - last_frame <= 0.48
        assert all(not (mode == MODE_APP and power) for ts, mode, _angle, power, _h in a.history if ts > trip)
        assert "device_watchdog" in ev["ended"]  # host saw the trip count rise and ended its motion
        assert ev["watchdog"][0]["during_motion"] is True
        assert not t.is_moving and not a.latched  # host's stop burst cleared the latch
    finally:
        await t.disconnect()
        await server.close()


# ------------------------------------------------------------------ runtime: R5 orchestration
async def test_link_test_preconditions(settings, tts):
    rt = await make_runtime(settings, tts, device_watchdog_s=0.4)
    try:
        r = await rt.run_link_test("on_a_table")
        assert not r.ok and "wheels_off_ground" in r.message
        r = await rt.run_link_test("on_floor", freeze_s=3.0)
        assert not r.ok and "1.5" in r.message
        r = await rt.run_link_test("wheels_off_ground", primitive="rotate_left")
        assert not r.ok and "translation" in r.message
        r = await rt.run_link_test("wheels_off_ground")
        assert not r.ok and r.data["reason_code"] == "disarmed"
        await rt.arm()
        await rt.start_session(task="essay", duration_min=25)
        r = await rt.run_link_test("wheels_off_ground")
        assert not r.ok and "IDLE" in r.message
    finally:
        await rt.shutdown()


async def test_link_test_on_the_simulator_with_a_device_watchdog(settings, tts):
    rt = await make_runtime(settings, tts, device_watchdog_s=0.4)
    try:
        seen: list = []
        rt.bus.add_listener(lambda e: seen.append(e))
        await rt.arm()
        r = await rt.run_link_test("wheels_off_ground", speed=80, freeze_s=1.0)
        res = r.data["result"]
        assert r.ok and res["outcome"] == "completed" and res["simulated"] is True
        assert res["speed"] == 40  # LINK_TEST profile clamp
        assert res["frames_written_during_freeze"] == 0
        dw = res["device_watchdog"]
        assert dw["tripped"] is True and 0.38 <= dw["wheels_stopped_after_s"] <= 0.5
        types = [e.event_type for e in seen]
        for et in (EventType.ROBOT_LINK_TEST_STARTED, EventType.ROBOT_LINK_FROZEN, EventType.ROBOT_DEVICE_WATCHDOG, EventType.ROBOT_LINK_TEST_FINISHED):
            assert et in types
        assert all(e.simulated for e in seen if e.event_type.value.startswith("robot.link"))
        assert rt.displacement == []  # nothing to replay for a link test
        assert rt.snapshot()["robot"]["link_tests"][-1]["test_id"] == res["test_id"]
    finally:
        await rt.shutdown()


async def test_link_test_on_the_simulator_without_a_device_watchdog(settings, tts):
    rt = await make_runtime(settings, tts, device_watchdog_s=None)
    try:
        await rt.arm()
        res = (await rt.run_link_test("wheels_off_ground", freeze_s=1.0)).data["result"]
        dw = res["device_watchdog"]
        assert dw["tripped"] is False
        assert 1.0 <= dw["wheels_stopped_after_s"] <= 1.2  # ran until the host's stop after the freeze
    finally:
        await rt.shutdown()


async def test_emergency_stop_interrupts_a_link_test(settings, tts):
    rt = await make_runtime(settings, tts, device_watchdog_s=None)
    try:
        await rt.arm()
        task = asyncio.create_task(rt.run_link_test("wheels_off_ground", freeze_s=4.0))
        await asyncio.sleep(1.0)
        await rt.emergency_stop(speak=False)
        res = (await asyncio.wait_for(task, 2.0)).data["result"]
        assert res["outcome"] == "interrupted"
        assert res["frozen_for_s"] < 1.0
        assert rt.safety.estop_latched and not rt.transport.tx_frozen and not rt.transport.is_moving
    finally:
        await rt.shutdown()


async def zeus_runtime(settings, tts, server, motor_commands="bench"):
    """Runtime on the Zeus profile in bench mode (control link): motion needs the patched
    firmware's telemetry, as on the real robot."""
    settings = settings.model_copy(update={"zeus_motor_commands": motor_commands, "zeus_port": server.port})
    rt = await make_runtime(settings, tts)
    await rt.disconnect_robot()
    r = await rt.connect_robot("zeus", host="127.0.0.1")
    assert r.ok, r.message
    for _ in range(100):  # the stop burst's echo carries the patch telemetry
        if rt.transport.get_status().firmware.get("locked_in_patch") or not server.arduino.patched:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.1)
    return rt


async def test_link_test_against_the_patched_firmware_model(settings, tts):
    server = await FakeESP32("modern", patched=True).start()
    rt = await zeus_runtime(settings, tts, server)
    try:
        assert not (await rt.arm()).ok  # motor power not checked yet
        gate = {c["id"]: c for c in rt.hardware_gate()["checks"]}
        assert gate["motor_power"]["ok"] is False and gate["motor_power"]["blocking"]
        assert (await rt.confirm_motor_power()).ok
        assert {c["id"]: c for c in rt.hardware_gate()["checks"]}["motor_power"]["ok"]
        assert (await rt.arm()).ok
        res = (await rt.run_link_test("wheels_off_ground", freeze_s=1.0)).data["result"]
        assert res["outcome"] == "completed" and res["simulated"] is False
        assert res["frames_written_during_freeze"] == 0 and res["link_closed_after_s"] is None
        dw = res["device_watchdog"]
        assert dw["source"] == "firmware_patch" and dw["tripped"] is True
        assert (dw["trips_before"], dw["trips_after"]) == (0, 1)
        assert any("video" in n for n in res["notes"])  # the host never claims the wheels stopped
        assert not server.arduino.motors_active
    finally:
        await rt.shutdown()
        await server.close()


async def test_link_test_on_stock_firmware_is_refused_even_in_bench_mode(settings, tts):
    """Stock firmware reports no device-side expiry, so the hardware gate stays shut: no
    arming, no link test, and the Arduino only ever sees Stop frames."""
    server = await FakeESP32("modern", patched=False).start()
    rt = await zeus_runtime(settings, tts, server)
    try:
        armed = await rt.arm()
        assert not armed.ok and "physical motion is locked" in armed.message
        res = await rt.run_link_test("wheels_off_ground", freeze_s=1.0)
        assert not res.ok and res.data["reason_code"] == "hardware_motion_locked"
        stops = [ln for ln in server.arduino.region_lines if ";1;" in ln[3:12]]  # F = index 5
        assert server.arduino.region_lines and len(stops) == len(server.arduino.region_lines)
        assert not server.arduino.motors_active
        gate = rt.hardware_gate()
        assert not gate["motion_permitted"]
        assert next(c for c in gate["checks"] if c["id"] == "device_expiry")["ok"] is False
    finally:
        await rt.shutdown()
        await server.close()


async def test_motor_power_check_is_for_the_real_robot_and_cleared_when_the_link_drops(settings, tts):
    sim = await make_runtime(settings, tts)
    try:
        r = await sim.confirm_motor_power()
        assert not r.ok and "only the real robot" in r.message
        assert (await sim.arm()).ok  # the simulator needs no physical check
    finally:
        await sim.shutdown()

    server = await FakeESP32("modern", patched=True).start()
    rt = await zeus_runtime(settings, tts, server)
    try:
        assert (await rt.confirm_motor_power()).ok
        assert rt.snapshot()["safety"]["motor_power_checked"]
        await rt.disconnect_robot()
        assert not rt.safety.motor_power_valid()  # it may have been switched off meanwhile
        r = await rt.confirm_motor_power()
        assert not r.ok and "connect the robot first" in r.message
    finally:
        await rt.shutdown()
        await server.close()


async def test_an_unknown_firmware_patch_version_keeps_motion_locked(settings, tts):
    """Only patch v2 has been reviewed: a robot reporting another version is not trusted."""
    server = await FakeESP32("modern", patched=True).start()
    server.arduino.patch_version = 3
    rt = await zeus_runtime(settings, tts, server)
    try:
        for _ in range(100):
            if rt.transport.get_status().firmware.get("unknown_patch_version") is not None:
                break
            await asyncio.sleep(0.02)
        caps = rt.transport.get_status().capabilities
        assert not caps.motion_permitted
        assert any("patch version 3" in r for r in caps.motion_lock_reasons)
        assert (await rt.confirm_motor_power()).ok
        ok = await rt.arm()
        assert not ok.ok and "patch version 3" in ok.message
    finally:
        await rt.shutdown()
        await server.close()


async def test_return_is_refused_on_the_real_robot_until_calibrated(settings, tts):
    server = await FakeESP32("modern", patched=True).start()
    rt = await zeus_runtime(settings, tts, server)
    try:
        r = await rt.return_home()
        assert not r.ok and r.data["reason_code"] == "return_uncalibrated"
        r = await rt.handle_utterance("come back", source="voice")
        assert not r.ok and not [e for e in rt.bus.history() if e.event_type == EventType.ROBOT_MOTION_REQUESTED]
    finally:
        await rt.shutdown()
        await server.close()

