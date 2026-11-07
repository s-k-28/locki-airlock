"""Typed action requests: how the language model (or the behaviour engine) asks the robot
to do something. A request is never an order.

The model writes one JSON object. ``parse_reply`` turns it into a ``ModelReply``;
``validate`` checks the action name against a closed list, the arguments against typed,
range-checked models (unknown fields are refused), and the name against the actions the
current state allows. Anything else (an unknown name, a motor command, a speed, a
malformed object) is refused and logged; it can never reach the robot. Physical requests
(retreat, return) are only *requests*: the runtime passes them to the safety controller,
which approves or refuses them exactly like a request from the dashboard, and today it
refuses all of them (motor power unverified, patch not flashed, tracking unverified).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ActionName(StrEnum):
    START_SESSION = "start_session"
    SET_GOAL = "set_goal"
    EXTEND_SESSION = "extend_session"
    PAUSE_SESSION = "pause_session"
    RESUME_SESSION = "resume_session"
    END_SESSION = "end_session"
    SET_QUIET = "set_quiet"
    SET_STYLE = "set_style"
    RELEASE_PHONE = "release_phone"
    REQUEST_RETREAT = "request_retreat"
    REQUEST_RETURN = "request_return"
    SET_LED = "set_led"
    STOP = "stop"
    NONE = "none"


# Need the safety controller's approval (and today never get it).
PHYSICAL = frozenset({ActionName.REQUEST_RETREAT, ActionName.REQUEST_RETURN})


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArgs(_Args):
    pass


class StartSessionArgs(_Args):
    minutes: int | None = Field(default=None, ge=5, le=180)
    goal: str | None = Field(default=None, max_length=80)


class GoalArgs(_Args):
    goal: str = Field(min_length=2, max_length=80)


class MinutesArgs(_Args):
    minutes: int = Field(ge=1, le=60)


class QuietArgs(_Args):
    minutes: int = Field(default=10, ge=1, le=120)


class StyleArgs(_Args):
    style: Literal["chill", "roast", "serious"]


class LedArgs(_Args):
    color: str = Field(max_length=20)


ARGS: dict[ActionName, type[_Args]] = {
    ActionName.START_SESSION: StartSessionArgs,
    ActionName.SET_GOAL: GoalArgs,
    ActionName.EXTEND_SESSION: MinutesArgs,
    ActionName.SET_QUIET: QuietArgs,
    ActionName.SET_STYLE: StyleArgs,
    ActionName.SET_LED: LedArgs,
}


@dataclass
class ActionRequest:
    name: ActionName
    args: dict = field(default_factory=dict)
    origin: str = "llm"  # llm | behavior | voice_rule

    def to_payload(self) -> dict:
        return {"name": self.name.value, "args": self.args, "origin": self.origin, "physical": self.name in PHYSICAL}


@dataclass
class ModelReply:
    """What the model wrote, parsed. ``action`` is None when it named none (or was refused);
    ``problems`` says why anything was refused."""

    say: str = ""
    action: ActionRequest | None = None
    raw: str = ""
    problems: list[str] = field(default_factory=list)
    requested: str | None = None  # the action name the model wrote, even if refused


_JSON = re.compile(r"\{.*\}", re.S)


def _coerce_ints(args: dict) -> dict:
    """Models write "minutes": "40" or 40.0 now and then; accept clean whole numbers only."""
    out = {}
    for k, v in args.items():
        if k == "minutes" and isinstance(v, str) and re.fullmatch(r"\s*\d{1,3}\s*", v):
            v = int(v)
        elif k == "minutes" and isinstance(v, float) and v.is_integer():
            v = int(v)
        out[k] = v
    return out


_WH = re.compile(r"\b(what|who|where|when|why|how|which)\b(.*)", re.I | re.S)


def goal_from_question(goal: str, user_text: str) -> bool:
    """True when a proposed goal is just the topic of a question the user asked ("what's the
    capital of Australia?" -> "capital of Australia"): a goal is what they said they work on."""
    m = _WH.search(user_text or "")
    if not m:
        return False
    asked = set(re.findall(r"[a-z]+", m.group(2).lower()))
    words = [w for w in re.findall(r"[a-z]+", (goal or "").lower()) if len(w) > 2]
    return bool(words) and sum(w in asked for w in words) / len(words) >= 0.6


def validate(name: str, args, allowed: set[ActionName] | frozenset[ActionName], origin: str = "llm") -> tuple[ActionRequest | None, str | None]:
    """(request, None) if acceptable, else (None, reason). Never raises."""
    try:
        action = ActionName(str(name).strip().lower())
    except ValueError:
        return None, f"unknown action {str(name)[:40]!r}"
    if action == ActionName.NONE:
        return None, None
    if action not in allowed:
        return None, f"{action.value} is not allowed right now"
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return None, "args must be an object"
    model = ARGS.get(action, NoArgs)
    try:
        parsed = model.model_validate(_coerce_ints(args))
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ())) or "args"
        return None, f"bad args for {action.value}: {where}: {first.get('msg', 'invalid')}"
    clean = {k: v for k, v in parsed.model_dump().items() if v is not None}
    for key in ("goal",):
        if key in clean:
            clean[key] = re.sub(r"\s+", " ", str(clean[key])).strip(" .\"'")
    return ActionRequest(action, clean, origin), None


def parse_reply(text: str, allowed: set[ActionName] | frozenset[ActionName], origin: str = "llm") -> ModelReply:
    """Parse the model's JSON. Tolerates text around the object and a missing closing brace;
    never trusts anything it cannot validate."""
    out = ModelReply(raw=(text or "")[:2000])
    m = _JSON.search(text or "")
    blob = m.group(0) if m else (text or "").strip()
    data = None
    for candidate in (blob, blob + '"}', blob + "}"):
        try:
            data = json.loads(candidate)
            break
        except (json.JSONDecodeError, TypeError):
            continue
    if not isinstance(data, dict):
        out.problems.append("reply was not a JSON object")
        return out
    say = data.get("say")
    out.say = say.strip() if isinstance(say, str) else ""
    if not isinstance(say, str):
        out.problems.append("no 'say' text")
    name = data.get("action", "none")
    out.requested = str(name)[:40] if name is not None else None
    if name is None or (isinstance(name, str) and name.strip().lower() in ("", "none", "null")):
        return out
    extra = set(data) - {"action", "args", "say"}
    if extra:
        out.problems.append(f"ignored extra fields: {', '.join(sorted(extra)[:4])}")
    req, why = validate(name, data.get("args"), allowed, origin)
    if why:
        out.problems.append(why)
    out.action = req
    return out


class SayStream:
    """Pulls the spoken text out of the model's JSON while it is still being written, so the
    first sentence can go to the voice before the whole object is finished.

    Feed it the text deltas; ``sentences()`` returns complete sentences of the ``say`` value
    not yet returned. Only valid while ``say`` is the last key (the prompt asks for that order)."""

    def __init__(self) -> None:
        self.buf = ""
        self._emitted = 0

    def feed(self, delta: str) -> None:
        self.buf += delta

    def _say_so_far(self) -> tuple[str, bool]:
        m = re.search(r'"say"\s*:\s*"', self.buf)
        if not m:
            return "", False
        s, i, done = [], m.end(), False
        while i < len(self.buf):
            ch = self.buf[i]
            if ch == "\\" and i + 1 < len(self.buf):
                nxt = self.buf[i + 1]
                s.append({"n": " ", "t": " ", '"': '"', "\\": "\\", "/": "/"}.get(nxt, nxt))
                i += 2
                continue
            if ch == '"':
                done = True
                break
            s.append(ch)
            i += 1
        return "".join(s), done

    def head(self) -> str:
        """Everything before the "say" key (action and args), for early validation."""
        m = re.search(r'"say"\s*:', self.buf)
        return self.buf[: m.start()] if m else self.buf

    def sentences(self, final: bool = False) -> list[str]:
        text, done = self._say_so_far()
        pending = text[self._emitted :]
        out: list[str] = []
        while True:
            m = re.search(r"[.!?…]+[\"')\]]*\s+", pending)
            if not m:
                break
            sentence = pending[: m.end()].strip()
            if sentence:
                out.append(sentence)
            self._emitted += m.end()
            pending = pending[m.end() :]
        if (done or final) and pending.strip():
            out.append(pending.strip())
            self._emitted += len(pending)
        return out

    @property
    def said(self) -> str:
        return self._say_so_far()[0]
