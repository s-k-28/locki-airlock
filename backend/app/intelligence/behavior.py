"""When LOCKED IN speaks up on its own, and when it stays quiet.

The runtime hands every relevant event to this engine (vision, voice, session, reach,
focus timer, robot link, motor safety, conversation). The engine keeps a bounded,
timestamped history and, for each event that could deserve a reaction, decides: speak or
stay silent; how urgent; whether it was already answered; which line style; whether a
physical action could even be requested. Every decision - staying silent included - is
recorded with the rules that fired, so the dashboard can show why.

Principles (each is a rule id in the decision record):

* B1 a *possible* reach (a hand heading for the phone) is not a distraction: at most a
  light heads-up, never counted, never escalated, never claimed as seen grabbing;
* B2 *confirmed* reaches escalate with how many happened in the last ``ESCALATION_WINDOW_S``;
  one reach episode gets one remark (the motion policy's intervention cooldown, R3);
* B3 hysteresis: the camera must be gone ``CAMERA_LOSS_HOLD_S`` before the loss is
  announced, so a blip stays silent; focus states arrive already debounced (the vision
  layer's FocusEstimator needs a sustained look-away or absence), and praise needs a whole
  ``FOCUS_STREAK_S`` without a reach;
* B4 a budget for unprompted remarks (``GAP_S`` by urgency), doubled for ten minutes after
  the user objects ("why do you keep doing that?");
* B5 boundaries: quiet mode silences every unprompted remark except essential ones (time's
  up, the robot lost mid-session); nothing unprompted while the user is talking;
* B6 physical actions are only *requested* (the motion policy and the safety controller
  decide); the outcome is attached to the decision.

No scores are invented: every input is a recorded event, a count of recorded events or a
timer.
"""

from __future__ import annotations

import re
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from .context import SessionContext

ESCALATION_WINDOW_S = 600.0
HEADS_UP_GAP_S = 20.0  # the M4 REACH_WARNING_COOLDOWN_S
POSSIBLE_REACH_MIN_CONF = 0.5
CAMERA_LOSS_HOLD_S = 3.0
COMPLAINT_WINDOW_S = 600.0
GAP_S = {"low": 60.0, "medium": 10.0, "high": 8.0}
URGENCY_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3, "essential": 4, "critical": 5}

# "why do you keep doing that", "this is so annoying", "leave me alone", "stop nagging" ...
COMPLAINT = re.compile(
    r"\b(why do you keep|stop (doing that|nagging|bugging|commenting|it)|so annoying|(this|that|you)('re| are| is)? (so |really |actually )*(annoying|irritating)"
    r"|leave me alone|get off my back|you('re| are) (so )?(loud|much)|chill out|relax|not helpful|go away)\b",
    re.I,
)


@dataclass
class BehaviorDecision:
    event: str
    speak: bool
    urgency: str
    reason: str
    rules: list[str] = field(default_factory=list)
    category: str | None = None  # fixed line used when no generated line is ready
    generate: bool = False  # try a context-specific generated line first
    event_text: str | None = None  # how the event is described to the language model
    escalation: int = 0
    physical: str | None = None  # what happened to a physical request, if any
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    at: float = field(default_factory=time.time)
    # Filled in by the runtime once it acted on the decision.
    said: str | None = None
    source: str | None = None  # generated | prepared | cached | template | silent
    extra_numbers: list[int] = field(default_factory=list)

    def to_payload(self) -> dict:
        return asdict(self)


class BehaviorEngine:
    def __init__(
        self,
        ctx: SessionContext,
        clock: Callable[[], float] = time.monotonic,
        user_talking: Callable[[], bool] = lambda: False,
    ):
        self.ctx = ctx
        self._clock = clock
        self.user_talking = user_talking
        self.history: deque[tuple[float, str, dict]] = deque(maxlen=200)
        self.decisions: deque[BehaviorDecision] = deque(maxlen=60)
        self.last_unprompted: float | None = None
        self.last_heads_up = 0.0
        self.camera_lost_since: float | None = None
        self.camera_loss_announced = False
        self.confirmed: deque[float] = deque(maxlen=50)
        self.on_decision: Callable[[BehaviorDecision], None] | None = None

    # ------------------------------------------------------------------ events
    def note(self, kind: str, **data) -> None:
        self.history.append((self._clock(), kind, data))

    def reset_session(self) -> None:
        self.confirmed.clear()
        self.camera_lost_since = None
        self.camera_loss_announced = False

    def user_turn(self, text: str) -> bool:
        """Record a user turn; True if it objects to the robot's remarks (backs off)."""
        self.note("user_turn", text=text[:120])
        if COMPLAINT.search(text or ""):
            self.ctx.note_complaint()
            self.note("complaint", text=text[:120])
            return True
        return False

    # ------------------------------------------------------------------ helpers
    def _multiplier(self) -> float:
        return 2.0 if self.ctx.complaints_within(COMPLAINT_WINDOW_S) else 1.0

    def _gap_ok(self, urgency: str, rules: list[str]) -> bool:
        if self.last_unprompted is None or urgency in ("essential", "critical"):
            return True
        gap = GAP_S.get(urgency, 60.0) * self._multiplier()
        since = self._clock() - self.last_unprompted
        if since < gap:
            rules.append(f"B4 last unprompted remark {since:.0f}s ago (< {gap:.0f}s for {urgency})")
            return False
        return True

    def _record(self, d: BehaviorDecision) -> BehaviorDecision:
        self.decisions.append(d)
        if d.speak:
            self.last_unprompted = self._clock()
        if self.on_decision is not None:
            self.on_decision(d)
        return d

    def _silent(self, event: str, reason: str, rules: list[str], urgency: str = "none", **kw) -> BehaviorDecision:
        return self._record(BehaviorDecision(event, False, urgency, reason, rules, **kw))

    def _boundaries(self, event: str, urgency: str, rules: list[str], **kw) -> BehaviorDecision | None:
        """Quiet mode and the user talking: the same for every unprompted remark."""
        if urgency not in ("essential", "critical") and self.ctx.quiet():
            rules.append("B5 quiet mode -> noted, not said")
            return self._silent(event, "Quiet mode: noted on the dashboard, not said aloud.", rules, urgency, **kw)
        if urgency != "critical" and self.user_talking():
            rules.append("B5 the user is talking -> do not talk over them")
            return self._silent(event, "The user is talking; not interrupting.", rules, urgency, **kw)
        return None

    def recent_confirmed(self) -> int:
        now = self._clock()
        return sum(1 for t in self.confirmed if now - t <= ESCALATION_WINDOW_S)

    # ------------------------------------------------------------------ decisions
    def possible_reach(self, *, running: bool, monitoring: bool, confidence: float | None, last_intervention: float | None) -> BehaviorDecision:
        """A hand heading for the phone, not confirmed. A heads-up at most."""
        rules = [f"input: possible reach (confidence {'n/a' if confidence is None else f'{confidence:.2f}'})", "B1 possible reach: not counted as a distraction"]
        ev = "possible_reach"
        self.note(ev, confidence=confidence)
        if not running:
            rules.append("no running session -> nothing to protect")
            return self._silent(ev, "No running session.", rules)
        if not monitoring:
            rules.append("state is not monitoring -> quiet")
            return self._silent(ev, "Already handling a reach (or not monitoring).", rules)
        if confidence is not None and confidence < POSSIBLE_REACH_MIN_CONF:
            rules.append(f"B1 confidence {confidence:.2f} < {POSSIBLE_REACH_MIN_CONF} -> quiet")
            return self._silent(ev, "Too uncertain to say anything.", rules)
        now = self._clock()
        if now - self.last_heads_up < HEADS_UP_GAP_S:
            rules.append(f"B1 heads-up {now - self.last_heads_up:.0f}s ago (< {HEADS_UP_GAP_S:.0f}s) -> quiet")
            return self._silent(ev, "Already gave a heads-up recently.", rules, "low")
        if last_intervention is not None and now - last_intervention < HEADS_UP_GAP_S:
            rules.append(f"B4 intervention {now - last_intervention:.0f}s ago -> quiet")
            return self._silent(ev, "Spoke about the phone moments ago.", rules, "low")
        blocked = self._boundaries(ev, "low", rules)
        if blocked is not None:
            return blocked
        self.last_heads_up = now
        rules.append("B1 light heads-up (fixed line, not an accusation)")
        return self._record(BehaviorDecision(ev, True, "low", "A hand is heading for the phone: a light heads-up, not counted.", rules, category="reach_warning"))

    def confirmed_reach(self, *, policy, counted: bool, elapsed_s: float, physical: str | None = None) -> BehaviorDecision:
        """A confirmed reach, after the motion policy decided (``policy`` is its Decision) and,
        if it asked for a retreat, after the safety controller answered (``physical``)."""
        ev = "confirmed_reach"
        now = self._clock()
        if counted:
            self.confirmed.append(now)
        n = self.recent_confirmed()
        rules = [f"input: confirmed reach; {n} in the last {ESCALATION_WINDOW_S / 60:.0f} min", *(f"policy: {r}" for r in policy.rules[1:])]
        if physical is None and policy.action == "voice_only":
            physical = "no motion requested: " + policy.explanation
        self.note(ev, recent=n, counted=counted)
        if not policy.speak or not policy.category:
            return self._silent(ev, policy.explanation or "The motion policy chose not to react.", rules, "medium", physical=physical, escalation=n)
        urgency = "high" if n >= 2 else "medium"
        blocked = self._boundaries(ev, urgency, rules, physical=physical, escalation=n)
        if blocked is not None:
            return blocked
        if self.ctx.complaints_within(COMPLAINT_WINDOW_S) >= 2 and n < 3:
            rules.append("B4 the user objected twice recently -> only repeated reaches get a remark")
            return self._silent(ev, "The user asked for fewer remarks; this one is noted only.", rules, urgency, physical=physical, escalation=n)
        if n >= 3:
            rules.append(f"B2 escalation level 3: reach #{n} in {ESCALATION_WINDOW_S / 60:.0f} min")
        elif n == 2:
            rules.append("B2 escalation level 2: second reach in the window")
        else:
            rules.append("B2 first reach in the window")
        when = "less than 3 minutes into the session" if elapsed_s < 180 else f"{int(elapsed_s // 60)} minutes into the session"
        times = {1: "the first time", 2: "the second time", 3: "the third time"}.get(n, f"reach number {n}")
        text = f"The user's hand reached for the phone and the camera confirmed it, {when}. That is {times} in the last ten minutes."
        if self.ctx.complaints_within(COMPLAINT_WINDOW_S):
            text += " The user recently objected to your remarks: keep it short and light."
        return self._record(
            BehaviorDecision(ev, True, urgency, f"Confirmed reach #{n} in the window: react ({urgency}).", rules, category=policy.category,
                             generate=True, event_text=text, escalation=n, physical=physical, extra_numbers=[n])
        )

    def focus_streak(self, *, running: bool, streak_s: float) -> BehaviorDecision | None:
        """Praise after a real streak: the camera sees focus now and no reach for the streak."""
        ev = "focus_streak"
        rules = [f"input: camera sees focus; {streak_s / 60:.0f} min without a reach"]
        if not running:
            return None
        blocked = self._boundaries(ev, "low", rules)
        if blocked is not None:
            return blocked
        if not self._gap_ok("low", rules):
            return self._silent(ev, "Spoke recently; praise can wait.", rules, "low")
        minutes = int(streak_s // 60)
        rules.append("B3 praise a real streak once")
        return self._record(
            BehaviorDecision(ev, True, "low", f"{minutes} focused minutes without a reach: praise once.", rules, category="focus_streak",
                             generate=True, event_text=f"The camera has seen the user focused, with no reach for the phone, for {minutes} minutes straight.",
                             extra_numbers=[minutes])
        )

    def camera_lost(self) -> None:
        self.note("camera_lost")
        if self.camera_lost_since is None:
            self.camera_lost_since = self._clock()
            self.camera_loss_announced = False

    def camera_back(self, *, running: bool) -> BehaviorDecision | None:
        self.note("camera_back")
        was = self.camera_lost_since
        announced = self.camera_loss_announced
        self.camera_lost_since = None
        self.camera_loss_announced = False
        if was is None:
            return None
        gone = self._clock() - was
        rules = [f"input: camera back after {gone:.1f}s"]
        if not announced:
            rules.append(f"B3 loss shorter than {CAMERA_LOSS_HOLD_S:.0f}s or never announced -> no news")
            return self._silent("camera_back", "A blip; nothing was said about it, nothing to say now.", rules)
        if not running:
            return self._silent("camera_back", "No running session.", rules)
        blocked = self._boundaries("camera_back", "low", rules)
        if blocked is not None:
            return blocked
        rules.append("B3 announced loss is over -> say so once")
        return self._record(BehaviorDecision("camera_back", True, "low", "The camera is back after an announced loss.", rules, category="camera_back"))

    def tick(self, *, running: bool) -> list[BehaviorDecision]:
        out = []
        now = self._clock()
        if self.camera_lost_since is not None and not self.camera_loss_announced and now - self.camera_lost_since >= CAMERA_LOSS_HOLD_S:
            self.camera_loss_announced = True
            rules = [f"input: camera gone for {now - self.camera_lost_since:.1f}s", f"B3 held >= {CAMERA_LOSS_HOLD_S:.0f}s -> real loss"]
            if not running:
                out.append(self._silent("camera_lost", "No running session.", rules))
            else:
                blocked = self._boundaries("camera_lost", "medium", rules)
                if blocked is not None:
                    out.append(blocked)
                else:
                    rules.append("say once that the phone is not being watched")
                    out.append(self._record(BehaviorDecision("camera_lost", True, "medium", "Camera lost: the user should know nothing is watching the phone.", rules, category="camera_lost")))
        return out

    def milestone(self, name: str, *, policy) -> BehaviorDecision:
        """Time milestones (the motion policy's decide_milestone already applied its rules)."""
        ev = f"milestone_{name}"
        urgency = "essential" if name in ("time_up", "complete") else "low"
        rules = [f"input: milestone {name}", *(f"policy: {r}" for r in policy.rules[1:])]
        self.note(ev)
        if not policy.speak:
            return self._silent(ev, policy.explanation, rules, urgency)
        blocked = self._boundaries(ev, urgency, rules)
        if blocked is not None:
            return blocked
        return self._record(BehaviorDecision(ev, True, urgency, policy.explanation, rules, category=policy.category))

    def essential(self, event: str, category: str, reason: str) -> BehaviorDecision:
        """Robot lost mid-session and similar: always said (the user needs to know)."""
        self.note(event)
        rules = [f"input: {event}", "essential: said even in quiet mode"]
        if self.user_talking():
            rules.append("said after a short delay; the user was talking")
        return self._record(BehaviorDecision(event, True, "essential", reason, rules, category=category))

    def record_external(self, event: str, speak: bool, reason: str, rules: list[str], urgency: str = "low", **kw) -> BehaviorDecision:
        """Decisions made elsewhere (e.g. the focus-distraction nudge) still show up here."""
        return self._record(BehaviorDecision(event, speak, urgency, reason, rules, **kw))

    # ------------------------------------------------------------------ anticipation
    def next_reach_text(self, elapsed_s: float) -> tuple[str, int]:
        """The event description for a line prepared before the next confirmed reach."""
        n = self.recent_confirmed() + 1
        when = "less than 3 minutes into the session" if elapsed_s < 180 else f"{int(elapsed_s // 60)} minutes into the session"
        times = {1: "the first time", 2: "the second time", 3: "the third time"}.get(n, f"reach number {n}")
        text = f"The user's hand reached for the phone and the camera confirmed it, {when}. That is {times} in the last ten minutes."
        if self.ctx.complaints_within(COMPLAINT_WINDOW_S):
            text += " The user recently objected to your remarks: keep it short and light."
        return text, n

    # ------------------------------------------------------------------ dashboard
    def snapshot(self) -> dict:
        now = self._clock()
        return {
            "decisions": [d.to_payload() for d in reversed(self.decisions)][:25],
            "confirmed_recent": self.recent_confirmed(),
            "last_unprompted_s": None if self.last_unprompted is None else round(now - self.last_unprompted, 1),
            "gap_multiplier": self._multiplier(),
            "camera_lost_for_s": None if self.camera_lost_since is None else round(now - self.camera_lost_since, 1),
            "rules": {"escalation_window_s": ESCALATION_WINDOW_S, "heads_up_gap_s": HEADS_UP_GAP_S,
                      "camera_loss_hold_s": CAMERA_LOSS_HOLD_S, "gap_s": GAP_S, "possible_reach_min_confidence": POSSIBLE_REACH_MIN_CONF},
        }
