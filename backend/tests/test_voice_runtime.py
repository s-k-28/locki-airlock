"""Personality + voice wired into the runtime and the API (simulated robot, no audio, mocked
Fish Audio)."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
from fastapi.testclient import TestClient

from app.config import EGIRL_VOICE_ID, Settings
from app.intelligence.state_machine import State
from app.main import create_app
from app.runtime import LockedInRuntime
from app.simulation.scenarios import simulate_reach
from app.telemetry.events import EventType
from app.voice.audio_cache import AudioCache
from app.voice.content_guard import profanity_level
from app.voice.fish_audio import FishAudioVoice
from app.voice.usage import UsageLedger

from .conftest import RecordingTTS, make_runtime
from .test_fish_audio import KEY, Fake
from .test_speaker import FakeEngine


def client_for(settings, **kw):
    runtime = LockedInRuntime(settings, persist_settings=False, tts_engine=RecordingTTS(), enable_vision=False, enable_voice=False, **kw)
    return runtime, TestClient(create_app(settings, runtime))


# ------------------------------------------------------------------ voice commands
async def test_voice_commands_change_personality_profanity_and_tone(rt):
    await rt.handle_utterance("unhinged mode")
    assert rt.rs.coach.personality == "unhinged" and rt.rs.coach.unhinged_opt_in  # saying it is the opt-in
    await rt.handle_utterance("you can swear a little")
    assert rt.rs.coach.profanity == "mild"
    await rt.handle_utterance("stop swearing")
    assert rt.rs.coach.profanity == "off"
    await rt.handle_utterance("be serious")
    assert rt.rs.coach.tone == "serious" and rt.style().pool == "plain"
    await rt.handle_utterance("you can joke again")
    assert rt.rs.coach.tone == "normal"
    r = await rt.handle_utterance("stop roasting me")
    assert rt.rs.coach.tone == "gentle" and rt.style().pool == "chill" and r.reply
    r = await rt.handle_utterance("judge demo mode")
    assert rt.rs.coach.personality == "judge_demo" and r.reply == "Judge demo mode! Best behavior."


async def test_asking_for_help_drops_the_insults(rt):
    rt.update_settings({"coach": {"personality": "unhinged", "unhinged_opt_in": True, "profanity": "strong"}})
    await rt.start_session("calculus", duration_min=25, speak=False)
    r = await rt.handle_utterance("I'm stuck")
    assert r.ok and rt.personality_snapshot()["supportive_s_left"] > 250
    assert not rt.style().insults_allowed and profanity_level(r.reply) == 0


async def test_reaching_the_target_is_celebrated(rt):
    await rt.start_session("calculus", duration_min=25, target_units=2, unit_label="problems", speak=False)
    await rt.add_progress(1)
    r = await rt.add_progress(1)
    assert rt.last_reply["category"] == "target_reached" and r.reply
    assert any(e.payload.get("milestone") == "target_reached" for e in rt.bus.history(50) if e.event_type == EventType.SESSION_MILESTONE)
    assert rt.snapshot()["expression"]["name"] == "celebration"


async def test_emergency_stop_cancels_jokes_and_never_jokes(settings):
    eng = FakeEngine(0.4)
    rt = await make_runtime(settings, eng)
    try:
        rt.speaker.say("a long joke", priority=3)
        await asyncio.sleep(0.05)
        rt.speaker.say("another joke", priority=3)
        await rt.emergency_stop()
        await asyncio.sleep(0.2)
        outcomes = {h["text"]: h["outcome"] for h in rt.speaker.history}
        assert outcomes["a long joke"] == "interrupted" and outcomes["another joke"].startswith("cancelled")
        assert rt.last_reply["text"] == "Emergency stop. I'm not moving."
        assert rt.snapshot()["expression"]["name"] == "estop"
    finally:
        await rt.shutdown()


async def test_give_me_my_phone_cuts_speech_and_stops_the_robot(settings):
    eng = FakeEngine(0.5)
    rt = await make_runtime(settings, eng)
    try:
        await rt.arm()
        await rt.start_session("essay", duration_min=25, speak=False)
        assert (await rt.manual_move("backward", speed=40, duration_s=1.0)).ok
        rt.speaker.say("a long roast in progress", priority=3)
        await asyncio.sleep(0.05)
        r = await rt.handle_utterance("give me my phone")
        assert r.ok and rt.sm.state == State.RELEASED and rt.sessions.current is None
        assert rt.safety.active is None
        await asyncio.sleep(0.1)
        assert {h["text"]: h["outcome"] for h in rt.speaker.history}["a long roast in progress"] == "interrupted"
    finally:
        await rt.shutdown()


async def test_llm_text_never_moves_the_robot_and_obeys_the_style(rt):
    class FakeLLM:
        def __init__(self, reply):
            self.reply_text, self.styles = reply, []

        async def reply(self, text, facts, style):
            self.styles.append(style)
            return self.reply_text

        def describe(self):
            return {"provider": "fake"}

    await rt.arm()
    await rt.set_autonomy(True)
    await rt.start_session("calculus", duration_min=25, speak=False)
    rt.llm = FakeLLM("Moving forward now. Retreat backward at full speed!")
    r = await rt.handle_utterance("why is calculus so hard")
    assert r.reply.startswith("Moving forward now.")  # spoken, nothing more
    await asyncio.sleep(0.05)
    assert rt.safety.active is None
    assert not any(e.event_type == EventType.ROBOT_MOTION_REQUESTED for e in rt.bus.history(200))
    assert rt.llm.styles[-1].mode == "roast"
    rt.update_settings({"coach": {"personality": "judge_demo", "profanity": "strong"}})
    rt.llm = FakeLLM("Holy shit, calculus is brutal.")
    r = await rt.handle_utterance("why is calculus so hard")
    assert "shit" not in (r.reply or "")  # judge demo is clean: the reply was rejected


async def test_robot_does_not_obey_its_own_voice(rt):
    rt.speaker.say("Emergency stop. I'm not moving.", priority=0)
    await asyncio.sleep(0.05)
    r = await rt.handle_utterance("emergency stop i'm not moving", source="voice", during_playback=True)
    assert not r.ok and "echo" in r.message and rt.sm.state != State.EMERGENCY_STOP
    r = await rt.handle_utterance("how much time is left", source="voice", during_playback=True)
    assert not r.ok and "talking" in r.message
    rt.speaker.say("That's empty air, genius.", priority=3)
    await asyncio.sleep(0.05)
    await rt.start_session("essay", duration_min=25, speak=False)
    r = await rt.handle_utterance("give me my phone", source="voice", during_playback=True)
    assert r.ok and rt.sm.state == State.RELEASED  # the user talking over the robot


# ------------------------------------------------------------------ API
def test_voice_config_presets_and_validation(settings):
    rt, c = client_for(settings)
    with c:
        cfg = c.get("/api/voice/config").json()
        assert cfg["profile"]["reference_id"] == EGIRL_VOICE_ID and cfg["profile"]["provider"] == "fish_audio"
        assert cfg["profile"]["cache_behavior"] == "cache_only" and cfg["active_preset"] == "egirl"
        assert {p["id"] for p in cfg["presets"]} >= {"egirl", "original", "sarcastic-bro", "chaotic-coach", "calm-study-buddy"}
        assert cfg["fish"]["configured"] is False and cfg["fish"]["status"]["state"] == "no_key"
        assert {m["id"] for m in cfg["fish"]["models"]} == {"s2.1-pro-free", "s2.1-pro", "s2-pro", "drama-3", "s1"}
        assert c.put("/api/voice/profile", json={"reference_id": "not-a-voice"}).status_code == 422
        assert c.put("/api/voice/profile", json={"model": "eleven_v4_turbo"}).status_code == 422
        ok = c.put("/api/voice/profile", json={"fish": {"speed": 1.15}}).json()
        assert ok["ok"] and ok["data"]["profile"]["fish"]["speed"] == 1.15 and rt.rs.active_preset is None

        assert c.post("/api/voice/presets/original/apply").json()["ok"] and rt.rs.voice_profile.provider == "system"
        r = c.post("/api/voice/presets/chaotic-coach/apply").json()
        assert "opt-in" in r["message"] and r["data"]["personality"]["effective_mode"] == "roast"
        saved = c.post("/api/voice/presets", json={"name": "My Bro"}).json()
        assert saved["ok"] and any(p["id"] == "my-bro" for p in c.get("/api/voice/presets").json()["presets"])
        assert c.delete("/api/voice/presets/my-bro").json()["ok"]
        assert not c.delete("/api/voice/presets/egirl").json()["ok"]


def test_personality_endpoint_caps_and_opt_in(settings):
    rt, c = client_for(settings)
    with c:
        p = c.post("/api/voice/personality", json={"mode": "judge_demo", "profanity": "strong"}).json()["data"]["personality"]
        assert p["max_profanity"] == "off"
        p = c.post("/api/voice/personality", json={"mode": "unhinged"}).json()["data"]["personality"]
        assert p["effective_mode"] == "roast" and not p["insults_allowed"]
        p = c.post("/api/voice/personality", json={"unhinged_opt_in": True}).json()["data"]["personality"]
        assert p["effective_mode"] == "unhinged" and p["insults_allowed"]


def test_paid_calls_need_a_key_a_plan_and_a_confirmation(settings):
    rt, c = client_for(settings)
    with c:
        r = c.post("/api/voice/test", json={"live": True}).json()
        assert not r["ok"] and "FISH_AUDIO_API_KEY" in r["message"]
        assert c.post("/api/voice/test", json={"live": False}).json()["ok"]  # cache / local route: free
        plan = c.post("/api/voice/cache/plan", json={"scope": "current"}).json()["data"]["plan"]
        assert plan["missing"] > 20 and plan["bytes"] > 0 and plan["estimated_usd"] == 0.0 and plan["configured"] is False
        no = c.post("/api/voice/cache/generate", json={"plan_id": plan["plan_id"], "confirm": False, "max_bytes": plan["bytes"]}).json()
        assert not no["ok"] and "confirmation" in no["message"]
        low = c.post("/api/voice/cache/generate", json={"plan_id": plan["plan_id"], "confirm": True, "max_bytes": 1}).json()
        assert not low["ok"] and "approved" in low["message"]
        nokey = c.post("/api/voice/cache/generate", json={"plan_id": plan["plan_id"], "confirm": True, "max_bytes": plan["bytes"]}).json()
        assert not nokey["ok"] and "FISH_AUDIO_API_KEY" in nokey["message"]


def test_approved_cache_generation_runs_against_the_mock(settings, tmp_path):
    rt, c = client_for(settings)
    fake = Fake()
    rt.fish = FishAudioVoice(KEY, AudioCache(tmp_path / "fish"), UsageLedger(tmp_path / "u.json"), transport=httpx.MockTransport(fake))
    with c:
        plan = c.post("/api/voice/cache/plan", json={"scope": "current"}).json()["data"]["plan"]
        r = c.post("/api/voice/cache/generate", json={"plan_id": plan["plan_id"], "confirm": True, "max_bytes": plan["bytes"]}).json()
        assert r["ok"]
        for _ in range(200):
            job = c.get("/api/voice/config").json()["cache"]["job"]
            if job and job["state"] != "running":
                break
            time.sleep(0.02)
        assert job["state"] == "finished" and job["done"] == plan["missing"]
        assert c.get("/api/voice/config").json()["cache"]["missing"] == 0
        assert len(fake.requests) == plan["missing"]


def test_tests_never_see_the_developers_real_key(settings):
    # A real key in the project .env must never reach the test suite (this once leaked a
    # real request into a test run).
    assert not settings.fish_audio_api_key and not Settings().fish_audio_api_key
    assert settings.fish_audio_base_url == "http://127.0.0.1:9"


def test_the_api_key_never_leaves_the_backend(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, robot_driver="mock", FISH_AUDIO_API_KEY=KEY)
    assert settings.fish_audio_api_key == KEY
    rt, c = client_for(settings)
    with c:
        assert c.get("/api/voice/config").json()["fish"]["configured"] is True
        for path in ("/api/state", "/api/voice/config", "/api/settings", "/api/health", "/api/voice/history", "/api/voice/presets"):
            assert KEY not in json.dumps(c.get(path).json()), path


async def test_robot_failures_get_their_own_lines_only_when_they_happen(rt):
    await rt.arm()
    await rt.set_autonomy(True)
    await rt.start_session("essay", duration_min=25, speak=False)
    await rt.disconnect_robot()  # an operator click: no "robot lost" line
    assert rt.last_reply is None or rt.last_reply["category"] != "robot_lost"
    await rt.connect_robot("mock")
    rt._on_robot_disconnected("link lost (no data from robot)")  # a real drop mid-session
    assert rt.last_reply["category"] == "robot_lost"
    rt._on_robot_connected({"transport": "mock", "simulated": True})
    assert rt.last_reply["category"] == "robot_back"


async def test_a_blocked_retreat_gets_a_robot_failure_line(rt, monkeypatch):
    from app.safety.controller import SafetyDecision

    await rt.arm()
    await rt.set_autonomy(True)
    await rt.start_session("essay", duration_min=25, speak=False)
    rt.sessions.current.started_at_mono -= 400

    async def deny(req, **kw):
        return SafetyDecision(approved=False, reason_code="device_watchdog_unverified", reason="R5 not passed", request=req)

    monkeypatch.setattr(rt.safety, "request", deny)
    await simulate_reach(rt, approach_ms=10)
    assert rt.last_reply["category"] == "retreat_blocked"  # it could not move, and says so
    assert rt.safety.active is None
