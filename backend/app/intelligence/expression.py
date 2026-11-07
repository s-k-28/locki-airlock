"""Robot expression: a light state for each moment (intro, warning, celebration, break...).

Shown on the dashboard. Hardware is only driven where a verified interface exists, and on
the Zeus Car none does: the stock sketch's RGB strip only shows its own mode colours (set in
``modeHandler()``) and no WebSocket region controls it, so LOCKED IN sends no LED frames
(the transports report ``led_control: none``; the simulator reports ``simulated``).
Celebratory motion is deliberately absent until the hardware tests pass.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass

from ..telemetry.events import EventBus, EventSource, EventType


@dataclass(frozen=True)
class Expression:
    name: str
    label: str
    color: str  # dashboard token: live / hit / sim / danger / calm / party / dim
    pattern: str  # sweep / pulse / flash / sparkle / breathe / solid
    duration_s: float


EXPRESSIONS: dict[str, Expression] = {
    "intro": Expression("intro", "Locking in", "live", "sweep", 2.0),
    "warning": Expression("warning", "Reach detected", "hit", "pulse", 2.5),
    "progress": Expression("progress", "Nice", "live", "flash", 1.5),
    "celebration": Expression("celebration", "Celebrating", "party", "sparkle", 4.0),
    "break": Expression("break", "On a break", "calm", "breathe", 0.0),
    "released": Expression("released", "Phone released", "dim", "solid", 3.0),
    "stopped": Expression("stopped", "Stopped", "danger", "solid", 3.0),
    "estop": Expression("estop", "EMERGENCY STOP", "danger", "solid", 0.0),
}

CATEGORY_EXPRESSION = {
    "session_start": "intro",
    "resume": "intro",
    "reach_early": "warning",
    "reach_first": "warning",
    "reach_repeat": "warning",
    "reach_repeat_many": "warning",
    "reach_holding": "warning",
    "progress_ack": "progress",
    "target_reached": "celebration",
    "end_completed": "celebration",
    "pause": "break",
    "release": "released",
    "stop_ack": "stopped",
    "estop_ack": "estop",
}


class ExpressionController:
    def __init__(self, bus: EventBus, led_control: Callable[[], str] = lambda: "none", clock: Callable[[], float] = time.time):
        self.bus = bus
        self._led_control = led_control
        self._clock = clock
        self.current: dict | None = None

    def show(self, name: str, simulated: bool = False) -> dict | None:
        expr = EXPRESSIONS.get(name)
        if expr is None:
            return None
        led = self._led_control()
        now = self._clock()
        self.current = {
            **asdict(expr),
            "at": now,
            "until": None if expr.duration_s == 0 else now + expr.duration_s,
            "hardware": "simulated" if led == "simulated" else "dashboard only",
        }
        self.bus.publish(EventType.ROBOT_EXPRESSION, EventSource.ROBOT, dict(self.current), simulated=simulated)
        return self.current

    def for_category(self, category: str, simulated: bool = False) -> dict | None:
        name = CATEGORY_EXPRESSION.get(category)
        return self.show(name, simulated) if name else None

    def clear(self) -> None:
        self.current = None

    def snapshot(self) -> dict | None:
        c = self.current
        if c is None:
            return None
        if c["until"] is not None and self._clock() > c["until"]:
            return None
        return c
