"""Voice trial tooling: scoring, dry run, persistence. Utterances are injected as if they came
from the microphone (source="voice"); this tests the bookkeeping, not speech recognition."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.intelligence.state_machine import State
from app.main import create_app
from app.runtime import LockedInRuntime
from app.voice.trials import PROMPTS

from .conftest import RecordingTTS


async def say(rt, text: str, conf: float = 0.9):
    return await rt.handle_utterance(text, source="voice", stt_confidence=conf, stt_ms=480.0)


async def test_prompts_score_correct_wrong_slots_rejected_and_missed(rt):
    vt = rt.voice_trials
    vt.start_block("A", "quiet room")
    vt.set_prompt("start_session", "Start a 25-minute session.", {"duration_min": 25})
    r = await say(rt, "start a 25 minute session")
    assert r.data["trial"]["outcome"] == "correct"
    vt.set_prompt("start_session", "Start a 25-minute session.", {"duration_min": 25})
    assert (await say(rt, "start a 40 minute session")).data["trial"]["outcome"] == "wrong_slots"
    vt.set_prompt("time_left", "How much time is left?")
    assert (await say(rt, "how am I doing")).data["trial"]["outcome"] == "wrong_intent"
    vt.set_prompt("pause", "Pause.")
    assert (await say(rt, "banana telescope")).data["trial"]["outcome"] == "rejected"
    vt.set_prompt("status", "How am I doing?")
    assert vt.miss()["outcome"] == "no_response"
    s = vt.snapshot()["groups"][0]
    assert (s["prompts"], s["correct"], s["wrong_slots"], s["wrong_intent"], s["rejected"], s["no_response"]) == (5, 1, 1, 1, 1, 1)
    assert s["accuracy"] == 0.2 and s["stt_ms_p50"] == 480.0


async def test_dry_run_never_acts_but_stop_words_still_halt_motion(rt):
    await rt.arm()
    rt.voice_trials.start_block("A", "quiet room")
    rt.voice_trials.set_prompt("start_session", "Lock in.")
    r = await say(rt, "lock in, 25 minutes")
    assert r.data["trial"]["outcome"] == "correct"
    assert rt.sessions.current is None and rt.sm.state == State.IDLE  # scored, not executed
    assert (await rt.manual_move("forward", speed=40, duration_s=1.0)).ok
    rt.voice_trials.set_prompt("stop_motion", "Stop.")
    await say(rt, "stop")
    assert rt.safety.active is None  # the stop-word fail-safe runs before any trial logic


async def test_execute_mode_runs_the_command(rt):
    rt.voice_trials.start_block("B", "quiet room", execute=True)
    rt.voice_trials.set_prompt("start_session", "Lock in.")
    r = await say(rt, "lock in, 25 minutes")
    assert r.ok and rt.sessions.active


async def test_near_misses_and_quiet_windows_count_false_activations(rt):
    vt = rt.voice_trials
    vt.start_block("A", "hackathon noise")
    vt.set_prompt("none", "My phone is over there somewhere.")
    assert (await say(rt, "my phone is over there somewhere")).data["trial"]["outcome"] in ("correct_reject", "false_activation")
    vt.start_quiet(60)
    assert (await say(rt, "pass me the charger")).data["trial"]["outcome"] == "correct_reject"
    assert (await say(rt, "give me my phone")).data["trial"]["outcome"] == "false_activation"
    blk = vt.end_block()["summary"]
    assert blk["quiet_false_activations"] == 1 and blk["risky_errors"] >= 1  # a stray "release" ends sessions


async def test_wake_word_mode_is_scored_as_configured(rt):
    rt.rs.voice.listen_mode = "wake_word"
    rt.voice_trials.start_block("A", "quiet room")
    rt.voice_trials.set_prompt("time_left", "How much time is left?")
    rec = (await say(rt, "how much time is left")).data["trial"]
    assert rec["outcome"] == "rejected" and rec["rejected_reason"] == "not addressed to LOCKED IN"


async def test_trials_are_written_as_text_lines(rt, settings):
    vt = rt.voice_trials
    vt.start_block("A", "quiet room")
    vt.set_prompt("pause", "Pause.")
    await say(rt, "pause")
    vt.end_block()
    lines = [json.loads(x) for x in (settings.data_dir / "experiments" / "voice_trials.jsonl").read_text().splitlines()]
    assert [x["kind"] for x in lines] == ["utterance", "block"]
    assert lines[0]["heard_text"] == "pause" and "audio" not in json.dumps(lines)


def test_voice_trial_api(settings):
    runtime = LockedInRuntime(settings, persist_settings=False, tts_engine=RecordingTTS(), enable_vision=False, enable_voice=False)
    with TestClient(create_app(settings, runtime)) as c:
        assert len(c.get("/api/experiments/voice/prompts").json()["prompts"]) == len(PROMPTS)
        assert c.post("/api/experiments/voice/prompt", json={"expected": "pause", "phrase": "Pause."}).status_code == 409
        assert c.post("/api/experiments/voice/block", json={"speaker": "A", "condition": "quiet"}).json()["ok"]
        assert c.post("/api/experiments/voice/prompt", json={"expected": "teleport", "phrase": "x"}).status_code == 422
        assert c.post("/api/experiments/voice/prompt", json={"expected": "pause", "phrase": "Pause."}).json()["ok"]
        assert c.post("/api/experiments/voice/miss").json()["record"]["outcome"] == "no_response"
        assert c.post("/api/experiments/voice/quiet", json={"seconds": 5}).status_code == 422
        snap = c.get("/api/state").json()["experiment"]["voice"]
        assert snap["active"] and snap["block"]["speaker"] == "A"
        assert c.post("/api/experiments/voice/end").json()["block"]["summary"]["no_response"] == 1
