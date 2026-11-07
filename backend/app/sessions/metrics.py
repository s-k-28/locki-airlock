"""Session metrics built only from observed signals and user reports.

Presence time is integrated from focus-state changes over *active* (unpaused)
session time. When the camera is off the time is counted as UNKNOWN - it is
never guessed.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum


class FocusState(StrEnum):
    FOCUSED = "FOCUSED"
    POSSIBLY_DISTRACTED = "POSSIBLY_DISTRACTED"
    AWAY = "AWAY"
    UNKNOWN = "UNKNOWN"


@dataclass
class SessionCounters:
    reach_suspected: int = 0
    reach_confirmed: int = 0
    reach_suppressed: int = 0
    reach_while_holding: int = 0
    interventions: int = 0
    voice_commands: int = 0
    robot_moves: int = 0
    motions_blocked: int = 0
    pauses: int = 0
    false_trigger_labels: int = 0


@dataclass
class FocusAccumulator:
    """Time-weighted focus-state durations. Only accumulates while ``counting``."""

    clock: Callable[[], float] = time.monotonic
    state: FocusState = FocusState.UNKNOWN
    counting: bool = False
    durations: dict[str, float] = field(default_factory=lambda: {s.value: 0.0 for s in FocusState})
    transitions: list[tuple[float, str]] = field(default_factory=list)
    _since: float | None = None

    def reset(self) -> None:
        self.durations = {s.value: 0.0 for s in FocusState}
        self.transitions = []
        self._since = self.clock() if self.counting else None

    def _bank(self) -> None:
        now = self.clock()
        if self.counting and self._since is not None:
            self.durations[self.state.value] += max(0.0, now - self._since)
        self._since = now

    def set_state(self, state: FocusState) -> None:
        if state == self.state:
            return
        self._bank()
        self.state = state
        if self.counting:
            self.transitions.append((round(self.clock(), 3), state.value))

    def set_counting(self, counting: bool) -> None:
        if counting == self.counting:
            return
        self._bank()
        self.counting = counting

    def snapshot(self) -> dict[str, float]:
        self._bank()
        return {k: round(v, 2) for k, v in self.durations.items()}


def compute_metrics(
    *,
    elapsed_active_s: float,
    remaining_s: float,
    planned_s: float,
    completed_units: int,
    target_units: int | None,
    focus: dict[str, float],
    counters: SessionCounters,
    vision_available: bool,
) -> dict:
    presence = focus["FOCUSED"] + focus["POSSIBLY_DISTRACTED"]
    away = focus["AWAY"]
    unknown = focus["UNKNOWN"]
    known = presence + away
    observed_fraction = known / elapsed_active_s if elapsed_active_s > 0 else 0.0
    return {
        "elapsed_active_s": round(elapsed_active_s, 1),
        "remaining_s": round(remaining_s, 1),
        "planned_s": round(planned_s, 1),
        "progress_fraction": round(min(1.0, elapsed_active_s / planned_s), 4) if planned_s else 0.0,
        "completed_units": completed_units,
        "target_units": target_units,
        "presence_s": round(presence, 1),
        "focused_s": round(focus["FOCUSED"], 1),
        "possibly_distracted_s": round(focus["POSSIBLY_DISTRACTED"], 1),
        "away_s": round(away, 1),
        "unknown_s": round(unknown, 1),
        # Fraction of active time the camera actually observed. Below ~0.5 the presence
        # numbers are reported as low-confidence in the UI.
        "observed_fraction": round(min(1.0, observed_fraction), 3),
        # Presence ratio is only defined over observed time; None means "unavailable".
        "presence_ratio": round(presence / known, 3) if known >= 5.0 else None,
        "vision_available": vision_available,
        "reach_suspected": counters.reach_suspected,
        "reach_confirmed": counters.reach_confirmed,
        "reach_suppressed": counters.reach_suppressed,
        "reach_while_holding": counters.reach_while_holding,
        "interventions": counters.interventions,
        "voice_commands": counters.voice_commands,
        "robot_moves": counters.robot_moves,
        "motions_blocked": counters.motions_blocked,
        "pauses": counters.pauses,
        "false_trigger_labels": counters.false_trigger_labels,
    }
