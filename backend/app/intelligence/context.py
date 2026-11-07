"""What LOCKED IN knows about the session, as the language model and the dashboard see it.

Bounded and explicit: a few short rings, never an ever-growing transcript. Two kinds of
facts are kept apart and labelled that way in the prompt:

* observations: produced by the robot's own sensors, timers and actions (a confirmed
  reach, the camera dropping, a line it spoke, a session it started). Only the runtime
  records these, from real events, so the model can state them as fact;
* user statements: what the user said ("I have a calculus test tomorrow"). Claims, kept
  verbatim (capped), never presented as something the robot saw.

Nothing here is written to disk and no audio or video is kept: transcripts live in memory
for the session (the dashboard shows them) and are dropped with it.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

MAX_TURNS = 10  # 5 exchanges
MAX_OBSERVATIONS = 24
MAX_STATEMENTS = 8
MAX_TEXT = 200
QUIET_DEFAULT_MIN = 10.0


@dataclass
class Observation:
    mono: float
    at: float
    kind: str  # reach_confirmed | reach_possible | camera_lost | camera_back | robot_lost | robot_back | ...
    text: str  # how the prompt and the dashboard say it: "hand reached for the phone (confirmed)"
    data: dict = field(default_factory=dict)


@dataclass
class Statement:
    mono: float
    at: float
    text: str  # the user's own words, capped


@dataclass
class Turn:
    mono: float
    at: float
    role: str  # "user" | "robot"
    text: str
    source: str = ""  # user: voice | typed. robot: generated | cached | template | fallback
    proactive: bool = False  # robot spoke without being spoken to


@dataclass
class Goal:
    text: str
    source: str  # "user_said" (voice / typed) or "operator" (dashboard)
    mono: float
    at: float


def clean(text: str, limit: int = MAX_TEXT) -> str:
    text = re.sub(r"\s+", " ", (text or "").replace('"', "'")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def ago(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 45:
        return "just now"
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f"{max(1, minutes)} min ago"
    h, m = divmod(minutes, 60)
    return f"{h} h {m} min ago" if m else f"{h} h ago"


class SessionContext:
    """Bounded memory of the current session (and the conversation around it)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time):
        self._clock = clock
        self._wall = wall
        self.turns: deque[Turn] = deque(maxlen=MAX_TURNS)
        self.observations: deque[Observation] = deque(maxlen=MAX_OBSERVATIONS)
        self.statements: deque[Statement] = deque(maxlen=MAX_STATEMENTS)
        self.goal: Goal | None = None
        self.quiet_until: float | None = None  # monotonic; None = not quiet
        self.quiet_reason: str | None = None
        self.complaints: deque[float] = deque(maxlen=6)  # when the user objected to the robot's remarks
        self.last_robot_line: Turn | None = None
        self.session_id: str | None = None

    # ------------------------------------------------------------------ lifecycle
    def new_session(self, session_id: str | None) -> None:
        """A new session keeps the conversation (the user may have set it up by talking) but
        starts a fresh observation log; boundaries the user set still hold."""
        self.session_id = session_id
        self.observations.clear()

    def clear(self) -> None:
        self.turns.clear()
        self.observations.clear()
        self.statements.clear()
        self.goal = None
        self.quiet_until = None
        self.quiet_reason = None
        self.complaints.clear()
        self.last_robot_line = None
        self.session_id = None

    # ------------------------------------------------------------------ recording
    def observe(self, kind: str, text: str, **data) -> Observation:
        o = Observation(self._clock(), self._wall(), kind, clean(text), data)
        self.observations.append(o)
        return o

    def user_said(self, text: str, source: str = "voice") -> None:
        text = clean(text)
        if not text:
            return
        now, wall = self._clock(), self._wall()
        self.turns.append(Turn(now, wall, "user", text, source))
        self.statements.append(Statement(now, wall, text))

    def robot_said(self, text: str, source: str, proactive: bool = False) -> None:
        text = clean(text)
        if not text:
            return
        t = Turn(self._clock(), self._wall(), "robot", text, source, proactive)
        self.turns.append(t)
        self.last_robot_line = t

    def set_goal(self, text: str, source: str = "user_said") -> None:
        text = clean(text, 80)
        if text:
            self.goal = Goal(text, source, self._clock(), self._wall())

    def set_quiet(self, minutes: float | None, reason: str = "user asked") -> None:
        """minutes None = until the user says otherwise."""
        self.quiet_until = float("inf") if minutes is None else self._clock() + max(0.5, minutes) * 60
        self.quiet_reason = reason

    def end_quiet(self) -> None:
        self.quiet_until = None
        self.quiet_reason = None

    def quiet(self) -> bool:
        if self.quiet_until is None:
            return False
        if self._clock() >= self.quiet_until:
            self.end_quiet()
            return False
        return True

    def note_complaint(self) -> None:
        self.complaints.append(self._clock())

    def complaints_within(self, seconds: float) -> int:
        now = self._clock()
        return sum(1 for t in self.complaints if now - t <= seconds)

    # ------------------------------------------------------------------ queries
    def count(self, kind: str, within_s: float | None = None) -> int:
        now = self._clock()
        return sum(1 for o in self.observations if o.kind == kind and (within_s is None or now - o.mono <= within_s))

    def last(self, kind: str) -> Observation | None:
        return next((o for o in reversed(self.observations) if o.kind == kind), None)

    def recent_robot_lines(self, n: int = 4) -> list[str]:
        return [t.text for t in self.turns if t.role == "robot"][-n:]

    # ------------------------------------------------------------------ prompt
    def render_prefix(
        self,
        *,
        style_line: str,
        session_line: str,
        camera_line: str,
        allowed_actions: list[str],
        exclude_text: str | None = None,
        anchor: tuple[float, float] | None = None,
    ) -> str:
        """Everything in the context block except the line being answered. It does not depend
        on what the user is about to say, so the model can read it while they are still talking
        (see LocalLLM.prefill). ``exclude_text``: the user's line, once it has been recorded.
        ``anchor`` (monotonic, wall): render the times as of that moment (the user started
        talking), so the text read early and the text read after Whisper are the same."""
        now, wall = anchor if anchor is not None else (self._clock(), self._wall())
        lines = [f"STYLE: {style_line}", f"NOW: {time.strftime('%H:%M', time.localtime(wall))}. {session_line}"]
        if self.goal is not None:
            label = "GOAL (user said)" if self.goal.source == "user_said" else "GOAL (set on the dashboard)"
            lines.append(f"{label}: {self.goal.text}")
        lines.append(f"CAMERA: {camera_line}")
        # Only what was known when the user started talking; anything newer goes after the
        # prefix (render_suffix), so the prefilled text stays valid.
        obs = [o for o in self.observations if anchor is None or o.mono <= now][-8:]
        lines.append("OBSERVED this session:" if obs else "OBSERVED this session: nothing yet.")
        lines += [f"- {ago(now - o.mono)}: {o.text}" for o in obs]
        excluded = clean(exclude_text) if exclude_text is not None else None
        turns = [t for t in self.turns if anchor is None or t.mono <= now or (t.role == "user" and t.text == excluded)]
        statements = list(self.statements)
        if excluded is not None and turns and turns[-1].role == "user" and turns[-1].text == excluded:
            # The line being answered is shown at the end, once. It is taken out *before* the
            # window is cut, so the window is the same as when the user started talking (the
            # prefilled prefix must match what the model reads after Whisper).
            turns = turns[:-1]
            if statements and statements[-1].text == excluded:
                statements = statements[:-1]
        convo = turns[-6:]
        # Older things the user said that have scrolled out of the conversation window.
        shown = {t.mono for t in convo if t.role == "user"}
        older = [s for s in statements if s.mono not in shown][-3:]
        if older:
            lines.append("USER SAID earlier:")
            lines += [f'- {ago(now - s.mono)}: "{s.text}"' for s in older]
        boundaries = []
        if self.quiet():
            until = "until the user says otherwise" if self.quiet_until == float("inf") else f"for {ago(-(self.quiet_until - now)).replace(' ago', '')} more"
            boundaries.append(f"quiet mode {until} (no unprompted remarks; answer only when spoken to, briefly)")
        if sum(1 for t in self.complaints if t <= now and now - t <= 600):
            boundaries.append("the user objected to your remarks recently: fewer, kinder interruptions")
        lines.append("BOUNDARIES: " + ("; ".join(boundaries) if boundaries else "none"))
        recent = self.recent_robot_lines(3)
        if recent:
            lines.append("RECENT LINES: " + " | ".join(f'"{t}"' for t in recent))
        if convo:
            lines.append("CONVERSATION:")
            lines += [f"- {ago(now - t.mono)}, {'user' if t.role == 'user' else 'you'}: {t.text}" for t in convo]
        lines.append("ALLOWED ACTIONS: " + ", ".join(allowed_actions + ["none"]))
        return "\n".join(lines)  # no trailing newline: the suffix starts with one

    def render_suffix(self, *, user_text: str | None = None, event: str | None = None, hint: str | None = None, anchor: tuple[float, float] | None = None) -> str:
        out = ""
        if anchor is not None:
            late = [o for o in self.observations if o.mono > anchor[0] and o.kind != "complaint"]
            if late:
                out += "\nALSO OBSERVED while the user was talking:" + "".join(f"\n- just now: {o.text}" for o in late)
            said = [t for t in self.turns if t.mono > anchor[0] and t.role == "robot"]
            if said:  # a line that was still playing (or was cut off) when the user started
                out += "\nYOU WERE SAYING as the user started talking:" + "".join(f"\n- {t.text}" for t in said)
        out += f"\nPARSER HINT: {hint}" if hint else ""
        if user_text is not None:
            return out + f'\n\nUSER JUST SAID: "{clean(user_text)}"'
        return out + f'\n\nEVENT: {event}\nNobody spoke to you. Say one short line about this event (action "none").'

    def render(
        self,
        *,
        style_line: str,
        session_line: str,
        camera_line: str,
        allowed_actions: list[str],
        user_text: str | None = None,
        event: str | None = None,
        hint: str | None = None,
        anchor: tuple[float, float] | None = None,
    ) -> str:
        """The per-turn context block (see prompting.PERSONA for how the model reads it)."""
        prefix = self.render_prefix(
            style_line=style_line, session_line=session_line, camera_line=camera_line, allowed_actions=allowed_actions, exclude_text=user_text,
            anchor=anchor,
        )
        return prefix + self.render_suffix(user_text=user_text, event=event, hint=hint, anchor=anchor)

    # ------------------------------------------------------------------ dashboard
    def snapshot(self) -> dict:
        now = self._clock()
        quiet = self.quiet()
        return {
            "goal": None if self.goal is None else {"text": self.goal.text, "source": self.goal.source, "at": self.goal.at},
            "quiet": quiet,
            "quiet_left_s": None if not quiet or self.quiet_until == float("inf") else round(self.quiet_until - now, 1),
            "quiet_reason": self.quiet_reason if quiet else None,
            "complaints_10min": self.complaints_within(600),
            "observations": [{**asdict(o), "age_s": round(now - o.mono, 1)} for o in reversed(self.observations)],
            "statements": [{"text": s.text, "at": s.at, "age_s": round(now - s.mono, 1)} for s in reversed(self.statements)],
            "turns": [{**asdict(t), "age_s": round(now - t.mono, 1)} for t in reversed(self.turns)],
            "limits": {"turns": MAX_TURNS, "observations": MAX_OBSERVATIONS, "statements": MAX_STATEMENTS},
        }
