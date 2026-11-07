"""Simulated robot. Everything it reports is labelled ``simulated``.

Models the parts of the real system that matter for testing the control path:
command latency, a device-side motion watchdog, link loss, a forward range sensor
against configurable obstacles, and planar mecanum kinematics so the dashboard can
show where the simulated robot went.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from .base import RobotStatus, RobotTelemetry, RobotTransport, SendReceipt, TransportCapabilities
from .commands import MotionPrimitive

log = logging.getLogger(__name__)


@dataclass
class Box:
    """Axis-aligned obstacle in world metres."""

    x0: float
    y0: float
    x1: float
    y1: float


@dataclass
class _Motion:
    primitive: MotionPrimitive
    speed: int
    started: float
    deadline: float


class MockRobotTransport(RobotTransport):
    name = "mock"

    def __init__(
        self,
        *,
        latency_s: float = 0.03,
        speed_mps_at_100: float = 0.35,
        rot_dps_at_100: float = 180.0,
        device_watchdog_s: float | None = 0.5,  # None models stock firmware (no device-side stop)
        host_link_timeout_s: float = 1.5,
        obstacles: list[Box] | None = None,
        tick_s: float = 0.02,
        clock: Callable[[], float] = time.monotonic,
    ):
        super().__init__()
        self.latency_s = latency_s
        self.speed_mps_at_100 = speed_mps_at_100
        self.rot_dps_at_100 = rot_dps_at_100
        self.device_watchdog_s = device_watchdog_s
        self.host_link_timeout_s = host_link_timeout_s
        self.obstacles = obstacles or []
        self.tick_s = tick_s
        self._clock = clock
        self._connected = False
        self._link_state = "disconnected"
        self._connected_since: float | None = None
        self._pose = {"x": 0.0, "y": 0.0, "theta": 0.0}
        self._motion: _Motion | None = None
        self._loop_task: asyncio.Task | None = None
        self._link_dead_until: float = 0.0
        self._tx_frozen_until: float = 0.0
        self._deferred: list[tuple[str, dict]] = []  # what the host only learns once it transmits again
        self._last_frame_at: float | None = None
        self._last_rx: float | None = None
        self._last_tx: float | None = None
        self._frames_sent = 0
        self._frames_received = 0
        self._last_error: str | None = None
        self._last_telemetry = RobotTelemetry(pose=dict(self._pose))
        self.travel_log: list[dict] = []

    # ------------------------------------------------------------- lifecycle
    async def connect(self) -> None:
        if self._connected:
            return
        self._link_state = "connecting"
        await asyncio.sleep(self.latency_s)
        self._connected = True
        self._link_state = "connected"
        self._connected_since = time.time()
        self._last_rx = self._clock()
        self._link_dead_until = 0.0
        self._loop_task = asyncio.create_task(self._physics_loop(), name="mock-robot-physics")
        self.callbacks.on_connected({"transport": "mock", "simulated": True, "firmware": {"name": "simulator"}})

    async def disconnect(self) -> None:
        if not self._connected:
            return
        self._halt("disconnect")
        self._connected = False
        self._link_state = "disconnected"
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        self.callbacks.on_disconnected("operator disconnect")

    # --------------------------------------------------------------- motion
    async def move(self, primitive: MotionPrimitive, speed: int, max_duration_s: float) -> SendReceipt:
        now = self._clock()
        if not self._connected:
            return SendReceipt(False, now, "not connected")
        if primitive == MotionPrimitive.STOP:
            return await self.stop("move(stop)")
        if self.tx_frozen:
            return SendReceipt(False, now, "transmit paused by the link test")
        self._frames_sent += 1
        self._last_tx = now
        if self._link_dead(now):
            # The frame leaves the laptop but never arrives.
            return SendReceipt(True, now, "sent (simulated link is down; frame lost)")
        asyncio.get_running_loop().call_later(self.latency_s, self._arrive_move, primitive, speed, max_duration_s, now)
        return SendReceipt(True, now, "sent to simulator")

    def _arrive_move(self, primitive: MotionPrimitive, speed: int, max_duration_s: float, sent_at: float) -> None:
        if not self._connected:
            return
        now = self._clock()
        self._last_frame_at = now
        self._frames_received += 1
        self._last_rx = now
        self._motion = _Motion(primitive, int(speed), now, now + max_duration_s)
        self.callbacks.on_ack(
            {"ack": "simulated", "primitive": primitive.value, "latency_ms": round((now - sent_at) * 1000, 1)}
        )

    async def stop(self, reason: str = "") -> SendReceipt:
        now = self._clock()
        if not self._connected:
            return SendReceipt(False, now, "not connected")
        self._resume_tx()
        self._frames_sent += 1
        self._last_tx = now
        if self._link_dead(now):
            return SendReceipt(True, now, "sent (simulated link is down; frame lost)")
        asyncio.get_running_loop().call_later(self.latency_s, self._arrive_stop, reason)
        return SendReceipt(True, now, "stop sent to simulator")

    def _arrive_stop(self, reason: str) -> None:
        self._last_frame_at = self._clock()
        self._frames_received += 1
        self._last_rx = self._last_frame_at
        self._halt(reason or "stop")

    def _halt(self, why: str) -> None:
        if self._motion is not None:
            m = self._motion
            self._motion = None
            now = self._clock()
            ran = now - m.started
            self.travel_log.append({"primitive": m.primitive.value, "speed": m.speed, "ran_s": round(ran, 3), "why": why, "stopped_at": now})
            info = {"primitive": m.primitive.value, "ran_s": round(ran, 3)}
            if why == "device_watchdog" and self.tx_frozen:
                # A real robot only answers frames, so during a link test the host hears about
                # this when it transmits again.
                self._deferred.append(("ended", info))
                return
            self.callbacks.on_motion_ended(why, info)
            if why == "device_watchdog":
                self.callbacks.on_device_watchdog({"during_motion": True, "simulated": True})

    # ------------------------------------------------------------ test hooks
    def simulate_link_loss(self, duration_s: float) -> None:
        """Frames stop arriving for ``duration_s``. The simulated device watchdog stops
        the wheels after ``device_watchdog_s``; the host declares the link lost after
        ``host_link_timeout_s`` without receiving anything."""
        self._link_dead_until = self._clock() + duration_s

    def _link_dead(self, now: float) -> bool:
        return now < self._link_dead_until

    def freeze_tx(self, duration_s: float) -> bool:
        """Link test: the host stops sending. The real host refreshes motion at 10 Hz, so the
        last frame arrived just now; the simulated device watchdog (if any) counts from here."""
        if not self._connected:
            return False
        now = self._clock()
        self._tx_frozen_until = now + max(0.0, min(6.0, float(duration_s)))
        self._last_frame_at = now
        return True

    @property
    def tx_frozen(self) -> bool:
        return self._tx_frozen_until > 0.0

    def _resume_tx(self) -> None:
        self._tx_frozen_until = 0.0
        deferred, self._deferred = self._deferred, []
        for _kind, info in deferred:
            self.callbacks.on_motion_ended("device_watchdog", info)
            self.callbacks.on_device_watchdog({"during_motion": False, "during_link_test": True, "simulated": True})

    def reset_pose(self) -> None:
        self._pose = {"x": 0.0, "y": 0.0, "theta": 0.0}

    # --------------------------------------------------------------- physics
    async def _physics_loop(self) -> None:
        last = self._clock()
        next_telemetry = last
        try:
            while self._connected:
                await asyncio.sleep(self.tick_s)
                now = self._clock()
                dt = now - last
                last = now
                link_dead = self._link_dead(now)
                if not link_dead:
                    self._last_rx = now  # the simulator "answers" while the link is up
                if self._tx_frozen_until and now >= self._tx_frozen_until:
                    self._resume_tx()  # cap elapsed without stop(): end, never resume
                    self._halt("freeze_timeout")
                silent = link_dead or self.tx_frozen
                m = self._motion
                if m is not None:
                    if now >= m.deadline:
                        self._halt("deadline")
                    elif (
                        silent
                        and self.device_watchdog_s is not None
                        and self._last_frame_at is not None
                        and now - self._last_frame_at > self.device_watchdog_s
                    ):
                        self._halt("device_watchdog")
                    else:
                        self._integrate(m, dt)
                if self._last_rx is not None and now - self._last_rx > self.host_link_timeout_s:
                    self._link_state = "error"
                    self._halt("link_lost")
                    self._connected = False
                    self._last_error = "simulated link lost"
                    self.callbacks.on_disconnected("link lost (no data from robot)")
                    return
                if now >= next_telemetry:
                    next_telemetry = now + 0.1
                    self._last_telemetry = RobotTelemetry(
                        ultrasonic_cm=self._range_cm(), pose={k: round(v, 4) for k, v in self._pose.items()}
                    )
                    self.callbacks.on_telemetry(self._last_telemetry)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("mock physics loop crashed")
            self._connected = False
            self.callbacks.on_disconnected("simulator crashed")

    def _integrate(self, m: _Motion, dt: float) -> None:
        v = m.speed / 100.0 * self.speed_mps_at_100
        w = m.speed / 100.0 * self.rot_dps_at_100
        vf = vr = 0.0
        p = m.primitive
        if p == MotionPrimitive.FORWARD:
            vf = v
        elif p == MotionPrimitive.BACKWARD:
            vf = -v
        elif p == MotionPrimitive.STRAFE_RIGHT:
            vr = v
        elif p == MotionPrimitive.STRAFE_LEFT:
            vr = -v
        elif p == MotionPrimitive.ROTATE_RIGHT:
            self._pose["theta"] = (self._pose["theta"] + w * dt) % 360
        elif p == MotionPrimitive.ROTATE_LEFT:
            self._pose["theta"] = (self._pose["theta"] - w * dt) % 360
        th = math.radians(self._pose["theta"])
        # theta measured clockwise from +y (world "forward" at start)
        self._pose["x"] += (vf * math.sin(th) + vr * math.cos(th)) * dt
        self._pose["y"] += (vf * math.cos(th) - vr * math.sin(th)) * dt

    def _range_cm(self) -> float | None:
        """Forward ultrasonic: distance to the nearest obstacle along the heading, <= 2 m."""
        if not self.obstacles:
            return None
        th = math.radians(self._pose["theta"])
        dx, dy = math.sin(th), math.cos(th)
        best = None
        for b in self.obstacles:
            t = _ray_box(self._pose["x"], self._pose["y"], dx, dy, b)
            if t is not None and t <= 2.0 and (best is None or t < best):
                best = t
        return None if best is None else round(best * 100, 1)

    # ---------------------------------------------------------------- status
    def get_status(self) -> RobotStatus:
        now = self._clock()
        m = self._motion
        return RobotStatus(
            transport="mock",
            simulated=True,
            connected=self._connected,
            link_state=self._link_state if self._connected or self._link_state == "error" else "disconnected",
            target="simulator",
            last_error=self._last_error,
            connected_since=self._connected_since if self._connected else None,
            last_rx_age_s=None if self._last_rx is None else round(now - self._last_rx, 3),
            last_tx_age_s=None if self._last_tx is None else round(now - self._last_tx, 3),
            rtt_ms=round(self.latency_s * 2000, 1),
            rtt_kind="simulated",
            frames_sent=self._frames_sent,
            frames_received=self._frames_received,
            motion="idle" if m is None else f"{m.primitive.value}@{m.speed}",
            battery_v=None,
            firmware={"name": "simulator"},
            capabilities=TransportCapabilities(
                simulated=True,
                obstacle_sensing=True,
                device_watchdog="simulated" if self.device_watchdog_s is not None else "none",
                device_watchdog_timeout_s=self.device_watchdog_s,
                motion_ack="simulated",
                notes=["Simulated robot: no physical motion happens."],
            ),
        )

    def get_telemetry(self) -> RobotTelemetry:
        return self._last_telemetry


def _ray_box(ox: float, oy: float, dx: float, dy: float, b: Box) -> float | None:
    tmin, tmax = 0.0, math.inf
    for o, d, lo, hi in ((ox, dx, b.x0, b.x1), (oy, dy, b.y0, b.y1)):
        if abs(d) < 1e-9:
            if not (lo <= o <= hi):
                return None
        else:
            t1, t2 = (lo - o) / d, (hi - o) / d
            if t1 > t2:
                t1, t2 = t2, t1
            tmin, tmax = max(tmin, t1), min(tmax, t2)
            if tmin > tmax:
                return None
    return tmin
