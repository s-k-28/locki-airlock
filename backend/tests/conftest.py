from __future__ import annotations

import asyncio

import pytest

from app.config import RuntimeSettings, Settings
from app.runtime import LockedInRuntime
from app.voice.tts import TTSEngine


class RecordingTTS(TTSEngine):
    """Captures what the robot would say, instantly."""

    name = "recording"

    def __init__(self) -> None:
        self.spoken: list[str] = []

    def available(self) -> bool:
        return True

    async def speak(self, text: str) -> None:
        self.spoken.append(text)
        await asyncio.sleep(0)


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture(autouse=True)
def _no_real_cloud_voice(monkeypatch):
    """Tests never use the developer's real Fish Audio key or network: keys from the
    environment are blanked (and the ``settings`` fixture ignores .env), and any request that
    still slips through goes to a closed local port and fails at once."""
    for name in ("FISH_AUDIO_API_KEY", "LOCKEDIN_FISH_AUDIO_API_KEY", "FISH_API_KEY"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("LOCKEDIN_FISH_AUDIO_BASE_URL", "http://127.0.0.1:9")
    # The local language model (2.3 GB) is never loaded by a unit test: tests that exercise the
    # conversation engine inject a scripted model; tests/test_local_llm.py loads the real one.
    monkeypatch.setenv("LOCKEDIN_LLM_PROVIDER", "none")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path, robot_driver="mock", mock_autoconnect=True)


@pytest.fixture
def tts() -> RecordingTTS:
    return RecordingTTS()


async def make_runtime(settings: Settings, tts: RecordingTTS, **mock_kwargs) -> LockedInRuntime:
    rs = RuntimeSettings()
    rs.coach.min_seconds_between_interventions = 0.0
    rt = LockedInRuntime(
        settings,
        runtime_settings=rs,
        persist_settings=False,
        tts_engine=tts,
        enable_vision=False,
        enable_voice=False,
        mock_kwargs=mock_kwargs,
    )
    await rt.start()
    for _ in range(50):
        if rt.transport is not None and rt.transport.connected and rt.sm.state.value == "IDLE":
            break
        await asyncio.sleep(0.01)
    return rt


@pytest.fixture
async def rt(settings, tts):
    runtime = await make_runtime(settings, tts)
    yield runtime
    await runtime.shutdown()


async def wait_for(predicate, timeout: float = 2.0, interval: float = 0.01) -> bool:
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()
