"""Fish Audio provider, clip cache, budget and the voice router. Every HTTP call goes to an
httpx.MockTransport: no real Fish Audio request, no credits."""

from __future__ import annotations

import json
import logging
import struct

import httpx
import numpy as np
import pytest

from app.config import FishVoiceSettings, VoiceProfile
from app.voice.audio_cache import AudioCache
from app.voice.fish_audio import (
    CacheJob,
    CachePlanItem,
    FishAudioClient,
    FishAudioError,
    FishAudioVoice,
    parse_wav,
)
from app.voice.tts import AudioOut, SpeechRequest, SpeechResult, TTSEngine, VoiceRouter
from app.voice.usage import UsageLedger, utf8_bytes

KEY = "fa-test-SECRET-key-0123456789"  # gitleaks:allow -- synthetic credential used only by mocked HTTP tests
VOICE = "ca3007f96ae7499ab87d27ea3599956a"


def wav_bytes(seconds=0.2, sr=24000, streaming=False, float32=False) -> bytes:
    n = int(seconds * sr)
    x = 0.3 * np.sin(2 * np.pi * 220 * np.arange(n) / sr)
    if float32:
        data, tag, bits = x.astype("<f4").tobytes(), 3, 32
    else:
        data, tag, bits = (x * 32767).astype("<i2").tobytes(), 1, 16
    fmt = struct.pack("<HHIIHH", tag, 1, sr, sr * bits // 8, bits // 8, bits)
    size = 0xFFFFFFFF if streaming else len(data)
    riff = 0xFFFFFFFF if streaming else 4 + 8 + len(fmt) + 8 + len(data)
    return b"RIFF" + struct.pack("<I", riff) + b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", size) + data


class Fake:
    """MockTransport handler that records requests."""

    def __init__(self, status=200, body: bytes | dict | None = None, raise_exc: Exception | None = None):
        self.status, self.body, self.raise_exc = status, body, raise_exc
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_exc is not None:
            raise self.raise_exc
        if isinstance(self.body, dict):
            return httpx.Response(self.status, json=self.body)
        return httpx.Response(self.status, content=self.body if self.body is not None else wav_bytes())


class FakeAudio(AudioOut):
    def __init__(self):
        self.played: list[tuple] = []

    async def play(self, pcm, sample_rate, device=None, volume=1.0):
        self.played.append((len(pcm), sample_rate, device, volume))


class FakeSystem(TTSEngine):
    name = "system-fake"

    def __init__(self):
        self.spoken: list[str] = []

    def available(self):
        return True

    async def speak(self, text):
        self.spoken.append(text)

    async def speak_request(self, req):
        self.spoken.append(req.text)
        return SpeechResult("system_live")


def voice(tmp_path, fake: Fake | None = None, key: str | None = KEY) -> FishAudioVoice:
    return FishAudioVoice(
        key,
        AudioCache(tmp_path / "cache"),
        UsageLedger(tmp_path / "usage.json"),
        transport=httpx.MockTransport(fake) if fake else None,
    )


def profile(**kw) -> VoiceProfile:
    base = dict(provider="fish_audio", reference_id=VOICE, model="s2.1-pro-free")
    base.update(kw)
    return VoiceProfile(**base)


def router(fv, p, audio=None, system=None):
    holder = {"p": p}
    r = VoiceRouter(profile=lambda: holder["p"], system=system or FakeSystem(), cloud=fv, audio=audio or FakeAudio())
    return r, holder


# ------------------------------------------------------------------ client
async def test_tts_request_matches_the_documented_api():
    fake = Fake()
    client = FishAudioClient(KEY, transport=httpx.MockTransport(fake))
    pcm, sr = await client.tts("[excited] Nice try.", VOICE, "s2.1-pro-free", {"latency": "low", "speed": 1.2, "temperature": 0.8, "top_p": 0.6})
    req = fake.requests[0]
    assert req.method == "POST" and req.url.path == "/v1/tts" and req.url.host == "api.fish.audio"
    assert req.headers["authorization"] == f"Bearer {KEY}" and req.headers["model"] == "s2.1-pro-free"
    body = json.loads(req.content)
    assert body == {
        "text": "[excited] Nice try.", "reference_id": VOICE, "format": "wav", "sample_rate": 24000, "latency": "low",
        "normalize": True, "prosody": {"speed": 1.2, "volume": 0}, "temperature": 0.8, "top_p": 0.6,
    }
    assert sr == 24000 and len(pcm) == 4800 and pcm.dtype == np.int16


def test_wav_parsing_tolerates_streamed_headers_and_float():
    for kw in ({}, {"streaming": True}, {"float32": True}):
        pcm, sr = parse_wav(wav_bytes(**kw))
        assert sr == 24000 and abs(len(pcm) - 4800) <= 1 and pcm.dtype == np.int16
    with pytest.raises(FishAudioError) as e:
        parse_wav(b"<html>error</html>" * 4)
    assert e.value.kind == "bad_audio"


async def test_errors_are_mapped_and_never_contain_the_key():
    cases = [(401, "auth"), (402, "no_credit"), (429, "rate_limited"), (503, "busy"), (400, "invalid")]
    for status, kind in cases:
        client = FishAudioClient(KEY, transport=httpx.MockTransport(Fake(status, {"status": status, "message": "nope"})))
        with pytest.raises(FishAudioError) as e:
            await client.tts("hi", VOICE, "s2.1-pro", {})
        assert e.value.kind == kind and KEY not in e.value.message and str(status) in e.value.message
    client = FishAudioClient(KEY, transport=httpx.MockTransport(Fake(raise_exc=httpx.ConnectError("dns"))))
    with pytest.raises(FishAudioError) as e:
        await client.tts("hi", VOICE, "s2.1-pro", {})
    assert e.value.kind == "offline"
    assert KEY not in repr(client)


async def test_unknown_models_are_refused_before_any_request():
    fake = Fake()
    client = FishAudioClient(KEY, transport=httpx.MockTransport(fake))
    with pytest.raises(FishAudioError):
        await client.tts("hi", VOICE, "eleven_flash_v2_5", {})  # Fish would silently use s2.1-pro
    assert fake.requests == []


# ------------------------------------------------------------------ router
async def test_text_only_and_missing_key_fall_back_honestly(tmp_path):
    r, holder = router(voice(tmp_path, key=None), profile(provider="none"))
    assert (await r.speak_request(SpeechRequest("hi"))).source == "text_only"
    system = FakeSystem()
    r, holder = router(voice(tmp_path, key=None), profile(), system=system)
    res = await r.speak_request(SpeechRequest("hi"))
    # The cache is checked first (it needs no key); an uncached line says why it fell back.
    assert res.source == "system_live" and res.detail == "not cached; no Fish Audio API key" and system.spoken == ["hi"]


async def test_cached_clip_plays_with_no_network(tmp_path):
    fake = Fake(raise_exc=httpx.ConnectError("offline"))  # the robot's Wi-Fi: no internet
    fv = voice(tmp_path, fake)
    p = profile()
    req = SpeechRequest("Nice try.", cue="amused", cacheable=True)
    tts_text = fv.text_for(req, p)
    fv.cache.put(fv.cache_key(tts_text, p), *parse_wav(wav_bytes()), meta={"text": tts_text}, persist=True)
    audio = FakeAudio()
    r, _ = router(fv, p, audio=audio)
    assert (await r.speak_request(req)).source == "fish_cache"
    assert fake.requests == [] and audio.played[0][1] == 24000


async def test_cache_only_never_calls_the_api(tmp_path):
    fake = Fake()
    fv = voice(tmp_path, fake)
    r, _ = router(fv, profile(cache_behavior="cache_only"))
    res = await r.speak_request(SpeechRequest("Uncached line.", cacheable=True))
    assert res.source == "system_live" and "cache only" in res.detail and fake.requests == []


async def test_live_generation_when_allowed_then_plays_from_cache(tmp_path):
    fake = Fake()
    fv = voice(tmp_path, fake)
    r, _ = router(fv, profile(cache_behavior="cache_then_live"))
    req = SpeechRequest("Okay, that was actually clean.", cue="impressed", cacheable=True)
    assert (await r.speak_request(req)).source == "fish_live"
    assert fv.usage.used_today("live") == utf8_bytes("[impressed] Okay, that was actually clean.")
    assert (await r.speak_request(req)).source == "fish_cache"
    assert len(fake.requests) == 1


async def test_lines_with_session_details_are_not_written_to_disk(tmp_path):
    fv = voice(tmp_path, Fake())
    r, _ = router(fv, profile(cache_behavior="cache_then_live"))
    await r.speak_request(SpeechRequest("12 minutes left on calculus.", cacheable=False))
    assert fv.cache.index == {}
    await r.speak_request(SpeechRequest("Nice try.", cacheable=True))
    assert len(fv.cache.index) == 1 and list(fv.cache.index.values())[0]["text"] == "Nice try."


async def test_network_loss_falls_back_and_backs_off(tmp_path):
    fake = Fake(raise_exc=httpx.ConnectError("no route"))
    fv = voice(tmp_path, fake)
    r, _ = router(fv, profile(cache_behavior="cache_then_live"))
    first = await r.speak_request(SpeechRequest("Line one.", cacheable=True))
    assert first.source == "system_live" and "unavailable" in first.detail
    second = await r.speak_request(SpeechRequest("Line two.", cacheable=True))
    assert "offline" in second.detail and len(fake.requests) == 1  # no retry storm
    assert fv.status["state"] == "offline"


async def test_safety_lines_never_wait_for_the_network(tmp_path):
    fake = Fake()
    fv = voice(tmp_path, fake)
    r, _ = router(fv, profile(cache_behavior="cache_then_live"))
    res = await r.speak_request(SpeechRequest("Stopped.", allow_live=False, priority=0, cacheable=True))
    assert res.source == "system_live" and fake.requests == []


async def test_daily_budget_stops_live_generation(tmp_path):
    fake = Fake()
    fv = voice(tmp_path, fake)
    r, _ = router(fv, profile(cache_behavior="cache_then_live", daily_live_bytes=10))
    res = await r.speak_request(SpeechRequest("This line is longer than ten bytes.", cacheable=True))
    assert "budget" in res.detail and fake.requests == []


def test_cache_key_covers_voice_model_settings_and_text(tmp_path):
    fv = voice(tmp_path)
    p = profile()
    base = fv.cache_key("Nice try.", p)
    variants = [
        fv.cache_key("Nice try!", p),
        fv.cache_key("Nice try.", profile(reference_id="0" * 32)),
        fv.cache_key("Nice try.", profile(model="s2.1-pro")),
        fv.cache_key("Nice try.", profile(fish=FishVoiceSettings(speed=1.1))),
    ]
    assert base not in variants and len(set(variants)) == 4
    fv.cache.put(base, *parse_wav(wav_bytes()), meta={}, persist=True)
    assert fv.cached("Nice try.", p) is not None
    assert fv.cached("Nice try.", profile(reference_id="0" * 32)) is None  # switching voice: old clip unused


def test_delivery_cues_only_for_s2_models(tmp_path):
    fv = voice(tmp_path)
    req = SpeechRequest("That's what I'm talking about!", cue="very excited")
    assert fv.text_for(req, profile()) == "[very excited] That's what I'm talking about!"
    assert fv.text_for(req, profile(model="s1")) == "That's what I'm talking about!"
    assert fv.text_for(req, profile(fish=FishVoiceSettings(cues=False))) == "That's what I'm talking about!"


async def test_voice_check_and_listing_use_free_lookups(tmp_path):
    model = {"_id": VOICE, "title": "E-girl", "languages": ["en"], "samples": [{"audio": "https://cdn.example/sample.mp3"}], "author": {"nickname": "twangodev"}}
    fake = Fake(200, model)
    fv = voice(tmp_path, fake)
    status = await fv.check(profile())
    assert status["state"] == "online" and status["voice"]["title"] == "E-girl" and status["voice"]["sample_url"].endswith(".mp3")
    fake.body = {"total": 1, "items": [model]}
    voices = await fv.voices(mine=True)
    assert voices[0]["id"] == VOICE
    assert [r.method for r in fake.requests] == ["GET", "GET"] and fake.requests[1].url.params["self"] == "true"
    fake.status, fake.body = 404, {"message": "Nothing matches"}
    assert (await fv.check(profile()))["state"] == "voice_not_found"


async def test_api_credit_is_a_free_get(tmp_path):
    fake = Fake(200, {"_id": "x", "user_id": "u", "credit": "12.3400", "cumulative_top_up": "20", "created_at": "t", "updated_at": "t", "has_phone_sha256": False, "has_free_credit": True})
    fv = voice(tmp_path, fake)
    c = await fv.credit()
    assert c["credit"] == "12.3400" and c["has_free_credit"] is True
    req = fake.requests[0]
    assert req.method == "GET" and req.url.path == "/wallet/self/api-credit"


async def test_cache_job_generates_missing_clips_and_stops_on_a_rejected_key(tmp_path):
    fake = Fake()
    fv = voice(tmp_path, fake)
    p = profile()
    items = [CachePlanItem("reach_first", t, t, fv.cache_key(t, p), utf8_bytes(t)) for t in ("Nice try.", "I saw that.")]
    state = await CacheJob(fv, items, p).run()
    assert state["state"] == "finished" and state["done"] == 2 and len(fv.cache.index) == 2
    assert fv.usage.used_today("cache") == sum(i.nbytes for i in items)
    fake.status, fake.body = 401, {"message": "invalid key"}
    more = [CachePlanItem("x", "New line.", "New line.", fv.cache_key("New line.", p), 9)]
    state = await CacheJob(fv, more, p).run()
    assert state["state"] == "stopped" and "rejected" in state["error"]


async def test_the_key_never_reaches_logs(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    fv = voice(tmp_path, Fake(401, {"message": "invalid"}))
    r, _ = router(fv, profile(cache_behavior="cache_then_live"))
    await r.speak_request(SpeechRequest("hello", cacheable=True))
    assert KEY not in caplog.text
    assert KEY not in json.dumps(fv.describe(), default=str)


async def test_cached_clip_plays_without_an_api_key(tmp_path):
    """Playing a cached clip needs neither the key nor the network (only live generation does):
    a laptop without the key in .env still speaks the cached voice pack offline."""
    fv = voice(tmp_path, key=None)
    p = profile()
    req = SpeechRequest("Nice try.", cue="amused", cacheable=True)
    tts_text = fv.text_for(req, p)
    fv.cache.put(fv.cache_key(tts_text, p), *parse_wav(wav_bytes()), meta={"text": tts_text}, persist=True)
    audio = FakeAudio()
    r, _ = router(fv, p, audio=audio)
    assert (await r.speak_request(req)).source == "fish_cache" and len(audio.played) == 1
    res = await r.speak_request(SpeechRequest("Not cached.", cacheable=True))
    assert res.source == "system_live" and "no Fish Audio API key" in res.detail
