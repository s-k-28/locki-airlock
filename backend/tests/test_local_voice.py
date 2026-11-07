"""Local voice (macOS `say`): a render someone is waiting for goes before the background
prewarm, clips are written atomically, and the next "N minutes left." is rendered ahead
during a session. `say` itself is replaced by writing a short WAV."""

from __future__ import annotations

import threading
import time
import wave

import numpy as np

from app.voice.tts import CachedSystemTTS

from .conftest import make_runtime


class FakeSay(CachedSystemTTS):
    def __init__(self, cache_dir, delay: float = 0.0, fail: set[str] | None = None) -> None:
        super().__init__(cache_dir)
        self.system = "Darwin"
        self.delay = delay
        self.fail = fail or set()
        self.calls: list[tuple[str, float, float]] = []  # (text, started, ended)
        self.started = threading.Event()

    def available(self) -> bool:
        return True

    def _say_to(self, text: str, path: str) -> None:
        t0 = time.monotonic()
        self.started.set()
        time.sleep(self.delay)
        if text in self.fail:
            with open(path, "wb") as fh:
                fh.write(b"half a clip")
            self.calls.append((text, t0, time.monotonic()))
            raise RuntimeError("say failed")
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(22050)
            w.writeframes(np.zeros(2205, dtype=np.int16).tobytes())
        self.calls.append((text, t0, time.monotonic()))

    def texts(self) -> list[str]:
        return [c[0] for c in self.calls]


def test_a_live_render_is_kept_so_asking_again_is_instant(tmp_path):
    say = FakeSay(tmp_path)
    assert say.clip_for("24 minutes left.") is None
    clip = say.render("24 minutes left.")
    assert clip is not None and say.clip_for("24 minutes left.") is clip
    for n in range(say.DYNAMIC_KEEP + 4):
        say.render(f"{n} minutes left.")
    assert say.clip_for("24 minutes left.") is None  # only the most recent few are kept
    assert say.clip_for(f"{say.DYNAMIC_KEEP + 3} minutes left.") is not None


def test_prewarm_pauses_while_someone_waits_for_a_render(tmp_path):
    say = FakeSay(tmp_path, delay=0.1)
    worker = threading.Thread(target=say.prewarm, args=(["p1", "p2", "p3"],))
    worker.start()
    assert say.started.wait(2.0)
    say.render("25 minutes left.")  # arrives while p1 is being rendered
    worker.join(5.0)
    started = {text: (t0, t1) for text, t0, t1 in say.calls}
    assert sorted(started, key=lambda t: started[t][0]) == ["p1", "25 minutes left.", "p2", "p3"]
    live_end = started["25 minutes left."][1]
    assert started["p2"][0] >= live_end  # the prewarm waited until the live render was done
    assert say.prewarmed == 3


def test_prewarm_stops_at_shutdown_without_waiting_for_the_rest(tmp_path):
    say = FakeSay(tmp_path, delay=0.05)
    say._demand = 1  # a render that never finishes: the prewarm must still notice the shutdown
    worker = threading.Thread(target=say.prewarm, args=(["p1", "p2"],))
    worker.start()
    time.sleep(0.15)
    say.prewarm_cancelled = True
    worker.join(2.0)
    assert not worker.is_alive()
    assert say.texts() == []


def test_a_failed_render_leaves_no_half_written_clip(tmp_path):
    say = FakeSay(tmp_path, fail={"bad line"})
    say.prewarm(["bad line", "good line"])
    assert not say._path("bad line").exists()
    assert list(tmp_path.glob("*.part")) == []
    assert say.clip_for("bad line") is None
    assert say.clip_for("good line") is not None
    assert say.prewarmed == 1


def test_ensure_renders_only_what_is_missing(tmp_path):
    say = FakeSay(tmp_path)
    say.prewarm(["Fixed line."])
    assert say.ensure(["Fixed line.", "25 minutes left.", "24 minutes left."]) == 2
    assert say.ensure(["25 minutes left.", "24 minutes left."]) == 0
    assert say.texts() == ["Fixed line.", "25 minutes left.", "24 minutes left."]
    assert say.rendered_ahead == 2
    assert say.describe()["rendered_ahead"] == 2


async def test_prewarm_order_puts_lines_the_local_voice_will_speak_first(settings, tts):
    rt = await make_runtime(settings, tts)
    try:
        rt._cached_line_check = lambda: (lambda text, cue: text == "cloud has this")
        lines = [
            ("reach_first", "reaction", None),
            ("session_start", "cloud has this", None),
            ("session_start", "reply", None),
            ("estop_ack", "stop", None),
            ("pause", "reaction", None),  # same text as a P3 reaction: ranked by its best use
        ]
        assert rt._prewarm_order(lines) == ["stop", "reaction", "reply", "cloud has this"]
    finally:
        await rt.shutdown()


async def test_time_left_is_rendered_ahead_during_a_session(settings, tts, tmp_path):
    rt = await make_runtime(settings, tts)
    say = FakeSay(tmp_path / "say")
    rt._local_tts = lambda: say
    try:
        rt._render_ahead()
        assert rt._render_ahead_task is None  # no session: nothing to predict
        res = await rt.start_session(task="essay", duration_min=25)
        assert res.ok
        rt._render_ahead()
        await rt._render_ahead_task
        assert say.texts() == ["25 minutes left.", "24 minutes left."]
        assert say.clip_for(rt.composer.time_left(rt.facts())) is not None
    finally:
        await rt.shutdown()


def test_volume_settings_are_parsed():
    from app.voice.tts import parse_volume_settings

    muted = parse_volume_settings("output volume:16, input volume:42, alert volume:100, output muted:true")
    assert muted == {"volume": 16, "muted": True}
    assert parse_volume_settings("output volume:missing value, input volume:42, alert volume:100, output muted:false") == {
        "volume": None, "muted": False,
    }
    assert parse_volume_settings("execution error") is None


async def test_a_muted_robot_speaker_is_reported(settings, tts):
    rt = await make_runtime(settings, tts)
    try:
        rt.rs.voice.output_device = "JLab GO Party"
        rt._output_probe = (time.monotonic(), ["JLab GO Party", "MacBook Air Speakers"], "JLab GO Party")
        assert rt.audio_output_status()["warning"] is None  # not checked yet: no claim either way
        rt._os_volume = {"volume": 16, "muted": True, "checked_at": time.time()}
        out = rt.audio_output_status()
        assert out["warning"].startswith("muted in macOS") and out["system_volume"]["applies"]
        assert rt.snapshot()["voice"]["output"]["warning"] == out["warning"]
        rt._os_volume = {"volume": 0, "muted": False, "checked_at": time.time()}
        assert "volume is 0" in rt.audio_output_status()["warning"]
        # macOS reports the default output only: no warning about a speaker it did not measure.
        rt._output_probe = (time.monotonic(), ["JLab GO Party", "MacBook Air Speakers"], "MacBook Air Speakers")
        out = rt.audio_output_status()
        assert out["warning"] is None and out["system_volume"]["applies"] is False
    finally:
        await rt.shutdown()
