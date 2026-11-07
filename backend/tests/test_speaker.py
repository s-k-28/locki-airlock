"""Speech queue: priorities, interruption, stale lines, device failures. No audio hardware."""

from __future__ import annotations

import asyncio

from app.telemetry.events import EventBus
from app.voice.tts import (
    P0_SAFETY,
    P1_ESSENTIAL,
    P2_REPLY,
    P3_AMBIENT,
    AudioDeviceError,
    Speaker,
    SpeechResult,
    TTSEngine,
    echo_similarity,
)


class FakeEngine(TTSEngine):
    name = "fake"

    def __init__(self, seconds: float = 0.15, source: str = "fish_cache"):
        self.seconds = seconds
        self.source = source
        self.started: list[str] = []
        self.finished: list[str] = []
        self.fail_with: Exception | None = None

    def available(self) -> bool:
        return True

    async def speak(self, text: str) -> None:  # pragma: no cover - speak_request is used
        await asyncio.sleep(self.seconds)

    async def speak_request(self, req) -> SpeechResult:
        self.started.append(req.text)
        if self.fail_with is not None:
            raise self.fail_with
        await asyncio.sleep(self.seconds)
        self.finished.append(req.text)
        return SpeechResult(self.source)


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


async def make(seconds=0.15, clock=None):
    eng = FakeEngine(seconds)
    sp = Speaker(eng, EventBus(), clock=clock or (lambda: asyncio.get_running_loop().time()))
    sp.start()
    return sp, eng


async def settle(sp: Speaker, timeout: float = 3.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if not sp._items and sp._current is None:
            return
        await asyncio.sleep(0.01)


def outcomes(sp: Speaker) -> dict[str, str]:
    return {h["text"]: h["outcome"] for h in sp.history}


async def test_higher_priority_goes_first():
    sp, eng = await make(0.1)
    sp.say("answer one", priority=P2_REPLY)
    await asyncio.sleep(0.02)  # "answer one" is now playing
    sp.say("joke", priority=P3_AMBIENT, max_age_s=10)
    sp.say("answer two", priority=P2_REPLY)
    sp.say("status", priority=P1_ESSENTIAL)
    await settle(sp)
    assert eng.finished == ["answer one", "status", "answer two", "joke"]
    await sp.close()


async def test_safety_line_cuts_speech_and_clears_the_queue():
    sp, eng = await make(0.3)
    sp.say("a long answer", priority=P2_REPLY)
    await asyncio.sleep(0.05)
    sp.say("status later", priority=P1_ESSENTIAL)
    sp.say("a joke", priority=P3_AMBIENT)
    sp.say("Stopped.", priority=P0_SAFETY)
    await settle(sp)
    assert eng.finished == ["Stopped."]
    o = outcomes(sp)
    assert o["a long answer"] == "interrupted"
    assert o["status later"].startswith("cancelled") and o["a joke"].startswith("cancelled")
    await sp.close()


async def test_no_outdated_joke_plays_after_an_interrupt():
    sp, eng = await make(0.2)
    sp.say("first joke", priority=P3_AMBIENT)
    await asyncio.sleep(0.02)
    sp.say("second joke", priority=P3_AMBIENT)
    sp.stop_all("emergency stop")
    sp.say("Emergency stop. I'm not moving.", priority=P0_SAFETY, force=True)
    await settle(sp)
    await asyncio.sleep(0.3)
    assert "second joke" not in eng.started
    assert eng.finished == ["Emergency stop. I'm not moving."]
    await sp.close()


async def test_a_burst_of_reach_jokes_speaks_at_most_one_more():
    sp, eng = await make(0.25)
    sp.say("an answer", priority=P2_REPLY)
    await asyncio.sleep(0.02)
    for i in range(5):
        sp.say(f"insult {i}", priority=P3_AMBIENT, max_age_s=10)
    await settle(sp)
    jokes = [t for t in eng.finished if t.startswith("insult")]
    assert jokes == ["insult 4"]  # only the newest waited; the rest were replaced
    assert sum(1 for h in sp.history if h["outcome"] == "replaced") == 4
    await sp.close()


async def test_an_answer_cuts_a_playing_joke():
    sp, eng = await make(0.3)
    sp.say("a joke", priority=P3_AMBIENT)
    await asyncio.sleep(0.05)
    sp.say("how much time is left: 5 minutes", priority=P2_REPLY)
    await settle(sp)
    assert outcomes(sp)["a joke"] == "interrupted"
    assert eng.finished == ["how much time is left: 5 minutes"]
    await sp.close()


async def test_mute_silences_now_and_drops_new_lines_except_forced():
    sp, eng = await make(0.3)
    sp.say("talking", priority=P2_REPLY)
    await asyncio.sleep(0.05)
    sp.stop_all("mute")
    sp.muted = True
    assert sp.say("a joke", priority=P3_AMBIENT) is False
    assert sp.say("Stopped.", priority=P0_SAFETY, force=True) is True
    await settle(sp)
    o = outcomes(sp)
    assert o["talking"] == "interrupted" and o["a joke"] == "muted"
    assert eng.finished == ["Stopped."]
    await sp.close()


async def test_duplicate_lines_are_suppressed():
    sp, eng = await make(0.05)
    assert sp.say("Nice try.", priority=P3_AMBIENT)
    assert sp.say("Nice try.", priority=P3_AMBIENT) is False
    await settle(sp)
    assert eng.finished == ["Nice try."]
    await sp.close()


async def test_stale_lines_are_dropped():
    clock = Clock()
    sp, eng = await make(0.05, clock=clock)
    eng.seconds = 0.0
    sp.say("blocking answer", priority=P2_REPLY)
    sp.say("old joke", priority=P3_AMBIENT)
    clock.t += 10  # the joke waited longer than its 4 s shelf life
    await settle(sp)
    assert outcomes(sp)["old joke"] == "stale"
    await sp.close()


async def test_device_failure_degrades_to_text_and_recovers():
    clock = Clock()
    sp, eng = await make(0.0, clock=clock)
    eng.fail_with = AudioDeviceError("output device JLab GO unavailable")
    sp.say("first", priority=P2_REPLY)
    await settle(sp)
    assert outcomes(sp)["first"] == "device error" and sp.audio_error
    eng.fail_with = None
    sp.say("second", priority=P2_REPLY)
    await settle(sp)
    assert "second" not in eng.started  # within the retry window: text only, no hang
    assert next(h for h in sp.history if h["text"] == "second")["source"] == "text_only"
    clock.t += 20
    sp.say("third", priority=P2_REPLY)
    await settle(sp)
    assert "third" in eng.finished and sp.audio_error is None
    await sp.close()


async def test_push_to_talk_barge_in_keeps_status_lines():
    sp, eng = await make(0.3)
    sp.say("an answer", priority=P2_REPLY)
    await asyncio.sleep(0.05)
    sp.say("status", priority=P1_ESSENTIAL)
    sp.say("a joke", priority=P3_AMBIENT)
    sp.barge_in()
    await settle(sp)
    o = outcomes(sp)
    assert o["an answer"] == "interrupted" and o["a joke"].startswith("cancelled")
    assert "status" in eng.finished
    await sp.close()


async def test_history_records_where_audio_came_from():
    sp, eng = await make(0.0)
    eng.source = "fish_live"
    sp.say("hello there", priority=P2_REPLY)
    await settle(sp)
    h = sp.history[-1]
    assert h["source"] == "fish_live" and h["source_label"] == "Live Fish Audio" and h["priority"] == "P2"
    await sp.close()


async def test_echo_score_recognises_the_robots_own_line():
    sp, eng = await make(0.0)
    sp.say("Emergency stop. I'm not moving.", priority=P0_SAFETY)
    await settle(sp)
    assert sp.echo_score("emergency stop im not moving") >= 0.6
    assert sp.echo_score("stop") >= 0.6  # a fragment of its own line, still echo
    assert sp.echo_score("give me my phone") < 0.6
    assert echo_similarity("[excited] Holy shit", "holy shit") == 1.0
    await sp.close()


async def test_a_comedic_beat_waits_but_a_safety_line_does_not():
    sp, eng = await make(0.05)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    sp.say("Nice try.", priority=P3_AMBIENT, delay_s=0.4)
    await asyncio.sleep(0.05)
    sp.say("Stopped.", priority=P0_SAFETY)
    await settle(sp)
    assert eng.finished == ["Stopped."]  # the joke waiting in its beat was cut by the stop
    sp.say("I saw that.", priority=P3_AMBIENT, delay_s=0.3)
    started = loop.time()
    await settle(sp)
    assert eng.started[-1] == "I saw that." and loop.time() - started >= 0.3
    assert loop.time() - t0 < 2
    await sp.close()
