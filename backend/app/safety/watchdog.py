"""Host-side watchdogs.

These run inside the same asyncio loop as everything else, so they cannot protect
against the laptop itself freezing - that is what the device-side timeout is for
(docs/HARDWARE_PROTOCOL.md section 6). What they do catch:

* orphan motion: the transport reports motion that the safety controller did not
  approve or no longer tracks;
* overrun: an approved motion is still running past its deadline plus grace;
* loop stalls: if the event loop was blocked long enough that timing-based stops
  may have been late, any motion is stopped and the stall is reported.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from ..telemetry.events import EventBus, EventSource, EventType
from .controller import SafetyController

log = logging.getLogger(__name__)


class MotionWatchdog:
    def __init__(
        self,
        safety: SafetyController,
        bus: EventBus,
        *,
        period_s: float = 0.05,
        overrun_grace_s: float = 0.3,
        stall_threshold_s: float = 0.25,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.safety = safety
        self.bus = bus
        self.period_s = period_s
        self.overrun_grace_s = overrun_grace_s
        self.stall_threshold_s = stall_threshold_s
        self._clock = clock
        self._task: asyncio.Task | None = None
        self.fired = 0
        self.max_lag_s = 0.0
        self.last_lag_s = 0.0
        self._orphan_since: float | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="motion-watchdog")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        expected = self._clock() + self.period_s
        while True:
            await asyncio.sleep(max(0.0, expected - self._clock()))
            now = self._clock()
            lag = now - expected
            self.last_lag_s = lag
            self.max_lag_s = max(self.max_lag_s, lag)
            expected = now + self.period_s
            try:
                await self.check(lag)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("watchdog check failed")

    async def check(self, lag_s: float = 0.0) -> None:
        t = self.safety.transport
        active = self.safety.active
        now = self._clock()
        moving = bool(t and t.connected and t.is_moving)

        if lag_s > self.stall_threshold_s:
            self.bus.publish(EventType.SAFETY_LOOP_STALL, EventSource.SAFETY, {"lag_ms": round(lag_s * 1000, 1), "moving": moving})
            if moving or active is not None:
                await self._fire("event loop stalled during motion", {"lag_ms": round(lag_s * 1000, 1)})
                return

        if active is not None:
            deadline = active.started_mono + active.motion.duration_s + self.overrun_grace_s
            if now > deadline:
                await self._fire("motion overran its deadline", {"request_id": active.motion.request_id})
            self._orphan_since = None
            return

        if moving:
            # Give the transport a short window to report the stop it was already sent.
            if self._orphan_since is None:
                self._orphan_since = now
            elif now - self._orphan_since > self.overrun_grace_s:
                self._orphan_since = None
                await self._fire("transport reports motion the safety layer is not tracking", {})
        else:
            self._orphan_since = None

    async def _fire(self, why: str, info: dict) -> None:
        self.fired += 1
        self.bus.publish(EventType.SAFETY_WATCHDOG_FIRED, EventSource.SAFETY, {"why": why, **info})
        await self.safety.stop(reason=f"watchdog: {why}", source="watchdog")
