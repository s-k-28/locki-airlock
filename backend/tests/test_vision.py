"""Reach detector, zone geometry and focus estimator on synthetic trajectories, plus
pipeline plumbing on a generated video file. MediaPipe accuracy itself is measured
on real trials (docs/PERFORMANCE.md), not here."""

import time

import numpy as np
import pytest

from app.config import VisionTuning
from app.sessions.metrics import FocusState
from app.vision.focus_observation import FaceObs, FocusEstimator, yaw_ratio_from_keypoints
from app.vision.phone_zone import point_in_polygon, polygon_area, signed_distance
from app.vision.reach import HandObs, ReachDetector

ZONE = [(0.6, 0.6), (0.9, 0.6), (0.9, 0.9), (0.6, 0.9)]
FPS = 30.0


def hand_at(cx: float, cy: float, score: float = 0.95) -> HandObs:
    """21 landmarks: wrist at (cx, cy+0.06), fingertips fanned above it, index tip at (cx, cy)."""
    pts = [(cx, cy + 0.06)]
    for finger, dx in enumerate((-0.03, -0.012, 0.0, 0.012, 0.024)):
        for j in range(4):
            frac = (j + 1) / 4
            pts.append((cx + dx * frac, cy + 0.06 - 0.06 * frac + (0.004 if finger != 2 else 0)))
    pts[8] = (cx, cy)  # index fingertip leads the reach
    return HandObs(pts, score)


def run(detector: ReachDetector, path, t0: float = 0.0, fps: float = FPS):
    events = []
    t = t0
    for pos in path:
        hands = [] if pos is None else [hand_at(*pos) if len(pos) == 2 else hand_at(pos[0], pos[1], pos[2])]
        events += [(round(t, 3), e) for e in detector.update(t, hands)]
        t += 1 / fps
    return events, t


def kinds(events):
    return [e.kind for _, e in events if e.kind not in ("hand_detected", "hand_lost")]


def lerp(a, b, n):
    return [(a[0] + (b[0] - a[0]) * i / (n - 1), a[1] + (b[1] - a[1]) * i / (n - 1)) for i in range(n)]


# ------------------------------------------------------------------ geometry
def test_zone_geometry():
    assert point_in_polygon(0.7, 0.7, ZONE)
    assert not point_in_polygon(0.5, 0.7, ZONE)
    assert signed_distance(0.7, 0.7, ZONE) == pytest.approx(-0.1)
    assert signed_distance(0.5, 0.75, ZONE) == pytest.approx(0.1)
    assert signed_distance(0.95, 0.95, ZONE) == pytest.approx(np.hypot(0.05, 0.05))
    assert polygon_area(ZONE) == pytest.approx(0.09)
    tri = [(0.2, 0.2), (0.4, 0.2), (0.3, 0.4)]
    assert point_in_polygon(0.3, 0.25, tri) and not point_in_polygon(0.21, 0.39, tri)


# ------------------------------------------------------------------ reach detector
def test_deliberate_reach_is_confirmed_once_with_latency_bound():
    d = ReachDetector(ZONE)
    path = lerp((0.2, 0.75), (0.72, 0.75), 18) + [(0.72, 0.75)] * 15
    events, _ = run(d, path)
    ks = kinds(events)
    assert ks.count("approaching") == 1 and ks.count("confirmed") == 1
    t_enter = next(t for t, e in events if e.kind == "approaching")
    t_conf = next(t for t, e in events if e.kind == "confirmed")
    conf = next(e for _, e in events if e.kind == "confirmed")
    assert conf.data["approach_seen"] is True
    assert conf.confidence > 0.6
    # confirm_dwell 180 ms after entering the zone, at 30 fps
    assert t_conf - t_enter < 0.5


def test_hand_resting_in_frame_never_repeats():
    d = ReachDetector(ZONE)
    path = lerp((0.2, 0.75), (0.72, 0.75), 18) + [(0.72, 0.75)] * 300  # 10 s parked in the zone
    events, _ = run(d, path)
    assert kinds(events).count("confirmed") == 1
    assert "suppressed" not in kinds(events)  # disarmed until the hand leaves


def test_quick_pass_through_is_not_a_reach():
    d = ReachDetector(ZONE)
    path = lerp((0.2, 0.75), (0.98, 0.75), 12) + [None] * 15  # crosses the zone in ~3 frames, then leaves frame
    events, _ = run(d, path)
    assert "confirmed" not in kinds(events)
    assert kinds(events)[-1] == "cleared"


def test_hovering_near_zone_is_cleared_not_confirmed():
    d = ReachDetector(ZONE)
    path = lerp((0.2, 0.75), (0.55, 0.75), 15) + [(0.55, 0.75)] * 10 + lerp((0.55, 0.75), (0.2, 0.75), 15)
    events, _ = run(d, path)
    ks = kinds(events)
    assert "approaching" in ks and "confirmed" not in ks
    cleared = [e for _, e in events if e.kind == "cleared"]
    assert cleared and cleared[0].data["reason"] == "left_zone_unconfirmed"


def test_low_confidence_hands_are_ignored():
    d = ReachDetector(ZONE)
    path = [(x, y, 0.3) for x, y in lerp((0.2, 0.75), (0.72, 0.75), 18)] + [(0.72, 0.75, 0.3)] * 20
    events, _ = run(d, path)
    assert kinds(events) == []


def test_repeat_reach_inside_cooldown_is_suppressed_then_allowed_after():
    d = ReachDetector(ZONE, VisionTuning(reach_cooldown_s=3.0))
    reach = lerp((0.2, 0.75), (0.72, 0.75), 18) + [(0.72, 0.75)] * 10
    away = lerp((0.72, 0.75), (0.2, 0.75), 15)
    events, t = run(d, reach + away + reach + away)  # second reach ~1.5 s later
    assert kinds(events).count("confirmed") == 1
    assert kinds(events).count("suppressed") == 1
    cleared = [e for _, e in events if e.kind == "cleared"]
    assert cleared and cleared[-1].data["reason"] == "suppressed_by_cooldown"
    events2, _ = run(d, [None] * 60 + reach, t0=t)  # 2 s gap: cooldown over
    assert "confirmed" in kinds(events2)


def test_static_hand_needs_longer_dwell():
    d = ReachDetector(ZONE)
    events, _ = run(d, [(0.72, 0.75)] * 20)  # appears inside, no approach, 0.66 s
    assert "confirmed" not in kinds(events)
    events, _ = run(d, [(0.72, 0.75)] * 15, t0=20 / FPS)  # total > 900 ms static dwell
    conf = [e for _, e in events if e.kind == "confirmed"]
    assert conf and conf[0].data["approach_seen"] is False


def test_missing_frames_expire_tracks_and_close_episode():
    d = ReachDetector(ZONE)
    events, t = run(d, lerp((0.2, 0.75), (0.58, 0.75), 15))
    assert "approaching" in kinds(events)
    later = d.tick(t + 1.0)  # camera stalled for a second
    assert [e.kind for e in later] == ["cleared", "hand_lost"]
    assert d.tracks == {}


def test_reset_on_camera_loss():
    d = ReachDetector(ZONE)
    run(d, lerp((0.2, 0.75), (0.58, 0.75), 15))
    out = d.reset("camera lost")
    assert any(e.kind == "cleared" for e in out) and d.tracks == {}


def test_two_hands_are_tracked_independently():
    d = ReachDetector(ZONE)
    t = 0.0
    events = []
    for i in range(40):
        far = hand_at(0.15, 0.2)
        x = 0.2 + min(i, 18) * (0.52 / 18)
        events += d.update(t, [far, hand_at(x, 0.75)])
        t += 1 / FPS
    assert sum(1 for e in events if e.kind == "hand_detected") == 2
    assert sum(1 for e in events if e.kind == "confirmed") == 1


def test_stricter_tuning_delays_confirmation():
    loose = ReachDetector(ZONE, VisionTuning(confirm_dwell_ms=100))
    strict = ReachDetector(ZONE, VisionTuning(confirm_dwell_ms=500))
    path = lerp((0.2, 0.75), (0.72, 0.75), 18) + [(0.72, 0.75)] * 30
    t_loose = next(t for t, e in run(loose, path)[0] if e.kind == "confirmed")
    t_strict = next(t for t, e in run(strict, path)[0] if e.kind == "confirmed")
    assert t_strict - t_loose == pytest.approx(0.4, abs=0.07)


# ------------------------------------------------------------------ focus
def test_focus_glance_away_is_not_distraction():
    f = FocusEstimator()
    t = 0.0
    changes = []
    for i in range(300):  # 10 s
        yaw = 0.6 if 90 <= i < 150 else 0.0  # 2 s glance
        c = f.update(t, FaceObs(True, 0.9, yaw))
        if c:
            changes.append(c[0])
        t += 1 / 30
    assert changes == [FocusState.FOCUSED]


def test_focus_sustained_turn_and_absence():
    f = FocusEstimator(distracted_after_s=8, away_after_s=4)
    t = 0.0
    seen = []

    def feed(n, obs):
        nonlocal t
        for _ in range(n):
            c = f.update(t, obs)
            if c:
                seen.append(c[0])
            t += 0.1

    feed(20, FaceObs(True, 0.9, 0.0))
    feed(100, FaceObs(True, 0.9, 0.7))  # 10 s turned away
    feed(80, FaceObs(False))  # 8 s gone
    assert seen == [FocusState.FOCUSED, FocusState.POSSIBLY_DISTRACTED, FocusState.AWAY]
    assert f.update(t, None, camera_ok=False)[0] == FocusState.UNKNOWN


def test_yaw_ratio():
    assert yaw_ratio_from_keypoints([(0.4, 0.5), (0.6, 0.5), (0.5, 0.6)]) == pytest.approx(0.0)
    assert yaw_ratio_from_keypoints([(0.4, 0.5), (0.6, 0.5), (0.58, 0.6)]) == pytest.approx(0.4)
    assert yaw_ratio_from_keypoints([(0.5, 0.5)]) is None


# ------------------------------------------------------------------ pipeline plumbing
def test_pipeline_on_video_file_emits_events_and_jpeg(tmp_path, monkeypatch):
    cv2 = pytest.importorskip("cv2")
    from app.vision import pipeline as pl

    video = tmp_path / "clip.avi"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 30, (320, 240))
    for _ in range(60):
        writer.write(np.full((240, 320, 3), 40, dtype=np.uint8))
    writer.release()

    path = lerp((0.2, 0.75), (0.72, 0.75), 18) + [(0.72, 0.75)] * 42

    class ScriptedHands:
        def __init__(self, *a, **k):
            self.i = 0

        def detect(self, rgb, ts):
            pos = path[min(self.i, len(path) - 1)]
            self.i += 1
            return [hand_at(*pos)]

        def close(self):
            pass

    monkeypatch.setattr(pl, "HandTracker", ScriptedHands)
    monkeypatch.setattr(pl, "FaceObserver", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no face model in test")))
    got = []
    svc = pl.VisionService(tmp_path, emit=got.append, width=320, height=240)
    svc.configure(zone=ZONE)
    svc.stream_clients = 1
    svc.start(str(video))
    deadline = time.time() + 6
    while time.time() < deadline and not any(e.kind == "confirmed" for e in got):
        time.sleep(0.05)
    snap = svc.snapshot()
    jpg = svc.latest_jpeg()
    svc.stop()
    k = [e.kind for e in got]
    assert "camera_connected" in k and "approaching" in k and "confirmed" in k
    confirmed = next(e for e in got if e.kind == "confirmed")
    assert confirmed.frame_mono is not None and confirmed.confidence > 0.5
    assert jpg is not None and jpg[:2] == b"\xff\xd8"
    assert snap["stats"]["frames_processed"] > 10
    assert snap["models"]["face"] is False  # reported honestly
