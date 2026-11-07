"""Focus-session timing and user-reported progress.

Uses a monotonic clock for durations (immune to wall-clock jumps) and records
wall-clock timestamps only for display. Pauses are excluded from active time.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field


class SessionError(RuntimeError):
    pass


@dataclass
class Session:
    session_id: str
    task: str
    planned_duration_s: float
    target_units: int | None
    unit_label: str
    started_at_wall: float
    started_at_mono: float
    completed_units: int = 0
    paused_at_mono: float | None = None
    paused_total_s: float = 0.0
    pause_count: int = 0
    ended_at_wall: float | None = None
    ended_at_mono: float | None = None
    end_reason: str | None = None
    progress_log: list[dict] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.ended_at_mono is None

    @property
    def paused(self) -> bool:
        return self.paused_at_mono is not None


class SessionManager:
    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ):
        self._clock = clock
        self._wall = wall_clock
        self.current: Session | None = None
        self.last: Session | None = None

    # ------------------------------------------------------------ lifecycle
    def start(
        self,
        task: str,
        duration_s: float,
        target_units: int | None = None,
        unit_label: str = "tasks",
    ) -> Session:
        if self.current is not None and self.current.active:
            raise SessionError("a session is already active")
        if not (60 <= duration_s <= 4 * 3600):
            raise SessionError("session duration must be between 1 minute and 4 hours")
        if target_units is not None and not (1 <= target_units <= 500):
            raise SessionError("target must be between 1 and 500")
        self.current = Session(
            session_id=f"s-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
            task=(task or "Focus session").strip()[:120],
            planned_duration_s=float(duration_s),
            target_units=target_units,
            unit_label=(unit_label or "tasks").strip()[:24],
            started_at_wall=self._wall(),
            started_at_mono=self._clock(),
        )
        return self.current

    def pause(self) -> Session:
        s = self._require_active()
        if s.paused:
            raise SessionError("session is already paused")
        s.paused_at_mono = self._clock()
        s.pause_count += 1
        return s

    def resume(self) -> Session:
        s = self._require_active()
        if not s.paused:
            raise SessionError("session is not paused")
        s.paused_total_s += self._clock() - s.paused_at_mono
        s.paused_at_mono = None
        return s

    def end(self, reason: str) -> Session:
        s = self._require_active()
        now = self._clock()
        if s.paused:
            s.paused_total_s += now - s.paused_at_mono
            s.paused_at_mono = None
        s.ended_at_mono = now
        s.ended_at_wall = self._wall()
        s.end_reason = reason
        self.last = s
        self.current = None
        return s

    def add_progress(self, units: int = 1, note: str | None = None) -> Session:
        s = self._require_active()
        if not (-50 <= units <= 50) or units == 0:
            raise SessionError("progress must be a non-zero change of at most 50 units")
        s.completed_units = max(0, s.completed_units + units)
        s.progress_log.append(
            {"at_active_s": round(self.active_seconds(s), 1), "units": units, "total": s.completed_units, "note": note}
        )
        return s

    def extend(self, seconds: float) -> Session:
        """More time on the running session (a spoken "give me ten more minutes")."""
        s = self._require_active()
        if not (60 <= seconds <= 3600):
            raise SessionError("an extension must be between 1 and 60 minutes")
        if s.planned_duration_s + seconds > 4 * 3600:
            raise SessionError("a session cannot be longer than 4 hours")
        s.planned_duration_s += float(seconds)
        return s

    def set_task(self, task: str) -> Session:
        s = self._require_active()
        task = (task or "").strip()[:120]
        if not task:
            raise SessionError("empty task")
        s.task = task
        return s

    def adopt_unit_label(self, label: str) -> bool:
        """Use the unit the user speaks ("two problems") if the session never set one."""
        s = self._require_active()
        if s.target_units is None and s.unit_label == "tasks" and label and label != "tasks":
            s.unit_label = label.strip()[:24]
            return True
        return False

    # -------------------------------------------------------------- queries
    def active_seconds(self, s: Session | None = None) -> float:
        s = s or self.current
        if s is None:
            return 0.0
        end = s.ended_at_mono if s.ended_at_mono is not None else self._clock()
        paused = s.paused_total_s
        if s.paused_at_mono is not None:
            paused += end - s.paused_at_mono
        return max(0.0, end - s.started_at_mono - paused)

    def wall_seconds(self, s: Session | None = None) -> float:
        s = s or self.current
        if s is None:
            return 0.0
        end = s.ended_at_mono if s.ended_at_mono is not None else self._clock()
        return max(0.0, end - s.started_at_mono)

    def remaining_seconds(self) -> float:
        s = self.current
        if s is None:
            return 0.0
        return max(0.0, s.planned_duration_s - self.active_seconds(s))

    def is_due(self) -> bool:
        return self.current is not None and not self.current.paused and self.remaining_seconds() <= 0.0

    @property
    def active(self) -> bool:
        return self.current is not None and self.current.active

    @property
    def paused(self) -> bool:
        return self.current is not None and self.current.paused

    def snapshot(self) -> dict | None:
        s = self.current
        if s is None:
            return None
        d = asdict(s)
        d.pop("progress_log", None)
        d.update(
            active=True,
            paused=s.paused,
            elapsed_active_s=round(self.active_seconds(s), 3),
            remaining_s=round(self.remaining_seconds(), 3),
            progress_log=s.progress_log[-20:],
        )
        return d

    def _require_active(self) -> Session:
        if self.current is None or not self.current.active:
            raise SessionError("no active session")
        return self.current
