"""The vision pipeline with marker tracking, frame by frame: a real ArUco marker in
rendered frames, a scripted hand model, and the real reach detector."""

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from app.config import VisionTuning  # noqa: E402
from app.vision.pipeline import VisionService  # noqa: E402

from .test_robot_tracker import H, W, hand, render  # noqa: E402

FPS = 30.0
ZONE = [(0.70, 0.55), (0.85, 0.55), (0.85, 0.75), (0.70, 0.75)]  # just right of the marker


class ScriptedHands:
    """Stands in for MediaPipe: returns the hand the test placed for the current frame."""

    def __init__(self):
        self.current = None

    def detect(self, rgb, ts):
        return [] if self.current is None else [hand(*self.current)]

    def close(self):
        pass


def make_service(tmp_path, hold_s=8.0):
    got = []
    svc = VisionService(tmp_path, emit=got.append, width=W, height=H)
    svc.hands_model = ScriptedHands()
    svc.face_model = None
    svc.configure(zone=ZONE, tuning=VisionTuning(reach_cooldown_s=0.5), hold_s=hold_s, marker_ids=[7, 8])
    return svc, got


def frame(marker_xy, seed, extra=()):
    gray = render([(7, marker_xy[0], marker_xy[1], 60, 0), *extra], seed=seed)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def feed(svc, n0, n, marker_at, hand_at=None, t0=100.0, extra=()):
    for i in range(n0, n0 + n):
        svc.hands_model.current = hand_at(i) if hand_at else None
        xy = marker_at(i)
        bgr = frame(xy, i, extra) if xy is not None else cv2.cvtColor(render([], seed=i), cv2.COLOR_GRAY2BGR)
        svc.process_frame(i + 1, bgr, t0 + i / FPS, mirrored=False)
    return n0 + n


def kinds(got, kind):
    return [e for e in got if e.kind == kind]


def test_zone_follows_the_robot_and_reaches_are_judged_against_the_new_zone(tmp_path):
    svc, got = make_service(tmp_path)
    i = feed(svc, 0, 5, lambda i: (420, 300))
    svc.attach_zone(ZONE)
    i = feed(svc, i, 10, lambda i: (420, 300))
    assert svc.tracker.state == "TRACKING"
    assert np.allclose(svc.zone, ZONE, atol=0.003)

    # Robot retreats 120 px left and 30 px down over 0.6 s.
    svc.set_robot_moving(True)
    start = i
    i = feed(svc, i, 18, lambda k: (420 - 120 * (k - start + 1) / 18, 300 + 30 * (k - start + 1) / 18))
    svc.set_robot_moving(False)
    i = feed(svc, i, 15, lambda k: (300, 330))
    shifted = [(x - 120 / W, y + 30 / H) for x, y in ZONE]
    assert svc.tracker.state == "TRACKING"
    assert np.allclose(svc.zone, shifted, atol=0.004)

    # A hand reaching for where the phone USED to be: no reach.
    s0 = i
    i = feed(svc, i, 40, lambda k: (300, 330), hand_at=lambda k: (0.98 - min(k - s0, 14) * 0.014, 0.65))
    assert kinds(got, "confirmed") == []
    i = feed(svc, i, 15, lambda k: (300, 330))  # hand leaves

    # A hand reaching for where the phone IS now: confirmed.
    s1 = i
    i = feed(svc, i, 40, lambda k: (300, 330), hand_at=lambda k: (0.30 + min(k - s1, 15) * 0.019, 0.71))
    confirmed = kinds(got, "confirmed")
    assert len(confirmed) == 1 and confirmed[0].data["approach_seen"]

    tracking = [e.data["to"] for e in kinds(got, "zone_tracking")]
    assert tracking[:1] == ["TRACKING"]


def test_marker_hidden_by_the_reaching_hand_keeps_the_zone_while_stationary(tmp_path):
    svc, got = make_service(tmp_path)
    i = feed(svc, 0, 5, lambda i: (420, 300))
    svc.attach_zone(ZONE)
    i = feed(svc, i, 5, lambda i: (420, 300))
    # The hand covers the marker as it reaches into the zone right next to it.
    s0 = i
    i = feed(svc, i, 25, lambda k: None, hand_at=lambda k: (0.55 + min(k - s0, 12) * 0.018, 0.65))
    assert svc.tracker.state == "HELD"
    assert len(kinds(got, "confirmed")) == 1


def test_lost_marker_after_motion_pauses_reach_detection_until_reacquired(tmp_path):
    svc, got = make_service(tmp_path)
    i = feed(svc, 0, 5, lambda i: (420, 300))
    svc.attach_zone(ZONE)
    i = feed(svc, i, 5, lambda i: (420, 300))
    svc.set_robot_moving(True)
    i = feed(svc, i, 3, lambda k: None)  # robot moves while the marker is out of view
    svc.set_robot_moving(False)
    assert svc.tracker.state == "LOST"
    assert svc.snapshot()["zone"] is None
    s0 = i
    i = feed(svc, i, 40, lambda k: None, hand_at=lambda k: (0.55 + min(k - s0, 12) * 0.018, 0.65))
    assert kinds(got, "confirmed") == []  # never confirm against an unknown zone
    i = feed(svc, i, 4, lambda k: (420, 300))
    assert svc.tracker.state == "TRACKING"


def test_hold_expires_into_lost(tmp_path):
    svc, got = make_service(tmp_path, hold_s=1.0)
    i = feed(svc, 0, 5, lambda i: (420, 300))
    svc.attach_zone(ZONE)
    i = feed(svc, i, 5, lambda i: (420, 300))
    i = feed(svc, i, 45, lambda k: None)  # 1.5 s hidden, robot never moved
    assert svc.tracker.state == "LOST"
    assert [e.data["to"] for e in kinds(got, "zone_tracking")][-2:] == ["HELD", "LOST"]


def test_spurious_marker_elsewhere_does_not_drag_the_zone(tmp_path):
    svc, got = make_service(tmp_path)
    i = feed(svc, 0, 5, lambda i: (420, 300))
    svc.attach_zone(ZONE)
    i = feed(svc, i, 5, lambda i: (420, 300))
    i = feed(svc, i, 2, lambda k: (180, 120))  # two frames of the "marker" far away
    i = feed(svc, i, 5, lambda k: (420, 300))
    assert np.allclose(svc.zone, ZONE, atol=0.003)
    assert svc.tracker.stats["rejected_jumps"] >= 1


def test_static_mode_is_unchanged_by_markers(tmp_path):
    svc, got = make_service(tmp_path)
    feed(svc, 0, 10, lambda i: (300, 330))
    assert svc.zone_mode == "static" and svc.zone == ZONE
    assert kinds(got, "zone_tracking") == []


def test_attach_without_a_visible_marker_is_refused(tmp_path):
    svc, _ = make_service(tmp_path)
    feed(svc, 0, 5, lambda k: None)
    with pytest.raises(ValueError, match="no robot marker visible"):
        svc.attach_zone(ZONE)
    assert svc.zone_mode == "static"


def test_stage_timings_are_reported(tmp_path):
    svc, _ = make_service(tmp_path)
    feed(svc, 0, 5, lambda i: (420, 300))
    svc.attach_zone(ZONE)
    feed(svc, 5, 10, lambda i: (420, 300))
    stages = svc.stage_stats()
    assert {"markers", "to_rgb", "hands", "reach"} <= set(stages)
    assert stages["markers"]["p50"] is not None
