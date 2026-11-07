"""Milestone 3 hardware profile, end to end against stand-ins for the ESP32 (WebSocket) and its
camera (MJPEG): what `make hardware` configures, what the dashboard is told, and what is never
done (no fallback to simulation or to the Mac webcam, no arming, no motor frames)."""

from __future__ import annotations

import asyncio
import time

from fastapi.testclient import TestClient

from app.config import RuntimeSettings
from app.runtime import LockedInRuntime
from app.vision.pipeline import EGO_SETTLE_S, VisionService

from .conftest import wait_for
from .fake_esp32 import FakeESP32
from .fake_mjpeg import FakeEsp32Camera, closed_port


def hardware_settings(settings, ws_port: int, camera_url: str, **extra):
    return settings.model_copy(
        update={
            "robot_driver": "zeus",
            "zeus_host": "127.0.0.1",
            "zeus_port": ws_port,
            "zeus_autoconnect": True,
            "zeus_camera_url": camera_url,
            "camera_source": "zeus",
            "vision_autostart": True,
            "offline": True,
            **extra,
        }
    )


async def start_runtime(settings, tts, enable_vision=True) -> LockedInRuntime:
    rt = LockedInRuntime(
        settings, runtime_settings=RuntimeSettings(), persist_settings=False, tts_engine=tts,
        enable_vision=enable_vision, enable_voice=False,
    )
    await rt.start()
    return rt


async def test_hardware_profile_connects_both_links_and_stays_locked(settings, tts):
    esp = await FakeESP32("legacy").start()
    cam = FakeEsp32Camera(fps=15).start()
    rt = await start_runtime(hardware_settings(settings, esp.port, cam.url), tts)
    try:
        assert await wait_for(lambda: rt.transport is not None and rt.transport.connected, timeout=5)
        assert await wait_for(lambda: rt.vision_live, timeout=8)
        snap = rt.snapshot()
        v = snap["vision"]
        assert v["source_label"] == "ZEUS ESP32-CAM" and v["source"] == cam.url and v["mount"] == "robot"
        assert v["source"] != "0" and v["backend"] == "http"
        m = snap["mode"]
        assert m["label"] == "ZEUS · MOTION LOCKED" and m["camera_label"] == "ZEUS ESP32-CAM"
        assert m["robot_profile"] == "zeus" and not m["robot_simulated"] and m["physical_motion"] == "locked"
        assert m["offline"] is True
        st = snap["robot"]["status"]
        assert st["simulated"] is False and st["capabilities"]["link_mode"] == "monitor"
        assert st["firmware"]["camera_firmware_family"] == "single_client"
        gate = snap["robot"]["gate"]
        assert not gate["motion_permitted"] and gate["motor_commands"] == "disabled"
        assert {c["id"] for c in gate["checks"] if c["blocking"] and not c["ok"]} >= {"config", "link", "device_expiry"}
        assert not snap["safety"]["armed"]
        r = await rt.arm()
        assert not r.ok and "physical motion is locked" in r.message
        r = await rt.manual_move("forward")
        assert not r.ok
        assert esp.arduino.region_lines == []
    finally:
        await rt.shutdown()
        await esp.close()
        cam.close()


async def test_unreachable_zeus_camera_is_reported_never_replaced_by_the_webcam(settings, tts):
    url = f"http://127.0.0.1:{closed_port()}/mjpg"
    rt = await start_runtime(hardware_settings(settings, closed_port(), url), tts)
    try:
        assert await wait_for(lambda: rt.vision is not None and rt.vision.camera is not None and rt.vision.camera.attempts >= 1, timeout=6)
        v = rt.vision.snapshot()
        assert v["camera"] in ("opening", "reconnecting") and v["source_label"] == "ZEUS ESP32-CAM"
        assert v["source"] == url and v["link"]["last_error"]
        assert rt.mode()["label"] == "ZEUS DISCONNECTED" and not rt.mode()["camera_live"]
    finally:
        await rt.shutdown()


async def test_camera_switch_without_restarting_the_app(settings, tts):
    cam1 = FakeEsp32Camera(fps=15).start()
    cam2 = FakeEsp32Camera(fps=15).start()
    rt = await start_runtime(hardware_settings(settings, closed_port(), cam1.url, zeus_autoconnect=False), tts)
    try:
        assert await wait_for(lambda: rt.vision.snapshot()["camera"] == "live", timeout=8)
        r = await rt.start_vision(cam2.url)
        assert r.ok and r.data["camera"]["label"].startswith("STREAM 127.0.0.1")
        assert await wait_for(lambda: rt.vision.snapshot()["camera"] == "live", timeout=8)
        v = rt.vision.snapshot()
        assert v["source"] == cam2.url and v["mount"] == "fixed"
        sent = cam1.frames_sent
        await asyncio.sleep(0.4)
        assert cam1.frames_sent - sent <= 2  # the first stream was released
        assert rt.rs.camera_source == cam2.url and rt.camera_choice == cam2.url
    finally:
        await rt.shutdown()
        cam1.close()
        cam2.close()


async def test_robot_camera_has_its_own_zone_and_refuses_marker_tracking(settings, tts):
    cam = FakeEsp32Camera(fps=15).start()
    rt = await start_runtime(hardware_settings(settings, closed_port(), cam.url, zeus_autoconnect=False), tts)
    try:
        assert await wait_for(lambda: rt.vision_live, timeout=8)
        assert rt.zone_setting_key == "robot_camera_zone"
        r = await rt.attach_zone()
        assert not r.ok and "cannot see the robot's own markers" in r.message
        rt.rs.phone_zone.mode = "marker"  # saved for the Mac webcam; must not apply here
        rt._apply_vision_tuning()
        assert rt.vision.snapshot()["zone_mode"] == "static"
        assert rt.vision.zone == [tuple(p) for p in rt.rs.robot_camera_zone.points]
    finally:
        await rt.shutdown()
        cam.close()


def test_zone_api_saves_the_zone_of_the_camera_in_use(settings, monkeypatch):
    from app.main import create_app

    app = create_app(settings.model_copy(update={"vision_preload": False, "tts_engine": "none"}))
    with TestClient(app) as c:
        zone = {"points": [[0.1, 0.5], [0.9, 0.5], [0.9, 1.0], [0.1, 1.0]], "mode": "static"}
        r = c.put("/api/vision/zone", json=zone).json()
        assert r["camera"] == "phone_zone"  # no camera running: the fixed camera's zone
        rt = app.state.runtime
        rt.vision = VisionService(rt.settings.models_dir, emit=lambda ev: None)
        rt.vision.mount = "robot"
        r = c.put("/api/vision/zone", json=zone).json()
        assert r["camera"] == "robot_camera_zone"
        assert rt.rs.robot_camera_zone.points == [tuple(p) for p in zone["points"]]


def test_robot_camera_pauses_reach_while_the_robot_moves():
    vs = VisionService("/nonexistent", emit=lambda ev: None)
    vs.mount = "robot"
    assert vs._ego_motion_gate() == [] and not vs.detector.paused
    vs.set_robot_moving(True)
    vs._ego_motion_gate()
    assert vs.detector.paused and vs.snapshot()["ego_motion_paused"]
    vs.set_robot_moving(False)
    vs._ego_motion_gate()
    assert vs.detector.paused  # settling: the image may still be moving
    vs._ego_settle_until = time.monotonic() - 0.01
    vs._ego_motion_gate()
    assert not vs.detector.paused and vs.detector.stats["rebases"] >= 1
    assert EGO_SETTLE_S > 0
    fixed = VisionService("/nonexistent", emit=lambda ev: None)  # a fixed camera is unaffected
    fixed.set_robot_moving(True)
    assert not fixed.detector.paused


def test_robot_camera_without_a_face_is_unknown_not_away():
    from app.vision.focus_observation import FocusEstimator

    est = FocusEstimator()
    change = est.update(1.0, None, camera_ok=False, unknown_reason="robot camera: no face in view")
    assert change is None or change[0].value == "UNKNOWN"
    assert est.state.value == "UNKNOWN"
