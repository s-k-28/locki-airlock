"""Track the robot in the webcam view with ArUco markers so the phone zone moves with it.

The calibrated zone is stored in each marker's own frame (origin at the marker centre,
x along its top edge, unit = one marker side). Every frame, each visible marker gives an
estimate of where the zone is now; estimates are fused, checked for plausibility and
smoothed. Geometry is a similarity transform (translation, rotation, uniform scale): the
robot moves on a plane and the zone sits next to the marker, so perspective changes over
one retreat are small, and four parameters are far less jittery than a homography
extrapolated from a 5 cm marker.

States:
  DISABLED   zone is fixed in camera coordinates (no marker tracking)
  SEARCHING  tracking on, the robot's marker has not been confirmed yet: zone unknown
  TRACKING   a marker was seen this frame (or within the confirm window)
  HELD       no marker visible, but the robot has not moved since the last fix: the last
             pose is kept for ``hold_s`` (a hand reaching for the phone often covers it)
  LOST       no marker, and either the robot moved since the last fix or the hold expired:
             zone unknown, reach confirmations pause until the marker is seen again

Detection runs on the *unmirrored* frame: ArUco codes are not mirror-symmetric, so a
mirrored marker does not decode. Corners are then mapped into the mirrored display space
that the zone and hand landmarks use.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

DICTIONARIES = ("DICT_4X4_50", "DICT_4X4_100", "DICT_5X5_50", "DICT_6X6_50")

DISABLED, SEARCHING, TRACKING, HELD, LOST = "DISABLED", "SEARCHING", "TRACKING", "HELD", "LOST"
ZONE_KNOWN = frozenset({TRACKING, HELD})


@dataclass
class MarkerObservation:
    marker_id: int
    corners: np.ndarray  # (4, 2) pixel coordinates in display space, marker's TL, TR, BR, BL
    frame_w: int
    frame_h: int

    @property
    def center(self) -> np.ndarray:
        return self.corners.mean(axis=0)

    @property
    def side(self) -> float:
        c = self.corners
        return float(np.mean([np.linalg.norm(c[(i + 1) % 4] - c[i]) for i in range(4)]))

    @property
    def angle(self) -> float:
        d = self.corners[1] - self.corners[0]
        return math.atan2(float(d[1]), float(d[0]))

    def squareness(self) -> float:
        """1.0 for a perfect square; lower for skewed or elongated quads."""
        c = self.corners
        sides = [np.linalg.norm(c[(i + 1) % 4] - c[i]) for i in range(4)]
        diag = [np.linalg.norm(c[2] - c[0]), np.linalg.norm(c[3] - c[1])]
        if min(sides) <= 0 or min(diag) <= 0:
            return 0.0
        return float(min(sides) / max(sides) * min(diag) / max(diag))


def to_marker_frame(points_px: np.ndarray, obs: MarkerObservation) -> np.ndarray:
    """Pixel points -> coordinates in the marker's frame (units of marker sides)."""
    c, s, a = obs.center, obs.side, obs.angle
    rot = np.array([[math.cos(-a), -math.sin(-a)], [math.sin(-a), math.cos(-a)]])
    return (points_px - c) @ rot.T / s


def from_marker_frame(points_m: np.ndarray, obs: MarkerObservation) -> np.ndarray:
    c, s, a = obs.center, obs.side, obs.angle
    rot = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
    return points_m * s @ rot.T + c


@dataclass
class ZoneAnchor:
    """The phone zone expressed relative to one or more markers on the robot."""

    marker_points: dict[int, list[list[float]]]
    reference_side_px: dict[int, float]
    frame_size: tuple[int, int]

    @classmethod
    def attach(cls, zone_norm: Sequence[Sequence[float]], observations: Sequence[MarkerObservation]) -> ZoneAnchor:
        if not observations:
            raise ValueError("no robot marker visible: put the robot in view of the camera, then save the zone again")
        w, h = observations[0].frame_w, observations[0].frame_h
        pts_px = np.array([[x * w, y * h] for x, y in zone_norm], dtype=float)
        marker_points, sides = {}, {}
        for obs in observations:
            marker_points[obs.marker_id] = to_marker_frame(pts_px, obs).round(5).tolist()
            sides[obs.marker_id] = round(obs.side, 2)
        return cls(marker_points, sides, (w, h))

    def project(self, obs: MarkerObservation) -> np.ndarray | None:
        """Zone polygon in *normalised* display coordinates estimated from one marker."""
        pts = self.marker_points.get(obs.marker_id)
        if pts is None:
            return None
        px = from_marker_frame(np.array(pts, dtype=float), obs)
        return px / np.array([obs.frame_w, obs.frame_h], dtype=float)

    def to_dict(self) -> dict:
        return {
            "marker_points": {str(k): v for k, v in self.marker_points.items()},
            "reference_side_px": {str(k): v for k, v in self.reference_side_px.items()},
            "frame_size": list(self.frame_size),
        }

    @classmethod
    def from_dict(cls, d: dict) -> ZoneAnchor:
        return cls(
            {int(k): v for k, v in d["marker_points"].items()},
            {int(k): float(v) for k, v in d["reference_side_px"].items()},
            (int(d["frame_size"][0]), int(d["frame_size"][1])),
        )


class MarkerDetector:
    """cv2.aruco wrapper: detection, mirroring, and per-quad plausibility filtering."""

    def __init__(self, dictionary: str = "DICT_4X4_50", ids: Sequence[int] | None = None, min_side_px: float = 12.0, min_squareness: float = 0.45):
        import cv2

        if dictionary not in DICTIONARIES:
            raise ValueError(f"unsupported ArUco dictionary {dictionary}")
        self._cv2 = cv2
        aruco = cv2.aruco
        params = aruco.DetectorParameters()
        params.cornerRefinementMethod = aruco.CORNER_REFINE_SUBPIX  # steadier corners -> steadier zone
        self._detector = aruco.ArucoDetector(aruco.getPredefinedDictionary(getattr(aruco, dictionary)), params)
        self.dictionary = dictionary
        self.ids = set(ids) if ids is not None else None
        self.min_side_px = min_side_px
        self.min_squareness = min_squareness
        self.rejected_ids = 0
        self.rejected_shape = 0

    def detect(self, gray: np.ndarray, mirrored: bool) -> list[MarkerObservation]:
        h, w = gray.shape[:2]
        img = self._cv2.flip(gray, 1) if mirrored else gray
        corners, ids, _ = self._detector.detectMarkers(img)
        out: list[MarkerObservation] = []
        if ids is None:
            return out
        for quad, mid in zip(corners, ids.ravel(), strict=False):
            mid = int(mid)
            if self.ids is not None and mid not in self.ids:
                self.rejected_ids += 1
                continue
            c = quad.reshape(4, 2).astype(float)
            if mirrored:
                c[:, 0] = (w - 1) - c[:, 0]
            obs = MarkerObservation(mid, c, w, h)
            if obs.side < self.min_side_px or obs.squareness() < self.min_squareness:
                self.rejected_shape += 1
                continue
            out.append(obs)
        return out


@dataclass
class TrackerConfig:
    hold_s: float = 8.0  # keep the last pose this long while the robot is stationary
    confirm_frames: int = 3  # consecutive consistent frames to (re)acquire or accept a jump
    jump_frac: float = 0.08  # of the frame diagonal: larger moves while stationary need confirming
    max_step_frac: float = 0.35  # of the diagonal per frame: faster than this is never physical
    disagree_frac: float = 0.06  # markers whose zone estimates differ more than this are outliers
    size_ratio_min: float = 0.4  # observed / calibrated marker side
    size_ratio_max: float = 2.5
    alpha: float = 0.6  # smoothing toward the new estimate
    deadband_frac: float = 0.003  # ignore sub-pixel jitter


@dataclass
class TrackerUpdate:
    state: str
    zone: list[tuple[float, float]] | None
    moved: bool = False  # zone moved enough that hand velocities should be re-based
    events: list[dict] = field(default_factory=list)


class RobotTracker:
    def __init__(self, anchor: ZoneAnchor | None, config: TrackerConfig | None = None):
        self.anchor = anchor
        self.cfg = config or TrackerConfig()
        self.state = SEARCHING if anchor is not None else DISABLED
        self.zone: np.ndarray | None = None
        self.last_fix_t: float | None = None
        self.moved_since_fix = False
        self._moving = False
        self._streak = 0
        self._candidate: np.ndarray | None = None
        self.used_markers: list[int] = []
        self.last_t: float | None = None
        self.stats = {"fixes": 0, "rejected_jumps": 0, "rejected_size": 0, "rejected_disagree": 0, "holds": 0, "losses": 0}

    # ---------------------------------------------------------------- inputs
    def set_anchor(self, anchor: ZoneAnchor | None) -> list[dict]:
        self.anchor = anchor
        self.zone = None
        self._streak = 0
        self._candidate = None
        return self._set_state(SEARCHING if anchor is not None else DISABLED, "anchor changed")

    def set_robot_moving(self, moving: bool) -> None:
        if self._moving and not moving:
            # A motion finished. Unless a marker is seen again, the old pose is stale.
            self.moved_since_fix = True
        if moving:
            self.moved_since_fix = True
        self._moving = moving

    # ---------------------------------------------------------------- update
    def update(self, t: float, observations: Sequence[MarkerObservation]) -> TrackerUpdate:
        if self.anchor is None:
            return TrackerUpdate(DISABLED, None)
        self.last_t = t
        events: list[dict] = []
        estimate, used = self._fuse(observations)
        moved = False

        if estimate is not None:
            if self.state in (SEARCHING, LOST) or self.zone is None:
                self._streak = self._streak + 1 if self._consistent(self._candidate, estimate) else 1
                self._candidate = estimate
                if self._streak >= self.cfg.confirm_frames:
                    self.zone = estimate
                    moved = True
                    self._fix(t, used)
                    events += self._set_state(TRACKING, f"marker {used} confirmed over {self._streak} frames")
            else:
                step = self._distance(self.zone, estimate)
                if step > self.cfg.max_step_frac:
                    self.stats["rejected_jumps"] += 1
                    self._streak = 0
                elif step > self.cfg.jump_frac and not self._moving:
                    # Big move while the robot was told to stand still: a glitch, a false
                    # detection, or someone moved the robot by hand. Accept only if it persists.
                    self._streak = self._streak + 1 if self._consistent(self._candidate, estimate) else 1
                    self._candidate = estimate
                    if self._streak >= self.cfg.confirm_frames:
                        self.zone = estimate
                        moved = True
                        self._fix(t, used)
                        events += self._set_state(TRACKING, "marker jump confirmed")
                    else:
                        self.stats["rejected_jumps"] += 1
                else:
                    self._streak = 0
                    self._candidate = None
                    if step > self.cfg.deadband_frac:
                        self.zone = self.zone + self.cfg.alpha * (estimate - self.zone)
                        moved = step > 0.01
                    self._fix(t, used)
                    if self.state != TRACKING:
                        events += self._set_state(TRACKING, f"marker {used} visible")
        else:
            self._streak = 0
            self._candidate = None
            if self.state == TRACKING:
                if self._moving or self.moved_since_fix:
                    events += self._set_state(LOST, "marker not visible after the robot moved")
                else:
                    self.stats["holds"] += 1
                    events += self._set_state(HELD, "marker hidden; robot stationary, holding last pose")
            elif self.state == HELD:
                if self._moving or self.moved_since_fix:
                    events += self._set_state(LOST, "robot moved while the marker was hidden")
                elif self.last_fix_t is not None and t - self.last_fix_t > self.cfg.hold_s:
                    events += self._set_state(LOST, f"marker hidden for more than {self.cfg.hold_s:g}s")

        zone = None if self.state not in ZONE_KNOWN or self.zone is None else [(float(x), float(y)) for x, y in self.zone]
        return TrackerUpdate(self.state, zone, moved, events)

    # ---------------------------------------------------------------- helpers
    def _fix(self, t: float, used: list[int]) -> None:
        self.last_fix_t = t
        # A fix taken while the robot is still moving does not describe where it stops.
        if not self._moving:
            self.moved_since_fix = False
        self.used_markers = used
        self.stats["fixes"] += 1

    def _set_state(self, state: str, reason: str) -> list[dict]:
        if state == self.state:
            return []
        old = self.state
        self.state = state
        if state == LOST:
            self.stats["losses"] += 1
        return [{"from": old, "to": state, "reason": reason}]

    @staticmethod
    def _distance(a: np.ndarray | None, b: np.ndarray | None) -> float:
        """Mean vertex distance in normalised units, scaled to the frame diagonal (= sqrt(2) for 1x1)."""
        if a is None or b is None or a.shape != b.shape:
            return math.inf
        return float(np.mean(np.linalg.norm(a - b, axis=1))) / math.sqrt(2)

    def _consistent(self, a: np.ndarray | None, b: np.ndarray) -> bool:
        return a is not None and self._distance(a, b) <= self.cfg.jump_frac

    def _fuse(self, observations: Sequence[MarkerObservation]) -> tuple[np.ndarray | None, list[int]]:
        anchor = self.anchor
        if anchor is None:
            return None, []
        estimates: list[tuple[int, np.ndarray, float]] = []
        for obs in observations:
            ref = anchor.reference_side_px.get(obs.marker_id)
            if ref is None:
                continue
            ratio = obs.side / ref if ref > 0 else 0.0
            if not (self.cfg.size_ratio_min <= ratio <= self.cfg.size_ratio_max):
                self.stats["rejected_size"] += 1
                continue
            poly = anchor.project(obs)
            if poly is not None:
                estimates.append((obs.marker_id, poly, obs.side))
        if not estimates:
            return None, []
        if len(estimates) > 1:
            # Drop estimates that disagree with the consensus (closest to the median polygon).
            median = np.median(np.stack([e[1] for e in estimates]), axis=0)
            keep = [e for e in estimates if self._distance(e[1], median) <= self.cfg.disagree_frac]
            if len(keep) < len(estimates):
                self.stats["rejected_disagree"] += len(estimates) - len(keep)
            if not keep:
                # No consensus: prefer the marker closest to the current track, else the largest.
                ref_poly = self.zone if self.zone is not None else None
                if ref_poly is not None:
                    keep = [min(estimates, key=lambda e: self._distance(e[1], ref_poly))]
                else:
                    keep = [max(estimates, key=lambda e: e[2])]
            estimates = keep
        weights = np.array([e[2] for e in estimates], dtype=float)
        fused = np.tensordot(weights / weights.sum(), np.stack([e[1] for e in estimates]), axes=1)
        return fused, sorted(e[0] for e in estimates)

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "markers": self.used_markers,
            "last_fix_age_s": None
            if self.last_fix_t is None or self.last_t is None
            else round(self.last_t - self.last_fix_t, 2),
            "moved_since_fix": self.moved_since_fix,
            "anchored_markers": sorted(self.anchor.marker_points) if self.anchor else [],
            "stats": dict(self.stats),
        }


def marker_image(marker_id: int, dictionary: str = "DICT_4X4_50", px: int = 600, quiet_cells: int = 1) -> np.ndarray:
    """A printable marker with a white quiet zone and a caption."""
    import cv2

    aruco = cv2.aruco
    d = aruco.getPredefinedDictionary(getattr(aruco, dictionary))
    bits = {"DICT_4X4": 4, "DICT_5X5": 5, "DICT_6X6": 6}[dictionary[:8]] + 2
    cell = px // bits
    marker = aruco.generateImageMarker(d, marker_id, cell * bits)
    pad = cell * quiet_cells
    canvas = np.full((marker.shape[0] + 2 * pad + 60, marker.shape[1] + 2 * pad), 255, np.uint8)
    canvas[pad : pad + marker.shape[0], pad : pad + marker.shape[1]] = marker
    cv2.putText(canvas, f"LOCKED IN robot marker  {dictionary} id {marker_id}  (print the black square at 50-60 mm)", (pad, canvas.shape[0] - 22), cv2.FONT_HERSHEY_SIMPLEX, 0.45, 0, 1, cv2.LINE_AA)
    return canvas
