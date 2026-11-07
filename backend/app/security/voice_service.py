"""Loopback-only AIRLOCK voice surface; existing runtime/queue, no robot routes.

Run from backend: python -m app.security.voice_service --init-token ../data/airlock-voice/local-token.private.json
Then: python -m app.security.voice_service --token-file ../data/airlock-voice/local-token.private.json [--enable-audio]
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import ipaddress
import json
import os
import secrets
import stat
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..api.routes import router as existing_routes
from ..api.websocket import handle_ws
from ..config import RuntimeSettings, Settings
from ..runtime import LockedInRuntime
from ..voice.tts import CachedSystemTTS, NullTTS
from .announcements import LINES, AnnouncementRejected, IncidentAnnouncement, IncidentAnnouncer


def read_local_token(path: Path) -> str:
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("Local announcement token file must have permissions 0600")
    token = json.loads(path.read_text()).get("token")
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError("Local announcement token must contain at least 32 characters")
    return token


def make_runtime(data_dir: Path) -> LockedInRuntime:
    settings = Settings(
        _env_file=None, data_dir=data_dir, robot_driver="mock", mock_autoconnect=False,
        zeus_autoconnect=False, zeus_motor_commands="disabled", vision_autostart=False,
        vision_preload=False, voice_autostart=False, stt_engine="none", tts_engine="none",
        llm_provider="none", offline=True, fish_audio_api_key=None, anthropic_api_key=None,
    )
    settings.fish_audio_api_key = None
    settings.anthropic_api_key = None
    runtime_settings = RuntimeSettings()
    runtime_settings.coach.personality = "judge_demo"
    runtime_settings.coach.profanity = "off"
    runtime_settings.coach.unhinged_opt_in = False
    runtime_settings.vision_enabled = False
    runtime_settings.voice_profile.provider = "system"
    runtime_settings.voice_profile.local_voice = "system"
    runtime_settings.active_preset = None
    runtime_settings.conversation.generated_replies = False
    runtime_settings.conversation.proactive_generated = False
    runtime_settings.conversation.anticipate = False
    return LockedInRuntime(settings, runtime_settings=runtime_settings, persist_settings=False, tts_engine=NullTTS(), enable_vision=False, enable_voice=False, enable_llm=False)


def create_voice_app(token_file: Path, data_dir: Path, *, enable_audio: bool = False, runtime: LockedInRuntime | None = None) -> FastAPI:
    token = read_local_token(token_file)
    origins = ["http://127.0.0.1:5174", "http://localhost:5174"]
    rt = runtime or make_runtime(data_dir)

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
        if request.url.path == "/api/airlock/announce":
            if not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {token}"):
                return JSONResponse({"detail": "Announcement adapter credential required"}, 401)
            try:
                if int(request.headers.get("content-length", "0")) > 8192:
                    return JSONResponse({"detail": "Announcement payload exceeds 8192 bytes"}, 413)
            except ValueError:
                return JSONResponse({"detail": "Invalid Content-Length"}, 400)
        return await call_next(request)

    # Reuse exact existing read handlers. Never include the entire legacy router:
    # /robot/*, /command, /session/*, camera and microphone test routes do not exist.
    readable = {"/api/health", "/api/state", "/api/events", "/api/settings", "/api/voice/config", "/api/voice/history", "/api/voice/devices", "/api/voice/presets"}
    for route in existing_routes.routes:
        if getattr(route, "path", "") in readable and "GET" in getattr(route, "methods", set()):
            app.router.routes.append(route)

    @app.get("/api/airlock/voice-status")
    async def voice_status():
        return {"ok": True, "audio_enabled": enable_audio, "adapter": "cached macOS system voice" if enable_audio else "text-only (audio opt-in disabled)", "microphone": "disabled", "model": "disabled", "robot": "disconnected; routes absent", "speech": rt.speaker.snapshot()}

    @app.post("/api/airlock/announce")
    async def announce(body: IncidentAnnouncement, request: Request):
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied, f"Bearer {token}"):
            raise HTTPException(401, "Announcement adapter credential required")
        try:
            return app.state.announcer.announce(body)
        except AnnouncementRejected as error:
            raise HTTPException(error.status_code, str(error)) from error

    @app.patch("/api/settings")
    async def speech_settings(patch: dict):
        if set(patch) != {"voice"} or not isinstance(patch["voice"], dict) or set(patch["voice"]) != {"muted"} or not isinstance(patch["voice"]["muted"], bool):
            raise HTTPException(422, "AIRLOCK voice settings accept only voice.muted; no security or robot settings")
        muted = patch["voice"]["muted"]
        rt.rs.voice.muted = muted
        rt.speaker.muted = muted
        if muted:
            rt.speaker.stop_all("operator muted speech; security state unchanged")
        return {"ok": True, "runtime": rt.rs.model_dump(mode="json"), "message": "Speech muted" if muted else "Speech unmuted"}

    @app.post("/api/voice/start")
    async def mic_disabled():
        return {"ok": False, "message": "This announcement adapter has no microphone or ASR. Use the separately consented existing voice runtime for microphone tests."}

    @app.post("/api/voice/stop")
    async def stop_speech():
        rt.speaker.stop_all("operator stopped speech; security state unchanged")
        return {"ok": True, "message": "Speech stopped. Security state is unchanged."}

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
        await handle_ws(ws, rt)  # rt.voice is always None: incoming PTT cannot record.

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init-token", type=Path)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--data-dir", type=Path, default=Path("../data/airlock-voice"))
    parser.add_argument("--port", type=int, default=8077)
    parser.add_argument("--enable-audio", action="store_true")
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

    app = create_voice_app(args.token_file, args.data_dir, enable_audio=args.enable_audio)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
