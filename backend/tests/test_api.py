from fastapi.testclient import TestClient

from app.main import create_app
from app.runtime import LockedInRuntime

from .conftest import RecordingTTS


def client_for(settings):
    runtime = LockedInRuntime(settings, persist_settings=False, tts_engine=RecordingTTS(), enable_vision=False, enable_voice=False)
    return TestClient(create_app(settings, runtime))


def test_health_and_state(settings):
    with client_for(settings) as c:
        h = c.get("/api/health").json()
        assert h["ok"] and h["robot"]["simulated"] is True
        s = c.get("/api/state").json()
        assert s["state"]["name"] == "IDLE"
        assert s["mode"]["code"] == "C"
        assert s["robot"]["status"]["capabilities"]["simulated"] is True


def test_session_lifecycle_over_rest(settings):
    with client_for(settings) as c:
        assert c.post("/api/session/start", json={"task": "essay", "duration_min": 30, "target_units": 3, "unit_label": "pages"}).json()["ok"]
        assert c.post("/api/session/progress", json={"units": 1}).json()["ok"]
        assert c.post("/api/session/pause").json()["state"] == "PAUSED"
        assert c.post("/api/session/resume").json()["state"] == "SESSION_ACTIVE"
        r = c.post("/api/session/end", json={"reason": "user"}).json()
        assert r["ok"] and r["data"]["summary"]["metrics"]["completed_units"] == 1
        sessions = c.get("/api/sessions").json()
        assert sessions and sessions[0]["task"] == "essay"
        detail = c.get(f"/api/sessions/{sessions[0]['session_id']}").json()
        assert detail["end_reason"] == "user"
        assert c.get("/api/sessions/../../etc").status_code == 404


def test_typed_command_endpoint(settings):
    with client_for(settings) as c:
        r = c.post("/api/command", json={"text": "lock in, 25 minutes"}).json()
        assert r["ok"] and r["data"]["intent"]["intent"] == "start_session"
        r = c.post("/api/command", json={"text": "how much time is left"}).json()
        assert r["reply"].endswith("left.")


def test_manual_move_requires_arming(settings):
    with client_for(settings) as c:
        r = c.post("/api/robot/move", json={"primitive": "forward", "speed": 40, "duration_s": 0.2}).json()
        assert not r["ok"] and r["data"]["reason_code"] == "disarmed"
        assert c.post("/api/robot/arm").json()["ok"]
        r = c.post("/api/robot/move", json={"primitive": "forward", "speed": 40, "duration_s": 0.2}).json()
        assert r["ok"]
        bad = c.post("/api/robot/move", json={"primitive": "teleport"}).json()
        assert not bad["ok"]
        assert c.post("/api/robot/move", json={"primitive": "forward", "speed": 400}).status_code == 422


def test_zone_and_settings_validation(settings):
    with client_for(settings) as c:
        ok = c.put("/api/vision/zone", json={"points": [[0.1, 0.1], [0.4, 0.1], [0.4, 0.5]]})
        assert ok.status_code == 200
        bad = c.put("/api/vision/zone", json={"points": [[0.1, 0.1], [2.0, 0.1], [0.4, 0.5]]})
        assert bad.status_code == 422
        # milestone 1's "chaos" maps to roast, never to unhinged (that needs an opt-in)
        assert c.patch("/api/settings", json={"coach": {"personality": "chaos"}}).json()["runtime"]["coach"]["personality"] == "roast"
        assert c.patch("/api/settings", json={"motion": {"retreat_speed": 999}}).status_code == 422


def test_estop_and_reset(settings):
    with client_for(settings) as c:
        c.post("/api/robot/arm")
        assert c.post("/api/estop").json()["state"] == "EMERGENCY_STOP"
        assert not c.post("/api/robot/arm").json()["ok"]
        assert c.post("/api/reset").json()["state"] == "IDLE"


def test_vision_endpoints_report_offline_honestly(settings):
    with client_for(settings) as c:
        assert c.get("/api/vision/frame.jpg").status_code == 503
        s = c.get("/api/state").json()
        assert s["vision"]["running"] is False and s["vision"]["camera"] == "offline"


def test_simulation_endpoints_are_labelled(settings):
    import time

    with client_for(settings) as c:
        c.post("/api/session/start", json={"duration_min": 25})
        assert c.post("/api/sim/reach", json={"confirm": True}).json()["ok"]
        time.sleep(0.4)
        events = c.get("/api/events?types=vision.phone_reach_confirmed").json()
        assert events and all(e["simulated"] for e in events)


def test_link_loss_simulation_only_on_mock(settings):
    with client_for(settings) as c:
        assert c.post("/api/robot/simulate-link-loss", json={"duration_s": 1}).json()["ok"]


def test_marker_png_is_printable_and_detectable(settings):
    import cv2
    import numpy as np

    from app.vision.robot_tracker import MarkerDetector

    with client_for(settings) as c:
        r = c.get("/api/vision/marker.png?marker_id=8&px=300")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_GRAYSCALE)
        assert [o.marker_id for o in MarkerDetector(ids=[8]).detect(img, mirrored=False)] == [8]
        assert c.get("/api/vision/marker.png?marker_id=99").status_code == 422


def test_zone_attach_needs_a_live_camera_and_detach_restores_fixed_mode(settings):
    with client_for(settings) as c:
        r = c.post("/api/vision/zone/attach", json={}).json()
        assert not r["ok"] and "camera" in r["message"]
        assert c.post("/api/vision/zone/attach", json={"points": [[0.1, 0.1], [2.0, 0.1], [0.4, 0.5]]}).status_code == 422
        r = c.post("/api/vision/zone/detach").json()
        assert r["ok"]
        assert c.get("/api/state").json()["settings"]["phone_zone"]["mode"] == "static"
