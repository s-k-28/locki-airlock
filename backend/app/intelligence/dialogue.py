"""Open-ended conversation with the local language model, inside rules it cannot override.

One user turn (runtime._converse) or one proactive line (behaviour engine):

1. the context block is rendered from ``SessionContext`` and the recorded ``SessionFacts``,
   offering only the actions the current state allows;
2. the local model streams one JSON object ``{"action", "args", "say"}``;
3. the action is validated (actions.py: closed list, typed range-checked args, allowed now);
   the runtime then runs it through the same handlers the dashboard uses, and physical
   requests go to the safety controller;
4. the reply is checked before a word of it is spoken (``check_reply``): numbers must be
   in the session facts (or the user's own words), the style rules hold, it must not say
   the robot moved, must not claim to have seen something it did not record, must not
   guess the user's feelings, must not repeat a recent line; it is cut to two sentences
   unless the user asked for an explanation. A reply that fails is not spoken.

While the model is still writing, complete sentences of ``say`` are released to the voice
as soon as the action in front of them has been validated (``on_sentence``), so the first
words play before the reply is finished.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..voice.content_guard import check as guard_check
from ..voice.conversation import SessionFacts, fmt_duration, plural
from .actions import PHYSICAL, ActionName, ActionRequest, ModelReply, SayStream, parse_reply, validate
from .local_llm import Generation

EXPLAIN = re.compile(r"\b(why|explain|what do you mean|how come|tell me (about|more)|what happened|walk me through)\b", re.I)
_MOTION_CLAIM = re.compile(
    r"\b(i'?m|i am|i'?ll|i will|watch me|let me|gonna|going to|about to)\s+(now\s+)?"
    r"(move|moving|drive|driving|roll|rolling|back(ing)?\s+(up|off|away)|retreat(ing)?|come|coming|head(ing)?\s+(over|back|your way)|scoot(ing)?)\b"
    r"|\b(backing|rolling|driving|moving) (up|off|away|back|over|toward|towards|closer)\b|\bon my way\b|\bfull speed ahead\b",
    re.I,
)
_SEEN_REACH = re.compile(r"\b(i (just )?(saw|see|caught|noticed|spotted|watched)|saw that|caught you)\b", re.I)
# "still reaching for the phone", "grabbing your phone again", "went for it": accusations that
# need a recorded reach (or the user's own words about it).
_REACH_CLAIM = re.compile(r"\b(reach(es|ed|ing)?|grab(s|bed|bing)?|snatch(ed|ing)?|went for|going for|go for)\b[^.!?]{0,25}\b(phone|it)\b", re.I)
_USER_REACH_WORDS = re.compile(r"\b(reach|grab|phone)", re.I)
# With the camera off the robot sees nothing: no "I'm watching you", "I see you", "tracking".
_WATCHING_CLAIM = re.compile(
    r"\b(i'?m|i am|i'?ve been|i have been|i'?ll be|i keep)\s+(\w+\s+)?(watching|tracking|monitoring|seeing|looking at|staring at|eyeing)\b"
    r"|\bi\s+(can\s+)?(see|watch|spot)\s+(you|your|that|it)\b|\bi see everything\b|\bmy (eyes|camera) (is|are) on\b",
    re.I,
)
_FEELING = re.compile(
    r"\byou(?:'re| are| seem| look| sound| feel| must be)\s+(?:so |really |pretty |kinda |a bit |very )?"
    r"(stressed|tired|sad|anxious|frustrated|upset|angry|mad|bored|exhausted|overwhelmed|nervous|depressed|sleepy|annoyed)\b",
    re.I,
)
_SAY_STARTED = re.compile(r'"say"\s*:\s*"')
_EMOJI = re.compile("[\U0001f300-\U0001faff\u2600-\u27bf\ufe0f\u200d]")
_STAGE = re.compile(r"\*[^*]{1,40}\*|\([^)]{0,40}(laughs|sighs|beeps|whirs|smirks)[^)]{0,20}\)", re.I)
_ORDINALS = {"third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10}
_NUMBER_WORDS = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "ninety": 90, "twice": 2, "thrice": 3, "half": -1,
}
# Short punchy fragments are fine ("Calculus tomorrow? Then the phone waits. Go."): length is
# capped in words, with a sentence cap as a backstop.
MAX_SENTENCES, MAX_SENTENCES_EXPLAIN = 3, 5
MIN_STREAM_WORDS = 4  # a first sentence shorter than this waits for the next one
RETRY_WITHIN_S = 2.5  # a refused reply is retried once if this much time has not passed yet
STYLE_LINES = {
    "chill": "chill. Warm, relaxed, lightly sarcastic.",
    "roast": "roast. Casual trash talk like a real friend who is on their side.",
    "unhinged": "unhinged. High-energy roasting the user explicitly opted into.",
    "judge_demo": "judge demo. Energetic, memorable and presentation-safe.",
    "plain": "serious. Plain and factual, no jokes.",
}
PROFANITY_LINES = ["no swearing at all", "mild swearing only (damn, hell, ass), rarely", "swearing allowed, not in every sentence"]


def style_line(style) -> str:
    if style.supportive:
        tone = "supportive. The user asked for help: be kind and genuinely useful, no teasing."
    else:
        tone = STYLE_LINES.get(style.pool, STYLE_LINES["roast"])
    insults = "one playful insult at most" if style.insults_allowed else "no insults aimed at the user"
    return f"{tone} {PROFANITY_LINES[style.max_profanity].capitalize()}; {insults}."


def session_line(f: SessionFacts) -> str:
    if not f.session_active:
        return "No session running."
    done, total = fmt_duration(f.elapsed_s), fmt_duration(f.planned_s)
    state = "PAUSED" if f.paused else "running"
    parts = [f"Session {state}: {done} of {total} done, {fmt_duration(f.remaining_s)} left."]
    if f.target_units:
        parts.append(f"Progress: {f.completed_units} of {plural(f.target_units, f.unit_label)}.")
    elif f.completed_units:
        parts.append(f"Progress: {plural(f.completed_units, f.unit_label)} done.")
    if f.vision_available:
        parts.append(f"Confirmed phone reaches this session: {f.reaches_confirmed}.")
    else:
        parts.append("Phone reaches this session: unknown (not watching).")
    return " ".join(parts)


def allowed_actions(*, session_active: bool, paused: bool, quiet: bool, holding_phone: bool) -> list[ActionName]:
    a = []
    if not session_active:
        a += [ActionName.START_SESSION, ActionName.SET_GOAL]
    else:
        a += [ActionName.SET_GOAL, ActionName.EXTEND_SESSION]
        a += [ActionName.RESUME_SESSION] if paused else [ActionName.PAUSE_SESSION]
        a += [ActionName.END_SESSION]
    a += [ActionName.SET_QUIET, ActionName.SET_STYLE]
    if session_active or holding_phone:
        a += [ActionName.RELEASE_PHONE, ActionName.REQUEST_RETREAT]
    a += [ActionName.STOP]
    return a


def numbers_in(text: str) -> set[int]:
    out = {int(n) for n in re.findall(r"\b(\d{1,4})(?:st|nd|rd|th)?\b", text)}
    low = text.lower().replace("-", " ")
    for word, value in {**_NUMBER_WORDS, **_ORDINALS}.items():
        if value > 0 and re.search(rf"\b{word}\b", low):
            out.add(value)
    return out


def similar(a: str, b: str) -> float:
    wa, wb = set(re.findall(r"[a-z']+", a.lower())), set(re.findall(r"[a-z']+", b.lower()))
    return len(wa & wb) / max(1, len(wa | wb))


@dataclass
class ReplyCheck:
    ok: bool
    text: str
    reason: str = ""


def check_reply(
    text: str,
    *,
    facts: SessionFacts,
    style,
    user_text: str = "",
    action: ActionRequest | None = None,
    reach_recorded: bool = False,
    recent_lines: list[str] | tuple[str, ...] = (),
    explain: bool = False,
    extra_numbers: set[int] | frozenset[int] = frozenset(),
    camera_on: bool = True,
) -> ReplyCheck:
    """Whether a generated line may be spoken, and the cleaned text if so."""
    m = _EMOJI.search(text or "")
    if m and re.search(r"[A-Za-z]", (text or "")[m.end():]):
        # "I ❤️ you" would be spoken as "I you": refuse (the model is retried) instead of mangling it.
        return ReplyCheck(False, text, "emoji in the middle of a sentence")
    t = _STAGE.sub(" ", _EMOJI.sub("", text or ""))
    t = re.sub(r"\s+", " ", t).strip().strip('"').strip()
    if not t:
        return ReplyCheck(False, "", "empty reply")
    sentences = [s for s in re.split(r"(?<=[.!?…])\s+", t) if s]
    limit = MAX_SENTENCES_EXPLAIN if explain else MAX_SENTENCES
    if len(sentences) > limit:
        t = " ".join(sentences[:limit])
    if len(t.split()) > (60 if explain else 32):
        return ReplyCheck(False, t, "too long")
    allowed = facts.numbers() | numbers_in(user_text) | set(extra_numbers)
    if action is not None:
        allowed |= {int(v) for v in action.args.values() if isinstance(v, int)}
    allowed |= {n * 60 for n in list(allowed) if n < 10}  # "an hour" style restatements stay possible
    bad = sorted(n for n in numbers_in(t) if n not in allowed and n > 1)
    if bad:
        return ReplyCheck(False, t, f"numbers not in the session record: {bad}")
    if _MOTION_CLAIM.search(t) and not (action is not None and action.name in PHYSICAL):
        return ReplyCheck(False, t, "claims the robot is moving (wheels are locked)")
    if _SEEN_REACH.search(t) and not reach_recorded:
        return ReplyCheck(False, t, "claims to have seen something that was not recorded")
    if _REACH_CLAIM.search(t) and not reach_recorded and not _USER_REACH_WORDS.search(user_text or ""):
        return ReplyCheck(False, t, "says the user reached for the phone, which was not recorded")
    if not camera_on and _WATCHING_CLAIM.search(t):
        return ReplyCheck(False, t, "claims to be watching while the camera is off")
    feel = _FEELING.search(t)
    if feel and feel.group(1).lower() not in (user_text or "").lower():
        return ReplyCheck(False, t, f"guesses the user's feelings ({feel.group(1)})")
    verdict = guard_check(t, max_profanity=style.max_profanity, allow_insults=style.insults_allowed)
    if not verdict.ok:
        return ReplyCheck(False, t, f"style rules: {verdict.reason}")
    for line in recent_lines:
        if similar(t, line) >= 0.75:
            return ReplyCheck(False, t, "repeats a recent line")
    return ReplyCheck(True, t)


@dataclass
class DialogueResult:
    say: str | None = None  # checked text to speak (None: use a fixed line)
    action: ActionRequest | None = None
    requested: str | None = None  # action name the model wrote (even if refused)
    problems: list[str] = field(default_factory=list)
    rejected_say: str | None = None  # what the model wrote, when it was not spoken
    streamed: list[str] = field(default_factory=list)  # sentences already handed to the voice
    generation: Generation | None = None
    context_chars: int = 0
    ms: float = 0.0
    retried: bool = False
    salvaged: bool = False  # the spoken text is what was left after refusing a sentence

    def to_payload(self) -> dict:
        g = self.generation
        return {
            "say": self.say,
            "action": None if self.action is None else self.action.to_payload(),
            "requested": self.requested,
            "problems": self.problems,
            "rejected_say": self.rejected_say,
            "streamed": len(self.streamed),
            "retried": self.retried,
            "ms": round(self.ms, 1),
            "llm": None if g is None else {
                "finish": g.finish, "first_token_ms": g.first_token_ms, "total_ms": g.total_ms, "gen_tokens": g.gen_tokens,
                "prompt_tokens": g.prompt_tokens, "gen_tps": g.gen_tps, "error": g.error, "prefilled": g.stats.get("prefilled"),
            },
        }


class DialogueEngine:
    """Runs one generation and applies the rules above. Stateless between calls: the
    memory is ``SessionContext``; the model is ``LocalLLM`` (or a test double with the same
    ``generate`` coroutine)."""

    def __init__(self, llm, *, timeout_s: float = 6.0, max_tokens: int = 110, temperature: float = 0.75):
        self.llm = llm
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens
        self.temperature = temperature

    @property
    def available(self) -> bool:
        return self.llm is not None and getattr(self.llm, "state", "ready") == "ready"

    async def respond(
        self,
        context: str,
        *,
        allowed: list[ActionName],
        facts: SessionFacts,
        style,
        user_text: str = "",
        reach_recorded: bool = False,
        recent_lines: list[str] | tuple[str, ...] = (),
        on_sentence: Callable[[str], None] | None = None,
        background: bool = False,
        proactive: bool = False,
        extra_numbers: set[int] | frozenset[int] = frozenset(),
        timeout_s: float | None = None,
        seed: int | None = None,
        retry_within_s: float = RETRY_WITHIN_S,
        camera_on: bool = True,
    ) -> DialogueResult:
        """One reply. If the guards refuse the words before any of them were spoken, the model
        gets one more try (told why), as long as that still fits in ``retry_within_s``."""
        t0 = time.monotonic()
        res = DialogueResult(context_chars=len(context))
        budget = timeout_s or self.timeout_s
        attempt_context = context
        salvaged: str | None = None
        for attempt in range(2):
            await self._attempt(
                res, attempt_context, allowed=allowed, facts=facts, style=style, user_text=user_text, reach_recorded=reach_recorded,
                recent_lines=recent_lines, on_sentence=on_sentence, background=background, proactive=proactive,
                extra_numbers=extra_numbers, timeout_s=max(1.0, budget - (time.monotonic() - t0)), seed=seed, camera_on=camera_on,
            )
            elapsed = time.monotonic() - t0
            if res.salvaged and attempt == 0:
                # Part of the reply was refused. A whole new reply keeps the context better than
                # the leftover sentences; they are the fallback if there is no time or no luck.
                salvaged, res.say = res.say, None
            retryable = res.say is None and not res.streamed and res.rejected_say is not None and res.generation is not None and res.generation.ok
            if attempt == 1 or not retryable or res.action is not None or elapsed > retry_within_s:
                break
            why = res.problems[-1].removeprefix("reply not spoken: ").removeprefix("sentence dropped: ") if res.problems else "it broke a rule"
            attempt_context = context + f"\nNOTE: your last reply was not used ({why}). Write a different one that follows the rules."
            res.retried = True
        if res.say is None and not res.streamed and salvaged:
            res.say = salvaged
        res.ms = (time.monotonic() - t0) * 1000
        return res

    async def _attempt(
        self, res: DialogueResult, context: str, *, allowed, facts, style, user_text, reach_recorded, recent_lines, on_sentence,
        background, proactive, extra_numbers, timeout_s, seed, camera_on=True,
    ) -> None:
        allowed_set = frozenset([] if proactive else allowed)
        explain = bool(EXPLAIN.search(user_text or ""))
        cap = MAX_SENTENCES_EXPLAIN if explain else MAX_SENTENCES
        stream = SayStream()
        early: dict = {"checked": False, "ok": False, "action": None, "held": None}
        res.say, res.rejected_say, res.salvaged = None, None, False

        def check(sentence: str, action) -> ReplyCheck:
            return check_reply(sentence, facts=facts, style=style, user_text=user_text, action=action, reach_recorded=reach_recorded,
                               recent_lines=recent_lines, explain=explain, extra_numbers=extra_numbers, camera_on=camera_on)

        def head_action() -> tuple[bool, ActionRequest | None]:
            """Once everything in front of "say" is written: is the action acceptable?"""
            import json

            head = stream.head().rstrip().rstrip(",")
            try:
                data = json.loads(head + "}") if head.lstrip().startswith("{") else None
            except json.JSONDecodeError:
                return False, None
            if not isinstance(data, dict):
                return False, None
            name = data.get("action", "none")
            if name in (None, "", "none"):
                return True, None
            req, why = validate(name, data.get("args"), allowed_set)
            if why or req is None or req.name in PHYSICAL:
                return False, req  # refused, or the safety decision has to come first
            return True, req

        def release(sentences: list[str], action) -> None:
            """Hand checked sentences to the voice; a very short opener waits for the next one
            ("Drama?" alone is a bad first sound if the next sentence is then refused)."""
            for sentence in sentences:
                if not early["ok"] or len(res.streamed) >= cap:
                    return
                if early["held"]:
                    sentence, early["held"] = early["held"] + " " + sentence, None
                if not res.streamed and len(sentence.split()) < MIN_STREAM_WORDS:
                    early["held"] = sentence
                    continue
                verdict = check(sentence, action)
                if not verdict.ok:
                    early["ok"] = False  # stop here; what was already said stays said
                    res.problems.append(f"{'streamed reply stopped' if res.streamed else 'reply not spoken'}: {verdict.reason}")
                    return
                res.streamed.append(verdict.text)
                on_sentence(verdict.text)

        def on_delta(delta: str) -> None:
            stream.feed(delta)
            if not _SAY_STARTED.search(stream.buf):
                return  # the action in front of "say" is complete only once its value starts
            if not early["checked"]:
                early["checked"] = True
                early["ok"], early["action"] = head_action()
            if early["ok"]:
                release(stream.sentences(), early["action"])

        gen = await self.llm.generate(
            context,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            timeout_s=timeout_s,
            on_delta=on_delta if on_sentence is not None else None,
            background=background,
            seed=seed,
        )
        res.generation = gen
        if not gen.ok:
            res.problems.append(f"model: {gen.finish}{' (' + gen.error + ')' if gen.error else ''}")
            return
        parsed: ModelReply = parse_reply(gen.text, allowed_set)
        res.requested = parsed.requested
        res.problems += parsed.problems
        res.action = parsed.action
        streaming = on_sentence is not None and early["checked"] and early["ok"]
        if res.streamed or streaming:
            release(stream.sentences(final=True), res.action)
            if early["held"] and early["ok"]:  # the whole reply was one short sentence
                held, early["held"] = early["held"], None
                verdict = check(held, res.action)
                if verdict.ok:
                    res.streamed.append(verdict.text)
                    on_sentence(verdict.text)
                else:
                    res.problems.append(f"reply not spoken: {verdict.reason}")
            res.say = " ".join(res.streamed) or None
            if res.say is None:
                res.rejected_say = parsed.say or None
        elif parsed.requested not in (None, "none") and parsed.action is None:
            # The words went with an action that was refused; they would describe something
            # that is not going to happen. A fixed refusal is spoken instead.
            res.rejected_say = parsed.say or None
            res.problems.append("reply not spoken: it accompanied a refused action")
        else:
            verdict = check(parsed.say, res.action)
            if verdict.ok:
                res.say = verdict.text
            else:
                # Keep the sentences that pass on their own ("Because you keep reaching for it.
                # 100% of the time." -> the first sentence), if enough is left to make sense.
                kept = [check(sent, res.action) for sent in re.split(r"(?<=[.!?…])\s+", parsed.say or "") if sent.strip()]
                good = [k.text for k in kept if k.ok]
                if good and len(" ".join(good).split()) >= 3 and len(good) < len(kept):
                    res.say = " ".join(good)
                    res.salvaged = True
                    res.rejected_say = parsed.say or None
                    res.problems.append(f"sentence dropped: {verdict.reason}")
                else:
                    res.rejected_say = parsed.say or None
                    res.problems.append(f"reply not spoken: {verdict.reason}")
