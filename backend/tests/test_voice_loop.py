"""Milestone 4 through the runtime: every voice turn is recorded from speech to audible reply,
natural phrases act, "stop" holds the robot still, the robot's own voice is never obeyed,
reactions stay rare, and the new hardware gates fail closed. Simulated robot, no audio."""

from __future__ import annotations

import time

from app.intelligence.state_machine import State
from app.runtime import FOCUS_STREAK_S, REACH_WARNING_COOLDOWN_S, STOP_SUPPRESS_S, VisionEvent
from app.sessions.metrics import FocusState
from app.telemetry.events import EventType
from app.voice.personality import LIBRARY
from app.voice.tts import SpeechRequest, SpeechResult

from .conftest import RecordingTTS, make_runtime, wait_for


class PlayingTTS(RecordingTTS):
    """Reports when playback started, as the real audio output does."""

    async def speak_request(self, req: SpeechRequest) -> SpeechResult:
        self.spoken.append(req.text)
        return SpeechResult("fish_cache", started_mono=time.monotonic(), output_latency_ms=120.0)


def texts(category: str) -> set[str]:
    return {ln.text for ln in LIBRARY[category]}


# ------------------------------------------------------------------ voice turns
async def test_every_turn_is_recorded_from_speech_to_the_reply_playing(settings):
    rt = await make_runtime(settings, PlayingTTS())
    try:
        heard = time.monotonic()
        r = await rt.handle_utterance("Lock me in for 25 minutes.", source="voice", stt_confidence=0.9, stt_ms=812.0, heard_mono=heard)
        assert r.ok and rt.sessions.active
        turn = rt.voice_turns[-1]
        assert turn["text"] == "Lock me in for 25 minutes." and turn["stt_ms"] == 812.0
        assert turn["intent"]["intent"] == "start_session" and turn["intent"]["rule"] == "start_session_natural"
        assert turn["ok"] and turn["action"].startswith("session started")
        assert turn["replies"][0]["category"] == "session_start"
        assert await wait_for(lambda: turn["reply_to_audio_ms"] is not None)
        assert turn["reply_source"] == "Cached Fish Audio" and turn["output_latency_ms"] == 120.0
        assert 0 <= turn["reply_to_audio_ms"] < 1000
        published = [e for e in rt.bus.history() if e.event_type == EventType.VOICE_TURN]
        assert published[-1].payload["id"] == turn["id"] and "heard_mono" not in published[-1].payload
        assert rt.snapshot()["voice"]["turns"][0]["id"] == turn["id"]
    finally:
        await rt.shutdown()


async def test_a_reply_that_never_plays_has_no_latency(rt):
    """The test TTS reports no playback start (like text-only output): no number is invented."""
    await rt.handle_utterance("Lock me in for 25 minutes.", source="voice", heard_mono=time.monotonic())
    turn = rt.voice_turns[-1]
    assert await wait_for(lambda: rt.speaker.spoken >= 1)
    assert turn["reply_to_audio_ms"] is None


async def test_spoken_lock_me_in_turns_the_camera_on_but_typed_does_not(rt):
    calls = []

    async def ensure():
        calls.append(1)
        return "camera starting"

    rt._ensure_monitoring = ensure
    await rt.handle_utterance("lock me in", source="typed")
    assert calls == []
    await rt.handle_utterance("I'm done")
    r = await rt.handle_utterance("lock me in", source="voice")
    assert r.ok and calls == [1] and r.data["monitoring"] == "camera starting"


async def test_hands_free_mic_does_not_answer_what_it_does_not_understand(rt, tts):
    rt.rs.voice.listen_mode = "always_on"
    before = len(tts.spoken)
    r = await rt.handle_utterance("so anyway the weather is nice", source="voice")
    assert not r.ok and "hands-free: not answered" in r.message
    assert rt.voice_turns[-1]["replies"] == []
    rt.rs.voice.listen_mode = "push_to_talk"  # pressing the button means it was meant for the robot
    r = await rt.handle_utterance("so anyway the weather is nice", source="voice")
    assert r.reply
    assert await wait_for(lambda: len(tts.spoken) > before)


async def test_roast_me_and_who_are_you(rt):
    rt.update_settings({"coach": {"personality": "chill"}})
    r = await rt.handle_utterance("Roast me.", source="voice")
    assert r.ok and rt.rs.coach.personality == "roast" and r.reply in texts("roast_me")
    r = await rt.handle_utterance("Who are you?", source="voice")
    assert r.ok and r.reply in texts("about")


async def test_a_contradictory_movement_request_moves_nothing(rt):
    r = await rt.handle_utterance("come back, no, go left", source="voice")
    assert not r.ok and "conflicting movement command" in r.message
    assert not [e for e in rt.bus.history() if e.event_type == EventType.ROBOT_MOTION_REQUESTED]


# ------------------------------------------------------------------ stop
async def test_stop_holds_the_robot_still_for_a_while(rt):
    assert rt.safety.arm()[0]
    await rt.start_session("essay", duration_min=25, speak=False)
    r = await rt.handle_utterance("stop", source="voice")
    assert r.ok and f"new motion refused for {STOP_SUPPRESS_S:.0f} s" in r.message
    assert r.data["hardware_stop"]["kind"] == "simulated"
    left = rt.snapshot()["safety"]["motion_suppressed_s"]
    assert left is not None and 0 < left <= STOP_SUPPRESS_S
    moved = await rt.manual_move("backward", speed=40, duration_s=0.2)
    assert not moved.ok and moved.data["reason_code"] == "motion_suppressed"
    assert [e for e in rt.bus.history() if e.event_type == EventType.SAFETY_MOTION_SUPPRESSED]
    rt.safety.suppressed_until = time.monotonic() - 0.1  # 15 s later
    assert (await rt.manual_move("backward", speed=40, duration_s=0.2)).ok


async def test_the_action_log_shows_requests_denials_and_stops(rt):
    assert rt.safety.arm()[0]
    assert (await rt.manual_move("forward", speed=40, duration_s=0.15)).ok
    assert await wait_for(lambda: rt.actions and rt.actions[-1]["status"] in ("simulated, ended", "ended"), timeout=3.0)
    await rt.stop_robot(origin="operator")
    rt.safety.suppress_motion(STOP_SUPPRESS_S, "test")
    await rt.manual_move("forward", speed=40, duration_s=0.15)
    log = rt.snapshot()["robot"]["actions"]  # newest first
    assert log[0]["status"] == "denied" and log[0]["reason_code"] == "motion_suppressed"
    assert log[1]["kind"] == "stop" and log[1]["origin"] == "operator"
    assert log[2]["what"] == "forward" and not log[2]["physical"]


# ------------------------------------------------------------------ the robot's own voice
async def test_whisper_padding_around_the_robots_own_line_is_still_echo(rt):
    """Measured on the real JLab speaker (docs/AUDIO_TESTS.md A6): the mic heard "I found one
    too. Emergency stop. I'm not moving." right after the robot said the last two sentences."""
    rt.speaker.say("Emergency stop. I'm not moving.", priority=0)
    assert await wait_for(lambda: rt.speaker.spoken >= 1 and not rt.speaker.speaking)
    r = await rt.handle_utterance("I found one too. Emergency stop. I'm not moving.", source="voice", during_playback=False)
    assert not r.ok and "echo" in r.message
    assert rt.sm.state != State.EMERGENCY_STOP


async def test_the_users_own_stop_right_after_stopped_is_obeyed(rt):
    rt.speaker.say("Stopped.", priority=0)
    assert await wait_for(lambda: rt.speaker.spoken >= 1 and not rt.speaker.speaking)
    r = await rt.handle_utterance("stop", source="voice")
    assert r.ok and "stop" in r.message


async def test_words_added_on_top_of_the_echo_are_acted_on(rt):
    await rt.start_session("essay", duration_min=25, speak=False)
    rt.speaker.say("Paused. Don't get comfortable.", priority=2)
    assert await wait_for(lambda: rt.speaker.spoken >= 1 and not rt.speaker.speaking)
    r = await rt.handle_utterance("paused don't get comfortable give me my phone", source="voice")
    assert r.ok and rt.sm.state == State.RELEASED
    assert rt.voice_turns[-1]["echo_removed"] >= 0.6


# ------------------------------------------------------------------ reactions
async def test_a_hand_heading_for_the_phone_gets_one_warning_not_a_stream(rt):
    await rt.start_session("essay", duration_min=25, speak=False)
    ev = VisionEvent("approach", frame_mono=time.monotonic())
    rt._maybe_warn_reach(ev)
    said = [e for e in rt.bus.history() if e.event_type == EventType.VOICE_RESPONSE_GENERATED and e.payload["category"] == "reach_warning"]
    assert len(said) == 1 and said[0].payload["text"] in texts("reach_warning")
    rt._maybe_warn_reach(ev)  # again within the cooldown
    rt._last_reach_warning -= REACH_WARNING_COOLDOWN_S / 2
    rt._maybe_warn_reach(ev)
    said = [e for e in rt.bus.history() if e.event_type == EventType.VOICE_RESPONSE_GENERATED and e.payload["category"] == "reach_warning"]
    assert len(said) == 1
    await rt.pause_session()
    rt._last_reach_warning = 0.0
    rt._maybe_warn_reach(ev)  # paused: nothing to protect
    assert len([e for e in rt.bus.history() if e.event_type == EventType.VOICE_RESPONSE_GENERATED and e.payload["category"] == "reach_warning"]) == 1


async def test_a_focus_streak_is_praised_once_and_only_when_seen(rt):
    await rt.start_session("essay", duration_min=60, speak=False)
    rt._session_started_mono = time.monotonic() - FOCUS_STREAK_S - 1
    rt._evaluate_focus_streak()  # focus unknown (no camera): no praise
    assert not [e for e in rt.bus.history() if e.payload.get("category") == "focus_streak"]
    rt.focus.set_state(FocusState.FOCUSED)
    rt._evaluate_focus_streak()
    rt._evaluate_focus_streak()
    praised = [e for e in rt.bus.history() if e.event_type == EventType.VOICE_RESPONSE_GENERATED and e.payload["category"] == "focus_streak"]
    assert len(praised) == 1


async def test_time_left_needs_no_live_render(rt):
    await rt.start_session("essay", duration_min=25, speak=False)
    r = await rt.handle_utterance("how much longer", source="voice")
    assert r.ok and r.reply in ("25 minutes left.", "24 minutes left.")


# ------------------------------------------------------------------ gates that fail closed
async def test_perception_check_fails_closed(settings, tts):
    rt = await make_runtime(settings, tts)
    try:
        ok, why = rt._perception_check()
        assert not ok and "LOCKEDIN_ZEUS_TRACKING_VERIFIED=false" in why
        rt.settings = rt.settings.model_copy(update={"zeus_tracking_verified": True})
        rt.vision_status = {"running": False}
        assert rt._perception_check() == (False, "camera not live")
        rt.vision_status = {"running": True, "camera": "live", "link": {"frame_age_ms": 2500}}
        assert rt._perception_check() == (False, "camera frames 2500 ms old")
        rt.vision_status = {"running": True, "camera": "live", "zone_mode": "marker", "tracking": {"state": "LOST"}}
        assert rt._perception_check() == (False, "robot marker not tracked")
        rt.vision_status = {"running": True, "camera": "live", "zone_mode": "marker", "tracking": {"state": "TRACKING"}, "link": {"frame_age_ms": 80}}
        assert rt._perception_check() == (True, "camera live, robot tracked")
    finally:
        await rt.shutdown()


async def test_e_stop_while_speaking_cuts_the_line(rt, tts):
    rt.speaker.say("This is a long line about your focus that keeps going for quite a while.", priority=3)
    r = await rt.handle_utterance("emergency stop", source="voice", during_playback=True)
    assert r.ok and rt.sm.state == State.EMERGENCY_STOP and rt.safety.estop_latched


async def test_repeated_start_and_stop_stay_consistent(rt):
    for _ in range(3):
        assert (await rt.handle_utterance("lock me in for 10 minutes")).ok
        r = await rt.handle_utterance("lock me in for 10 minutes")
        assert not r.ok or "already" in (r.message + (r.reply or "")).lower()
        assert (await rt.handle_utterance("I'm done")).ok
        assert not rt.sessions.active
