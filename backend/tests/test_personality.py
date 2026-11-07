"""Personality engine and content guard: what the robot may say, in which mode, how often."""

from __future__ import annotations

import random
import re

from app.config import RuntimeSettings
from app.voice import content_guard
from app.voice.conversation import ResponseComposer, SessionFacts, as_style, grounding_violations
from app.voice.llm import system_prompt
from app.voice.personality import INSULT_GAP_S, LIBRARY, MODES, PersonalityEngine, Style, cacheable_lines


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def facts(**kw) -> SessionFacts:
    base = dict(
        session_active=True, task="calculus", elapsed_s=90, remaining_s=300, planned_s=25 * 60,
        completed_units=3, target_units=3, unit_label="problems", reaches_confirmed=3, vision_available=True,
    )
    base.update(kw)
    return SessionFacts(**base)


ALL_LINES = [(cat, ln) for cat, lines in LIBRARY.items() for ln in lines]


# ------------------------------------------------------------------ the library itself
def test_every_line_is_tagged_with_its_measured_profanity():
    for cat, ln in ALL_LINES:
        assert content_guard.profanity_level(ln.text) == ln.prof, (cat, ln.text)


def test_no_line_contains_slurs_threats_or_humiliation():
    for cat, ln in ALL_LINES:
        verdict = content_guard.check(ln.text, max_profanity=2, allow_insults=True)
        assert verdict.ok, (cat, ln.text, verdict.reason)


def test_insults_are_tagged_and_unhinged_only():
    for cat, ln in ALL_LINES:
        words = set(re.findall(r"[a-z]+", ln.text.lower()))
        if words & content_guard.INSULT_WORDS:
            assert ln.insult, (cat, ln.text)
        if ln.insult:
            assert ln.modes == {"unhinged"}, (cat, ln.text)


def test_judge_demo_is_always_clean_and_chill_at_most_mild():
    for cat, ln in ALL_LINES:
        if "judge_demo" in ln.modes:
            assert ln.prof == 0 and not ln.insult, (cat, ln.text)
        if "chill" in ln.modes:
            assert ln.prof <= 1, (cat, ln.text)


def test_safety_acknowledgements_never_joke():
    for cat in ("stop_ack", "estop_ack"):
        assert {ln.text for ln in LIBRARY[cat]} == {LIBRARY[cat][0].text}
        assert all(ln.modes >= set(MODES) | {"plain"} and ln.prof == 0 for ln in LIBRARY[cat])


def test_the_brief_examples_exist_in_the_right_modes():
    texts = {ln.text: ln for _c, ln in ALL_LINES}
    assert texts["Bro, we literally just started."].modes == {"roast"}
    assert texts["Get your ass back to work."].prof == 1
    assert texts["Dumbass, that's your {attempt_ordinal} attempt."].insult
    assert texts["Alright, lock the fuck in."].prof == 2
    assert texts["Okay, that was actually clean."].prof == 0
    assert texts["HOLY SHIT, YOU ACTUALLY DID IT."].prof == 2
    assert "judge_demo" in texts["That's what I'm talking about!"].modes


# ------------------------------------------------------------------ style rules
def test_profanity_is_the_users_choice_capped_by_mode_and_tone():
    assert Style(mode="judge_demo", profanity="strong").max_profanity == 0
    assert Style(mode="chill", profanity="strong").max_profanity == 1
    assert Style(mode="roast", profanity="strong").max_profanity == 2
    assert Style(mode="roast", profanity="off").max_profanity == 0
    assert Style(mode="roast", profanity="strong", tone="gentle").max_profanity == 0
    assert Style(mode="roast", profanity="strong", tone="serious").max_profanity == 0
    assert Style(mode="roast", profanity="strong", supportive=True).max_profanity == 1


def test_unhinged_needs_the_opt_in():
    assert Style(mode="unhinged").effective_mode == "roast"
    assert not Style(mode="unhinged", profanity="strong").insults_allowed
    assert Style(mode="unhinged", profanity="strong", unhinged_opt_in=True).insults_allowed


def test_legacy_names_never_opt_into_unhinged():
    assert as_style("coach").mode == "roast" and as_style("chaos").mode == "roast"
    assert RuntimeSettings.model_validate({"coach": {"personality": "chaos"}}).coach.personality == "roast"


def test_rendered_profanity_never_exceeds_the_setting():
    c = ResponseComposer(rng=random.Random(2))
    f = facts()
    for mode in MODES:
        for prof in ("off", "mild", "strong"):
            st = Style(mode=mode, profanity=prof, unhinged_opt_in=True)
            for cat in LIBRARY:
                for _ in range(4):
                    r = c.render(cat, f, st, new_personality="Roast", reason="disarmed")
                    assert content_guard.profanity_level(r.text) <= st.max_profanity, (mode, prof, cat, r.text)


def test_insults_are_paced_never_back_to_back():
    clock = Clock()
    eng = PersonalityEngine(rng=random.Random(4), clock=clock)
    st = Style(mode="unhinged", profanity="strong", unhinged_opt_in=True)
    slots = ResponseComposer()._slots(facts(reaches_confirmed=3))
    insults, flags = [], []
    for _ in range(80):
        clock.t += 10
        r = eng.render("reach_repeat", slots, st)
        flags.append(r.insult)
        if r.insult:
            insults.append(clock.t)
    assert insults, "unhinged mode should roast sometimes"
    assert all(b - a >= INSULT_GAP_S for a, b in zip(insults, insults[1:], strict=False))
    assert not any(a and b for a, b in zip(flags, flags[1:], strict=False))


def test_help_window_and_gentle_tone_drop_the_insults():
    eng = PersonalityEngine(rng=random.Random(5))
    slots = ResponseComposer()._slots(facts())
    for st in (
        Style(mode="unhinged", profanity="strong", unhinged_opt_in=True, supportive=True),
        Style(mode="unhinged", profanity="strong", unhinged_opt_in=True, tone="gentle"),
    ):
        for cat in ("reach_first", "reach_repeat", "reach_early"):
            for _ in range(10):
                r = eng.render(cat, slots, st)
                assert not r.insult and r.pool == "chill", (cat, r.text)


def test_serious_tone_speaks_plain_facts():
    c = ResponseComposer(rng=random.Random(6))
    r = c.render("reach_first", facts(), Style(mode="roast", tone="serious"))
    assert r.pool == "plain" and r.text.startswith("Phone reach detected")
    assert c.render("status", facts(), Style(tone="serious")).text.endswith(".")  # no flavour tag


# ------------------------------------------------------------------ facts, never fiction
def test_attempt_count_is_only_said_when_it_happened():
    eng = PersonalityEngine(rng=random.Random(7))
    st = Style(mode="unhinged", profanity="strong", unhinged_opt_in=True)
    comp = ResponseComposer()
    seen = {eng.render("reach_repeat", comp._slots(facts(reaches_confirmed=3)), st).text for _ in range(60)}
    assert any("third" in t for t in seen)
    for n in (1, 7):  # no ordinal for these counts: those lines are simply not eligible
        texts = {eng.render("reach_repeat", comp._slots(facts(reaches_confirmed=n)), st).text for _ in range(30)}
        assert not any("attempt." in t and "number" not in t for t in texts), texts


def test_no_rendered_line_states_a_number_the_session_does_not_have():
    c = ResponseComposer(rng=random.Random(8))
    f = facts()
    for mode in MODES:
        st = Style(mode=mode, profanity="strong", unhinged_opt_in=True)
        for cat in LIBRARY:
            for _ in range(3):
                text = c.compose(cat, f, st, new_personality="Roast", reason="disarmed")
                assert grounding_violations(text, f) == [], (cat, text)


def test_missing_facts_mean_the_line_is_skipped_not_invented():
    c = ResponseComposer(rng=random.Random(9))
    empty = SessionFacts()
    for _ in range(20):
        text = c.compose("reach_repeat", empty, Style(mode="roast"))
        assert not re.search(r"\d", text) and "{" not in text


def test_cacheable_lines_are_fixed_text_with_safety_lines_included():
    lines = cacheable_lines([Style(mode="roast", profanity="mild"), Style(tone="serious")])
    texts = {t for _c, t, _cue in lines}
    assert "Stopped." in texts and "Emergency stop. I'm not moving." in texts
    assert not any("{" in t for t in texts)
    assert "That's your third attempt. Bold." in texts  # small ordinals are expanded
    assert all(content_guard.profanity_level(t) <= 1 for t in texts)


# ------------------------------------------------------------------ guard and LLM prompt
def test_guard_blocks_slurs_threats_identity_attacks_and_humiliation():
    slur = sorted(content_guard.SLURS)[0]
    assert not content_guard.check(f"you {slur}", 2, True).ok
    assert not content_guard.check(f"what a bunch of {slur}s", 2, True).ok
    assert not content_guard.check("I'm going to kill you if you touch it", 2, True).ok
    assert not content_guard.check("Nobody likes you", 2, True).ok
    assert not content_guard.check("You're so ugly", 2, True).ok
    assert not content_guard.check("Because you're a girl you can't focus", 2, True).ok
    assert not content_guard.check("You're pathetic", 2, True).ok
    assert content_guard.check("Get your ass back to work.", 1, False).ok
    assert not content_guard.check("Get your ass back to work.", 0, False).ok
    assert not content_guard.check("Nice try, dumbass.", 2, False).ok
    assert content_guard.profanity_level("hello class, assignment passed") == 0
    assert content_guard.profanity_level("holy shit") == 2


def test_llm_prompt_follows_the_style():
    assert "none at all" in system_prompt(Style(mode="judge_demo", profanity="strong"))
    assert "No insults" in system_prompt(Style(mode="roast", profanity="strong"))
    assert "dumbass" in system_prompt(Style(mode="unhinged", profanity="strong", unhinged_opt_in=True))
    assert "helpful" in system_prompt(Style(mode="unhinged", profanity="strong", unhinged_opt_in=True, supportive=True))


def test_reactions_mostly_use_fixed_lines_for_timing():
    eng = PersonalityEngine(rng=random.Random(11))
    slots = ResponseComposer()._slots(facts())
    st = Style(mode="judge_demo")
    picks = [eng.render("reach_first", slots, st) for _ in range(200)]
    share = sum(1 for r in picks if r.cacheable) / len(picks)
    assert 0.7 <= share < 1.0  # mostly instant clips, still some variety with live numbers


def test_every_pool_has_an_instant_line_for_every_reaction():
    from app.voice.personality import INTENTS

    for cat, intent in INTENTS.items():
        if intent.priority != 3:
            continue
        for pool in ("chill", "roast", "unhinged", "judge_demo"):
            lines = [ln for ln in LIBRARY[cat] if pool in ln.modes and ln.prof == 0 and not ln.insult]
            assert any(not ln.slots for ln in lines), (cat, pool)


def test_no_line_repeats_within_a_session_until_the_pool_is_used_up():
    eng = PersonalityEngine(rng=random.Random(12))
    slots = ResponseComposer()._slots(facts())
    st = Style(mode="roast", profanity="strong")
    pool = {ln.text for ln in eng.candidates("reach_first", st, slots)}
    seen = [eng.render("reach_first", slots, st).template for _ in range(len(pool))]
    assert len(set(seen)) == len(pool)
    eng.new_session()
    assert eng._session_used == {}


def test_cached_lines_are_preferred_when_the_cloud_voice_has_them():
    eng = PersonalityEngine(rng=random.Random(13))
    slots = ResponseComposer()._slots(facts())
    st = Style(mode="roast")
    cached = {"I saw that."}
    for _ in range(10):
        assert eng.render("reach_first", slots, st, prefer=lambda text, cue: text in cached).text == "I saw that."


def test_judge_demo_has_no_generic_assistant_phrases():
    generic = ("you've got this", "keep that momentum", "let's figure it out", "great work so far", "stay strong", "you're welcome")
    for _cat, ln in ALL_LINES:
        if "judge_demo" in ln.modes:
            assert not any(g in ln.text.lower() for g in generic), ln.text


def test_voice_pack_is_small_grounded_and_covers_every_category():
    from app.voice.personality import voice_pack

    pack = voice_pack()
    assert 25 <= len(pack) <= 40 and len({t for _c, t, _q in pack}) == len(pack)
    cats = {c for c, _t, _q in pack}
    needed = {"session_start", "reach_first", "reach_repeat", "nudge_distracted", "progress_ack", "target_reached", "pause", "welcome_back", "release", "retreat_blocked"}
    assert needed <= cats
    for _c, text, _cue in voice_pack(include_chill=True):
        assert "{" not in text and not re.search(r"\d", text)  # no counts or times baked into audio
        assert content_guard.check(text, max_profanity=2, allow_insults=True).ok


def test_voice_pack_covers_every_category_in_the_main_modes_at_every_profanity_level():
    from app.voice.personality import VOICE_PACK, voice_pack

    pack = {(c, t) for c, t, _ in voice_pack()}
    cats = {c for c, _ in VOICE_PACK} - {"stop_ack", "estop_ack", "retreat_blocked", "robot_back"}
    for mode in ("roast", "unhinged", "judge_demo"):
        for prof in ("off", "mild", "strong"):
            st = Style(mode=mode, profanity=prof, unhinged_opt_in=True)
            for c in cats:
                assert any(
                    (c, ln.text) in pack or (ln.slots == ["attempt_ordinal"] and (c, ln.text.format(attempt_ordinal="third")) in pack)
                    for ln in LIBRARY[c]
                    if st.pool in ln.modes and ln.prof <= st.max_profanity and not (ln.insult and not st.insults_allowed)
                ), (mode, prof, c)
