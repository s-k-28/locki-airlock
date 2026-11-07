"""Standalone microphone wiring with injected input/STT. Never uses hardware."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.security.announcements import IncidentAnnouncement
from app.security.voice_service import SpeechSurface, create_voice_app, local_asr_preflight
from app.telemetry.events import EventType


class FakeTranscriber:
    state = "ready"
    closed = False

    def close(self):
        self.closed = True


class FakeInput:
    def __init__(self, *, emit, transcriber, **_kwargs):
        self.emit, self.transcriber = emit, transcriber
        self.running, self.transcribing, self.held = False, False, False
        self.closed = 0

    def start(self, _device, mode):
        assert mode == "push_to_talk"
        self.running = True

    def shutdown(self):
        self.running = self.held = False
        self.closed += 1
        self.transcriber.close()

    def ptt_start(self):
        self.held = True

    def ptt_stop(self):
        self.held = False

    def level(self):
        return {"rms_db": -80, "speaking": self.held}

    def snapshot(self):
        return {"running": self.running, "ptt_active": self.held, "mode": "push_to_talk"}


def surface(tmp_path, **options):
    return SpeechSurface(tmp_path, allow_microphone=True, input_factory=FakeInput,
                         transcriber_factory=lambda _settings: FakeTranscriber(),
                         asr_preflight=lambda _settings: {"ready": True}, **options)


def token(tmp_path):
    path = tmp_path / "local.private.json"
    path.write_text(json.dumps({"token": "a" * 48}))
    path.chmod(0o600)
    return path


def test_import_does_not_import_robot_runtime_or_load_voice_service():
    code = "import sys; import app.security.voice_service; assert 'app.runtime' not in sys.modules; assert 'app.voice.service' not in sys.modules; assert 'pywhispercpp' not in sys.modules"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=5, env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
    assert result.returncode == 0, result.stderr


async def test_allow_flag_does_not_start_input_or_load_asr(tmp_path):
    rt = surface(tmp_path)
    await rt.start()
    assert rt.voice is None and rt.snapshot()["voice"]["input"]["running"] is False
    assert (await rt.start_voice())["ok"]
    fake = rt.voice
    assert rt.snapshot()["voice"]["input"]["running"]
    assert rt.ptt(True)["ok"] and fake.held
    assert rt.ptt(False)["ok"] and not fake.held
    await rt.stop_voice()
    assert fake.closed and not rt.snapshot()["voice"]["input"]["running"]
    await rt.shutdown()


async def test_preflight_denial_never_constructs_transcriber_or_input(tmp_path):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("must not construct input or model")
    rt = SpeechSurface(tmp_path, allow_microphone=True, input_factory=forbidden,
                       transcriber_factory=forbidden, asr_preflight=lambda _s: {"ready": False, "reason": "pressure"})
    await rt.start()
    assert (await rt.start_voice()) == {"ok": False, "message": "pressure"}
    assert rt.voice is None
    await rt.shutdown()


async def test_late_asr_callbacks_after_stop_or_new_start_are_discarded(tmp_path):
    rt = surface(tmp_path)
    await rt.start()
    await rt.start_voice()
    old = rt.voice
    await rt.stop_voice()
    await rt.start_voice()
    before = len(rt.bus.history(limit=400))
    old.emit("utterance", {"text": "what happened", "confidence": 1})
    await asyncio.sleep(0.01)
    assert len(rt.bus.history(limit=400)) == before
    rt.voice.emit("utterance", {"text": "status", "confidence": 1})
    await asyncio.sleep(0.01)
    assert any(event.event_type == EventType.VOICE_TRANSCRIBED for event in rt.bus.history(limit=400))
    await rt.shutdown()


async def test_lost_ptt_stop_hits_independent_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr("app.security.voice_service.PTT_LIMIT_SECONDS", 0.02)
    rt = surface(tmp_path)
    await rt.start()
    await rt.start_voice()
    fake = rt.voice
    rt.ptt(True)
    await asyncio.sleep(0.06)
    assert rt.voice is None and not fake.running and not fake.held
    await rt.shutdown()


async def test_microphone_lease_expires_without_auto_restart(tmp_path):
    rt = surface(tmp_path)
    rt.microphone_lease_seconds = 0.02
    await rt.start()
    await rt.start_voice()
    fake = rt.voice
    await asyncio.sleep(0.06)
    assert rt.voice is None and fake.closed
    await rt.shutdown()


@pytest.mark.parametrize("text", ["disarm", "arm my desk", "panic", "approve action", "resolve incident", "lock screen", "move forward", "stop talking and disarm"])
async def test_untrusted_speech_has_no_security_or_robot_dispatch(tmp_path, text):
    rt = surface(tmp_path)
    await rt.start()
    result = rt.handle_transcript({"text": text, "confidence": 1})
    assert result["intent"] == "unsupported_read_only_voice"
    assert not result["security_action_executed"]
    assert not hasattr(rt, "transport") and not hasattr(rt, "handle_utterance")
    await rt.shutdown()


async def test_read_only_incident_reply_and_stop_talking_use_existing_speaker(tmp_path):
    rt = surface(tmp_path)
    await rt.start()
    rt.last_incident = IncidentAnnouncement(workstation_id="ws", session_id="session", incident_id="incident:test", rule_id="owner_panic", severity="high", simulated=True, updated_at_ms=1)
    result = rt.handle_transcript({"text": "what happened", "confidence": 1})
    assert result["reply"].startswith("Rehearsal.") and "manual" in result["reply"]
    assert rt.handle_transcript({"text": "stop talking", "confidence": 0})["intent"] == "stop_talking"
    assert not rt.speaker.snapshot()["queue"]
    await rt.shutdown()


def test_http_and_ws_consent_disconnect_and_snapshot(tmp_path):
    rt = surface(tmp_path)
    app = create_voice_app(token(tmp_path), tmp_path, runtime=rt)
    origin = {"Origin": "http://127.0.0.1:5174"}
    with TestClient(app) as client:
        assert client.post("/api/voice/start", json={}).status_code == 401
        assert client.post("/api/voice/start", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.post("/api/voice/ptt", json={"action": {}}, headers=origin).status_code == 422
        with client.websocket_connect("/ws", headers=origin) as ws:
            assert ws.receive_json()["data"]["voice"]["input"]["running"] is False
            assert client.post("/api/voice/start", json={}, headers=origin).json()["ok"]
            fake = rt.voice
            ws.send_json({"type": "ptt", "action": "start"})
            for _ in range(8):
                message = ws.receive_json()
                if message["type"] == "snapshot" and message["data"]["voice"]["input"].get("ptt_active"):
                    break
            assert fake.held
        assert not fake.running and not fake.held and rt.voice is None
        assert client.get("/api/voice/config").json()["profile"]["provider"] == "system"


async def test_stop_during_pending_start_waits_for_cleanup_and_drops_callbacks(tmp_path):
    entered, release = threading.Event(), threading.Event()
    instances = []
    class SlowInput(FakeInput):
        def start(self, device, mode):
            entered.set()
            assert release.wait(2)
            super().start(device, mode)
    def factory(**kwargs):
        instance = SlowInput(**kwargs)
        instances.append(instance)
        return instance
    rt = surface(tmp_path)
    rt.input_factory = factory
    await rt.start()
    pending = asyncio.create_task(rt.start_voice())
    assert await asyncio.to_thread(entered.wait, 1)
    stopping = asyncio.create_task(rt.stop_voice())
    await asyncio.sleep(0.01)
    assert not stopping.done()
    release.set()
    assert not (await pending)["ok"]
    assert (await stopping)["ok"]
    assert instances[0].closed and not instances[0].running and rt.voice is None
    await rt.shutdown()


async def test_stop_serializes_slow_shutdown_against_second_tab_start(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class SlowShutdown(FakeInput):
        def shutdown(self):
            entered.set()
            assert release.wait(2)
            super().shutdown()
    rt = surface(tmp_path)
    rt.input_factory = lambda **kwargs: SlowShutdown(**kwargs)
    await rt.start()
    assert (await rt.start_voice())["ok"]
    original = rt.voice
    stopping = asyncio.create_task(rt.stop_voice())
    assert await asyncio.to_thread(entered.wait, 1)
    assert not (await rt.start_voice())["ok"]
    release.set()
    assert (await stopping)["ok"]
    assert rt.voice is None and original.closed and not original.running
    await rt.shutdown()


async def test_cancelled_transcriber_construction_is_closed_before_releasing_start_lock(tmp_path):
    entered, release = threading.Event(), threading.Event()
    transcriber = FakeTranscriber()
    def factory(_settings):
        entered.set()
        assert release.wait(2)
        return transcriber
    rt = surface(tmp_path)
    rt.transcriber_factory = factory
    await rt.start()
    starting = asyncio.create_task(rt.start_voice())
    assert await asyncio.to_thread(entered.wait, 1)
    starting.cancel()
    await asyncio.sleep(0.01)
    assert not (await rt.start_voice())["ok"]
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert transcriber.closed and rt.voice is None
    await rt.shutdown()


async def test_cancelled_stop_keeps_teardown_lock_until_native_shutdown_finishes(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class SlowShutdown(FakeInput):
        def shutdown(self):
            entered.set()
            assert release.wait(2)
            super().shutdown()
    rt = surface(tmp_path)
    rt.input_factory = lambda **kwargs: SlowShutdown(**kwargs)
    await rt.start()
    await rt.start_voice()
    original = rt.voice
    stopping = asyncio.create_task(rt.stop_voice())
    assert await asyncio.to_thread(entered.wait, 1)
    stopping.cancel()
    await asyncio.sleep(0.01)
    assert not (await rt.start_voice())["ok"]
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert original.closed and rt.voice is None
    await rt.shutdown()


def test_asr_pressure_fails_before_any_package_or_asset_load(tmp_path, monkeypatch):
    monkeypatch.setattr("psutil.virtual_memory", lambda: SimpleNamespace(available=1))
    monkeypatch.setattr("app.security.voice_service.importlib.util.find_spec", lambda _name: pytest.fail("no package probing under pressure"))
    result = local_asr_preflight(SimpleNamespace(stt_engine="whisper.cpp", whisper_model="base.en", models_dir=tmp_path))
    assert not result["ready"] and "2GiB" in result["reason"]


def test_no_downloadable_faster_whisper_fallback_is_allowed(tmp_path, monkeypatch):
    monkeypatch.setattr("psutil.virtual_memory", lambda: SimpleNamespace(available=4*1024**3))
    result = local_asr_preflight(SimpleNamespace(stt_engine="faster-whisper", whisper_model="base.en", models_dir=tmp_path))
    assert not result["ready"] and "no downloadable fallback" in result["reason"]


def test_unpinned_or_oversized_local_asr_asset_fails_without_constructing_model(tmp_path, monkeypatch):
    monkeypatch.setattr("psutil.virtual_memory", lambda: SimpleNamespace(available=4*1024**3))
    monkeypatch.setattr("app.security.voice_service.importlib.util.find_spec", lambda _name: object())
    directory = tmp_path / "whispercpp"
    directory.mkdir()
    weights = directory / "ggml-base.en.bin"
    weights.write_bytes(b"x" * 1024**2)
    settings = SimpleNamespace(stt_engine="whisper.cpp", whisper_model="base.en", models_dir=tmp_path)
    assert "SHA256" in local_asr_preflight(settings)["reason"]
    with weights.open("wb") as stream:
        stream.truncate(201*1024**2)
    assert "size budget" in local_asr_preflight(settings)["reason"]
