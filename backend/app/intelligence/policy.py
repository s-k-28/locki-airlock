"""Transparent rule-based intervention policy.

The policy turns observations into *requests*: "say a line of category X",
"ask the safety layer for a short retreat". It never talks to the robot
directly and every decision carries the list of rules that fired, so the
dashboard can answer "why did it do that?".
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

from ..robot.commands import MotionOrigin, MotionPrimitive, MotionProfileName, MotionRequest
from ..sessions.metrics import FocusState
from .state_machine import MONITORING_STATES, State

Action = Literal["retreat", "voice_only", "none"]

INTENSITY_COOLDOWN_FACTOR = {"gentle": 3.0, "standard": 1.0, "strict": 0.5}
DISTRACTION_NUDGE_AFTER_S = {"gentle": 120.0, "standard": 45.0, "strict": 20.0}
EARLY_REACH_S = 180.0


@dataclass
class PolicyContext:
    state: State
    session_active: bool
    session_paused: bool
    elapsed_active_s: float
    remaining_s: float
    planned_s: float
    focus_state: FocusState
    robot_connected: bool
    robot_can_move: bool  # armed + autonomy enabled + not latched (safety re-checks everything)
    robot_displaced: bool
    reaches_confirmed: int
    seconds_since_intervention: float | None
    base_cooldown_s: float
    cooldown_multiplier: float = 1.0
    muted: bool = False
    autonomy_suspended: bool = False
    intensity: str = "standard"
    retreat_primitive: str = "backward"
    retreat_speed: int = 55
    retreat_duration_s: float = 0.6

    @property
    def cooldown_s(self) -> float:
        return self.base_cooldown_s * INTENSITY_COOLDOWN_FACTOR.get(self.intensity, 1.0) * self.cooldown_multiplier


@dataclass
class Decision:
    action: Action
    category: str | None
    rules: list[str] = field(default_factory=list)
    explanation: str = ""
    motion: MotionRequest | None = None
    speak: bool = True

    def to_payload(self) -> dict:
        d = asdict(self)
        d["motion"] = None if self.motion is None else self.motion.model_dump(mode="json")
        return d


class InterventionPolicy:
    def decide_reach(self, ctx: PolicyContext, confidence: float) -> Decision:
        rules: list[str] = [f"input: confirmed reach (confidence {confidence:.2f}) in state {ctx.state}"]

        if not ctx.session_active:
            rules.append("R1 no active session -> ignore")
            return Decision("none", None, rules, "No session is running, so reaches are not interventions.", speak=False)
        if ctx.session_paused or ctx.state == State.PAUSED:
            rules.append("R1 session paused -> ignore")
            return Decision("none", None, rules, "Monitoring is paused.", speak=False)
        if ctx.state == State.EMERGENCY_STOP:
            rules.append("R1 emergency stop latched -> ignore")
            return Decision("none", None, rules, "Emergency stop is latched.", speak=False)

        cooling = ctx.seconds_since_intervention is not None and ctx.seconds_since_intervention < ctx.cooldown_s
        if ctx.state == State.HOLDING or ctx.robot_displaced:
            rules.append("R2 phone already held away -> no motion")
            if cooling:
                rules.append(f"R3 intervention cooldown ({ctx.seconds_since_intervention:.0f}s < {ctx.cooldown_s:.0f}s) -> quiet")
                return Decision("none", None, rules, "Already holding the phone away; staying quiet (cooldown).", speak=False)
            return Decision("voice_only", "reach_holding", rules, "The phone is already away; a remark only.", speak=True)

        if cooling:
            rules.append(f"R3 intervention cooldown ({ctx.seconds_since_intervention:.0f}s < {ctx.cooldown_s:.0f}s) -> suppress")
            return Decision("none", None, rules, "Reached again inside the cooldown window; not reacting.", speak=False)

        category = self._reach_category(ctx, rules)

        if ctx.state not in MONITORING_STATES and ctx.state != State.PHONE_REACH_CONFIRMED:
            rules.append(f"R4 state {ctx.state} does not allow motion -> voice only")
            return Decision("voice_only", category, rules, "Robot cannot act in this state; voice only.", speak=True)
        if not ctx.robot_connected:
            rules.append("R4 robot offline -> voice only")
            return Decision("voice_only", category, rules, "Robot is offline; voice only.", speak=True)
        if ctx.autonomy_suspended:
            rules.append("R5 autonomy suspended after operator-labelled false triggers -> voice only")
            return Decision("voice_only", category, rules, "Autonomous retreat is suspended; voice only.", speak=True)
        if not ctx.robot_can_move:
            rules.append("R4 robot disarmed or autonomy off -> voice only")
            return Decision("voice_only", category, rules, "Autonomous motion is not enabled; voice only.", speak=True)

        rules.append(f"R6 request SHORT_RETREAT {ctx.retreat_primitive} (safety layer decides)")
        motion = MotionRequest(
            primitive=MotionPrimitive(ctx.retreat_primitive),
            speed=ctx.retreat_speed,
            duration_s=ctx.retreat_duration_s,
            profile=MotionProfileName.SHORT_RETREAT,
            origin=MotionOrigin.POLICY,
            reason=f"confirmed phone reach (confidence {confidence:.2f})",
        )
        return Decision("retreat", category, rules, "Confirmed reach during a session: request a short retreat.", motion, speak=True)

    def _reach_category(self, ctx: PolicyContext, rules: list[str]) -> str:
        if ctx.elapsed_active_s < EARLY_REACH_S:
            rules.append(f"L1 {ctx.elapsed_active_s:.0f}s into the session (< {EARLY_REACH_S:.0f}s) -> 'early' line")
            return "reach_early"
        if ctx.reaches_confirmed <= 1:
            rules.append("L2 first reach this session -> 'first' line")
            return "reach_first"
        if ctx.reaches_confirmed >= 3:
            rules.append(f"L3 reach #{ctx.reaches_confirmed} -> 'repeat, escalated' line")
            return "reach_repeat_many"
        rules.append(f"L3 reach #{ctx.reaches_confirmed} -> 'repeat' line")
        return "reach_repeat"

    def decide_focus(self, ctx: PolicyContext, previous: FocusState, held_for_s: float) -> Decision:
        rules = [f"input: focus {ctx.focus_state} for {held_for_s:.0f}s (was {previous})"]
        if not ctx.session_active or ctx.session_paused:
            rules.append("F0 no running session -> quiet")
            return Decision("none", None, rules, "Not in a running session.", speak=False)
        if ctx.focus_state == FocusState.AWAY:
            rules.append("F1 user away -> never talk to an empty room")
            return Decision("none", None, rules, "User appears to be away; staying quiet.", speak=False)
        if ctx.focus_state == FocusState.UNKNOWN:
            rules.append("F1 focus unknown -> quiet")
            return Decision("none", None, rules, "Not enough signal to judge focus.", speak=False)
        cooling = ctx.seconds_since_intervention is not None and ctx.seconds_since_intervention < ctx.cooldown_s * 3
        if ctx.focus_state == FocusState.POSSIBLY_DISTRACTED:
            threshold = DISTRACTION_NUDGE_AFTER_S.get(ctx.intensity, 45.0)
            if held_for_s < threshold:
                rules.append(f"F2 possibly distracted for {held_for_s:.0f}s (< {threshold:.0f}s) -> wait")
                return Decision("none", None, rules, "A glance away is not distraction; waiting.", speak=False)
            if cooling:
                rules.append("F3 nudge cooldown active -> quiet")
                return Decision("none", None, rules, "Nudged recently; staying quiet.", speak=False)
            rules.append(f"F2 sustained look-away {held_for_s:.0f}s -> gentle nudge")
            return Decision("voice_only", "nudge_distracted", rules, "Sustained look-away; one gentle nudge.", speak=True)
        if ctx.focus_state == FocusState.FOCUSED and previous == FocusState.AWAY and held_for_s < 5:
            rules.append("F4 back from away -> welcome back (if not cooling)")
            if cooling:
                return Decision("none", None, rules, "Back, but nudged recently.", speak=False)
            return Decision("voice_only", "welcome_back", rules, "User returned.", speak=True)
        rules.append("F5 focused -> stay quiet")
        return Decision("none", None, rules, "Focused; the best intervention is none.", speak=False)

    def decide_milestone(self, ctx: PolicyContext, milestone: str) -> Decision:
        rules = [f"input: milestone {milestone}"]
        if not ctx.session_active or ctx.session_paused:
            rules.append("M0 not running -> quiet")
            return Decision("none", None, rules, "Not announcing.", speak=False)
        if ctx.focus_state == FocusState.AWAY:
            rules.append("M1 user away -> quiet")
            return Decision("none", None, rules, "User away; not announcing.", speak=False)
        rules.append("M2 announce milestone")
        return Decision("voice_only", f"milestone_{milestone}", rules, f"Announce {milestone}.")
