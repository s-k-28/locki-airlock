"""Last check on anything LOCKED IN is about to say.

* Profanity is measured, not guessed: OFF (0) / MILD (1) / STRONG (2). Template lines are
  tagged with their level and a test keeps the tags honest against this lexicon; replies
  from the optional LLM are rejected when they exceed the user's chosen level.
* Never allowed at any setting: slurs, threats or encouragement of self-harm, attacks on
  identity or appearance, and deliberately humiliating words. Playful insults the user
  opted into ("dumbass" in Unhinged mode) are a separate, rate-limited tag on templates.

The slur list is stored rot13-encoded so the repository holds no plaintext slurs.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass

MILD = frozenset(
    {"damn", "damned", "dammit", "damnit", "hell", "ass", "asses", "crap", "crappy", "piss", "pissed", "arse"}
    | {"dumbass", "badass", "jackass", "smartass", "hardass"}
)
_STRONG_ROOTS = ("fuck", "shit")
STRONG = frozenset({"motherfucker", "goddamn", "bullshit"})

_SLURS_ROT13 = (
    "ergneq ergneqrq snttbg snt avttre avttn genaal fcvp puvax xvxr jrgonpx qlxr phag ornare "
    "tbbx gbjryurnq pbba fcnm furznyr ovgpu"
)
SLURS = frozenset(codecs.decode(_SLURS_ROT13, "rot13").split())

_THREATS = [
    re.compile(r"\b(i('ll| will)|i'm going to|im going to|gonna|going to) (kill|hurt|hit|beat|punch|stab|shoot|murder|choke) (you|ya|u)\b"),
    re.compile(r"\b(kill|hurt|harm|end) yourself\b|\bkys\b|\bgo die\b|\byou should die\b|\bi know where you live\b"),
]
_IDENTITY = re.compile(
    r"\b(because|cause|since|for being) (you('re| are)|ur) (a |an )?"
    r"(girl|woman|boy|man|gay|lesbian|trans|black|white|asian|latino|latina|mexican|indian|muslim|jewish|christian|"
    r"fat|ugly|short|poor|disabled|autistic|old|young)\b"
)
_APPEARANCE = re.compile(r"\byou('re| are)? (so |really |an? )?(fat|ugly|hideous|disgusting)\b")
HUMILIATING = frozenset({"worthless", "pathetic", "loser", "losers", "disgusting", "useless"})
_HUMILIATING_PHRASES = re.compile(r"\b(nobody (likes|loves) you|you('re| are) a (failure|disappointment|joke))\b")
INSULT_WORDS = frozenset({"dumbass", "idiot", "moron", "clown", "stupid", "dumb", "menace", "goofball"})


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z']+", text.lower().replace("’", "'"))


def profanity_level(text: str) -> int:
    level = 0
    for tok in _tokens(text):
        t = tok.strip("'")
        if t in STRONG or any(root in t for root in _STRONG_ROOTS):
            return 2
        if t in MILD:
            level = 1
    return level


def _stems(tok: str) -> set[str]:
    out = {tok}
    for suffix in ("s", "es", "ed", "ing"):
        if tok.endswith(suffix) and len(tok) > len(suffix) + 2:
            out.add(tok[: -len(suffix)])
    return out


@dataclass
class GuardResult:
    ok: bool
    reason: str = ""
    profanity: int = 0


def check(text: str, max_profanity: int = 0, allow_insults: bool = False) -> GuardResult:
    """Whether ``text`` may be spoken at this profanity level."""
    lowered = text.lower().replace("’", "'")
    toks = _tokens(lowered)
    if any(_stems(t) & SLURS for t in toks):
        return GuardResult(False, "slur")
    if any(p.search(lowered) for p in _THREATS):
        return GuardResult(False, "threat or self-harm")
    if _IDENTITY.search(lowered) or _APPEARANCE.search(lowered):
        return GuardResult(False, "identity or appearance attack")
    if any(t in HUMILIATING for t in toks) or _HUMILIATING_PHRASES.search(lowered):
        return GuardResult(False, "humiliating language")
    level = profanity_level(lowered)
    if level > max_profanity:
        return GuardResult(False, f"profanity level {level} above the chosen {max_profanity}", level)
    if not allow_insults and any(t in INSULT_WORDS for t in toks):
        return GuardResult(False, "insult while insults are off", level)
    return GuardResult(True, "", level)
