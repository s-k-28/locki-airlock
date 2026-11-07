"""Deterministic intent classification for voice and typed commands.

Safety-critical intents (stop, emergency stop, pause, release) are matched with
plain rules - no network, no model. Order matters: more specific phrases come
first so "stop roasting me" is a tone change, not a robot stop. Independently of
the chosen intent, any standalone stop word sets ``stop_word`` and the runtime
halts motion: stopping is always safe, so it does not need certainty.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class IntentName(StrEnum):
    EMERGENCY_STOP = "emergency_stop"
    STOP_MOTION = "stop_motion"
    RELEASE = "release"
    PAUSE = "pause"
    RESUME = "resume"
    END_SESSION = "end_session"
    START_SESSION = "start_session"
    PROGRESS = "progress"
    TIME_LEFT = "time_left"
    STATUS = "status"
    NEXT_STEP = "next_step"
    HARDER = "harder"
    TONE_DOWN = "tone_down"
    MUTE = "mute"
    UNMUTE = "unmute"
    SET_PERSONALITY = "set_personality"
    SET_PROFANITY = "set_profanity"
    SERIOUS = "serious"
    RESET_TONE = "reset_tone"
    HELP = "help"
    RETURN_ROBOT = "return_robot"
    GREETING = "greeting"
    THANKS = "thanks"
    ABOUT = "about"
    # Milestone 5: privacy controls by voice (the dashboard has the same switches).
    CAMERA_OFF = "camera_off"
    CAMERA_ON = "camera_on"
    MIC_OFF = "mic_off"
    UNKNOWN = "unknown"


SAFETY_CRITICAL = frozenset(
    {IntentName.EMERGENCY_STOP, IntentName.STOP_MOTION, IntentName.RELEASE, IntentName.PAUSE, IntentName.END_SESSION}
)
MOVES_ROBOT = frozenset({IntentName.RETURN_ROBOT})


@dataclass
class Intent:
    name: IntentName
    confidence: float
    text: str
    raw: str
    slots: dict = field(default_factory=dict)
    addressed: bool = False
    stop_word: bool = False
    rule: str = ""
    # Movement commands only: why the request is self-contradictory ("don't come back",
    # "come back, no, go left"). A conflicting movement command is never executed.
    conflict: str | None = None

    @property
    def safety_critical(self) -> bool:
        return self.name in SAFETY_CRITICAL

    @property
    def moves_robot(self) -> bool:
        return self.name in MOVES_ROBOT

    def to_payload(self) -> dict:
        return {
            "intent": self.name.value,
            "confidence": round(self.confidence, 3),
            "text": self.text,
            "slots": self.slots,
            "addressed": self.addressed,
            "stop_word": self.stop_word,
            "safety_critical": self.safety_critical,
            "moves_robot": self.moves_robot,
            "rule": self.rule,
            "conflict": self.conflict,
        }


# ------------------------------------------------------------ normalisation
_CONTRACTIONS = {
    "i'm": "i am", "im": "i am", "what's": "what is", "whats": "what is", "let's": "let us", "lets": "let us",
    "how's": "how is", "hows": "how is", "that's": "that is", "thats": "that is", "don't": "do not",
    "can't": "cannot", "you're": "you are", "we're": "we are", "it's": "it is", "i've": "i have",
    "i'll": "i will", "gimme": "give me", "gotta": "got to", "wanna": "want to", "gonna": "going to",
}

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}


def words_to_numbers(text: str) -> str:
    """'twenty five minutes' -> '25 minutes'; 'one hundred' -> '100'."""
    tokens = text.split()
    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _UNITS or tok in _TENS:
            value = _TENS.get(tok, _UNITS.get(tok, 0))
            j = i + 1
            if tok in _TENS and j < len(tokens) and tokens[j] in _UNITS and _UNITS[tokens[j]] < 10:
                value += _UNITS[tokens[j]]
                j += 1
            if j < len(tokens) and tokens[j] == "hundred":
                value *= 100
                j += 1
            out.append(str(value))
            i = j
        else:
            out.append(tok)
            i += 1
    return " ".join(out)


_WAKE = re.compile(
    r"^(?:(?:hey|yo|ok|okay|alright|hi)\s+)?(?:locked in|lock in|lockedin|lockin|lock it in|locked and|lockton)\b\s*"
)


def normalise(raw: str) -> str:
    text = raw.lower().strip()
    text = text.replace("’", "'").replace("‘", "'")
    text = re.sub(r"[-–—_/]", " ", text)
    text = re.sub(r"[^a-z0-9' .,!?]", " ", text)
    words = []
    for w in text.split():
        core = w.strip(".,!?")
        trail = w[len(w.rstrip(".,!?")):]
        words.append(_CONTRACTIONS.get(core, core) + trail)
    text = " ".join(words)
    text = re.sub(r"[.,!?]+", " ", text)
    text = text.replace("'", "")
    text = re.sub(r"\s+", " ", text).strip()
    text = words_to_numbers(text)
    text = re.sub(r"\ban hour and a half\b", "90 minutes", text)
    text = re.sub(r"\bhalf an hour\b|\bhalf hour\b", "30 minutes", text)
    text = re.sub(r"\b(an|a) hour\b", "60 minutes", text)
    text = re.sub(r"\ba (minute|min)\b", "1 minute", text)
    return text


# ------------------------------------------------------------------ slots
_DURATION = re.compile(r"\b(\d{1,3}(?:\.\d)?)\s*(minutes?|mins?|hours?|hrs?)\b")
_UNIT_WORDS = r"problems?|questions?|exercises?|pages?|tasks?|chapters?|sections?|flashcards?|cards?|items?|essays?|paragraphs?|slides?"
_TARGET = re.compile(rf"\b(\d{{1,3}})\s+(?:more\s+)?({_UNIT_WORDS})\b")
_TASK_PATTERNS = [
    re.compile(
        r"\bto (?:finish|do|work on|study|complete|get through|review|write|read|practice|grind|knock out)\s+"
        r"(?:my |the |some )?(?P<task>[a-z][a-z0-9 ]{1,50}?)(?=\s+(?:let us go|go|now|please|ok|okay)\b|$)"
    ),
    re.compile(r"\b(?:working on|work on|studying|study for|studying for)\s+(?:my |the )?(?P<task>[a-z][a-z0-9 ]{1,50}?)(?=\s+(?:let us go|now|please)\b|$)"),
    re.compile(r"\bstart (?:a |an |my |the )?(?P<task>(?!new\b|focus\b|\d)[a-z][a-z ]{1,30}?) session\b"),
    re.compile(r"\b(?:for|on) (?:my |the )?(?P<task>(?!\d)[a-z][a-z0-9 ]{1,40}?)(?=\s+(?:let us go|now|please)\b|$)"),
]
_TASK_STOPWORDS = {"it", "this", "that", "something", "stuff", "a session", "session", "minutes", "focus", "a bit"}


def extract_duration_minutes(text: str) -> float | None:
    if "pomodoro" in text and not _DURATION.search(text):
        return 25.0
    m = _DURATION.search(text)
    if not m:
        return None
    value = float(m.group(1))
    if m.group(2).startswith(("hour", "hr")):
        value *= 60
    return value if 1 <= value <= 240 else None


def extract_target(text: str) -> tuple[int, str] | None:
    m = _TARGET.search(text)
    if not m:
        return None
    unit = m.group(2)
    if not unit.endswith("s"):
        unit += "s"
    n = int(m.group(1))
    return (n, unit) if 1 <= n <= 500 else None


def extract_task(text: str) -> str | None:
    stripped = _DURATION.sub(" ", text)
    stripped = re.sub(rf"\b\d{{1,3}}\s+(?:more\s+)?(?:{_UNIT_WORDS})\b", " ", stripped)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    for pat in _TASK_PATTERNS:
        m = pat.search(stripped)
        if m:
            task = m.group("task").strip()
            task = re.sub(r"\b(the|my|some|a|an)$", "", task).strip()
            if task and task not in _TASK_STOPWORDS and len(task) >= 2:
                return task
    return None


def extract_progress(text: str) -> tuple[int, str | None] | None:
    """(units, unit_label) for a progress report, else None. Past tense only:
    "I have 25 minutes to finish 10 problems" is a plan, not progress. Text is already
    normalised, so "one" has become "1"."""
    verbs = r"(?:finished|done with|done|completed|did|knocked out|solved|got through|crushed)"
    m = re.search(
        rf"\b{verbs}\s+(?:(?P<n>\d{{1,2}})|a|an|another|the next|the|this|that|my)?\s*(?:more\s+)?(?P<unit>{_UNIT_WORDS}|ones?)\b",
        text,
    )
    if m:
        unit = m.group("unit")
        label = None if unit.startswith("one") else (unit if unit.endswith("s") else unit + "s")
        return (int(m.group("n")) if m.group("n") else 1), label
    if re.search(rf"\b{verbs}\s+another(?:\s+1)?\b", text):
        return 1, None
    if re.search(r"\b(1 more|another 1|another) (down|done)\b|\b(that is|thats) 1\b|\b1 down\b", text):
        return 1, None
    return None


def extract_progress_units(text: str) -> int | None:
    found = extract_progress(text)
    return None if found is None else found[0]


# ------------------------------------------------------------------ rules
@dataclass(frozen=True)
class _Rule:
    name: IntentName
    pattern: re.Pattern
    confidence: float
    label: str


def _r(name: IntentName, pattern: str, confidence: float = 0.95, label: str = "") -> _Rule:
    return _Rule(name, re.compile(pattern), confidence, label or name.value)


RULES: list[_Rule] = [
    _r(IntentName.EMERGENCY_STOP, r"\b(emergency stop|emergency|e stop|estop|kill switch|kill it)\b"),
    # Before stop/mute: "stop watching me" is about the camera, "stop listening" about the mic.
    _r(IntentName.CAMERA_OFF, r"\b(turn|switch|shut) (off|down) (the |your )?camera\b|\b(turn|switch|shut) (the |your )?camera off\b|\bcamera off\b|\bstop (watching|recording|filming) me\b|\bstop (the )?(video|camera)\b"),
    _r(IntentName.CAMERA_ON, r"\b(turn|switch) (on|back on) (the |your )?camera\b|\b(turn|switch) (the |your )?camera (back )?on\b|\bcamera (back )?on\b|\byou can (watch|look) again\b"),
    _r(IntentName.MIC_OFF, r"\b(stop listening|turn off (the |your )?(mic|microphone)|(mic|microphone) off|mute (the |your )?(mic|microphone))\b"),
    _r(IntentName.TONE_DOWN, r"\bstop (roasting|being mean|insulting|clowning|bullying|judging)\b|\b(be nice|be nicer|go easy|less (roasting|mean|sarcasm|sarcastic)|tone it down|ease up|calm down|enough roasting)\b"),
    _r(IntentName.SERIOUS, r"\b(be serious|serious mode|get serious|no (more )?jokes|stop joking|enough jokes|just the facts)\b"),
    _r(IntentName.SET_PROFANITY, r"\b(no (swearing|cursing|cussing|profanity)|stop (swearing|cursing|cussing)|do not (swear|curse|cuss)|keep it (clean|pg)|clean (language|mode)|family friendly)\b|\byou can (swear|curse|cuss)\b|\b(swear|curse|cuss) (a little|a bit|all you want|as much as you want)\b|\b(mild|light|full|strong|unlimited) (swearing|profanity|cursing|language)\b|\bno filter\b"),
    _r(IntentName.RESET_TONE, r"\b(you can (joke|roast me) again|roast me again|jokes are back|back to normal|normal mode|be funny again|joke again)\b"),
    _r(IntentName.UNMUTE, r"\b(unmute|you can talk( again)?|talk again|speak again|you can speak)\b"),
    _r(IntentName.MUTE, r"\b(stop talking|shut up|be quiet|quiet mode|mute( yourself)?|silence|hush|shush|shh+|zip it|pipe down|stop yapping|stop speaking)\b|^quiet( please)?$"),
    _r(IntentName.RELEASE, r"\b(give (me )?(back )?my phone( back)?|give (it|me it) back|phone back|release( the| my)? phone|release|i need my phone|let me (have|use|get) my phone|hand (me )?(over )?my phone|unlock my phone|i want my phone|(can|could|may) i (have|get) (my |the )?phone( back)?|give (it|that) (here|back)|phone please|i need (it|that) back)\b"),
    _r(IntentName.END_SESSION, r"\b(end|finish|stop|close|wrap up|terminate|cancel|quit) (the |this |my |our )?(focus |study )?session\b|\b(i am done|we are done|that is it for (today|now)|end it|i give up|done for (today|now)|call it a day|that is enough( for (today|now))?|i am finished( for (today|now))?|(stop|end|kill) the timer)\b"),
    _r(IntentName.STOP_MOTION, r"\b(stop|freeze|halt|stop moving|do not move|stay there|stay put)\b"),
    _r(IntentName.PAUSE, r"\b(pause|take a break|break time|give me (a|1) (minute|sec|second|moment|break)|brb|be right back|hold up|hold on|wait (a |1 )?(sec|second|minute|moment)|time out|timeout)\b|^1 (sec|second)$"),
    _r(IntentName.RESUME, r"\b(resume|unpause|continue|keep going|back to work|i am back|let us keep going|start again|let us go again)\b|^(ok |okay )?i am ready( to go| now)?$"),
    _r(IntentName.SET_PERSONALITY, r"\b(chill|roast|roasting|unhinged|judge|judge demo|demo|presentation|coach|chaos) mode\b|\bswitch to (chill|roast|unhinged|judge demo|coach|chaos)\b|\bgo unhinged\b"),
    _r(IntentName.SET_PERSONALITY, r"^roast me\b|\broast me( now| please| hard| bro)?$", 0.9, "roast_me"),
    # "help me focus" asks for a session, not for the stuck-on-the-work reply
    _r(IntentName.HELP, r"\b(help(?! me (focus|stay focused|focused|lock in|locked in|on track))( me)?|i am stuck|i need help|i do not (get|understand) (it|this)|explain (this|it)|this is (so )?(hard|confusing)|i am (confused|lost))\b"),
    _r(IntentName.HARDER, r"\b(make it harder|harder|be (strict|stricter|harsh|harsher|meaner|tougher)|go hard|turn it up|more strict|crank it up)\b"),
    _r(IntentName.TIME_LEFT, r"\b(how (much|many) (time|minutes)|time (left|remaining)|how long (left|is left|do i have|until|to go|now)|when (am i|is it) (done|over|free)|minutes left|how much longer|time check)\b"),
    _r(IntentName.STATUS, r"\b(how am i doing|how is it going|how are we doing|status|progress report|how did i do|my stats|how am i|am i doing (good|well|ok|okay|alright)|how is my focus|how am i looking)\b"),
    _r(IntentName.ABOUT, r"\b(who are you|what are you|what is your name|what do you do|what can you do|what are you for|introduce yourself)\b", 0.85),
    _r(IntentName.NEXT_STEP, r"\b(what should i do next|what is next|what next|what now|what do i do( now| next)?|next step|what should i do)\b"),
    _r(IntentName.START_SESSION, r"\b(start|begin|kick off|launch|new)\b.*\b(session|timer|pomodoro)\b|\bpomodoro\b|\b(i have|i got|i have got|give me|set (a )?timer for)\s+\d+(\.\d)?\s*(minutes?|mins?|hours?|hrs?)\b|\btime to (study|work|focus|lock in|grind)\b|\b\d+ minute (session|timer|block)\b|\bstudy session\b|\bfocus mode\b|^\d+(\.\d)?\s*(minutes?|mins?|hours?|hrs?)( session)?$"),
    _r(IntentName.START_SESSION, r"\block me in\b|\b(help me|keep me) (focus|stay focused|focused|lock in|locked in|on track)\b|\bi (need|want|have) to (focus|study|work|lock in|get (some )?work done)\b|\bstart (focusing|studying|working)\b|\b(take|hold|watch|guard) my phone\b", 0.9, "start_session_natural"),
    _r(IntentName.START_SESSION, r"\b(let us go|let us do this|let us get it|let us lock in)\b", 0.7, "start_session_weak"),
    _r(IntentName.RETURN_ROBOT, r"\b(come back|return( home| to (base|home|start))?|go back( home)?|back to (base|start)|come here)\b"),
    _r(IntentName.THANKS, r"\b(thanks|thank you|thank u|ty)\b", 0.85),
    _r(IntentName.GREETING, r"^(hi|hello|hey|yo|sup|what is up|good morning|good evening|good afternoon)( there)?$", 0.85),
]

_STOP_WORD = re.compile(r"\b(stop|freeze|halt|emergency)\b")

# Commands taken even while the robot is talking (barge-in): safety first, then the
# "respect" commands from the brief (stop roasting me, be serious, be quiet).
BARGE_IN_INTENTS = frozenset(
    {
        IntentName.EMERGENCY_STOP,
        IntentName.STOP_MOTION,
        IntentName.MUTE,
        IntentName.RELEASE,
        IntentName.PAUSE,
        IntentName.TONE_DOWN,
        IntentName.SERIOUS,
        IntentName.CAMERA_OFF,
        IntentName.MIC_OFF,
    }
)


def personality_from(text: str) -> str:
    if re.search(r"\bunhinged\b", text):
        return "unhinged"
    if re.search(r"\b(judge|demo|presentation)\b", text):
        return "judge_demo"
    if re.search(r"\bchill\b", text):
        return "chill"
    return "roast"  # roast, roasting, and the milestone 1 names coach / chaos


def profanity_from(text: str) -> str:
    if re.search(r"\b(all you want|as much as you want|full|strong|unlimited|no filter)\b", text):
        return "strong"
    if re.search(r"\b(you can (swear|curse|cuss)|a little|a bit|mild|light)\b", text) and not re.search(r"\b(no|stop|do not)\b", text):
        return "mild"
    return "off"


_NEGATION = re.compile(r"\b(do not|dont|never|no|not|cancel|never mind|nevermind|wait)\b")
_MOVES = re.compile(r"\b(come back|go back|come here|return|go away|back off|forward|backward|left|right)\b")


def movement_conflict(text: str) -> str | None:
    """A movement request that contradicts itself is not guessed at: "don't come back",
    "come back, no, go left". Stopping is never subject to this (stop words win earlier)."""
    if _NEGATION.search(text):
        return "negated or retracted"
    if len(set(_MOVES.findall(text))) > 1:
        return "more than one movement"
    return None


class IntentClassifier:
    def classify(self, raw: str, stt_confidence: float = 1.0) -> Intent:
        text = normalise(raw)
        addressed = False
        wake_only = False
        m = _WAKE.match(text)
        if m:
            addressed = True
            remainder = text[m.end():].strip()
            wake_only = remainder == ""
            text = remainder
        stop_word = bool(_STOP_WORD.search(text))
        conf_cap = max(0.0, min(1.0, stt_confidence))

        if wake_only:
            started_with_lock = bool(re.match(r"^(?:(?:hey|yo|ok|okay|alright|hi)\s+)?lock", normalise(raw)))
            name = IntentName.START_SESSION if started_with_lock else IntentName.GREETING
            return Intent(name, min(0.9, conf_cap), "", raw, {}, addressed, False, "wake_word_only")

        if not text:
            return Intent(IntentName.UNKNOWN, 0.0, text, raw, {}, addressed, stop_word, "empty")
        if addressed and re.fullmatch(r"(now|please|go|again|let us go|time|right now)", text):
            return Intent(IntentName.START_SESSION, min(0.9, conf_cap), text, raw, {}, addressed, False, "wake_word_go")

        # Progress must be checked before END_SESSION ("i am done with a problem").
        progress = extract_progress(text)
        if progress is not None and not re.search(r"\bsession\b", text):
            slots = {"units": progress[0]}
            if progress[1]:
                slots["unit_label"] = progress[1]
            return Intent(IntentName.PROGRESS, min(0.9, conf_cap), text, raw, slots, addressed, stop_word, "progress")

        for rule in RULES:
            if rule.pattern.search(text):
                slots: dict = {}
                if rule.name == IntentName.START_SESSION:
                    dur = extract_duration_minutes(text)
                    if dur is not None:
                        slots["duration_min"] = dur
                    task = extract_task(text)
                    if task:
                        slots["task"] = task
                    target = extract_target(text)
                    if target:
                        slots["target_units"], slots["unit_label"] = target
                elif rule.name == IntentName.SET_PERSONALITY:
                    slots["personality"] = personality_from(text)
                    if rule.label == "roast_me":
                        slots["roast_me"] = True
                elif rule.name == IntentName.SET_PROFANITY:
                    slots["profanity"] = profanity_from(text)
                intent = Intent(rule.name, min(rule.confidence, conf_cap), text, raw, slots, addressed, stop_word, rule.label)
                if intent.moves_robot:
                    intent.conflict = movement_conflict(text)
                return intent

        # A bare duration ("25 minutes") after the wake word starts a session.
        if addressed:
            dur = extract_duration_minutes(text)
            if dur is not None:
                slots = {"duration_min": dur}
                task = extract_task(text)
                if task:
                    slots["task"] = task
                return Intent(IntentName.START_SESSION, min(0.8, conf_cap), text, raw, slots, addressed, stop_word, "wake_plus_duration")

        return Intent(IntentName.UNKNOWN, min(0.3, conf_cap), text, raw, {}, addressed, stop_word, "no_rule")
