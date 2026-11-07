"""Standalone loopback AIRLOCK speech; no robot runtime initialization.

Run from backend: python -m app.security.voice_service --init-token ../data/airlock-voice/local-token.private.json
Then: python -m app.security.voice_service --token-file ../data/airlock-voice/local-token.private.json [--enable-audio]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import importlib.util
import ipaddress
import json
import os
import re
import secrets
import stat
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .. import __version__
from ..config import RuntimeSettings, Settings
from ..telemetry.events import EventBus, EventSource, EventType
from ..voice.tts import CachedSystemTTS, NullTTS, Speaker
from .announcements import LINES, AnnouncementRejected, IncidentAnnouncement, IncidentAnnouncer


def read_local_token(path: Path) -> str:
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("Local announcement token file must have permissions 0600")
    token = json.loads(path.read_text()).get("token")
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError("Local announcement token must contain at least 32 characters")
    return token


PTT_LIMIT_SECONDS = 10
ASR_READY_TIMEOUT_SECONDS = 20
ASR_MODEL_SHA256 = "a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002"


def local_asr_preflight(settings):
    """Verify local assets/resources. Never initialize or download a model."""
    import psutil

    if psutil.virtual_memory().available < 2 * 1024**3:
        return {"ready": False, "reason": "ASR not started: less than 2GiB available memory"}
    if settings.stt_engine != "whisper.cpp" or settings.whisper_model != "base.en":
        return {"ready": False, "reason": "AIRLOCK requires exact local whisper.cpp assets; no downloadable fallback"}
    package = "pywhispercpp"
    relative = f"whispercpp/ggml-{settings.whisper_model}.bin"
    weights = settings.models_dir / relative
    if importlib.util.find_spec(package) is None or not weights.is_file():
        return {"ready": False, "reason": "Local ASR package/weights unavailable; no download or substitute engine attempted"}
    if not 1024**2 <= weights.stat().st_size <= 200 * 1024**2:
        return {"ready": False, "reason": "ASR asset is outside the bounded base.en size budget"}
    digest = hashlib.sha256()
    with weights.open("rb") as stream:
        for block in iter(lambda: stream.read(1024**2), b""):
            digest.update(block)
    if digest.hexdigest() != ASR_MODEL_SHA256:
        return {"ready": False, "reason": "ASR asset failed pinned base.en SHA256 verification"}
    return {"ready": True, "engine": settings.stt_engine, "gpu": False}


def local_transcriber(settings):
    from ..voice.transcription import WhisperTranscriber

    return WhisperTranscriber(settings.whisper_model, "int8", settings.models_dir, engine=settings.stt_engine, use_gpu=False)


def local_input(**kwargs):
    from ..voice.service import VoiceService

    return VoiceService(**kwargs)


async def owned_thread(function, *args, **kwargs):
    """Cancellation must not release an ownership lock before native work ends."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


async def construct_owned(factory, cleanup_method, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(factory, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        instance = await task
        await owned_thread(getattr(instance, cleanup_method))
        raise


class SpeechSurface:
    """Only existing speech primitives. No robot runtime or security dispatcher."""

    def __init__(self, data_dir, *, allow_microphone=False, stt_engine="whisper.cpp", models_dir=None,
                 vad_engine="energy", microphone_lease_seconds=300, input_factory=local_input,
                 transcriber_factory=local_transcriber, asr_preflight=local_asr_preflight):
        if stt_engine != "whisper.cpp" or not 10 <= microphone_lease_seconds <= 900:
            raise ValueError("Select local Whisper and a microphone lease between 10 and 900 seconds")
        self.settings = Settings(_env_file=None, data_dir=data_dir, stt_engine=stt_engine, whisper_cpp_gpu=False,
                                 vad_engine=vad_engine, llm_provider="none", offline=True, voice_autostart=False)
        if models_dir is not None:
            self.settings.models_dir = Path(models_dir)
        self.settings.fish_audio_api_key = self.settings.anthropic_api_key = None
        self.settings.whisper_model = "base.en"
        self.rs = RuntimeSettings()
        self.rs.coach.personality, self.rs.coach.profanity = "judge_demo", "off"
        self.rs.voice_profile.provider, self.rs.voice_profile.local_voice = "system", "system"
        self.rs.voice_profile.reference_id, self.rs.active_preset = "", None
        self.rs.conversation.generated_replies = self.rs.conversation.proactive_generated = self.rs.conversation.anticipate = False
        self.bus = EventBus()
        self.speaker = Speaker(NullTTS(), self.bus, fallback=NullTTS())
        self.allow_microphone, self.microphone_lease_seconds = allow_microphone, microphone_lease_seconds
        self.input_factory, self.transcriber_factory, self.asr_preflight = input_factory, transcriber_factory, asr_preflight
        self.voice = self._starting_input = self._pending_transcriber = None
        self._mic_lock, self._input_epoch = asyncio.Lock(), 0
        self._loop = self._ptt_watchdog = self._mic_lease = self._mic_deadline = None
        self.started_at, self.run_id = time.time(), uuid.uuid4().hex[:12]
        self.last_incident, self.input_error, self.input_state = None, None, "off"
        self.turns = deque(maxlen=12)

    async def start(self):
        self._loop = asyncio.get_running_loop()
        self.bus.bind_loop(self._loop)
        self.speaker.start()

    async def shutdown(self):
        await self.stop_voice("service shutdown")
        self._loop = None
        if isinstance(self.speaker.engine, CachedSystemTTS):
            self.speaker.engine.prewarm_cancelled = True
        await self.speaker.close()

    async def start_voice(self):
        if not self.allow_microphone:
            return {"ok": False, "message": "Microphone permission disabled. Restart with --allow-microphone, then explicitly Enable mic."}
        if self._mic_lock.locked():
            return {"ok": False, "message": "Microphone/ASR start already pending"}
        if self.voice and self.voice.running:
            return {"ok": True, "message": "Microphone already enabled; hold to talk"}
        async with self._mic_lock:
            epoch, candidate, transcriber, promoted = self._input_epoch, None, None, False
            self.input_state, self.input_error = "starting", None
            try:
                ready = await asyncio.to_thread(self.asr_preflight, self.settings)
                if not ready.get("ready"):
                    raise RuntimeError(ready.get("reason", "Local ASR unavailable"))
                if epoch != self._input_epoch:
                    return {"ok": False, "message": "Microphone start cancelled"}
                transcriber = await construct_owned(self.transcriber_factory, "close", self.settings)
                self._pending_transcriber = transcriber
                deadline = time.monotonic() + ASR_READY_TIMEOUT_SECONDS
                while transcriber.state != "ready":
                    if epoch != self._input_epoch:
                        return {"ok": False, "message": "Microphone start cancelled"}
                    if transcriber.state in {"error", "unavailable"} or time.monotonic() >= deadline:
                        raise RuntimeError("Local ASR not ready; microphone was not opened")
                    await asyncio.sleep(0.05)
                if epoch != self._input_epoch:
                    return {"ok": False, "message": "Microphone start cancelled"}
                candidate = await construct_owned(self.input_factory, "shutdown", settings=self.settings, emit=lambda kind, data: self.input_event(kind, data, epoch), speaker=self.speaker, transcriber=transcriber)
                self._starting_input = candidate
                await owned_thread(candidate.start, self.rs.voice.input_device, "push_to_talk")
                if epoch != self._input_epoch:
                    return {"ok": False, "message": "Microphone start cancelled"}
                self.voice, promoted, self.input_state = candidate, True, "listening"
                self._mic_deadline = time.monotonic() + self.microphone_lease_seconds
                self._mic_lease = asyncio.create_task(self._expire_mic_lease())
                self.bus.publish(EventType.VOICE_LISTENING, EventSource.VOICE, {"mode": "push_to_talk", "engine": self.settings.stt_engine, "gpu": False})
                return {"ok": True, "message": "Microphone enabled. Hold to talk; spoken security actions cannot run."}
            except Exception as error:
                self.input_error = str(error)[:300]
                self.bus.publish(EventType.VOICE_ERROR, EventSource.VOICE, {"error": self.input_error})
                return {"ok": False, "message": self.input_error}
            finally:
                self._starting_input = self._pending_transcriber = None
                if not promoted:
                    self.input_state = "off"
                    if candidate:
                        await owned_thread(candidate.shutdown)
                    elif transcriber:
                        await owned_thread(transcriber.close)

    async def _expire_mic_lease(self):
        await asyncio.sleep(self.microphone_lease_seconds)
        await self.stop_voice("microphone consent lease elapsed")

    async def stop_voice(self, reason="operator stopped microphone"):
        self._input_epoch += 1
        for task in (self._ptt_watchdog, self._mic_lease):
            if task and task is not asyncio.current_task():
                task.cancel()
        self._ptt_watchdog = self._mic_lease = self._mic_deadline = None
        # Pending starts see the invalidated epoch and clean themselves before
        # releasing this lock. Hold it through teardown so a second tab cannot
        # reopen input while Stop is still claiming to shut it down.
        async with self._mic_lock:
            voice, self.voice, self.input_state = self.voice, None, "off"
            if voice:
                await owned_thread(voice.shutdown)
        self.bus.publish(EventType.VOICE_STOPPED, EventSource.VOICE, {"reason": reason, "security_state_unchanged": True})
        return {"ok": True, "message": "Microphone stopped. Security state unchanged."}

    def ptt(self, active):
        if not self.voice or not self.voice.running:
            return {"ok": False, "message": "Explicitly enable the microphone first"}
        if active:
            if self._ptt_watchdog and not self._ptt_watchdog.done():
                return {"ok": False, "message": "Push-to-talk already active"}
            self.speaker.barge_in()
            self.voice.ptt_start()
            self._ptt_watchdog = asyncio.create_task(self._expire_ptt())
        else:
            self.voice.ptt_stop()
            if self._ptt_watchdog:
                self._ptt_watchdog.cancel()
            self._ptt_watchdog = None
        return {"ok": True}

    async def _expire_ptt(self):
        await asyncio.sleep(PTT_LIMIT_SECONDS)
        await self.stop_voice("push-to-talk deadline elapsed")

    def input_event(self, kind, data, epoch):
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._input_event, kind, data, epoch)

    def _input_event(self, kind, data, epoch):
        if epoch != self._input_epoch or self.voice is None or not self.voice.running:
            return
        if kind == "utterance":
            return self.handle_transcript(data)
        event = {"speech_started": EventType.VOICE_SPEECH_STARTED, "speech_ended": EventType.VOICE_SPEECH_ENDED,
                 "error": EventType.VOICE_ERROR, "rejected": EventType.VOICE_COMMAND_REJECTED,
                 "recovered": EventType.VOICE_LISTENING}.get(kind)
        if event:
            self.bus.publish(event, EventSource.VOICE, data)

    def handle_transcript(self, data):
        text = str(data.get("text", ""))[:300].strip()
        normalized = " ".join(re.sub(r"[^a-z ]", " ", text.lower()).split())
        self.bus.publish(EventType.VOICE_TRANSCRIBED, EventSource.VOICE, {"text": text, "confidence": data.get("confidence", 0)})
        intent, reply = "unsupported_read_only_voice", None
        if normalized in {"stop talking", "be quiet", "stop speech", "mute", "mute speech"}:
            intent = "mute" if normalized in {"mute", "mute speech"} else "stop_talking"
            self.speaker.stop_all("recognized speech control; security state unchanged")
            if intent == "mute":
                self.speaker.muted = self.rs.voice.muted = True
        elif float(data.get("confidence", 0)) < self.rs.voice.min_command_confidence:
            intent = "low_confidence"
        elif data.get("during_playback") and self.speaker.echo_score(text) >= 0.7:
            intent = "echo_rejected"
        elif normalized in {"what happened", "what was that", "describe the last incident", "last incident"}:
            intent = "describe_incident"
            reply = LINES[(self.last_incident.simulated, self.last_incident.rule_id)] if self.last_incident else "No committed incident context has reached this adapter. Check the authenticated console for current security state."
        elif normalized in {"status", "voice status", "are you listening", "help"}:
            intent = "voice_status"
            reply = "I can stop speech and describe the latest received incident. Use the authenticated console for current security state and security actions."
        else:
            reply = "Use the authenticated console for security actions. This voice adapter cannot arm, disarm, approve, resolve, lock, or control hardware."
        if reply:
            self.speaker.say(reply, priority=2, category="airlock_voice_read_only", max_age_s=5)
            self.bus.publish(EventType.VOICE_RESPONSE_GENERATED, EventSource.VOICE, {"text": reply, "source": "fixed factual response; no model inference"})
        result = {"intent": intent, "security_action_executed": False, "reply": reply}
        self.turns.append({"text": text, "intent": intent, "at": time.time()})
        self.bus.publish(EventType.VOICE_COMMAND_RECOGNIZED, EventSource.VOICE, result)
        return result

    def personality_snapshot(self):
        return {"mode": "judge_demo", "effective_mode": "judge_demo", "label": "Judge demo", "unhinged_opt_in": False, "profanity": "off", "max_profanity": "off", "tone": "normal", "supportive_s_left": 0, "pool": "judge_demo", "insults_allowed": False}

    def voice_config(self):
        cached = getattr(self.speaker.engine, "prewarmed", 0)
        return {"profile": self.rs.voice_profile.model_dump(mode="json"), "active_preset": None, "presets": [], "personality": self.personality_snapshot(),
                "fish": {"configured": False, "models": [], "status": {"state": "offline"}, "daily_live_bytes": 0, "live_bytes_today": 0},
                "cache": {"lines": len(LINES), "cached": cached, "missing": max(0, len(LINES)-cached), "pack": {"lines": 0, "cached": 0}, "job": None}, "speaking_now": self.speaker.snapshot()["current"]}

    def snapshot(self):
        voice = {"tts": self.speaker.snapshot(), "input": self.voice.snapshot() if self.voice else {"running": False, "reason": self.input_error or "microphone not enabled", "stt": {"engine": self.settings.stt_engine, "state": "not loaded"}},
                 "state": "thinking" if self.voice and self.voice.transcribing else self.input_state, "last_utterance": None, "last_reply": None, "llm": None,
                 "personality": self.personality_snapshot(), "profile": self.rs.voice_profile.model_dump(mode="json"), "active_preset": None,
                 "cloud": {"configured": False, "status": {"state": "offline"}}, "cache_job": None, "turns": list(self.turns),
                 "output": {"device": self.rs.voice.output_device, "label": "System output", "listed": None, "devices": None, "system_volume": None, "warning": None, **self.speaker.output_status()},
                 "microphone_permission_enabled": self.allow_microphone,
                 "microphone_lease_remaining_s": max(0, self._mic_deadline-time.monotonic()) if self._mic_deadline else 0}
        return {"server_time": time.time(), "run_id": self.run_id, "version": __version__, "uptime_s": time.time()-self.started_at,
                "mode": {"code": "voice", "label": "AIRLOCK voice only", "robot_connected": False, "camera_live": False}, "state": {"name": "VOICE_ONLY"},
                "voice": voice, "settings": self.rs.model_dump(mode="json"), "perf": {}, "robot": {"driver": "none", "status": None}, "vision": {"running": False}}


def make_runtime(data_dir: Path, **kwargs) -> SpeechSurface:
    """Compatibility factory name; does not construct the robot runtime."""
    return SpeechSurface(data_dir, **kwargs)


def create_voice_app(token_file: Path, data_dir: Path, *, enable_audio=False, allow_microphone=False, runtime: SpeechSurface | None = None, **surface_options) -> FastAPI:
    token = read_local_token(token_file)
    origins = ["http://127.0.0.1:5174", "http://localhost:5174"]
    rt = runtime or make_runtime(data_dir, allow_microphone=allow_microphone, **surface_options)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # NullTTS at startup deliberately bypasses legacy full-library prewarm and
        # neural voice construction. Only the fixed AIRLOCK lines are then cached.
        await rt.start()
        if enable_audio:
            try:
                import sounddevice  # noqa: F401
            except ImportError as error:
                await rt.shutdown()
                raise RuntimeError("Cached audio needs sounddevice/PortAudio; install the existing voice extra before opting in") from error
            cached = CachedSystemTTS(data_dir / "airlock_system_cache")
            if not cached.available():
                await rt.shutdown()
                raise RuntimeError("System voice unavailable; audio opt-in cannot silently become claimed speech")
            await asyncio.to_thread(cached.prewarm, list(LINES.values()))
            if not all(cached.clip_for(text) is not None for text in LINES.values()):
                await rt.shutdown()
                raise RuntimeError("AIRLOCK warning cache incomplete; do not claim immediate cached playback")
            rt.speaker.engine = cached
        app.state.announcer = IncidentAnnouncer(rt.speaker, data_dir / "announcements.sqlite3", audio_enabled=enable_audio)
        try:
            yield
        finally:
            app.state.announcer.close()
            await rt.shutdown()

    app = FastAPI(title="AIRLOCK local voice adapter", lifespan=lifespan)
    app.state.runtime = rt
    app.state.audio_enabled = enable_audio
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST", "PATCH", "PUT", "OPTIONS"], allow_headers=["Content-Type", "Authorization"])

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        host = request.client.host if request.client else ""
        if host != "testclient":
            try:
                if not ipaddress.ip_address(host).is_loopback:
                    return JSONResponse({"detail": "Voice adapter accepts loopback clients only"}, 403)
            except ValueError:
                return JSONResponse({"detail": "Voice adapter accepts loopback clients only"}, 403)
        origin = request.headers.get("origin")
        if origin and origin not in origins:
            return JSONResponse({"detail": "Origin is not the local AIRLOCK UI"}, 403)
        if request.url.path in {"/api/voice/start", "/api/voice/ptt"} and origin not in origins and not hmac.compare_digest(request.headers.get("authorization", "").encode(), f"Bearer {token}".encode()):
            return JSONResponse({"detail": "Trusted UI request or private local capability required"}, 401)
        if request.url.path == "/api/airlock/announce":
            if not hmac.compare_digest(request.headers.get("authorization", "").encode(), f"Bearer {token}".encode()):
                return JSONResponse({"detail": "Announcement adapter credential required"}, 401)
            try:
                if int(request.headers.get("content-length", "0")) > 8192:
                    return JSONResponse({"detail": "Announcement payload exceeds 8192 bytes"}, 413)
            except ValueError:
                return JSONResponse({"detail": "Invalid Content-Length"}, 400)
        return await call_next(request)

    @app.get("/api/health")
    async def health():
        return {"ok": True, "version": __version__, "state": "VOICE_ONLY", "robot": None, "vision": {"running": False}, "voice": {"running": bool(rt.voice and rt.voice.running), "tts": rt.speaker.engine.name}}

    @app.get("/api/state")
    async def state():
        return rt.snapshot()

    @app.get("/api/events")
    async def events(limit: int = 100):
        return [event.model_dump(mode="json") for event in rt.bus.history(limit=min(max(limit, 0), 400))]

    @app.get("/api/settings")
    async def settings():
        return {"runtime": rt.rs.model_dump(mode="json"), "process": {"robot_driver": "none", "stt_engine": rt.settings.stt_engine, "llm_provider": "none", "tts_engine": rt.speaker.engine.name}}

    @app.get("/api/voice/config")
    async def voice_config():
        return rt.voice_config()

    @app.get("/api/voice/history")
    async def voice_history():
        return {"history": list(rt.speaker.history)}

    @app.get("/api/voice/presets")
    async def presets():
        return {"presets": [], "active": None}

    @app.get("/api/voice/devices")
    async def devices():
        try:
            import sounddevice as sd

            found = await asyncio.to_thread(sd.query_devices)
            return {"available": True, "inputs": [{"index": i, "name": item["name"]} for i, item in enumerate(found) if item["max_input_channels"]], "outputs": [{"index": i, "name": item["name"]} for i, item in enumerate(found) if item["max_output_channels"]]}
        except Exception as error:
            return {"available": False, "reason": str(error)[:200], "inputs": [], "outputs": []}

    @app.put("/api/voice/profile")
    @app.post("/api/voice/personality")
    async def unsupported_profile():
        raise HTTPException(422, "This standalone adapter supports fixed clean system speech; full voice profiles use the preserved voice application")

    @app.get("/api/airlock/voice-status")
    async def voice_status():
        return {"ok": True, "audio_enabled": enable_audio, "adapter": "cached macOS system voice" if enable_audio else "text-only (audio opt-in disabled)", "microphone": "running" if rt.voice and rt.voice.running else "off", "microphone_permission_enabled": rt.allow_microphone, "model": "no conversation model loaded", "robot": "not initialized; routes absent", "speech": rt.speaker.snapshot(), "input": rt.snapshot()["voice"]["input"]}

    @app.post("/api/airlock/announce")
    async def announce(body: IncidentAnnouncement, request: Request):
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
            raise HTTPException(401, "Announcement adapter credential required")
        try:
            result = app.state.announcer.announce(body)
            rt.last_incident = body
            return result
        except AnnouncementRejected as error:
            raise HTTPException(error.status_code, str(error)) from error

    @app.patch("/api/settings")
    async def speech_settings(patch: dict):
        if set(patch) != {"voice"} or not isinstance(patch["voice"], dict) or set(patch["voice"]) - {"muted", "input_device", "output_device"}:
            raise HTTPException(422, "Only voice mute and input/output preferences are accepted")
        if "input_device" in patch["voice"] and rt.voice is not None:
            raise HTTPException(409, "Stop the microphone before changing its device")
        try:
            rt.rs.voice = rt.rs.voice.model_validate({**rt.rs.voice.model_dump(), **patch["voice"]})
        except ValueError as error:
            raise HTTPException(422, "Invalid voice preference") from error
        rt.speaker.muted = rt.rs.voice.muted
        if rt.speaker.muted:
            rt.speaker.stop_all("operator muted speech; security state unchanged")
        if isinstance(rt.speaker.engine, CachedSystemTTS):
            rt.speaker.engine.output_device = rt.rs.voice.output_device
        return {"ok": True, "runtime": rt.rs.model_dump(mode="json")}

    @app.post("/api/voice/start")
    async def start_voice():
        return await rt.start_voice()

    @app.post("/api/voice/stop")
    async def stop_speech():
        rt.speaker.stop_all("operator stopped speech; security state unchanged")
        return await rt.stop_voice()

    @app.post("/api/voice/ptt")
    async def ptt(body: dict):
        if set(body) != {"action"} or not isinstance(body["action"], str) or body["action"] not in {"start", "stop"}:
            raise HTTPException(422, "Expected only action:start or action:stop")
        return rt.ptt(body["action"] == "start")

    @app.post("/api/airlock/voice-control")
    async def voice_control(body: dict):
        if body != {"intent": "stop_talking"}:
            raise HTTPException(422, "Only stop_talking is accepted; speech cannot arm or disarm security")
        rt.speaker.stop_all("stop talking; security state unchanged")
        return {"ok": True, "message": "Speech stopped. No security reducer was called."}

    @app.post("/api/voice/test")
    @app.post("/api/tts/test")
    async def rehearsal_test():
        # Authenticated bridge path is required for incident speech. UI audio test
        # is a fixed explicit rehearsal, never arbitrary text or cloud generation.
        if not enable_audio:
            return {"ok": False, "message": "Audio is disabled. Restart this adapter with --enable-audio to opt in."}
        text = LINES[(True, "unknown_usb")]
        queued = rt.speaker.say(text, priority=2, category="airlock_audio_rehearsal", cacheable=True)
        return {"ok": queued, "message": "Queued a clearly labeled rehearsal", "engine": rt.speaker.engine.name}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        host = ws.client.host if ws.client else ""
        try:
            allowed = host == "testclient" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            allowed = False
        if not allowed or (ws.headers.get("origin") and ws.headers["origin"] not in origins):
            await ws.close(code=1008)
            return
        await ws.accept()
        sub = rt.bus.subscribe(maxsize=128)
        send_lock = asyncio.Lock()

        async def send(message):
            async with send_lock:
                await ws.send_json(message)

        async def outbound():
            await send({"type": "snapshot", "data": rt.snapshot()})
            for event in rt.bus.history(limit=100):
                await send({"type": "event", "data": event.model_dump(mode="json"), "replay": True})
            while True:
                try:
                    event = await asyncio.wait_for(sub.get(), timeout=0.5)
                    await send({"type": "event", "data": event.model_dump(mode="json")})
                except TimeoutError:
                    pass
                await send({"type": "snapshot", "data": rt.snapshot()})
                if rt.voice and rt.voice.running:
                    await send({"type": "audio", "data": rt.voice.level()})

        async def inbound():
            while True:
                raw = await ws.receive_text()
                if len(raw) > 2048:
                    await ws.close(code=1009)
                    return
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("type") == "ping":
                    await send({"type": "pong", "t": message.get("t"), "server_time": time.time()})
                elif message.get("type") == "ptt" and isinstance(message.get("action"), str) and message["action"] in {"start", "stop"}:
                    if ws.headers.get("origin") not in origins and not hmac.compare_digest(ws.headers.get("authorization", "").encode(), f"Bearer {token}".encode()):
                        await ws.close(code=1008)
                        return
                    rt.ptt(message["action"] == "start")

        tasks = [asyncio.create_task(outbound()), asyncio.create_task(inbound())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                    task.result()
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(BaseException):
                    await task
            sub.close()
            await rt.stop_voice("console disconnected; explicit microphone restart required")

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init-token", type=Path)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--data-dir", type=Path, default=Path("../data/airlock-voice"))
    parser.add_argument("--port", type=int, default=8077)
    parser.add_argument("--enable-audio", action="store_true")
    parser.add_argument("--allow-microphone", action="store_true")
    parser.add_argument("--stt-engine", choices=["whisper.cpp"], default="whisper.cpp")
    parser.add_argument("--models-dir", type=Path)
    parser.add_argument("--vad-engine", choices=["energy", "silero"], default="energy")
    parser.add_argument("--microphone-lease-seconds", type=int, default=300)
    args = parser.parse_args()
    if args.init_token:
        args.init_token.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(args.init_token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump({"token": secrets.token_urlsafe(32)}, stream)
        print(f"Private local adapter credential created: {args.init_token}")
        return
    if not args.token_file:
        parser.error("--token-file is required; create one first with --init-token")
    import uvicorn

    app = create_voice_app(args.token_file, args.data_dir, enable_audio=args.enable_audio,
                           allow_microphone=args.allow_microphone, stt_engine=args.stt_engine,
                           models_dir=args.models_dir, vad_engine=args.vad_engine,
                           microphone_lease_seconds=args.microphone_lease_seconds)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
