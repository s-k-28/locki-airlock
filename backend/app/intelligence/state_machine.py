"""The LOCKED IN behaviour state machine.

Pure logic, no I/O. Every allowed (state, trigger) pair is listed explicitly in
``TRANSITIONS``. A pair can map to:

* a target ``State``;
* a guard function ``ctx -> State | None`` (``None`` = guard refused);
* ``IGNORE`` - the trigger is expected in that state and deliberately changes
  nothing (e.g. a hand in the phone zone while the robot is already holding the
  phone away).

Any pair not listed is rejected. Rejections are reported, never silently applied,
so unknown or out-of-order inputs can never produce motion.

Session timing lives in ``SessionManager``; the machine reads it through
``MachineContext`` so robot connectivity and session state stay consistent.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum


class State(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    IDLE = "IDLE"
    SESSION_ACTIVE = "SESSION_ACTIVE"
    POSSIBLE_PHONE_REACH = "POSSIBLE_PHONE_REACH"
    PHONE_REACH_CONFIRMED = "PHONE_REACH_CONFIRMED"
    RETREATING = "RETREATING"
    HOLDING = "HOLDING"
    RETURNING = "RETURNING"
    PAUSED = "PAUSED"
    RELEASED = "RELEASED"
    EMERGENCY_STOP = "EMERGENCY_STOP"


class Trigger(StrEnum):
    ROBOT_CONNECTED = "ROBOT_CONNECTED"
    ROBOT_DISCONNECTED = "ROBOT_DISCONNECTED"
    START_SESSION = "START_SESSION"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    END_SESSION = "END_SESSION"
    REACH_SUSPECTED = "REACH_SUSPECTED"
    REACH_CLEARED = "REACH_CLEARED"
    REACH_CONFIRMED = "REACH_CONFIRMED"
    REACH_HANDLED = "REACH_HANDLED"  # decision made without motion (voice only / suppressed / denied)
    RETREAT_STARTED = "RETREAT_STARTED"
    RETURN_REQUESTED = "RETURN_REQUESTED"
    MOTION_COMPLETED = "MOTION_COMPLETED"
    MOTION_ABORTED = "MOTION_ABORTED"
    RELEASE = "RELEASE"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    RESET = "RESET"
    HOME_SET = "HOME_SET"  # operator declares the robot's current position as home


@dataclass(frozen=True)
class MachineContext:
    robot_connected: bool = False
    session_active: bool = False
    session_paused: bool = False
    robot_displaced: bool = False


class _Ignore:
    def __repr__(self) -> str:
        return "IGNORE"


IGNORE = _Ignore()

Guard = Callable[[MachineContext], "State | None"]
Target = State | Guard | _Ignore

# States in which a robot motion is (or may be) in progress.
MOTION_STATES = frozenset({State.RETREATING, State.RETURNING})
# States that mean "a session is running and being monitored".
MONITORING_STATES = frozenset(
    {State.SESSION_ACTIVE, State.POSSIBLE_PHONE_REACH, State.PHONE_REACH_CONFIRMED}
)


# --------------------------------------------------------------------- guards
def session_target(ctx: MachineContext) -> State:
    """Where to settle once nothing transient is happening."""
    if not ctx.robot_connected:
        return State.DISCONNECTED
    if not ctx.session_active:
        return State.IDLE
    if ctx.session_paused:
        return State.PAUSED
    if ctx.robot_displaced:
        return State.HOLDING
    return State.SESSION_ACTIVE


def start_target(ctx: MachineContext) -> State | None:
    # ctx is evaluated as if the session had already started.
    if not ctx.session_active:
        return None
    return State.HOLDING if ctx.robot_displaced else State.SESSION_ACTIVE


def return_guard(ctx: MachineContext) -> State | None:
    if ctx.robot_connected and ctx.robot_displaced:
        return State.RETURNING
    return None


def reset_estop_target(ctx: MachineContext) -> State:
    return session_target(ctx)


def reset_released_target(ctx: MachineContext) -> State:
    return State.IDLE if ctx.robot_connected else State.DISCONNECTED


# Common rows shared by every "session in progress" state.
def _session_common() -> dict[Trigger, Target]:
    return {
        Trigger.PAUSE: State.PAUSED,
        Trigger.END_SESSION: State.IDLE,
        Trigger.RELEASE: State.RELEASED,
        Trigger.ROBOT_DISCONNECTED: State.DISCONNECTED,
        Trigger.EMERGENCY_STOP: State.EMERGENCY_STOP,
        Trigger.ROBOT_CONNECTED: IGNORE,
        Trigger.HOME_SET: IGNORE,
    }


def _no_home(row: dict[Trigger, Target]) -> dict[Trigger, Target]:
    return {k: v for k, v in row.items() if k != Trigger.HOME_SET}


TRANSITIONS: dict[State, dict[Trigger, Target]] = {
    State.DISCONNECTED: {
        Trigger.ROBOT_CONNECTED: session_target,
        Trigger.ROBOT_DISCONNECTED: IGNORE,
        # The session can run while the robot is offline (timer, voice, vision still
        # work); motion is impossible until the link is back.
        Trigger.START_SESSION: IGNORE,
        Trigger.PAUSE: IGNORE,
        Trigger.RESUME: IGNORE,
        Trigger.END_SESSION: IGNORE,
        Trigger.RELEASE: IGNORE,
        Trigger.REACH_SUSPECTED: IGNORE,
        Trigger.REACH_CLEARED: IGNORE,
        Trigger.REACH_CONFIRMED: IGNORE,
        Trigger.REACH_HANDLED: IGNORE,
        Trigger.MOTION_COMPLETED: IGNORE,
        Trigger.MOTION_ABORTED: IGNORE,
        Trigger.HOME_SET: IGNORE,
        Trigger.EMERGENCY_STOP: State.EMERGENCY_STOP,
    },
    State.IDLE: {
        Trigger.START_SESSION: start_target,
        Trigger.RETURN_REQUESTED: return_guard,
        Trigger.RELEASE: State.RELEASED,
        Trigger.REACH_SUSPECTED: IGNORE,
        Trigger.REACH_CLEARED: IGNORE,
        Trigger.REACH_CONFIRMED: IGNORE,
        Trigger.MOTION_COMPLETED: IGNORE,
        Trigger.MOTION_ABORTED: IGNORE,
        Trigger.ROBOT_CONNECTED: IGNORE,
        Trigger.ROBOT_DISCONNECTED: State.DISCONNECTED,
        Trigger.HOME_SET: IGNORE,
        Trigger.EMERGENCY_STOP: State.EMERGENCY_STOP,
    },
    State.SESSION_ACTIVE: {
        **_session_common(),
        Trigger.REACH_SUSPECTED: State.POSSIBLE_PHONE_REACH,
        Trigger.REACH_CONFIRMED: State.PHONE_REACH_CONFIRMED,
        Trigger.REACH_CLEARED: IGNORE,
        Trigger.RETURN_REQUESTED: return_guard,
        Trigger.MOTION_COMPLETED: IGNORE,
        Trigger.MOTION_ABORTED: IGNORE,
    },
    State.POSSIBLE_PHONE_REACH: {
        **_session_common(),
        Trigger.REACH_CONFIRMED: State.PHONE_REACH_CONFIRMED,
        Trigger.REACH_CLEARED: State.SESSION_ACTIVE,
        Trigger.REACH_SUSPECTED: IGNORE,
    },
    State.PHONE_REACH_CONFIRMED: {
        **_session_common(),
        Trigger.RETREAT_STARTED: State.RETREATING,
        Trigger.REACH_HANDLED: State.SESSION_ACTIVE,
        Trigger.REACH_SUSPECTED: IGNORE,
        Trigger.REACH_CONFIRMED: IGNORE,
        Trigger.REACH_CLEARED: IGNORE,
    },
    State.RETREATING: {
        **_no_home(_session_common()),
        Trigger.MOTION_COMPLETED: session_target,
        Trigger.MOTION_ABORTED: session_target,
        Trigger.REACH_SUSPECTED: IGNORE,
        Trigger.REACH_CONFIRMED: IGNORE,
        Trigger.REACH_CLEARED: IGNORE,
    },
    State.HOLDING: {
        **_session_common(),
        Trigger.RETURN_REQUESTED: return_guard,
        Trigger.HOME_SET: session_target,
        # The phone is already away from the user: reaching into the empty zone is noted
        # but never triggers another retreat.
        Trigger.REACH_SUSPECTED: IGNORE,
        Trigger.REACH_CONFIRMED: IGNORE,
        Trigger.REACH_CLEARED: IGNORE,
        Trigger.MOTION_COMPLETED: IGNORE,
        Trigger.MOTION_ABORTED: IGNORE,
    },
    State.RETURNING: {
        **_no_home(_session_common()),
        Trigger.MOTION_COMPLETED: session_target,
        Trigger.MOTION_ABORTED: session_target,
        Trigger.REACH_SUSPECTED: IGNORE,
        Trigger.REACH_CONFIRMED: IGNORE,
        Trigger.REACH_CLEARED: IGNORE,
    },
    State.PAUSED: {
        Trigger.RESUME: session_target,
        Trigger.END_SESSION: State.IDLE,
        Trigger.RELEASE: State.RELEASED,
        Trigger.RETURN_REQUESTED: return_guard,
        Trigger.PAUSE: IGNORE,
        Trigger.REACH_SUSPECTED: IGNORE,
        Trigger.REACH_CONFIRMED: IGNORE,
        Trigger.REACH_CLEARED: IGNORE,
        Trigger.REACH_HANDLED: IGNORE,
        Trigger.MOTION_COMPLETED: IGNORE,
        Trigger.MOTION_ABORTED: IGNORE,
        Trigger.ROBOT_CONNECTED: IGNORE,
        Trigger.ROBOT_DISCONNECTED: State.DISCONNECTED,
        Trigger.HOME_SET: IGNORE,
        Trigger.EMERGENCY_STOP: State.EMERGENCY_STOP,
    },
    State.RELEASED: {
        Trigger.START_SESSION: start_target,
        Trigger.RESET: reset_released_target,
        Trigger.RETURN_REQUESTED: return_guard,
        Trigger.RELEASE: IGNORE,
        Trigger.END_SESSION: IGNORE,
        Trigger.REACH_SUSPECTED: IGNORE,
        Trigger.REACH_CONFIRMED: IGNORE,
        Trigger.REACH_CLEARED: IGNORE,
        Trigger.MOTION_COMPLETED: IGNORE,
        Trigger.MOTION_ABORTED: IGNORE,
        Trigger.ROBOT_CONNECTED: IGNORE,
        Trigger.ROBOT_DISCONNECTED: State.DISCONNECTED,
        Trigger.HOME_SET: IGNORE,
        Trigger.EMERGENCY_STOP: State.EMERGENCY_STOP,
    },
    State.EMERGENCY_STOP: {
        # Latched until an operator explicitly resets it.
        Trigger.RESET: reset_estop_target,
        Trigger.EMERGENCY_STOP: IGNORE,
        # Asking for the phone during an e-stop ends the session; the latch stays.
        Trigger.RELEASE: IGNORE,
        Trigger.END_SESSION: IGNORE,
        Trigger.PAUSE: IGNORE,
        Trigger.ROBOT_CONNECTED: IGNORE,
        Trigger.ROBOT_DISCONNECTED: IGNORE,
        Trigger.REACH_SUSPECTED: IGNORE,
        Trigger.REACH_CONFIRMED: IGNORE,
        Trigger.REACH_CLEARED: IGNORE,
        Trigger.REACH_HANDLED: IGNORE,
        Trigger.MOTION_COMPLETED: IGNORE,
        Trigger.MOTION_ABORTED: IGNORE,
        Trigger.HOME_SET: IGNORE,
    },
}


@dataclass
class TransitionResult:
    accepted: bool
    trigger: Trigger
    from_state: State
    to_state: State
    ignored: bool = False
    reason: str = ""

    @property
    def changed(self) -> bool:
        return self.accepted and self.from_state != self.to_state


@dataclass
class TransitionRecord:
    at: float
    trigger: str
    from_state: str
    to_state: str
    reason: str


@dataclass
class StateMachine:
    state: State = State.DISCONNECTED
    clock: Callable[[], float] = time.monotonic
    history: deque[TransitionRecord] = field(default_factory=lambda: deque(maxlen=200))

    def __post_init__(self) -> None:
        self.entered_at = self.clock()

    def resolve(self, trigger: Trigger, ctx: MachineContext) -> TransitionResult:
        """Work out what ``trigger`` would do without applying it."""
        row = TRANSITIONS.get(self.state, {})
        if trigger not in row:
            return TransitionResult(
                False, trigger, self.state, self.state, reason=f"{trigger} is not valid in {self.state}"
            )
        target = row[trigger]
        if target is IGNORE:
            return TransitionResult(True, trigger, self.state, self.state, ignored=True)
        if isinstance(target, State):
            return TransitionResult(True, trigger, self.state, target)
        resolved = target(ctx)
        if resolved is None:
            return TransitionResult(
                False, trigger, self.state, self.state, reason=f"guard refused {trigger} in {self.state}"
            )
        return TransitionResult(True, trigger, self.state, resolved)

    def can(self, trigger: Trigger, ctx: MachineContext) -> bool:
        return self.resolve(trigger, ctx).accepted

    def fire(self, trigger: Trigger, ctx: MachineContext, reason: str = "") -> TransitionResult:
        result = self.resolve(trigger, ctx)
        if result.accepted and not result.ignored:
            if result.to_state != self.state:
                self.state = result.to_state
                self.entered_at = self.clock()
            self.history.append(
                TransitionRecord(self.clock(), str(trigger), str(result.from_state), str(result.to_state), reason)
            )
        return result

    def time_in_state(self) -> float:
        return self.clock() - self.entered_at
