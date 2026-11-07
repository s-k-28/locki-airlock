import pytest

from app.voice.intents import IntentClassifier, IntentName, normalise, words_to_numbers

clf = IntentClassifier()


@pytest.mark.parametrize(
    "text,intent",
    [
        ("Start a 25-minute session.", IntentName.START_SESSION),
        ("Lock in.", IntentName.START_SESSION),
        ("LOCKED IN, start a study session.", IntentName.START_SESSION),
        ("Pause.", IntentName.PAUSE),
        ("Stop.", IntentName.STOP_MOTION),
        ("Give me my phone.", IntentName.RELEASE),
        ("Okay, give my phone back.", IntentName.RELEASE),
        ("How much time is left?", IntentName.TIME_LEFT),
        ("What should I do next?", IntentName.NEXT_STEP),
        ("I finished a problem.", IntentName.PROGRESS),
        ("Make it harder.", IntentName.HARDER),
        ("Stop roasting me.", IntentName.TONE_DOWN),
        ("How am I doing?", IntentName.STATUS),
        ("Emergency stop!", IntentName.EMERGENCY_STOP),
        ("resume", IntentName.RESUME),
        ("end the session", IntentName.END_SESSION),
        ("I'm done", IntentName.END_SESSION),
        ("shut up", IntentName.MUTE),
        ("you can talk again", IntentName.UNMUTE),
        ("chaos mode", IntentName.SET_PERSONALITY),
        ("come back", IntentName.RETURN_ROBOT),
        ("hey", IntentName.GREETING),
        ("thanks", IntentName.THANKS),
        ("give me a minute", IntentName.PAUSE),
    ],
)
def test_spec_commands(text, intent):
    result = clf.classify(text)
    assert result.name == intent, (text, result.name, result.rule)


def test_start_session_extracts_duration_and_task():
    r = clf.classify("Lock in. I have 25 minutes to finish calculus.")
    assert r.name == IntentName.START_SESSION and r.addressed
    assert r.slots == {"duration_min": 25.0, "task": "calculus"}

    r = clf.classify("LOCKED IN, I have twenty five minutes to finish calculus. Let's go.")
    assert r.slots["duration_min"] == 25.0 and r.slots["task"] == "calculus"

    r = clf.classify("start a 25-minute session")
    assert r.slots == {"duration_min": 25.0}

    r = clf.classify("locked in start a study session")
    assert r.slots.get("task") == "study"


def test_start_session_with_target_units_is_not_progress():
    r = clf.classify("I have 40 minutes to finish 10 problems")
    assert r.name == IntentName.START_SESSION
    assert r.slots["target_units"] == 10 and r.slots["unit_label"] == "problems"
    assert r.slots["duration_min"] == 40.0


@pytest.mark.parametrize(
    "text,units",
    [
        ("I finished a problem", 1),
        ("I finished two problems", 2),
        ("done with another one", 1),
        ("completed 3 questions", 3),
        # Found with the voice-trial near-misses: these used to end the whole session.
        ("I'm done with this problem", 1),
        ("done with that question", 1),
        ("finished the chapter", 1),
    ],
)
def test_progress_units(text, units):
    r = clf.classify(text)
    assert r.name == IntentName.PROGRESS and r.slots["units"] == units


def test_done_alone_still_ends_the_session():
    assert clf.classify("I'm done").name == IntentName.END_SESSION
    assert clf.classify("I'm done for today").name == IntentName.END_SESSION


def test_hours_and_phrases():
    assert clf.classify("lock in for an hour").slots["duration_min"] == 60.0
    assert clf.classify("locked in half an hour").slots["duration_min"] == 30.0
    assert clf.classify("start a pomodoro").slots["duration_min"] == 25.0


def test_stop_word_flag_is_set_for_any_stop():
    # Even when the intent is a tone change, the runtime gets a stop signal.
    r = clf.classify("stop roasting me")
    assert r.name == IntentName.TONE_DOWN and r.stop_word
    assert clf.classify("freeze").stop_word
    assert not clf.classify("how am I doing").stop_word


def test_unrecognised_command_is_unknown_with_low_confidence():
    r = clf.classify("what's the capital of peru")
    assert r.name == IntentName.UNKNOWN
    assert r.confidence <= 0.3


def test_stt_confidence_caps_intent_confidence():
    r = clf.classify("come back", stt_confidence=0.5)
    assert r.name == IntentName.RETURN_ROBOT
    assert r.confidence == 0.5
    assert r.moves_robot


def test_safety_flags():
    for text in ("stop", "emergency stop", "pause", "give me my phone", "end session"):
        assert clf.classify(text).safety_critical, text
    assert not clf.classify("how am i doing").safety_critical


def test_normalisation():
    assert words_to_numbers("twenty five minutes") == "25 minutes"
    assert words_to_numbers("one hundred") == "100"
    assert normalise("I'm DONE!!") == "i am done"
    assert normalise("Let's go") == "let us go"


def test_wake_word_alone():
    assert clf.classify("locked in").name == IntentName.START_SESSION
    assert clf.classify("hey locked in").name == IntentName.START_SESSION
    r = clf.classify("locked in, 25 minutes")
    assert r.name == IntentName.START_SESSION and r.slots["duration_min"] == 25.0


def test_progress_carries_spoken_unit():
    r = clf.classify("I finished two problems")
    assert r.slots == {"units": 2, "unit_label": "problems"}
    assert "unit_label" not in clf.classify("done with another one").slots


@pytest.mark.parametrize(
    "text,intent,rule",
    [
        ("Lock me in for 25 minutes.", IntentName.START_SESSION, "start_session_natural"),
        ("Help me focus.", IntentName.START_SESSION, "start_session_natural"),
        ("help me stay focused", IntentName.START_SESSION, "start_session_natural"),
        ("Take my phone.", IntentName.START_SESSION, "start_session_natural"),
        ("Locked in, now.", IntentName.START_SESSION, "wake_word_go"),
        ("Locked in please", IntentName.START_SESSION, "wake_word_go"),
        ("Roast me.", IntentName.SET_PERSONALITY, "roast_me"),
        ("Can I have my phone?", IntentName.RELEASE, "release"),
        ("Hold on.", IntentName.PAUSE, "pause"),
        ("Wait a sec.", IntentName.PAUSE, "pause"),
        ("Time out.", IntentName.PAUSE, "pause"),
        ("I am ready.", IntentName.RESUME, "resume"),
        ("How much longer?", IntentName.TIME_LEFT, "time_left"),
        ("Time check.", IntentName.TIME_LEFT, "time_left"),
        ("Am I doing good?", IntentName.STATUS, "status"),
        ("Shush.", IntentName.MUTE, "mute"),
        ("Zip it.", IntentName.MUTE, "mute"),
        ("Pipe down.", IntentName.MUTE, "mute"),
        ("That is enough.", IntentName.END_SESSION, "end_session"),
        ("Stop the timer.", IntentName.END_SESSION, "end_session"),
        ("Enough roasting.", IntentName.TONE_DOWN, "tone_down"),
        ("Who are you?", IntentName.ABOUT, "about"),
        ("What can you do?", IntentName.ABOUT, "about"),
        # "help" on its own is still the stuck-on-the-work reply
        ("help me", IntentName.HELP, "help"),
        ("I need help", IntentName.HELP, "help"),
    ],
)
def test_natural_phrases(text, intent, rule):
    result = clf.classify(text)
    assert (result.name, result.rule) == (intent, rule), (text, result.name, result.rule)


def test_roast_me_switches_personality_and_says_so():
    r = clf.classify("Roast me.")
    assert r.slots == {"personality": "roast", "roast_me": True}


@pytest.mark.parametrize(
    "text,conflict",
    [
        ("Don't come back.", "negated or retracted"),
        ("Come back, no, go left.", "negated or retracted"),
        ("Never mind, come here.", "negated or retracted"),
        ("Come back.", None),
        ("Go back home.", None),
    ],
)
def test_contradictory_movement_requests_are_flagged(text, conflict):
    r = clf.classify(text)
    assert r.name == IntentName.RETURN_ROBOT and r.conflict == conflict
    assert r.to_payload()["conflict"] == conflict


def test_more_than_one_movement_is_a_conflict():
    from app.voice.intents import movement_conflict

    assert movement_conflict("come back then go forward") == "more than one movement"
    assert movement_conflict("come back") is None


def test_a_stop_word_is_never_a_conflict():
    # "stop, don't come back" must still stop: stop words are matched before movement.
    r = clf.classify("stop, don't come back")
    assert r.name == IntentName.STOP_MOTION and r.conflict is None
