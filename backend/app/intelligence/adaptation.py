"""History-driven adaptation, kept deliberately simple and visible.

Each rule watches logged history and nudges one parameter within hard bounds.
Every change is published as ``coach.adaptation`` with the rule, old value, new
value and reason. There is no learned model here; with a few sessions of logged
data a contextual bandit could replace the line-selection part, but a visible
baseline is more useful than unvalidated "learning".
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass

from ..telemetry.events import EventBus, EventSource, EventType

REPEAT_WINDOW_S = 600.0
REPEAT_THRESHOLD = 3
UNCERTAIN_WINDOW_S = 120.0
UNCERTAIN_THRESHOLD = 4
FALSE_TRIGGER_SUSPEND = 2
CALM_RESET_S = 900.0


@dataclass
class AdaptiveParams:
    cooldown_multiplier: float = 1.0
    escalation: int = 0
    confirm_dwell_bonus_ms: int = 0
    min_confidence_bonus: float = 0.0
    autonomy_suspended: bool = False


class AdaptationEngine:
    def __init__(self, bus: EventBus, clock: Callable[[], float] = time.monotonic):
        self.bus = bus
        self._clock = clock
        self.params = AdaptiveParams()
        self._reaches: deque[float] = deque()
        self._uncertain: deque[float] = deque()
        self._false_triggers = 0
        self._last_reach_at: float | None = None
        self.on_change: Callable[[AdaptiveParams], None] = lambda p: None

    def reset_for_session(self) -> None:
        """Vision strictness carries over (it reflects the environment); behaviour resets."""
        self._reaches.clear()
        self._uncertain.clear()
        self._false_triggers = 0
        self._last_reach_at = None
        self._set("cooldown_multiplier", 1.0, "A0", "new session")
        self._set("escalation", 0, "A0", "new session")
        self._set("autonomy_suspended", False, "A0", "new session")

    # ------------------------------------------------------------- inputs
    def record_confirmed_reach(self) -> None:
        now = self._clock()
        self._last_reach_at = now
        self._reaches.append(now)
        while self._reaches and now - self._reaches[0] > REPEAT_WINDOW_S:
            self._reaches.popleft()
        if len(self._reaches) >= REPEAT_THRESHOLD:
            self._reaches.clear()
            self._set("escalation", min(2, self.params.escalation + 1), "A1", f"{REPEAT_THRESHOLD} reaches in 10 min")
            self._set(
                "cooldown_multiplier",
                round(max(0.5, self.params.cooldown_multiplier * 0.75), 3),
                "A1",
                "repeated reaching: react more readily",
            )

    def record_uncertain(self) -> None:
        now = self._clock()
        self._uncertain.append(now)
        while self._uncertain and now - self._uncertain[0] > UNCERTAIN_WINDOW_S:
            self._uncertain.popleft()
        if len(self._uncertain) >= UNCERTAIN_THRESHOLD:
            self._uncertain.clear()
            self._raise_strictness("A2", f"{UNCERTAIN_THRESHOLD} unconfirmed approaches in 2 min")

    def record_false_trigger(self) -> None:
        self._false_triggers += 1
        self._raise_strictness("A3", "operator labelled a false trigger")
        if self._false_triggers >= FALSE_TRIGGER_SUSPEND:
            self._set("autonomy_suspended", True, "A3", f"{self._false_triggers} false triggers this session")

    def record_user_request(self, kind: str) -> None:
        if kind == "harder":
            self._set("escalation", min(2, self.params.escalation + 1), "U1", "user asked to make it harder")
        elif kind == "easier":
            self._set("escalation", 0, "U2", "user asked to tone it down")

    def resume_autonomy(self) -> None:
        self._set("autonomy_suspended", False, "U3", "operator re-enabled autonomous retreat")

    def tick(self, focused_now: bool) -> None:
        """Calm decay: a long reach-free focused stretch relaxes escalation."""
        if self._last_reach_at is None or not focused_now:
            return
        if self._clock() - self._last_reach_at >= CALM_RESET_S:
            self._last_reach_at = self._clock()
            if self.params.escalation > 0:
                self._set("escalation", self.params.escalation - 1, "A4", "15 min without a reach")
            if self.params.cooldown_multiplier < 1.0:
                self._set("cooldown_multiplier", min(1.0, round(self.params.cooldown_multiplier / 0.75, 3)), "A4", "15 min without a reach")

    # ------------------------------------------------------------ helpers
    def _raise_strictness(self, rule: str, reason: str) -> None:
        self._set("confirm_dwell_bonus_ms", min(480, self.params.confirm_dwell_bonus_ms + 120), rule, reason)
        self._set("min_confidence_bonus", round(min(0.2, self.params.min_confidence_bonus + 0.05), 3), rule, reason)

    def _set(self, name: str, value, rule: str, reason: str) -> None:
        old = getattr(self.params, name)
        if old == value:
            return
        setattr(self.params, name, value)
        self.bus.publish(
            EventType.COACH_ADAPTATION,
            EventSource.POLICY,
            {"rule": rule, "param": name, "old": old, "new": value, "reason": reason},
        )
        self.on_change(self.params)

    def snapshot(self) -> dict:
        return {**asdict(self.params), "false_triggers": self._false_triggers}
