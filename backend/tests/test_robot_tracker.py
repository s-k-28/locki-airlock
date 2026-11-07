"""ArUco robot tracking: real marker detection on rendered frames, geometry, the tracker
state machine (loss, hold, occlusion, false detections, re-acquire) and the reach
detector's behaviour when the zone moves or becomes unknown."""

import math

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from app.vision.reach import HandObs, ReachDetector  # noqa: E402
from app.vision.robot_tracker import (  # noqa: E402
    HELD,
    LOST,
    SEARCHING,
    TRACKING,
    MarkerDetector,
    MarkerObservation,
    RobotTracker,
    TrackerConfig,
    ZoneAnchor,
    from_marker_frame,
    marker_image,
    to_marker_frame,
)

W, H = 640, 480
TOL = 1e-5  # anchors are stored rounded to 5 decimals of a marker side (~0.0002 px here)
ZONE = [(0.58, 0.55), (0.80, 0.55), (0.80, 0.85), (0.58, 0.85)]


# ------------------------------------------------------------------ rendering helpers
def square(cx, cy, side, angle_deg) -> np.ndarray:
    a = math.radians(angle_deg)
    half = side / 2
    base = np.array([[-half, -half], [half, -half], [half, half], [-half, half]])
    rot = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
    return base @ rot.T + np.array([cx, cy])


def obs(mid, cx, cy, side=60, angle=0.0) -> MarkerObservation:
    return MarkerObservation(mid, square(cx, cy, side, angle), W, H)


def render(markers, occlude=None, mirror=False, seed=0) -> np.ndarray:
    """Gray frame with real ArUco markers composited at (id, cx, cy, side_px, angle_deg)."""
    rng = np.random.default_rng(seed)
    canvas = (150 + rng.integers(-8, 8, (H, W))).astype(np.uint8)
    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    for mid, cx, cy, side, angle in markers:
        cells = 6
        cell = 20
        tag = cv2.aruco.generateImageMarker(d, mid, cell * cells)
        pad = cell  # one-cell white quiet zone
        img = np.full((tag.shape[0] + 2 * pad, tag.shape[1] + 2 * pad), 255, np.uint8)
        img[pad:-pad, pad:-pad] = tag
        size = img.shape[0]
        scale = side / tag.shape[0]
        m = cv2.getRotationMatrix2D((size / 2, size / 2), -angle, scale)
        m[:, 2] += (cx - size / 2, cy - size / 2)
        warped = cv2.warpAffine(img, m, (W, H), flags=cv2.INTER_LINEAR, borderValue=0)
        mask = cv2.warpAffine(np.ones_like(img) * 255, m, (W, H), borderValue=0) > 127
        canvas[mask] = warped[mask]
    if occlude is not None:
        x0, y0, x1, y1 = occlude
        canvas[y0:y1, x0:x1] = 110  # a hand-coloured block over part of the marker
    return cv2.flip(canvas, 1) if mirror else canvas


def poly_err(a, b) -> float:
    return float(np.max(np.abs(np.array(a) - np.array(b))))


# ------------------------------------------------------------------ geometry
def test_marker_frame_round_trip_and_rigid_follow():
    o = obs(7, 300, 200, side=50, angle=20)
    pts = np.array([[350.0, 260.0], [420.0, 260.0], [420.0, 330.0]])
    back = from_marker_frame(to_marker_frame(pts, o), o)
    assert np.allclose(back, pts, atol=1e-9)

    anchor = ZoneAnchor.attach(ZONE, [obs(7, 300, 200)])
    assert poly_err(anchor.project(obs(7, 300, 200)), ZONE) < TOL
    moved = anchor.project(obs(7, 300 - 64, 200 + 24))  # robot moved left 64 px, down 24 px
    assert poly_err(moved, [(x - 0.1, y + 0.05) for x, y in ZONE]) < TOL
    turned = anchor.project(obs(7, 300, 200, angle=30))  # rotated about the marker centre
    c = np.array([300.0, 200.0])
    a = math.radians(30)
    rot = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
    expected = ((np.array(ZONE) * [W, H] - c) @ rot.T + c) / [W, H]
    assert poly_err(turned, expected) < TOL
    farther = anchor.project(obs(7, 300, 200, side=30))  # half size: robot farther away
    expected = ((np.array(ZONE) * [W, H] - c) * 0.5 + c) / [W, H]
    assert poly_err(farther, expected) < TOL


def test_anchor_serialisation_round_trip():
    anchor = ZoneAnchor.attach(ZONE, [obs(7, 300, 200), obs(8, 420, 200)])
    again = ZoneAnchor.from_dict(anchor.to_dict())
    assert poly_err(again.project(obs(8, 420, 200)), ZONE) < 1e-4


def test_attach_requires_a_visible_marker():
    with pytest.raises(ValueError, match="no robot marker visible"):
        ZoneAnchor.attach(ZONE, [])


# ------------------------------------------------------------------ real detection
def test_detector_finds_marker_with_correct_pose():
    det = MarkerDetector(ids=[7])
    found = det.detect(render([(7, 420, 330, 60, 15)]), mirrored=False)
    assert [o.marker_id for o in found] == [7]
    o = found[0]
    assert np.linalg.norm(o.center - [420, 330]) < 1.5
    assert abs(o.side - 60) < 2.0
    assert abs(math.degrees(o.angle) - 15) < 2.0


def test_mirrored_camera_is_handled_and_raw_detection_would_fail():
    frame = render([(7, 420, 330, 60, 0)], mirror=True)  # the pipeline mirrors webcam frames
    assert MarkerDetector(ids=[7]).detect(frame, mirrored=False) == []  # mirrored codes don't decode
    found = MarkerDetector(ids=[7]).detect(frame, mirrored=True)
    assert len(found) == 1
    assert np.linalg.norm(found[0].center - [W - 1 - 420, 330]) < 1.5  # display-space coordinates


def test_detector_ignores_unconfigured_ids_and_tiny_quads():
    det = MarkerDetector(ids=[7])
    found = det.detect(render([(7, 200, 200, 60, 0), (11, 450, 200, 60, 0)]), mirrored=False)
    assert [o.marker_id for o in found] == [7]
    assert det.rejected_ids == 1
    det2 = MarkerDetector(ids=[7], min_side_px=40)
    assert det2.detect(render([(7, 200, 200, 30, 0)]), mirrored=False) == []


def test_partial_occlusion_hides_one_marker_but_not_the_other():
    det = MarkerDetector(ids=[7, 8])
    both = det.detect(render([(7, 300, 300, 60, 0), (8, 420, 300, 60, 0)]), mirrored=False)
    assert sorted(o.marker_id for o in both) == [7, 8]
    covered = det.detect(render([(7, 300, 300, 60, 0), (8, 420, 300, 60, 0)], occlude=(270, 270, 330, 315)), mirrored=False)
    assert [o.marker_id for o in covered] == [8]


def test_printable_marker_round_trips_through_the_detector():
    img = marker_image(7, px=300)
    found = MarkerDetector(ids=[7]).detect(img, mirrored=False)
    assert [o.marker_id for o in found] == [7]


# ------------------------------------------------------------------ tracker state machine
def run(tracker, frames, t0=0.0, dt=1 / 30):
    t, ups = t0, []
    for observations in frames:
        ups.append(tracker.update(t, observations))
        t += dt
    return ups, t


def acquired(markers=(7,), hold_s=8.0) -> tuple[RobotTracker, float]:
    obs_list = [obs(m, 300 + 120 * i, 300) for i, m in enumerate(markers)]
    tracker = RobotTracker(ZoneAnchor.attach(ZONE, obs_list), TrackerConfig(hold_s=hold_s))
    ups, t = run(tracker, [obs_list] * 3)
    assert ups[-1].state == TRACKING
    return tracker, t


def test_acquire_requires_consistent_frames():
    anchor = ZoneAnchor.attach(ZONE, [obs(7, 300, 300)])
    tracker = RobotTracker(anchor)
    ups, _ = run(tracker, [[obs(7, 300, 300)]] * 2)
    assert [u.state for u in ups] == [SEARCHING, SEARCHING] and ups[-1].zone is None
    ups, _ = run(tracker, [[obs(7, 300, 300)]])
    assert ups[-1].state == TRACKING and poly_err(ups[-1].zone, ZONE) < TOL


def test_hidden_marker_holds_while_stationary_then_times_out():
    tracker, t = acquired(hold_s=2.0)
    ups, t = run(tracker, [[]] * 30, t0=t)  # 1 s: e.g. a hand covering the marker
    assert ups[-1].state == HELD and poly_err(ups[-1].zone, ZONE) < TOL
    ups, t = run(tracker, [[]] * 40, t0=t)  # past the 2 s hold
    assert ups[-1].state == LOST and ups[-1].zone is None


def test_robot_motion_while_hidden_invalidates_the_pose():
    tracker, t = acquired()
    run(tracker, [[]] * 3, t0=t)
    assert tracker.state == HELD
    tracker.set_robot_moving(True)
    ups, _ = run(tracker, [[]], t0=t + 0.2)
    assert ups[-1].state == LOST and ups[-1].zone is None


def test_zone_follows_a_visible_retreat_and_settles():
    tracker, t = acquired()
    tracker.set_robot_moving(True)
    path = [[obs(7, 300 - 4 * i, 300 + 2 * i)] for i in range(18)]  # 0.6 s retreat
    ups, t = run(tracker, path, t0=t)
    assert all(u.state == TRACKING for u in ups)
    tracker.set_robot_moving(False)
    ups, t = run(tracker, [[obs(7, 300 - 68, 334)]] * 10, t0=t)
    expected = ZoneAnchor.attach(ZONE, [obs(7, 300, 300)]).project(obs(7, 232, 334))
    assert ups[-1].state == TRACKING and poly_err(ups[-1].zone, expected) < 0.002
    assert not tracker.moved_since_fix


def test_motion_ending_out_of_view_is_lost_then_reacquired_elsewhere():
    tracker, t = acquired()
    tracker.set_robot_moving(True)
    run(tracker, [[obs(7, 290, 300)]] * 3, t0=t)
    tracker.set_robot_moving(False)
    ups, t = run(tracker, [[]], t0=t + 0.1)
    assert ups[-1].state == LOST
    ups, t = run(tracker, [[obs(7, 220, 330)]] * 3, t0=t)
    assert ups[-1].state == TRACKING
    expected = ZoneAnchor.attach(ZONE, [obs(7, 300, 300)]).project(obs(7, 220, 330))
    assert poly_err(ups[-1].zone, expected) < TOL


def test_single_frame_false_jump_is_rejected_while_stationary():
    tracker, t = acquired()
    ups, t = run(tracker, [[obs(7, 520, 120)], [obs(7, 300, 300)], [obs(7, 300, 300)]], t0=t)
    assert all(poly_err(u.zone, ZONE) < TOL for u in ups)
    assert tracker.stats["rejected_jumps"] >= 1


def test_persistent_relocation_while_stationary_is_accepted():
    tracker, t = acquired()  # e.g. someone picked the robot up and put it down elsewhere
    ups, t = run(tracker, [[obs(7, 450, 200)]] * 3, t0=t)
    expected = ZoneAnchor.attach(ZONE, [obs(7, 300, 300)]).project(obs(7, 450, 200))
    assert ups[-1].state == TRACKING and poly_err(ups[-1].zone, expected) < TOL


def test_physically_impossible_step_is_rejected_even_while_moving():
    tracker, t = acquired()
    tracker.set_robot_moving(True)
    ups, _ = run(tracker, [[obs(7, 620, 20)]], t0=t)
    assert poly_err(ups[-1].zone, ZONE) < TOL
    assert tracker.stats["rejected_jumps"] == 1


def test_implausible_size_counts_as_not_seen():
    tracker, t = acquired()
    ups, _ = run(tracker, [[obs(7, 300, 300, side=200)]], t0=t)  # 3.3x the calibrated side
    assert ups[-1].state == HELD
    assert tracker.stats["rejected_size"] == 1


def test_second_marker_keeps_tracking_when_one_is_covered():
    tracker, t = acquired(markers=(7, 8))
    ups, _ = run(tracker, [[obs(8, 420, 300)]] * 5, t0=t)
    assert all(u.state == TRACKING for u in ups)
    assert poly_err(ups[-1].zone, ZONE) < TOL
    assert tracker.used_markers == [8]


def test_disagreeing_markers_keep_the_one_consistent_with_the_track():
    tracker, t = acquired(markers=(7, 8))
    ups, _ = run(tracker, [[obs(7, 300, 300), obs(8, 600, 80)]] * 3, t0=t)  # 8 is a bogus detection
    assert poly_err(ups[-1].zone, ZONE) < TOL
    assert tracker.stats["rejected_disagree"] >= 1


def test_tracking_on_rendered_frames_end_to_end():
    det = MarkerDetector(ids=[7])
    first = det.detect(render([(7, 300, 300, 60, 0)]), mirrored=False)
    tracker = RobotTracker(ZoneAnchor.attach(ZONE, first))
    t = 0.0
    for cx in [300] * 3 + list(range(300, 236, -8)) + [236] * 8:
        tracker.set_robot_moving(cx != 300 and cx != 236)
        u = tracker.update(t, det.detect(render([(7, cx, 300, 60, 0)], seed=int(t * 100)), mirrored=False))
        t += 1 / 30
    expected = tracker.anchor.project(obs(7, 236, 300))
    assert u.state == TRACKING and poly_err(u.zone, expected) < 0.004  # ~2.5 px


# ------------------------------------------------------------------ reach detector with a moving zone
def hand(cx, cy, score=0.95) -> HandObs:
    pts = [(cx, cy + 0.06)] + [(cx + 0.01 * (i % 5 - 2), cy + 0.06 - 0.015 * (i // 5)) for i in range(20)]
    pts[8] = (cx, cy)
    return HandObs(pts, score)


def test_zone_moving_toward_a_resting_hand_is_not_a_reach_after_rebase():
    zone = [(0.6, 0.6), (0.8, 0.6), (0.8, 0.8), (0.6, 0.8)]
    det = ReachDetector(zone)
    t, events = 0.0, []
    for _ in range(10):
        events += det.update(t, [hand(0.45, 0.7)])  # hand resting left of the zone
        t += 1 / 30
    for i in range(1, 11):  # robot returns toward the hand: zone slides left 0.1
        shift = 0.01 * i
        det.configure(zone=[(x - shift, y) for x, y in zone])
        det.rebase()
        events += det.update(t, [hand(0.45, 0.7)])
        t += 1 / 30
    assert not any(e.kind in ("approaching", "confirmed") for e in events)


def test_paused_detector_never_confirms_and_needs_a_fresh_dwell():
    zone = [(0.6, 0.6), (0.8, 0.6), (0.8, 0.8), (0.6, 0.8)]
    det = ReachDetector(zone)
    det.set_paused(True)
    t, events = 0.0, []
    for i in range(40):
        x = 0.3 + min(i, 15) * 0.026
        events += det.update(t, [hand(x, 0.7)])
        t += 1 / 30
    assert not any(e.kind == "confirmed" for e in events)
    det.set_paused(False)
    later = []
    for _ in range(3):  # 100 ms inside: shorter than any dwell
        later += det.update(t, [hand(0.69, 0.7)])
        t += 1 / 30
    assert not any(e.kind == "confirmed" for e in later)
