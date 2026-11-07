"""Observable presence / attention proxy from face detection.

This is a proxy, not attention measurement. It only distinguishes:
  FOCUSED              face visible, roughly facing the camera
  POSSIBLY_DISTRACTED  face visible but turned away for a sustained period
  AWAY                 no face for a sustained period
  UNKNOWN              camera off, monitoring paused, or not enough evidence
Looking down at paper (pitch) is deliberately not penalised: only sideways head
turn (yaw) counts, and only after ``distracted_after_s``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..sessions.metrics import FocusState


@dataclass
class FaceObs:
    present: bool
    score: float = 0.0
    yaw_ratio: float | None = None
    box: tuple[float, float, float, float] | None = None  # x, y, w, h normalised


def yaw_ratio_from_keypoints(kps: list[tuple[float, float]]) -> float | None:
    """MediaPipe BlazeFace keypoints: 0 right eye, 1 left eye, 2 nose tip, ...
    Returns nose offset from the eye midpoint in units of inter-eye distance (~0 frontal)."""
    if len(kps) < 3:
        return None
    (rx, _), (lx, _), (nx, _) = kps[0], kps[1], kps[2]
    span = abs(rx - lx)
    if span < 1e-3:
        return None
    return (nx - (rx + lx) / 2.0) / span


class FocusEstimator:
    def __init__(
        self,
        away_after_s: float = 4.0,
        distracted_after_s: float = 8.0,
        focus_after_s: float = 1.0,
        yaw_threshold: float = 0.35,
        tau_s: float = 0.8,
    ):
        self.away_after_s = away_after_s
        self.distracted_after_s = distracted_after_s
        self.focus_after_s = focus_after_s
        self.yaw_threshold = yaw_threshold
        self.tau_s = tau_s
        self.state = FocusState.UNKNOWN
        self.p = 0.5
        self._last_t: float | None = None
        self._absent_since: float | None = None
        self._turned_since: float | None = None
        self._facing_since: float | None = None
        self.last_yaw: float | None = None

    def reset(self) -> None:
        self.__init__(self.away_after_s, self.distracted_after_s, self.focus_after_s, self.yaw_threshold, self.tau_s)

    def update(
        self, t: float, obs: FaceObs | None, camera_ok: bool = True, unknown_reason: str = "camera unavailable"
    ) -> tuple[FocusState, float, str] | None:
        """Returns (new_state, confidence, reason) when the state changes, else None."""
        if not camera_ok or obs is None:
            return self._set(FocusState.UNKNOWN, 1.0, unknown_reason)
        dt = 0.0 if self._last_t is None else max(0.0, t - self._last_t)
        self._last_t = t
        alpha = 1.0 - math.exp(-dt / self.tau_s) if dt > 0 else 0.3
        self.p += alpha * ((1.0 if obs.present else 0.0) - self.p)
        self.last_yaw = obs.yaw_ratio if obs.present else None
        turned = obs.present and obs.yaw_ratio is not None and abs(obs.yaw_ratio) > self.yaw_threshold

        if self.p < 0.3:
            self._absent_since = self._absent_since if self._absent_since is not None else t
        else:
            self._absent_since = None
        if self.p > 0.6 and turned:
            self._turned_since = self._turned_since if self._turned_since is not None else t
        else:
            self._turned_since = None
        if self.p > 0.6 and not turned:
            self._facing_since = self._facing_since if self._facing_since is not None else t
        else:
            self._facing_since = None

        if self._absent_since is not None and t - self._absent_since >= self.away_after_s:
            return self._set(FocusState.AWAY, round(1 - self.p, 3), f"no face for {t - self._absent_since:.0f}s")
        if self._turned_since is not None and t - self._turned_since >= self.distracted_after_s:
            return self._set(FocusState.POSSIBLY_DISTRACTED, round(self.p, 3), f"head turned away for {t - self._turned_since:.0f}s")
        if self._facing_since is not None and t - self._facing_since >= self.focus_after_s:
            return self._set(FocusState.FOCUSED, round(self.p, 3), "face visible and facing the screen")
        return None

    def _set(self, state: FocusState, conf: float, reason: str):
        if state == self.state:
            return None
        self.state = state
        return state, conf, reason
