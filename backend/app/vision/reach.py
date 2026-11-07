"""Reach detection from hand landmarks: pure logic, no camera, fully unit-testable.

What it detects: a tracked hand moving toward and then dwelling inside the
calibrated phone zone. What it does not detect: intent. A hand that enters the
zone for any reason looks the same; the dwell, approach and cooldown rules only
make accidental triggers rarer.

Per track:
  approaching  hand within ``approach_margin`` of the zone and closing faster than
               ``approach_min_speed`` for 2+ frames (or already inside) -> opens an episode
  confirmed    reach point inside the zone for ``confirm_dwell_ms`` after an observed
               approach, or ``static_dwell_ms`` without one; then the hand must leave by
               ``exit_margin`` before it can confirm again
  suppressed   would have confirmed, but inside the global ``reach_cooldown_s``
  cleared      episode closed (hand moved away / disappeared) without confirming
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..config import VisionTuning
from .phone_zone import signed_distance

# wrist, thumb tip, index MCP, index tip, middle MCP, middle tip, ring tip, pinky tip
KEY_POINTS = (0, 4, 5, 8, 9, 12, 16, 20)
ASSOCIATION_GATE = 0.25
APPROACH_MEMORY_S = 1.5
CLOSE_HYSTERESIS = 1.5


@dataclass
class HandObs:
    landmarks: Sequence[tuple[float, float]]  # 21 normalised (x, y)
    score: float
    handedness: str = ""


@dataclass
class ReachEvent:
    kind: str
    track_id: int
    confidence: float
    data: dict = field(default_factory=dict)


@dataclass
class Track:
    id: int
    first_seen: float
    last_seen: float
    centroid: tuple[float, float]
    d: float
    score: float
    inside_fraction: float
    reach_point: tuple[float, float]
    speed: float = 0.0  # EMA of closing speed toward the zone (units/s, + = approaching)
    approach_frames: int = 0
    approach_seen_at: float | None = None
    inside_since: float | None = None
    episode: bool = False
    confirmed_in_episode: bool = False
    suppressed_in_episode: bool = False
    armed: bool = True
    rebased: bool = False  # zone moved: next distance change is not hand motion


class ReachDetector:
    def __init__(self, zone: Sequence[tuple[float, float]], tuning: VisionTuning | None = None):
        self.zone = list(zone)
        self.tuning = tuning or VisionTuning()
        self.tracks: dict[int, Track] = {}
        self._next_id = 1
        self.last_confirm_at: float | None = None
        self.last_t: float | None = None
        self.paused = False
        self.stats = {"confirmed": 0, "suppressed": 0, "cleared": 0, "approaches": 0, "rebases": 0}

    def configure(self, zone: Sequence[tuple[float, float]] | None = None, tuning: VisionTuning | None = None) -> None:
        if zone is not None:
            self.zone = list(zone)
        if tuning is not None:
            self.tuning = tuning

    # ------------------------------------------------------------------ api
    def rebase(self) -> None:
        """The zone moved because the robot moved. The next change in hand-to-zone distance
        is not hand motion, and any dwell measured against the old zone does not count."""
        self.stats["rebases"] += 1
        for tr in self.tracks.values():
            tr.rebased = True
            tr.speed = 0.0
            tr.approach_frames = 0
            tr.inside_since = None

    def set_paused(self, paused: bool, reason: str = "zone_unknown") -> list[ReachEvent]:
        """While the zone's position is unknown, keep tracking hands but never confirm."""
        if paused == self.paused:
            return []
        self.paused = paused
        out: list[ReachEvent] = []
        for tr in self.tracks.values():
            if paused:
                out.extend(self._close(tr, reason))
            tr.approach_frames = 0
            tr.inside_since = None
            tr.approach_seen_at = None
        return out

    def reset(self, reason: str = "reset") -> list[ReachEvent]:
        out = []
        for tr in self.tracks.values():
            out.extend(self._close(tr, reason))
            out.append(ReachEvent("hand_lost", tr.id, 0.0, {"reason": reason}))
        self.tracks.clear()
        return out

    def tick(self, t: float) -> list[ReachEvent]:
        """Call when no frame arrived: expires stale tracks."""
        return self._expire(t)

    def update(self, t: float, hands: Sequence[HandObs]) -> list[ReachEvent]:
        tun = self.tuning
        events = self._expire(t)
        self.last_t = t
        feats = [self._features(h) for h in hands if h.score >= tun.min_hand_confidence and len(h.landmarks) >= 21]

        # greedy nearest-centroid association
        pairs = sorted(
            ((math.dist(f["centroid"], tr.centroid), i, tid) for i, f in enumerate(feats) for tid, tr in self.tracks.items()),
            key=lambda p: p[0],
        )
        used_f, used_t, matches = set(), set(), {}
        for dist, i, tid in pairs:
            if dist > ASSOCIATION_GATE or i in used_f or tid in used_t:
                continue
            used_f.add(i)
            used_t.add(tid)
            matches[i] = tid

        for i, f in enumerate(feats):
            if i in matches:
                tr = self.tracks[matches[i]]
                dt = t - tr.last_seen
                if tr.rebased:
                    tr.rebased = False  # distance jumped because the zone moved, not the hand
                elif dt > 0:
                    raw = -(f["d"] - tr.d) / dt
                    raw = max(-5.0, min(5.0, raw))
                    tr.speed = 0.5 * tr.speed + 0.5 * raw
                tr.last_seen = t
                tr.centroid, tr.d, tr.score = f["centroid"], f["d"], f["score"]
                tr.inside_fraction, tr.reach_point = f["inside_fraction"], f["reach_point"]
            else:
                tr = Track(
                    id=self._next_id, first_seen=t, last_seen=t, centroid=f["centroid"], d=f["d"], score=f["score"],
                    inside_fraction=f["inside_fraction"], reach_point=f["reach_point"],
                )
                self._next_id += 1
                self.tracks[tr.id] = tr
                events.append(ReachEvent("hand_detected", tr.id, tr.score, {"distance": round(tr.d, 3)}))
            events.extend(self._step(tr, t))
        return events

    # ------------------------------------------------------------------ internals
    def _features(self, h: HandObs) -> dict:
        lms = h.landmarks
        best_d, best_pt = math.inf, lms[0]
        for k in KEY_POINTS:
            x, y = lms[k][0], lms[k][1]
            d = signed_distance(x, y, self.zone)
            if d < best_d:
                best_d, best_pt = d, (x, y)
        inside = sum(1 for p in lms if signed_distance(p[0], p[1], self.zone) <= 0)
        cx = sum(p[0] for p in lms) / len(lms)
        cy = sum(p[1] for p in lms) / len(lms)
        return {
            "d": best_d,
            "reach_point": (best_pt[0], best_pt[1]),
            "inside_fraction": inside / len(lms),
            "centroid": (cx, cy),
            "score": float(h.score),
        }

    def _expire(self, t: float) -> list[ReachEvent]:
        out = []
        timeout = self.tuning.track_timeout_ms / 1000.0
        for tid in [tid for tid, tr in self.tracks.items() if t - tr.last_seen > timeout]:
            tr = self.tracks.pop(tid)
            out.extend(self._close(tr, "hand_lost"))
            out.append(ReachEvent("hand_lost", tr.id, 0.0, {"seen_s": round(tr.last_seen - tr.first_seen, 2)}))
        return out

    def _close(self, tr: Track, reason: str) -> list[ReachEvent]:
        if not tr.episode:
            return []
        tr.episode = False
        tr.approach_seen_at = None
        if tr.confirmed_in_episode:
            return []
        self.stats["cleared"] += 1
        why = "suppressed_by_cooldown" if tr.suppressed_in_episode else ("left_zone_unconfirmed" if reason == "moved_away" else reason)
        return [ReachEvent("cleared", tr.id, tr.score, {"reason": why})]

    def _step(self, tr: Track, t: float) -> list[ReachEvent]:
        tun = self.tuning
        out: list[ReachEvent] = []
        if self.paused:
            tr.approach_frames = 0
            tr.inside_since = None
            return out
        near = tr.d < tun.approach_margin
        approaching_now = near and tr.speed >= tun.approach_min_speed
        tr.approach_frames = tr.approach_frames + 1 if approaching_now else 0
        if approaching_now:
            tr.approach_seen_at = t
        inside = tr.d <= 0
        if inside:
            if tr.inside_since is None:
                tr.inside_since = t
        else:
            tr.inside_since = None

        if not tr.armed and tr.d > tun.exit_margin:
            tr.armed = True

        if not tr.episode and (tr.approach_frames >= 2 or inside):
            tr.episode = True
            tr.confirmed_in_episode = False
            tr.suppressed_in_episode = False
            self.stats["approaches"] += 1
            out.append(
                ReachEvent(
                    "approaching",
                    tr.id,
                    round(tr.score, 3),
                    {"distance": round(tr.d, 3), "speed": round(tr.speed, 3), "inside": inside, "track": tr.id},
                )
            )

        if tr.episode and inside and tr.armed and not tr.confirmed_in_episode and tr.inside_since is not None:
            dwell_ms = (t - tr.inside_since) * 1000
            approach_recent = tr.approach_seen_at is not None and tr.inside_since - tr.approach_seen_at <= APPROACH_MEMORY_S
            needed = tun.confirm_dwell_ms if approach_recent else tun.static_dwell_ms
            if dwell_ms >= needed:
                conf = self._confidence(tr, approach_recent, dwell_ms, needed)
                data = {
                    "dwell_ms": round(dwell_ms),
                    "approach_seen": approach_recent,
                    "inside_fraction": round(tr.inside_fraction, 3),
                    "speed": round(tr.speed, 3),
                    "track": tr.id,
                    "reach_point": [round(tr.reach_point[0], 3), round(tr.reach_point[1], 3)],
                }
                if self.last_confirm_at is not None and t - self.last_confirm_at < tun.reach_cooldown_s:
                    if not tr.suppressed_in_episode:
                        tr.suppressed_in_episode = True
                        self.stats["suppressed"] += 1
                        out.append(
                            ReachEvent("suppressed", tr.id, conf, {**data, "cooldown_left_s": round(tun.reach_cooldown_s - (t - self.last_confirm_at), 2)})
                        )
                else:
                    self.last_confirm_at = t
                    tr.confirmed_in_episode = True
                    tr.armed = False
                    self.stats["confirmed"] += 1
                    out.append(ReachEvent("confirmed", tr.id, conf, data))

        if tr.episode and tr.d > tun.approach_margin * CLOSE_HYSTERESIS:
            out.extend(self._close(tr, "moved_away"))
        return out

    @staticmethod
    def _confidence(tr: Track, approach_recent: bool, dwell_ms: float, needed: float) -> float:
        inside_term = min(1.0, tr.inside_fraction / 0.3)
        approach_term = 1.0 if approach_recent else 0.8
        dwell_term = min(1.0, dwell_ms / max(1.0, needed))
        conf = tr.score * (0.55 + 0.45 * inside_term) * approach_term * (0.9 + 0.1 * dwell_term)
        return round(max(0.0, min(1.0, conf)), 3)

    def snapshot(self) -> dict:
        return {
            "tracks": [
                {
                    "id": tr.id,
                    "distance": round(tr.d, 3),
                    "speed": round(tr.speed, 3),
                    "inside": tr.d <= 0,
                    "episode": tr.episode,
                    "armed": tr.armed,
                    "score": round(tr.score, 3),
                    "reach_point": [round(tr.reach_point[0], 3), round(tr.reach_point[1], 3)],
                }
                for tr in self.tracks.values()
            ],
            "stats": dict(self.stats),
            "paused": self.paused,
            "cooldown_left_s": None
            if self.last_confirm_at is None or self.last_t is None
            else round(max(0.0, self.tuning.reach_cooldown_s - (self.last_t - self.last_confirm_at)), 2),
        }
