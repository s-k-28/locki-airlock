"""AIRLOCK announcement boundary and existing Speaker preemption; no real audio."""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.security.announcements import LINES, AnnouncementRejected, IncidentAnnouncement, IncidentAnnouncer
from app.security.voice_service import create_voice_app, make_runtime, read_local_token
from app.telemetry.events import EventBus
from app.voice.tts import P0_SAFETY, P2_REPLY, P3_AMBIENT, NullTTS, Speaker, SpeechResult, TTSEngine


def facts(**changes):
    values = dict(workstation_id="test-desk", session_id="test-session", incident_id="incident:test-event", severity="medium", rule_id="unknown_usb", simulated=True, updated_at_ms=int(time.time() * 1000))
    values.update(changes)
    return IncidentAnnouncement(**values)


@pytest.fixture
def local_token(tmp_path):
    path = tmp_path / "token.private.json"
    path.write_text(json.dumps({"token": "a" * 48}))
    path.chmod(0o600)
    return path


def test_rehearsals_are_explicit_and_no_line_invents_notification_or_lock_success():
    assert len(LINES) == 8
    for (simulated, _rule), text in LINES.items():
        assert text.startswith("Rehearsal.") if simulated else text.startswith("Security alert.")
        assert "owner has been notified" not in text.lower()
        assert "lock confirmed" not in text.lower()
        assert "malware detected" not in text.lower()


@pytest.mark.parametrize("change", [
    {"severity": "critical"}, {"rule_id": "model_says_intruder"},
    {"simulated": "false"}, {"updated_at_ms": "1794084000000"},
    {"text": "Untrusted arbitrary speech"}, {"incident_id": "../../arbitrary"},
])
def test_only_typed_committed_facts_are_accepted(change):
    with pytest.raises(ValidationError):
        facts(**change)


def test_duplicate_is_durable_across_new_announcer_instance(tmp_path):
    first = Speaker(NullTTS(), EventBus())
    incident = facts()
    announcer = IncidentAnnouncer(first, tmp_path / "ledger.sqlite3")
    result = announcer.announce(incident)
    assert result["queued"] and result["priority"] == "P0"
    announcer.close()
    recovered = Speaker(NullTTS(), EventBus())
    restarted = IncidentAnnouncer(recovered, tmp_path / "ledger.sqlite3")
    duplicate = restarted.announce(incident)
    assert duplicate["duplicate"] and not duplicate["queued"]
    assert recovered.snapshot()["queue"] == 0
    restarted.close()


@pytest.mark.parametrize("simulated", [False, True])
def test_owner_panic_is_manual_intent_without_inferred_sensor_or_response_claims(tmp_path, simulated):
    speaker = Speaker(NullTTS(), EventBus())
    announcer = IncidentAnnouncer(speaker, tmp_path / "ledger.sqlite3")
    result = announcer.announce(facts(rule_id="owner_panic", severity="high", simulated=simulated))
    text = result["text"].lower()
    assert "owner" in text and "panic" in text and "manual" in text
    assert all(word not in text for word in ["usb", "person", "notified", "lock", "malware", "intrusion"])
    assert result["priority"] == "P0" and result["queued"]
    assert text.startswith("rehearsal.") if simulated else text.startswith("security alert.")
    assert not result["audio_enabled"]
    announcer.close()


def test_owner_panic_requires_the_committed_high_severity(tmp_path):
    speaker = Speaker(NullTTS(), EventBus())
    announcer = IncidentAnnouncer(speaker, tmp_path / "ledger.sqlite3")
    with pytest.raises(AnnouncementRejected, match="owner_panic.*high"):
        announcer.announce(facts(rule_id="owner_panic", severity="medium"))
    assert speaker.snapshot()["queue"] == 0
    announcer.close()


@pytest.mark.parametrize("change", [
    {"updated_at_ms": 1}, {"updated_at_ms": 200_000},
    {"severity": "medium", "rule_id": "correlated_usb_physical"},
    {"severity": "high", "rule_id": "unknown_usb"},
])
def test_stale_future_and_inconsistent_rules_never_queue(tmp_path, change):
    speaker = Speaker(NullTTS(), EventBus())
    announcer = IncidentAnnouncer(speaker, tmp_path / "ledger.sqlite3", clock=lambda: 100)
    with pytest.raises(AnnouncementRejected):
        announcer.announce(facts(updated_at_ms=100_000).model_copy(update=change))
    assert speaker.snapshot()["queue"] == 0
    announcer.close()


def test_warning_queue_and_station_cooldown_are_bounded(tmp_path):
    speaker = Speaker(NullTTS(), EventBus())
    announcer = IncidentAnnouncer(speaker, tmp_path / "ledger.sqlite3")
    announcer.announce(facts())
    with pytest.raises(AnnouncementRejected, match="cooldown"):
        announcer.announce(facts(incident_id="incident:second"))
    announcer.announce(facts(workstation_id="other", incident_id="incident:app", rule_id="unapproved_app"))
    assert speaker.snapshot()["queue"] == 2
    with pytest.raises(AnnouncementRejected, match="bounded"):
        announcer.announce(facts(workstation_id="third", incident_id="incident:third"))
    assert speaker.snapshot()["queue"] == 2
    announcer.close()


def test_mute_is_respected_by_security_announcements(tmp_path):
    speaker = Speaker(NullTTS(), EventBus())
    speaker.muted = True
    announcer = IncidentAnnouncer(speaker, tmp_path / "ledger.sqlite3")
    result = announcer.announce(facts())
    assert not result["queued"]
    assert result["status"] == "muted or duplicate; not spoken"
    assert speaker.snapshot()["queue"] == 0
    announcer.close()


class InterruptibleEngine(TTSEngine):
    name = "test-no-audio"

    def __init__(self):
        self.reply_started = asyncio.Event()
        self.safety_started = asyncio.Event()
        self.reply_cancelled = False
        self.started = []

    def available(self):
        return True

    async def speak(self, text):
        raise AssertionError("speak_request is used")

    async def speak_request(self, request):
        self.started.append(request)
        if request.priority == P2_REPLY:
            self.reply_started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                self.reply_cancelled = True
                raise
        if request.priority == P0_SAFETY:
            self.safety_started.set()
        return SpeechResult("text_only")


async def test_committed_incident_p0_preempts_current_p2_and_clears_ambient(tmp_path):
    engine = InterruptibleEngine()
    speaker = Speaker(engine, EventBus())
    speaker.start()
    announcer = IncidentAnnouncer(speaker, tmp_path / "ledger.sqlite3")
    try:
        speaker.say("a model-independent test conversation", priority=P2_REPLY)
        await asyncio.wait_for(engine.reply_started.wait(), 1)
        speaker.say("an obsolete ambient remark", priority=P3_AMBIENT)
        result = announcer.announce(facts())
        await asyncio.wait_for(engine.safety_started.wait(), 1)
        await asyncio.sleep(0)
        assert result["queued"]
        assert engine.reply_cancelled
        assert [request.priority for request in engine.started] == [P2_REPLY, P0_SAFETY]
        assert not engine.started[-1].allow_live
        assert engine.started[-1].text.startswith("Rehearsal.")
        assert any(item["text"] == "a model-independent test conversation" and item["outcome"] == "interrupted" for item in speaker.history)
        assert any(item["text"] == "an obsolete ambient remark" and item["outcome"].startswith("cancelled") for item in speaker.history)
    finally:
        announcer.close()
        await speaker.close()


def test_local_token_file_must_be_private(local_token):
    assert read_local_token(local_token) == "a" * 48
    local_token.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        read_local_token(local_token)


def test_service_preserves_snapshot_ws_but_has_no_robot_commands_camera_or_microphone(local_token, tmp_path):
    rt = make_runtime(tmp_path / "runtime")
    app = create_voice_app(local_token, tmp_path / "runtime", runtime=rt)
    with TestClient(app) as client:
        assert not hasattr(rt, "transport") and not hasattr(rt, "safety") and not hasattr(rt, "companion")
        assert rt.voice is None and not rt.allow_microphone
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/voice/config").json()["profile"]["provider"] == "system"
        assert client.get("/api/airlock/voice-status").json()["audio_enabled"] is False
        for path in ["/api/robot/arm", "/api/robot/move", "/api/command", "/api/session/start", "/api/vision/start", "/api/voice/test-mic"]:
            assert client.post(path, json={}).status_code == 404
        assert client.post("/api/voice/start", json={}, headers={"Origin": "http://127.0.0.1:5174"}).json()["ok"] is False
        assert client.patch("/api/settings", json={"robot_driver": "zeus"}).status_code == 422
        assert client.post("/api/airlock/voice-control", json={"intent": "disarm"}).status_code == 422
        assert client.post("/api/airlock/voice-control", json={"intent": "stop_talking"}).json()["ok"]
        assert client.patch("/api/settings", json={"voice": {"muted": True}}).json()["ok"]
        assert rt.speaker.muted
        assert not hasattr(rt, "transport") and rt.voice is None
        with client.websocket_connect("/ws") as ws:
            snapshot = ws.receive_json()
            assert snapshot["type"] == "snapshot"
            assert snapshot["data"]["voice"]["input"]["running"] is False


def test_announcement_auth_origin_and_typed_body(local_token, tmp_path):
    app = create_voice_app(local_token, tmp_path / "runtime")
    with TestClient(app) as client:
        body = facts().model_dump()
        assert client.post("/api/airlock/announce", json=body).status_code == 401
        headers = {"Authorization": "Bearer " + "a" * 48}
        assert client.post("/api/airlock/announce", json=body, headers={**headers, "Origin": "https://untrusted.example"}).status_code == 403
        assert client.post("/api/airlock/announce", json={**body, "text": "fake owner notified"}, headers=headers).status_code == 422
        first = client.post("/api/airlock/announce", json=body, headers=headers)
        assert first.status_code == 200
        assert first.json()["text"].startswith("Rehearsal.")
        assert first.json()["audio_enabled"] is False
        assert client.post("/api/airlock/announce", json=body, headers=headers).json()["duplicate"]
