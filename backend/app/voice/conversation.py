"""What the robot says.

Lines come from the personality library (personality.py). Any number in a line is
filled from ``SessionFacts`` - recorded session state - and lines that would need a
fact we do not have are skipped. Status, time left and next step are built directly
from the facts. If the optional LLM is enabled it may rephrase, but its output goes
through ``grounding_violations`` and is discarded if it mentions a number that is not
in the facts.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

from .personality import INTENTS, ORDINALS, PersonalityEngine, Rendered, Style


@dataclass
class SessionFacts:
    session_active: bool = False
    paused: bool = False
    task: str | None = None
    elapsed_s: float = 0.0
    remaining_s: float = 0.0
    planned_s: float = 0.0
    completed_units: int = 0
    target_units: int | None = None
    unit_label: str = "tasks"
    reaches_confirmed: int = 0
    vision_available: bool = False
    vision_note: str = "camera_off"  # why vision is unavailable: camera_off | marker_lost
    robot_moves: int = 0
    presence_ratio: float | None = None

    def numbers(self) -> set[int]:
        """Every integer a grounded sentence about this session may contain."""
        nums = {
            int(self.elapsed_s // 60), round(self.elapsed_s / 60), int(self.remaining_s // 60),
            round(self.remaining_s / 60), int(self.planned_s // 60), round(self.planned_s / 60),
            int(self.elapsed_s), int(self.remaining_s), self.completed_units, self.reaches_confirmed, self.robot_moves,
            int(-(-self.remaining_s // 60)),
        }
        if self.target_units is not None:
            nums |= {self.target_units, max(0, self.target_units - self.completed_units)}
        if self.presence_ratio is not None:
            nums.add(round(self.presence_ratio * 100))
        return nums


def fmt_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        s = int(round(seconds))
        return "less than a minute" if s < 10 else f"{s} seconds"
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    h, m = divmod(minutes, 60)
    hours = f"{h} hour{'s' if h != 1 else ''}"
    return hours if m == 0 else f"{hours} {m} minute{'s' if m != 1 else ''}"


def plural(n: int, word: str) -> str:
    """``word`` is given in plural form: plural(1, 'reaches') -> '1 reach'."""
    if n == 1:
        if word.endswith(("ches", "shes", "xes", "sses")):
            return f"{n} {word[:-2]}"
        return f"{n} {word[:-1] if word.endswith('s') else word}"
    return f"{n} {word if word.endswith('s') else word + 's'}"


def as_style(style) -> Style:
    """Accept a Style, a mode name (including the legacy chill/coach/chaos) or None."""
    if isinstance(style, Style):
        return style
    if isinstance(style, str):
        return Style(mode=LEGACY_MODES.get(style, style))
    return Style()


LEGACY_MODES = {"coach": "roast", "chaos": "roast"}
GROUNDED = ("status", "time_left", "next_step")


@dataclass
class ResponseComposer:
    rng: random.Random = field(default_factory=random.Random)
    engine: PersonalityEngine | None = None

    def __post_init__(self) -> None:
        if self.engine is None:
            self.engine = PersonalityEngine(rng=self.rng)

    def render(self, category: str, facts: SessionFacts, style=None, *, prefer=None, **extra) -> Rendered:
        style = as_style(style)
        if category in GROUNDED:
            text = getattr(self, category)(facts)
            tag = self.engine.status_tag(style) if category == "status" and facts.session_active else None
            intent = INTENTS[category]
            return Rendered(f"{text} {tag}" if tag else text, category, intent.name, intent.priority, style.pool)
        return self.engine.render(category, self._slots(facts, **extra), style, prefer=prefer)

    def compose(self, category: str, facts: SessionFacts, style=None, **extra) -> str:
        return self.render(category, facts, style, **extra).text

    def _slots(self, f: SessionFacts, **extra) -> dict:
        units_left = None if f.target_units is None else max(0, f.target_units - f.completed_units)
        slots = {
            "planned": fmt_duration(f.planned_s).capitalize() if f.planned_s else None,
            "elapsed": fmt_duration(f.elapsed_s),
            "remaining": fmt_duration(f.remaining_s) if f.session_active else None,
            "task": f.task or "the work",
            "reaches": plural(f.reaches_confirmed, "reaches") if f.reaches_confirmed else None,
            "reaches_n": f.reaches_confirmed or None,
            "attempt_ordinal": ORDINALS.get(f.reaches_confirmed),
            "completed_phrase": plural(f.completed_units, f.unit_label) + (f" of {f.target_units}" if f.target_units else "") + " down"
            if f.completed_units
            else None,
            "units_left_sentence": (
                f"{plural(units_left, f.unit_label)} to go." if units_left else "Target hit."
            )
            if units_left is not None
            else None,
            "summary": self.summary(f),
        }
        for k, v in extra.items():
            slots[k] = v
        return slots

    # ----------------------------------------------------- grounded answers
    def status(self, f: SessionFacts) -> str:
        if not f.session_active:
            return "No session running. Say 'lock in' to start one."
        parts = [f"You're {fmt_duration(f.elapsed_s)} in"]
        if f.target_units:
            parts.append(f"you've finished {f.completed_units} of {plural(f.target_units, f.unit_label)}")
        elif f.completed_units:
            parts.append(f"you've finished {plural(f.completed_units, f.unit_label)}")
        if f.vision_available:
            if f.reaches_confirmed:
                parts.append(f"I've caught {plural(f.reaches_confirmed, 'phone reaches')}")
            else:
                parts.append("no phone reaches so far")
        elif f.vision_note == "marker_lost":
            parts.append("I can't see the robot's marker, so I'm not counting reaches right now")
        else:
            parts.append("my camera is off, so I'm not counting reaches")
        sentence = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + ", and " + parts[-1]
        tail = " Paused right now." if f.paused else ""
        return sentence + "." + tail

    def time_left(self, f: SessionFacts) -> str:
        if not f.session_active:
            return "No session running."
        if f.remaining_s <= 0:
            return "Time's up."
        prefix = "Paused with " if f.paused else ""
        return f"{prefix}{fmt_duration(f.remaining_s)} left." if prefix else f"{fmt_duration(f.remaining_s).capitalize()} left."

    def next_step(self, f: SessionFacts) -> str:
        if not f.session_active:
            return "Start a session. Say 'lock in, 25 minutes'."
        if f.target_units:
            left = f.target_units - f.completed_units
            if left > 0:
                return f"{plural(left, f.unit_label).capitalize()} more and you're free."
            return f"You hit your target of {plural(f.target_units, f.unit_label)}. Bank the win or keep going."
        return f"Keep going on {f.task or 'the work'}. {fmt_duration(f.remaining_s).capitalize()} left."

    def summary(self, f: SessionFacts) -> str:
        bits = [f"{fmt_duration(f.elapsed_s)} focused"]
        if f.completed_units:
            bits.append(plural(f.completed_units, f.unit_label) + " done")
        if f.vision_available:
            bits.append(plural(f.reaches_confirmed, "phone reaches"))
        return ", ".join(bits) + "."


_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
    "forty": 40, "fifty": 50, "sixty": 60,
}


def grounding_violations(text: str, facts: SessionFacts) -> list[int]:
    """Numbers in ``text`` that are not supported by ``facts``. Empty list = grounded."""
    allowed = facts.numbers()
    found: list[int] = [int(n) for n in re.findall(r"\b\d+\b", text)]
    lowered = text.lower().replace("-", " ")
    for word, value in _NUMBER_WORDS.items():
        if re.search(rf"\b{word}\b", lowered):
            # 'one' is too common as a pronoun ("that one") to treat as a claim.
            if word != "one":
                found.append(value)
    return sorted({n for n in found if n not in allowed})
