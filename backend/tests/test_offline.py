"""Milestone 3 offline operation (the robot's Wi-Fi has no internet): the backend starts and
serves with every non-local connection blocked, Fish Audio is never contacted, cached clips
play and the rest falls back to the local voice, and the readiness check finds missing assets."""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.voice.audio_cache import AudioCache
from app.voice.fish_audio import OFFLINE_MODE, FishAudioError, FishAudioVoice, parse_wav
from app.voice.tts import SpeechRequest
from app.voice.usage import UsageLedger

from .test_fish_audio import FakeAudio, profile, router, wav_bytes

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import offline_check  # noqa: E402

KEY = "fa-test-not-a-real-key-000"


def test_network_guard_blocks_the_internet_and_allows_this_machine():
    with offline_check.NetworkGuard() as guard:
        with pytest.raises(OSError):
            socket.create_connection(("93.184.216.34", 443), timeout=1)
        with pytest.raises(socket.gaierror):
            socket.getaddrinfo("api.fish.audio", 443)
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        socket.create_connection(srv.getsockname(), timeout=1).close()  # loopback still works
        srv.close()
    assert any("93.184.216.34" in b for b in guard.blocked) and any("api.fish.audio" in b for b in guard.blocked)


def test_offline_startup_serves_everything_without_the_internet(settings):
    s = settings.model_copy(update={
        "offline": True, "fish_audio_api_key": KEY, "fish_audio_base_url": "https://api.fish.audio",
        "vision_preload": False, "tts_engine": "none",
    })
    with offline_check.NetworkGuard() as guard, TestClient(create_app(s)) as c:
        assert c.get("/api/health").json()["ok"]
        st = c.get("/api/state").json()
        assert st["offline"]["enabled"] and st["offline"]["llm"] == "off" and st["mode"]["offline"]
        assert c.post("/api/session/start", json={"task": "offline", "duration_min": 25}).json()["ok"]
        assert c.post("/api/estop").json()["ok"] and c.post("/api/reset").json()["ok"]
        c.post("/api/voice/cloud/check")
        plan = c.post("/api/voice/cache/plan", json={"scope": "pack"}).json()
        plan_id = plan["data"]["plan"]["plan_id"]  # an empty cache: every pack line is missing
        job = c.post("/api/voice/cache/generate", json={"plan_id": plan_id, "confirm": True, "max_bytes": 100000}).json()
        assert not job["ok"] and "offline mode" in job["message"]
        assert c.post("/api/session/end", json={"reason": "user"}).json()["ok"]
        assert "fa-test" not in c.get("/api/state").text  # the key never leaves the backend
    assert guard.blocked == []


async def test_offline_voice_plays_cached_clips_and_falls_back_to_the_local_voice(tmp_path):
    requests: list[httpx.Request] = []
    fv = FishAudioVoice(KEY, AudioCache(tmp_path / "cache"), UsageLedger(tmp_path / "u.json"),
                        transport=httpx.MockTransport(lambda r: requests.append(r) or httpx.Response(200, content=wav_bytes())),
                        offline=True)
    p = profile(cache_behavior="cache_then_live")  # even a live-enabled profile stays offline
    req = SpeechRequest("Nice try.", cue="amused", cacheable=True)
    tts_text = fv.text_for(req, p)
    fv.cache.put(fv.cache_key(tts_text, p), *parse_wav(wav_bytes()), meta={"text": tts_text}, persist=True)
    audio = FakeAudio()
    r, _ = router(fv, p, audio=audio)
    assert (await r.speak_request(req)).source == "fish_cache"
    res = await r.speak_request(SpeechRequest("A line nobody cached.", cacheable=True))
    assert res.source == "system_live" and "offline mode" in res.detail
    with pytest.raises(FishAudioError):
        await fv.credit()
    assert (await fv.check(p))["state"] == "offline_mode"
    assert requests == [] and fv.live_refusal(req, p) == OFFLINE_MODE and fv.describe()["offline_mode"]


def test_readiness_check_reports_missing_models_and_internet_assets(tmp_path):
    models = tmp_path / "models"
    (models / "whisper" / "base.en").mkdir(parents=True)
    (models / "hand_landmarker.task").write_bytes(b"x" * 20_000)
    res = {c.name: c for c in offline_check.check_models(models, "base.en")}
    assert res["hand_landmarker.task"].status == "ok"
    assert res["blaze_face_short_range.tflite"].status == "fail" and "make models" in res["blaze_face_short_range.tflite"].fix
    assert res["whisper base.en"].status == "fail" and "model.bin" in res["whisper base.en"].detail

    fe = tmp_path / "frontend"
    (fe / "src").mkdir(parents=True)
    (fe / "src" / "main.tsx").write_text("x")
    (fe / "dist" / "assets").mkdir(parents=True)
    (fe / "dist" / "index.html").write_text(
        '<html><head><link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">'
        '<svg xmlns="http://www.w3.org/2000/svg"></svg></head></html>'
    )
    (fe / "dist" / "assets" / "a.css").write_text("@font-face{src:url(/assets/x.woff2)} .x{background:url('http://192.168.4.1:9000/mjpg')}")
    refs = offline_check.scan_external_refs(fe / "dist")
    assert len(refs) == 1 and "fonts.googleapis.com" in refs[0]  # w3.org namespace and the robot are fine
    dash = {c.name: c for c in offline_check.check_dashboard(fe)}
    assert dash["no internet assets"].status == "fail" and dash["fonts"].status == "warn"
    assert dash["rebuild offline"].status == "warn"
    missing = offline_check.check_dashboard(tmp_path / "nothing")
    assert missing[0].status == "fail" and "make build" in missing[0].fix


def test_readiness_check_flags_a_voice_that_is_not_installed():
    c = offline_check.check_system_voice("Definitely Not A Voice 9000")
    assert c.status == "fail" if offline_check.shutil.which("say") else c.status in ("fail", "warn")
