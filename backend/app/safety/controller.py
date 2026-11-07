"""Safety controller: the only path from a motion request to the wheels.

Rules (docs/SAFETY.md has the reasoning):

* Disarmed by default. Arming is explicit and is cleared by any e-stop.
* STOP is always allowed and preempts everything.
* Emergency stop latches until an operator resets it; reset leaves the robot disarmed.
* Every motion is bounded by a profile (max speed, max duration) and has a deadline.
* Autonomous (policy) motion additionally needs: autonomy enabled, a cooldown since
  the last autonomous move, and - on real hardware - a verified device-side
  watchdog plus either fresh sensor coverage for the direction of travel or an
  operator's explicit "retreat path is clear" confirmation.
* Unknown safety inputs mean "stay stopped".
* A language model never reaches this layer: callers pass typed MotionRequests.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from ..robot.base import RobotTransport
from ..robot.commands import (
    AUTONOMOUS_ORIGINS,
    DEFAULT_PROFILES,
    ApprovedMotion,
    MotionOrigin,
    MotionPrimitive,
    MotionProfile,
    MotionProfileName,
    MotionRequest,
)
from ..telemetry.events import EventBus, EventSource, EventType

log = logging.getLogger(__name__)

# Which on-board sensor (if any) looks in the direction of travel.
SENSOR_COVERAGE: dict[MotionPrimitive, str | None] = {
    MotionPrimitive.FORWARD: "ultrasonic_cm",
    MotionPrimitive.STRAFE_LEFT: "ir_left_blocked",
    MotionPrimitive.STRAFE_RIGHT: "ir_right_blocked",
    MotionPrimitive.BACKWARD: None,
    MotionPrimitive.ROTATE_LEFT: None,
    MotionPrimitive.ROTATE_RIGHT: None,
}


class SafetyCheck(BaseModel):
    name: str
    ok: bool
    detail: str = ""


class SafetyDecision(BaseModel):
    approved: bool
    reason_code: str
    reason: str
    request: MotionRequest
    motion: ApprovedMotion | None = None
    checks: list[SafetyCheck] = Field(default_factory=list)


MOTOR_POWER_UNCHECKED = (
    "motor power not checked: the firmware cannot measure the battery. Check the motor battery is "
    "charged (its LEDs light with the power switch on; USB alone powers only the Arduino), the "
    "power switch is on and the wheels are clear, then confirm it"
)


@dataclass
class SafetyConfig:
    profiles: dict[MotionProfileName, MotionProfile] = field(default_factory=lambda: dict(DEFAULT_PROFILES))
    autonomous_cooldown_s: float = 10.0
    clear_path_ttl_s: float = 15 * 60
    # Real hardware only: the operator's motor power check (see MOTOR_POWER_UNCHECKED) holds
    # this long, and is cleared whenever the robot link drops (it may have been switched off).
    motor_power_ttl_s: float = 30 * 60
    obstacle_stop_cm: float = 25.0
    sensor_stale_s: float = 0.6
    min_speed: int = 1
    operator_min_interval_s: float = 0.08
    # Real hardware only: refuse autonomy until the device watchdog has been measured.
    require_verified_device_watchdog: bool = True
    transport_backup_margin_s: float = 0.2
    # Real hardware only, set from LOCKEDIN_ZEUS_MOTOR_COMMANDS: the profiles a real robot may
    # use (None = any) and whether it may move autonomously. "bench" = wheels-off-ground tests.
    real_profiles: frozenset[MotionProfileName] | None = frozenset()
    real_autonomy_allowed: bool = False
    real_motion_mode: str = "disabled"


@dataclass
class ActiveMotion:
    motion: ApprovedMotion
    started_mono: float
    cancel: asyncio.Event
    task: asyncio.Task | None = None
    outcome: str | None = None
    sent: bool = False


MotionFinished = Callable[[ApprovedMotion, str, dict], None]


class SafetyController:
    def __init__(
        self,
        *,
        bus: EventBus,
        get_transport: Callable[[], RobotTransport | None],
        config: SafetyConfig | None = None,
        default_speed: Callable[[MotionOrigin], int] = lambda origin: 50,
        default_duration: Callable[[MotionOrigin], float] = lambda origin: 0.4,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.bus = bus
        self._get_transport = get_transport
        self.config = config or SafetyConfig()
        self._default_speed = default_speed
        self._default_duration = default_duration
        self._clock = clock
        self.armed = False
        self.autonomy_enabled = False
        self.clear_path_confirmed_at: float | None = None
        self.motor_power_confirmed_at: float | None = None
        self.estop_latched = False
        self.estop_reason: str | None = None
        self.last_autonomous_motion_at: float | None = None
        self.last_operator_motion_at: float | None = None
        self.active: ActiveMotion | None = None
        self.on_motion_finished: MotionFinished = lambda motion, outcome, info: None
        self.blocked_counts: dict[str, int] = {}
        self.approved_count = 0
        # After a spoken "stop": no new motion of any kind for a while.
        self.suppressed_until: float | None = None
        self.suppressed_reason: str | None = None
        # Real robot, autonomous motion only: (ok, why) from the runtime's perception state.
        self.perception_check: Callable[[], tuple[bool, str]] | None = None

    def suppress_motion(self, seconds: float, reason: str) -> None:
        """Refuse every new motion request for ``seconds`` (a stop is not undone by the next reach)."""
        self.suppressed_until = self._clock() + seconds
        self.suppressed_reason = reason
        self.bus.publish(EventType.SAFETY_MOTION_SUPPRESSED, EventSource.SAFETY, {"seconds": seconds, "reason": reason})

    def confirm_motor_power(self, source: str = "operator") -> None:
        """The operator checked what no sensor reports: motor battery charged, power switch on,
        wheels clear. Required before a real robot may arm or move."""
        self.motor_power_confirmed_at = self._clock()
        self.bus.publish(
            EventType.SAFETY_MOTOR_POWER, EventSource.SAFETY,
            {"confirmed": True, "source": source, "valid_s": self.config.motor_power_ttl_s},
        )

    def clear_motor_power(self, reason: str) -> None:
        if self.motor_power_confirmed_at is not None:
            self.motor_power_confirmed_at = None
            self.bus.publish(EventType.SAFETY_MOTOR_POWER, EventSource.SAFETY, {"confirmed": False, "reason": reason})

    def motor_power_valid(self) -> bool:
        return (
            self.motor_power_confirmed_at is not None
            and self._clock() - self.motor_power_confirmed_at <= self.config.motor_power_ttl_s
        )

    def suppression_left(self) -> float | None:
        if self.suppressed_until is None:
            return None
        left = self.suppressed_until - self._clock()
        return round(left, 1) if left > 0 else None

    # ------------------------------------------------------------ arming
    @property
    def transport(self) -> RobotTransport | None:
        return self._get_transport()

    def arm(self, source: str = "operator") -> tuple[bool, str]:
        t = self.transport
        if self.estop_latched:
            return False, "emergency stop is latched; reset it first"
        if t is None or not t.connected:
            return False, "robot is not connected"
        caps = t.get_status().capabilities
        if not caps.simulated and not caps.motion_permitted:
            return False, "physical motion is locked: " + "; ".join(caps.motion_lock_reasons or ["not permitted by the hardware gate"])
        if not caps.simulated and not self.motor_power_valid():
            return False, MOTOR_POWER_UNCHECKED
        if not self.armed:
            self.armed = True
            self.bus.publish(
                EventType.SAFETY_ARMED, EventSource.SAFETY, {"source": source, "transport": t.name}, simulated=t.simulated
            )
        return True, "armed"

    async def disarm(self, source: str = "operator", reason: str = "operator disarm") -> None:
        was = self.armed
        self.armed = False
        self.autonomy_enabled = False
        self.clear_path_confirmed_at = None
        if self.active is not None:
            await self.stop(reason=f"disarmed: {reason}", source=source)
        if was:
            self.bus.publish(EventType.SAFETY_DISARMED, EventSource.SAFETY, {"source": source, "reason": reason})

    def set_autonomy(self, enabled: bool, clear_path_confirmed: bool = False, source: str = "operator") -> tuple[bool, str]:
        if enabled and not self.armed:
            return False, "arm the robot before enabling autonomous motion"
        t = self.transport
        if enabled and t is not None and not t.simulated and not self.config.real_autonomy_allowed:
            return False, f"autonomous motion is off on the real robot (motor commands: {self.config.real_motion_mode})"
        if enabled and self.estop_latched:
            return False, "emergency stop is latched"
        self.autonomy_enabled = enabled
        if enabled and clear_path_confirmed:
            self.clear_path_confirmed_at = self._clock()
        if not enabled:
            self.clear_path_confirmed_at = None
        self.bus.publish(
            EventType.SAFETY_AUTONOMY_CHANGED,
            EventSource.SAFETY,
            {"enabled": enabled, "clear_path_confirmed": bool(enabled and clear_path_confirmed), "source": source},
        )
        return True, "autonomy enabled" if enabled else "autonomy disabled"

    def clear_path_valid(self) -> bool:
        return (
            self.clear_path_confirmed_at is not None
            and self._clock() - self.clear_path_confirmed_at <= self.config.clear_path_ttl_s
        )

    # ---------------------------------------------------------- evaluation
    def evaluate(self, req: MotionRequest, *, state_allows: bool, state_name: str) -> SafetyDecision:
        checks: list[SafetyCheck] = []

        def deny(code: str, reason: str) -> SafetyDecision:
            checks.append(SafetyCheck(name=code, ok=False, detail=reason))
            return SafetyDecision(approved=False, reason_code=code, reason=reason, request=req, checks=checks)

        def ok(name: str, detail: str = "") -> None:
            checks.append(SafetyCheck(name=name, ok=True, detail=detail))

        t = self.transport
        now = self._clock()

        if self.estop_latched:
            return deny("estop_latched", f"emergency stop latched ({self.estop_reason})")
        ok("estop", "not latched")

        left = self.suppression_left()
        if left is not None:
            return deny("motion_suppressed", f"motion suppressed for {left:.0f} s more after {self.suppressed_reason}")

        if t is None or not t.connected:
            return deny("not_connected", "robot is not connected")
        status = t.get_status()
        ok("link", f"{status.transport} {status.link_state}")
        caps = status.capabilities

        if not caps.simulated and not caps.motion_permitted:
            return deny(
                "hardware_motion_locked",
                "physical motion is locked: " + "; ".join(caps.motion_lock_reasons or ["not permitted by the hardware gate"]),
            )
        ok("hardware_gate", caps.link_mode)
        if not caps.simulated:
            if not self.motor_power_valid():
                return deny("motor_power_unchecked", MOTOR_POWER_UNCHECKED)
            ok("motor_power", "checked by the operator")

        if req.primitive == MotionPrimitive.STOP:
            return deny("invalid_primitive", "stop is handled by stop(), not as a motion")
        ok("primitive", req.primitive.value)

        if not self.armed:
            return deny("disarmed", "robot is disarmed")
        ok("armed")

        if not state_allows:
            return deny("state", f"{req.origin.value} motion is not allowed in state {state_name}")
        ok("state", state_name)

        profile = self.config.profiles.get(req.profile)
        if profile is None:
            return deny("profile", f"unknown motion profile {req.profile}")
        if profile.name == MotionProfileName.LINK_TEST and req.origin != MotionOrigin.TEST:
            return deny("profile", "the LINK_TEST profile is reserved for the R5 link test")
        if req.primitive not in profile.allowed:
            return deny("profile", f"{req.primitive.value} is not allowed in profile {profile.name}")
        if not caps.simulated and self.config.real_profiles is not None and profile.name not in self.config.real_profiles:
            allowed = ", ".join(sorted(p.value for p in self.config.real_profiles)) or "none"
            return deny("real_profile", f"motor commands are '{self.config.real_motion_mode}': real-robot profiles allowed: {allowed}")
        ok("profile", profile.name)

        clamped: list[str] = []
        speed = req.speed if req.speed is not None else self._default_speed(req.origin)
        if speed > profile.max_speed:
            clamped.append(f"speed {speed:g} -> {profile.max_speed}")
            speed = profile.max_speed
        speed_i = int(round(speed))
        if speed_i < self.config.min_speed:
            return deny("bounds", "speed must be greater than zero")
        duration = req.duration_s if req.duration_s is not None else self._default_duration(req.origin)
        if duration <= 0:
            return deny("bounds", "duration must be greater than zero")
        if duration > profile.max_duration_s:
            clamped.append(f"duration {duration:g}s -> {profile.max_duration_s}s")
            duration = profile.max_duration_s
        ok("bounds", f"speed {speed_i}, {duration:.2f}s" + (f" (clamped: {', '.join(clamped)})" if clamped else ""))

        if self.active is not None and req.origin in (MotionOrigin.POLICY, MotionOrigin.RETURN, MotionOrigin.TEST):
            return deny("busy", f"motion {self.active.motion.request_id} is still running")

        if req.origin in (MotionOrigin.OPERATOR, MotionOrigin.VOICE) and self.last_operator_motion_at is not None:
            if now - self.last_operator_motion_at < self.config.operator_min_interval_s:
                return deny("rate", "manual commands are arriving too fast")

        autonomous = req.origin in AUTONOMOUS_ORIGINS
        if autonomous:
            if not self.autonomy_enabled:
                return deny("autonomy_disabled", "autonomous motion is disabled")
            if not caps.simulated and not self.config.real_autonomy_allowed:
                return deny("real_autonomy_off", f"autonomous motion is off on the real robot (motor commands: {self.config.real_motion_mode})")
            if not caps.simulated and self.perception_check is not None:
                seen, why = self.perception_check()
                if not seen:
                    return deny("perception_unreliable", f"fail closed: {why}")
                ok("perception", why)
            ok("autonomy", "enabled")
            if self.last_autonomous_motion_at is not None:
                since = now - self.last_autonomous_motion_at
                if since < self.config.autonomous_cooldown_s:
                    return deny(
                        "cooldown", f"last autonomous move was {since:.1f}s ago (< {self.config.autonomous_cooldown_s:g}s)"
                    )
            ok("cooldown")
            if not caps.simulated and self.config.require_verified_device_watchdog and caps.device_watchdog != "verified":
                return deny(
                    "device_watchdog_unverified",
                    "real robot's stop-on-link-loss behaviour has not been verified (docs/SAFETY.md, test R5)",
                )
            ok("device_watchdog", caps.device_watchdog)

        # Direction-specific obstacle check.
        sensor_field = SENSOR_COVERAGE.get(req.primitive)
        coverage = "none"
        if caps.obstacle_sensing and sensor_field is not None:
            tel = t.get_telemetry()
            age = time.time() - tel.timestamp
            value = getattr(tel, sensor_field)
            if age <= self.config.sensor_stale_s:
                coverage = "fresh"
                if sensor_field == "ultrasonic_cm" and value is not None and 0 < value < self.config.obstacle_stop_cm:
                    return deny("obstacle", f"obstacle {value:.0f} cm ahead (< {self.config.obstacle_stop_cm:g} cm)")
                if sensor_field != "ultrasonic_cm" and value is True:
                    return deny("obstacle", f"{sensor_field.replace('_', ' ')} reports an obstacle")
                ok("obstacle", f"{sensor_field}={value}")
            else:
                coverage = "stale"
                if autonomous:
                    return deny("sensor_stale", f"{sensor_field} data is {age:.1f}s old")
        if autonomous and not caps.simulated and coverage != "fresh":
            if not self.clear_path_valid():
                return deny(
                    "path_unverified",
                    f"no sensor covers {req.primitive.value} and no operator clear-path confirmation is active",
                )
            ok("path", "operator confirmed the retreat path is clear")

        motion = ApprovedMotion(
            request_id=req.request_id,
            primitive=req.primitive,
            speed=speed_i,
            duration_s=round(duration, 3),
            profile=profile.name,
            origin=req.origin,
            reason=req.reason,
            clamped=clamped,
        )
        return SafetyDecision(approved=True, reason_code="approved", reason="all checks passed", request=req, motion=motion, checks=checks)

    # ----------------------------------------------------------- execution
    async def request(self, req: MotionRequest, *, state_allows: bool, state_name: str) -> SafetyDecision:
        t = self.transport
        simulated = bool(t and t.simulated)
        self.bus.publish(
            EventType.ROBOT_MOTION_REQUESTED,
            EventSource.SAFETY,
            {
                "request_id": req.request_id,
                "primitive": req.primitive.value,
                "speed": req.speed,
                "duration_s": req.duration_s,
                "profile": req.profile.value,
                "origin": req.origin.value,
                "reason": req.reason,
            },
            simulated=simulated,
        )
        decision = self.evaluate(req, state_allows=state_allows, state_name=state_name)
        if not decision.approved:
            self.blocked_counts[decision.reason_code] = self.blocked_counts.get(decision.reason_code, 0) + 1
            self.bus.publish(
                EventType.ROBOT_MOTION_BLOCKED,
                EventSource.SAFETY,
                {
                    "request_id": req.request_id,
                    "primitive": req.primitive.value,
                    "origin": req.origin.value,
                    "reason_code": decision.reason_code,
                    "reason": decision.reason,
                    "checks": [c.model_dump() for c in decision.checks],
                },
                simulated=simulated,
            )
            return decision

        if self.active is not None:  # operator/voice preempting a manual move
            await self.stop(reason="preempted by a new manual command", source=req.origin.value, publish=False)

        motion = decision.motion
        assert motion is not None
        self.approved_count += 1
        now = self._clock()
        if motion.origin in AUTONOMOUS_ORIGINS:
            self.last_autonomous_motion_at = now
        if motion.origin in (MotionOrigin.OPERATOR, MotionOrigin.VOICE):
            self.last_operator_motion_at = now
        active = ActiveMotion(motion=motion, started_mono=now, cancel=asyncio.Event())
        self.active = active
        active.task = asyncio.create_task(self._run(active), name=f"motion-{motion.request_id}")
        return decision

    async def _run(self, active: ActiveMotion) -> None:
        motion = active.motion
        t = self.transport
        info: dict = {}
        outcome = "completed"
        try:
            if t is None or not t.connected:
                outcome = "aborted"
                info["detail"] = "robot disconnected before sending"
                return
            receipt = await t.move(
                motion.primitive, motion.speed, motion.duration_s + self.config.transport_backup_margin_s
            )
            if not receipt.sent:
                outcome = "aborted"
                info["detail"] = f"send failed: {receipt.detail}"
                return
            active.sent = True
            self.bus.publish(
                EventType.ROBOT_MOTION_SENT,
                EventSource.ROBOT,
                {
                    "request_id": motion.request_id,
                    "primitive": motion.primitive.value,
                    "speed": motion.speed,
                    "duration_s": motion.duration_s,
                    "profile": motion.profile.value,
                    "origin": motion.origin.value,
                    "clamped": motion.clamped,
                    "detail": receipt.detail,
                },
                simulated=t.simulated,
            )
            try:
                await asyncio.wait_for(active.cancel.wait(), timeout=motion.duration_s)
                outcome = active.outcome or "stopped"
            except TimeoutError:
                outcome = "completed"
            stop_receipt = await t.stop("commanded window elapsed" if outcome == "completed" else outcome)
            info["stop_sent"] = stop_receipt.sent
        except asyncio.CancelledError:
            outcome = "cancelled"
            if t is not None and t.connected:
                await t.stop("motion task cancelled")
            raise
        except Exception as exc:
            log.exception("motion execution failed")
            outcome = "error"
            info["detail"] = str(exc)
            if t is not None and t.connected:
                try:
                    await t.stop("motion error")
                except Exception:
                    log.exception("stop after motion error failed")
        finally:
            ran = self._clock() - active.started_mono
            info.update(ran_s=round(ran, 3), sent=active.sent)
            if self.active is active:
                self.active = None
            sim = bool(t and t.simulated)
            if outcome == "completed":
                self.bus.publish(
                    EventType.ROBOT_MOTION_COMPLETED,
                    EventSource.ROBOT,
                    {
                        "request_id": motion.request_id,
                        "primitive": motion.primitive.value,
                        "ran_s": info["ran_s"],
                        "note": "host-side completion: the commanded window ended and stop was sent. "
                        "Physical travel is not measured by this event.",
                    },
                    simulated=sim,
                )
            else:
                self.bus.publish(
                    EventType.ROBOT_MOTION_ABORTED,
                    EventSource.ROBOT,
                    {"request_id": motion.request_id, "primitive": motion.primitive.value, "outcome": outcome, **info},
                    simulated=sim,
                )
            try:
                self.on_motion_finished(motion, outcome, info)
            except Exception:
                log.exception("motion-finished hook failed")

    async def stop(self, reason: str, source: str = "operator", publish: bool = True) -> bool:
        """Always allowed. Cancels the active motion and sends stop to the robot."""
        t = self.transport
        sim = bool(t and t.simulated)
        if publish:
            self.bus.publish(EventType.ROBOT_STOP_REQUESTED, EventSource.SAFETY, {"reason": reason, "source": source}, simulated=sim)
        active = self.active
        if active is not None:
            active.outcome = "stopped" if "preempt" not in reason else "preempted"
            active.cancel.set()
        sent = False
        detail = "robot not connected"
        if t is not None and t.connected:
            receipt = await t.stop(reason)
            sent, detail = receipt.sent, receipt.detail
        if active is not None and active.task is not None and active.task is not asyncio.current_task():
            try:
                await asyncio.wait_for(asyncio.shield(active.task), timeout=1.0)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
            except Exception:
                pass
        if publish:
            self.bus.publish(
                EventType.ROBOT_STOP_SENT, EventSource.ROBOT, {"sent": sent, "detail": detail, "reason": reason, "source": source}, simulated=sim
            )
        return sent

    async def emergency_stop(self, reason: str, source: str = "operator") -> None:
        self.estop_latched = True
        self.estop_reason = reason
        self.armed = False
        self.autonomy_enabled = False
        self.clear_path_confirmed_at = None
        self.bus.publish(EventType.SAFETY_EMERGENCY_STOP, EventSource.SAFETY, {"reason": reason, "source": source})
        await self.stop(reason=f"EMERGENCY STOP: {reason}", source=source)

    def reset_estop(self, source: str = "operator") -> tuple[bool, str]:
        if not self.estop_latched:
            return True, "emergency stop was not latched"
        t = self.transport
        if t is not None and t.connected and t.is_moving:
            return False, "robot still reports motion; wait for it to stop"
        self.estop_latched = False
        self.estop_reason = None
        self.bus.publish(EventType.SAFETY_RESET, EventSource.SAFETY, {"source": source, "armed": False})
        return True, "emergency stop reset; robot remains disarmed"

    def notify_transport_motion_ended(self, why: str) -> None:
        """Transport stopped the wheels on its own (link loss, device watchdog...)."""
        active = self.active
        if active is not None and why in {"link_lost", "device_watchdog", "disconnect", "watchdog", "freeze_timeout"}:
            active.outcome = f"aborted:{why}"
            active.cancel.set()

    def snapshot(self) -> dict:
        now = self._clock()
        cooldown_left = 0.0
        if self.last_autonomous_motion_at is not None:
            cooldown_left = max(0.0, self.config.autonomous_cooldown_s - (now - self.last_autonomous_motion_at))
        a = self.active
        return {
            "armed": self.armed,
            "autonomy_enabled": self.autonomy_enabled,
            "clear_path_confirmed": self.clear_path_valid(),
            "clear_path_expires_in_s": (
                round(self.config.clear_path_ttl_s - (now - self.clear_path_confirmed_at), 1)
                if self.clear_path_valid()
                else None
            ),
            "motor_power_checked": self.motor_power_valid(),
            "motor_power_expires_in_s": (
                round(self.config.motor_power_ttl_s - (now - self.motor_power_confirmed_at), 1)
                if self.motor_power_valid()
                else None
            ),
            "estop_latched": self.estop_latched,
            "estop_reason": self.estop_reason,
            "motion_suppressed_s": self.suppression_left(),
            "motion_suppressed_reason": self.suppressed_reason if self.suppression_left() is not None else None,
            "autonomous_cooldown_left_s": round(cooldown_left, 2),
            "active_motion": None
            if a is None
            else {**a.motion.model_dump(mode="json"), "elapsed_s": round(now - a.started_mono, 3)},
            "approved_count": self.approved_count,
            "blocked_counts": dict(self.blocked_counts),
            "profiles": {k.value: v.model_dump(mode="json") for k, v in self.config.profiles.items()},
        }
