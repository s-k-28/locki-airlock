import random
import re

from app.voice.conversation import ResponseComposer, SessionFacts, fmt_duration, grounding_violations
from app.voice.personality import LIBRARY, Style


def facts(**kw) -> SessionFacts:
    base = dict(
        session_active=True, task="calculus", elapsed_s=12 * 60 + 10, remaining_s=12 * 60 + 50, planned_s=25 * 60,
        completed_units=2, target_units=None, unit_label="problems", reaches_confirmed=1, vision_available=True,
    )
    base.update(kw)
    return SessionFacts(**base)


def test_status_reports_only_recorded_facts():
    c = ResponseComposer(rng=random.Random(0))
    text = c.status(facts())
    assert text == "You're 12 minutes in, you've finished 2 problems, and I've caught 1 phone reach."
    assert grounding_violations(text, facts()) == []


def test_status_without_camera_does_not_claim_reach_counts():
    c = ResponseComposer()
    text = c.status(facts(vision_available=False, reaches_confirmed=0, completed_units=0))
    assert "camera is off" in text
    assert "reach" in text and "caught" not in text


def test_status_with_target():
    text = ResponseComposer().status(facts(target_units=5))
    assert "finished 2 of 5 problems" in text


def test_next_step_uses_target_math():
    c = ResponseComposer()
    assert c.next_step(facts(target_units=5)) == "3 problems more and you're free."
    assert "hit your target" in c.next_step(facts(target_units=2))


def test_time_left_formats():
    c = ResponseComposer()
    assert c.time_left(facts(remaining_s=301)) == "5 minutes left."
    assert c.time_left(facts(remaining_s=301, paused=True)) == "Paused with 5 minutes left."
    assert c.time_left(SessionFacts()) == "No session running."


def test_grounding_guard_flags_invented_numbers():
    f = facts()
    assert grounding_violations("You're 12 minutes in and did 7 problems.", f) == [7]
    assert grounding_violations("You've caught three reaches", f) == [3]
    assert grounding_violations("Keep that one going", f) == []  # 'one' as a pronoun is not a claim


def test_templates_never_render_with_missing_slots():
    c = ResponseComposer(rng=random.Random(1))
    empty = SessionFacts()
    styles = [Style(mode=m, profanity="strong", unhinged_opt_in=True) for m in ("chill", "roast", "unhinged", "judge_demo")]
    for category in LIBRARY:
        for style in styles + [Style(tone="serious")]:
            for _ in range(3):
                text = c.compose(category, empty, style, new_personality="Chill", reason="disarmed")
                assert "{" not in text and "None" not in text, (category, style, text)


def test_reach_lines_reference_real_counts():
    c = ResponseComposer(rng=random.Random(3))
    f = facts(reaches_confirmed=4)
    for _ in range(10):
        text = c.compose("reach_repeat", f, "coach")
        for n in re.findall(r"\d+", text):
            assert int(n) in f.numbers()


def test_composer_avoids_immediate_repeats():
    c = ResponseComposer(rng=random.Random(5))
    f = facts()
    lines = [c.compose("release", f, "coach") for _ in range(6)]
    assert all(a != b for a, b in zip(lines, lines[1:], strict=False))


def test_fmt_duration():
    assert fmt_duration(5) == "less than a minute"
    assert fmt_duration(45) == "45 seconds"
    assert fmt_duration(60) == "1 minute"
    assert fmt_duration(25 * 60) == "25 minutes"
    assert fmt_duration(90 * 60) == "1 hour 30 minutes"
