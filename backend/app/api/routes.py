"""REST API. Commands return ``{ok, message, state, reply, data}``; validation errors are 422."""

from __future__ import annotations

import asyncio
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..config import PhoneZone
from ..robot.mock import MockRobotTransport
from ..runtime import LockedInRuntime
from ..simulation import scenarios

router = APIRouter(prefix="/api")


def rt_of(request: Request) -> LockedInRuntime:
    return request.app.state.runtime


# ------------------------------------------------------------------ models
class StartSession(BaseModel):
    task: str | None = Field(None, max_length=120)
    duration_min: float | None = Field(None, ge=1, le=240)
    target_units: int | None = Field(None, ge=1, le=500)
    unit_label: str | None = Field(None, max_length=24)


class EndSession(BaseModel):
    reason: str = "user"


class Progress(BaseModel):
    units: int = Field(1, ge=-50, le=50)
    note: str | None = Field(None, max_length=200)


class Connect(BaseModel):
    driver: Literal["mock", "zeus"] | None = None
    host: str | None = Field(None, max_length=64, pattern=r"^[A-Za-z0-9.\-]+$")


class Autonomy(BaseModel):
    enabled: bool
    clear_path_confirmed: bool = False


class Move(BaseModel):
    primitive: str
    speed: float | None = Field(None, ge=0, le=100)
    duration_s: float | None = Field(None, gt=0, le=3)
    test_profile: bool = False


class TypedCommand(BaseModel):
    text: str = Field(min_length=1, max_length=300)


class VisionStart(BaseModel):
    source: str | None = Field(None, max_length=300)


class Seconds(BaseModel):
    seconds: float = Field(2.0, gt=0, le=10)


class LinkLoss(BaseModel):
    duration_s: float = Field(3.0, gt=0, le=30)


class LinkTest(BaseModel):
    # How the robot is set up decides the longest allowed freeze (wheels off the ground: 5 s,
    # on the floor: 1.5 s). There is no default: the operator has to say.
    setup: Literal["wheels_off_ground", "on_floor"]
    primitive: Literal["forward", "backward", "strafe_left", "strafe_right"] = "forward"
    speed: int = Field(30, ge=1, le=100)  # the LINK_TEST profile clamps to 40
    lead_s: float = Field(0.5, ge=0.3, le=1.0)
    freeze_s: float = Field(2.0, ge=0.5, le=5.0)


class TTSTest(BaseModel):
    text: str = Field("LOCKED IN audio check. Can you hear me?", max_length=200)


class SimReach(BaseModel):
    confirm: bool = True


class SimFocus(BaseModel):
    state: Literal["FOCUSED", "POSSIBLY_DISTRACTED", "AWAY", "UNKNOWN"]


class Trial(BaseModel):
    kind: Literal["reach", "non_reach"]
    note: str = Field("", max_length=200)


class Ptt(BaseModel):
    action: Literal["start", "stop"]


# ------------------------------------------------------------------ health / state
@router.get("/health")
async def health(request: Request):
    rt = rt_of(request)
    t = rt.transport
    return {
        "ok": True,
        "version": __version__,
        "uptime_s": round(time.time() - rt.started_at, 1),
        "state": rt.sm.state.value,
        "mode": rt.mode(),
        "robot": None if t is None else {"transport": t.name, "connected": t.connected, "simulated": t.simulated},
        "vision": {"running": rt.vision_status.get("running", False), "camera": rt.vision_status.get("camera")},
        "voice": {"running": bool(rt.voice and rt.voice.running), "tts": rt.speaker.engine.name},
        "ws_clients": rt.bus.subscriber_count,
    }


@router.get("/state")
async def state(request: Request):
    return rt_of(request).snapshot()


@router.get("/events")
async def events(request: Request, limit: int = 200, types: str | None = None):
    type_set = set(types.split(",")) if types else None
    return [e.model_dump(mode="json") for e in rt_of(request).bus.history(limit=min(limit, 1000), types=type_set)]


# ------------------------------------------------------------------ session
@router.post("/session/start")
async def session_start(body: StartSession, request: Request):
    r = await rt_of(request).start_session(body.task, body.duration_min, body.target_units, body.unit_label, origin="operator")
    return r.to_dict()


@router.post("/session/pause")
async def session_pause(request: Request):
    return (await rt_of(request).pause_session(origin="operator")).to_dict()


@router.post("/session/resume")
async def session_resume(request: Request):
    return (await rt_of(request).resume_session(origin="operator")).to_dict()


@router.post("/session/end")
async def session_end(body: EndSession, request: Request):
    return (await rt_of(request).end_session(reason=body.reason, origin="operator")).to_dict()


@router.post("/session/progress")
async def session_progress(body: Progress, request: Request):
    return (await rt_of(request).add_progress(body.units, origin="operator", note=body.note)).to_dict()


@router.post("/release")
async def release(request: Request):
    return (await rt_of(request).release(origin="operator")).to_dict()


@router.post("/estop")
async def estop(request: Request):
    return (await rt_of(request).emergency_stop(origin="operator")).to_dict()


@router.post("/reset")
async def reset(request: Request):
    return (await rt_of(request).reset(origin="operator")).to_dict()


# ------------------------------------------------------------------ robot
@router.post("/robot/connect")
async def robot_connect(body: Connect, request: Request):
    return (await rt_of(request).connect_robot(body.driver, body.host)).to_dict()


@router.post("/robot/disconnect")
async def robot_disconnect(request: Request):
    return (await rt_of(request).disconnect_robot()).to_dict()


@router.post("/robot/arm")
async def robot_arm(request: Request):
    return (await rt_of(request).arm()).to_dict()


@router.post("/robot/disarm")
async def robot_disarm(request: Request):
    return (await rt_of(request).disarm()).to_dict()


@router.post("/robot/motor-power")
async def robot_motor_power(request: Request):
    return (await rt_of(request).confirm_motor_power()).to_dict()


@router.post("/robot/autonomy")
async def robot_autonomy(body: Autonomy, request: Request):
    return (await rt_of(request).set_autonomy(body.enabled, body.clear_path_confirmed)).to_dict()


@router.post("/robot/move")
async def robot_move(body: Move, request: Request):
    return (await rt_of(request).manual_move(body.primitive, body.speed, body.duration_s, test_profile=body.test_profile)).to_dict()


@router.post("/robot/stop")
async def robot_stop(request: Request):
    return (await rt_of(request).stop_robot(origin="operator")).to_dict()


@router.post("/robot/return")
async def robot_return(request: Request):
    return (await rt_of(request).return_home(origin="operator")).to_dict()


@router.post("/robot/home")
async def robot_home(request: Request):
    return (await rt_of(request).set_home(origin="operator")).to_dict()


@router.post("/robot/simulate-link-loss")
async def robot_link_loss(body: LinkLoss, request: Request):
    rt = rt_of(request)
    if not isinstance(rt.transport, MockRobotTransport) or not rt.transport.connected:
        raise HTTPException(400, "link-loss simulation is only available on the connected mock robot")
    rt.transport.simulate_link_loss(body.duration_s)
    return {"ok": True, "message": f"simulated link loss for {body.duration_s:g}s"}


@router.post("/robot/link-test")
async def robot_link_test(body: LinkTest, request: Request):
    """R5: drive slowly, stop transmitting with the link open, record what happens."""
    return (
        await rt_of(request).run_link_test(body.setup, body.primitive, body.speed, body.lead_s, body.freeze_s)
    ).to_dict()


@router.get("/robot/link-test")
async def robot_link_tests(request: Request):
    return {"results": list(rt_of(request).link_tests)}


# ------------------------------------------------------------------ voice / commands
@router.post("/command")
async def typed_command(body: TypedCommand, request: Request):
    return (await rt_of(request).handle_utterance(body.text, source="typed", stt_confidence=1.0)).to_dict()


@router.post("/voice/start")
async def voice_start(request: Request):
    return (await rt_of(request).start_voice()).to_dict()


@router.post("/voice/stop")
async def voice_stop(request: Request):
    return (await rt_of(request).stop_voice()).to_dict()


@router.post("/voice/ptt")
async def voice_ptt(body: Ptt, request: Request):
    rt = rt_of(request)
    if rt.voice is None or not rt.voice.running:
        raise HTTPException(400, "voice input is not running")
    if body.action == "start":
        rt.ptt(True)  # barge-in, as over the WebSocket (Speaker.interrupt() is not awaitable)
        rt.voice.ptt_start()
    else:
        rt.voice.ptt_stop()
    return {"ok": True}


@router.get("/voice/devices")
async def voice_devices():
    try:
        import sounddevice as sd
    except Exception as exc:
        return {"available": False, "reason": str(exc), "inputs": [], "outputs": []}
    devices = await asyncio.to_thread(sd.query_devices)
    default_in, default_out = sd.default.device
    inputs, outputs = [], []
    for i, d in enumerate(devices):
        item = {"index": i, "name": d["name"], "default_samplerate": d["default_samplerate"]}
        if d["max_input_channels"] > 0:
            inputs.append({**item, "channels": d["max_input_channels"], "default": i == default_in})
        if d["max_output_channels"] > 0:
            outputs.append({**item, "channels": d["max_output_channels"], "default": i == default_out})
    return {"available": True, "inputs": inputs, "outputs": outputs}


@router.post("/voice/test-mic")
async def voice_test_mic(body: Seconds, request: Request):
    from ..voice.input import measure_input_level

    rt = rt_of(request)
    try:
        result = await asyncio.to_thread(measure_input_level, rt.rs.voice.input_device, body.seconds)
    except Exception as exc:
        return {"ok": False, "message": f"microphone test failed: {exc}"}
    return {"ok": True, **result}


@router.post("/tts/test")
async def tts_test(body: TTSTest, request: Request):
    rt = rt_of(request)
    rt.speaker.say(body.text, priority=2, category="test")
    return {"ok": True, "engine": rt.speaker.engine.name}


# ------------------------------------------------------------------ personality + voice
class PersonalityPatch(BaseModel):
    mode: Literal["chill", "roast", "unhinged", "judge_demo"] | None = None
    profanity: Literal["off", "mild", "strong"] | None = None
    tone: Literal["normal", "gentle", "serious"] | None = None
    unhinged_opt_in: bool | None = None


class PresetSave(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)


class VoiceTest(BaseModel):
    live: bool = False  # True asks Fish Audio for a fresh clip: the click is the approval
    text: str | None = Field(None, max_length=200)


class Sample(BaseModel):
    category: str = Field("reach_first", max_length=40)


class CachePlan(BaseModel):
    scope: Literal["pack", "pack_chill", "current", "all_modes"] = "pack"


class CacheGenerate(BaseModel):
    plan_id: str = Field(..., max_length=20)
    confirm: bool = False
    max_bytes: int = Field(..., ge=0, le=2_000_000)


@router.get("/voice/config")
async def voice_config(request: Request):
    return rt_of(request).voice_config()


@router.put("/voice/profile")
async def voice_profile(body: dict, request: Request):
    try:
        return rt_of(request).update_voice_profile(body).to_dict()
    except ValueError as exc:
        raise HTTPException(422, str(exc).splitlines()[0][:300]) from exc


@router.post("/voice/personality")
async def voice_personality(body: PersonalityPatch, request: Request):
    return rt_of(request).set_personality(body.mode, body.profanity, body.tone, body.unhinged_opt_in).to_dict()


@router.get("/voice/presets")
async def voice_presets(request: Request):
    rt = rt_of(request)
    return {"presets": rt.presets(), "active": rt.rs.active_preset}


@router.post("/voice/presets")
async def voice_preset_save(body: PresetSave, request: Request):
    return rt_of(request).save_preset(body.name).to_dict()


@router.post("/voice/presets/{preset_id}/apply")
async def voice_preset_apply(preset_id: str, request: Request):
    return rt_of(request).apply_preset(preset_id).to_dict()


@router.delete("/voice/presets/{preset_id}")
async def voice_preset_delete(preset_id: str, request: Request):
    return rt_of(request).delete_preset(preset_id).to_dict()


@router.post("/voice/cloud/check")
async def voice_cloud_check(request: Request):
    return {"status": await rt_of(request).check_cloud_voice()}


@router.post("/voice/cloud/credit")
async def voice_cloud_credit(request: Request):
    from ..voice.fish_audio import FishAudioError

    try:
        return {"ok": True, **(await rt_of(request).fish.credit())}
    except FishAudioError as exc:
        return {"ok": False, "message": exc.message}


@router.get("/voice/cloud/voices")
async def voice_cloud_voices(request: Request, mine: bool = True, q: str | None = None):
    from ..voice.fish_audio import FishAudioError

    try:
        return {"voices": await rt_of(request).cloud_voices(mine=mine, query=q)}
    except FishAudioError as exc:
        return {"voices": [], "error": exc.message}


@router.post("/voice/test")
async def voice_test(body: VoiceTest, request: Request):
    return (await rt_of(request).test_voice(live=body.live, text=body.text)).to_dict()


@router.post("/voice/sample")
async def voice_sample(body: Sample, request: Request):
    return rt_of(request).speak_sample(body.category).to_dict()


@router.post("/voice/cache/plan")
async def voice_cache_plan(body: CachePlan, request: Request):
    return rt_of(request).plan_cache(body.scope).to_dict()


@router.post("/voice/cache/generate")
async def voice_cache_generate(body: CacheGenerate, request: Request):
    return rt_of(request).start_cache_job(body.plan_id, body.confirm, body.max_bytes).to_dict()


@router.delete("/voice/cache/generate")
async def voice_cache_cancel(request: Request):
    return rt_of(request).cancel_cache_job().to_dict()


@router.get("/voice/history")
async def voice_history(request: Request):
    return {"history": list(rt_of(request).speaker.history)}


# ------------------------------------------------------------------ settings
@router.get("/settings")
async def get_settings(request: Request):
    rt = rt_of(request)
    s = rt.settings
    return {
        "runtime": rt.rs.model_dump(mode="json"),
        "process": {
            "robot_driver": rt.driver,
            "zeus_host": s.zeus_host,
            "zeus_port": s.zeus_port,
            "zeus_device_watchdog_verified": s.zeus_device_watchdog_verified,
            "stt_engine": s.stt_engine,
            "whisper_model": s.whisper_model,
            "tts_engine": rt.speaker.engine.name,
            "llm_provider": s.llm_provider,
            "llm_configured": rt.llm is not None,
            "fish_audio_configured": bool(s.fish_audio_api_key),
            "store_raw_video": s.store_raw_video,
        },
    }


@router.patch("/settings")
async def patch_settings(patch: dict, request: Request):
    rt = rt_of(request)
    try:
        rt.update_settings(patch)
    except Exception as exc:
        raise HTTPException(422, f"invalid settings: {exc}") from exc
    return {"ok": True, "runtime": rt.rs.model_dump(mode="json")}


# ------------------------------------------------------------------ vision
@router.put("/vision/zone")
async def put_zone(zone: PhoneZone, request: Request):
    rt = rt_of(request)
    key = rt.zone_setting_key  # the zone of the camera in use (fixed or robot-mounted)
    data = zone.model_dump(mode="json")
    if key == "robot_camera_zone":
        data.update(mode="static", anchor=None)
    rt.update_settings({key: data})
    return {"ok": True, "zone": getattr(rt.rs, key).model_dump(mode="json"), "camera": key}


class ZoneAttach(BaseModel):
    points: list[tuple[float, float]] | None = None


@router.post("/vision/zone/attach")
async def zone_attach(body: ZoneAttach, request: Request):
    if body.points is not None:
        try:
            PhoneZone(points=body.points)  # same bounds / point-count rules as a saved zone
        except ValueError as exc:
            raise HTTPException(422, f"invalid zone: {exc}") from exc
    return (await rt_of(request).attach_zone(body.points)).to_dict()


@router.post("/vision/zone/detach")
async def zone_detach(request: Request):
    return (await rt_of(request).detach_zone()).to_dict()


@router.get("/vision/marker.png")
async def marker_png(marker_id: int = 7, dictionary: str = "DICT_4X4_50", px: int = 600):
    import cv2

    from ..vision.robot_tracker import DICTIONARIES, marker_image

    if dictionary not in DICTIONARIES or not (0 <= marker_id < 50) or not (120 <= px <= 2400):
        raise HTTPException(422, "invalid marker request")
    ok, buf = cv2.imencode(".png", marker_image(marker_id, dictionary, px))
    if not ok:
        raise HTTPException(500, "could not render marker")
    return Response(buf.tobytes(), media_type="image/png", headers={"Content-Disposition": f'inline; filename="locked-in-marker-{marker_id}.png"'})


@router.post("/vision/start")
async def vision_start(body: VisionStart, request: Request):
    return (await rt_of(request).start_vision(body.source)).to_dict()


@router.post("/vision/stop")
async def vision_stop(request: Request):
    return (await rt_of(request).stop_vision()).to_dict()


@router.get("/vision/cameras")
async def vision_cameras(request: Request):
    from ..vision.webcam import probe_cameras

    rt = rt_of(request)
    busy = rt.vision_status.get("source") if rt.vision_live else None
    return {"cameras": await asyncio.to_thread(probe_cameras, 4, busy)}


@router.get("/vision/frame.jpg")
async def vision_frame(request: Request):
    rt = rt_of(request)
    jpg = rt.vision.latest_jpeg() if rt.vision is not None else None
    if jpg is None:
        raise HTTPException(503, "CAMERA OFFLINE")
    return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/vision/stream.mjpg")
async def vision_stream(request: Request):
    rt = rt_of(request)
    if rt.vision is None:
        raise HTTPException(503, "CAMERA OFFLINE")
    boundary = "lockedinframe"

    async def frames():
        last_id = -1
        rt.vision.stream_clients += 1
        try:
            while not await request.is_disconnected():
                item = rt.vision.latest_jpeg_with_id()
                if item is None or item[0] == last_id:
                    await asyncio.sleep(0.02)
                    continue
                last_id, jpg = item
                yield (
                    f"--{boundary}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpg)}\r\n\r\n".encode() + jpg + b"\r\n"
                )
                await asyncio.sleep(1 / 20)
        finally:
            rt.vision.stream_clients -= 1

    return StreamingResponse(
        frames(), media_type=f"multipart/x-mixed-replace; boundary={boundary}", headers={"Cache-Control": "no-store"}
    )


# ------------------------------------------------------------------ simulation (Mode C)
@router.post("/sim/reach")
async def sim_reach(body: SimReach, request: Request):
    rt = rt_of(request)
    asyncio.create_task(scenarios.simulate_reach(rt, confirm=body.confirm))
    return {"ok": True, "message": "simulated reach injected (labelled SIMULATED)"}


@router.post("/sim/focus")
async def sim_focus(body: SimFocus, request: Request):
    await scenarios.simulate_focus(rt_of(request), body.state)
    return {"ok": True}


@router.post("/sim/demo")
async def sim_demo_start(request: Request):
    rt = rt_of(request)
    if rt._demo_task is not None and not rt._demo_task.done():
        return {"ok": False, "message": "demo already running"}
    ok, why = await scenarios.prepare_demo(rt)
    if not ok:
        return {"ok": False, "message": f"demo not started: {why}"}
    rt._demo_task = asyncio.create_task(scenarios.run_demo(rt, prepared=True), name="demo-scenario")
    return {"ok": True, "message": "demo scenario started (simulated perception)"}


@router.delete("/sim/demo")
async def sim_demo_stop(request: Request):
    rt = rt_of(request)
    if rt._demo_task is not None:
        rt._demo_task.cancel()
    return {"ok": True}


# ------------------------------------------------------------------ experiments
@router.post("/experiments/trial/start")
async def trial_start(body: Trial, request: Request):
    return {"ok": True, "trial": rt_of(request).start_trial(body.kind, body.note)}


@router.post("/experiments/trial/end")
async def trial_end(request: Request):
    return {"ok": True, "trial": rt_of(request).end_trial()}


@router.get("/experiments/summary")
async def trial_summary(request: Request):
    return rt_of(request).trial_summary()


@router.post("/experiments/false-trigger")
async def false_trigger(request: Request):
    rt_of(request).label_false_trigger()
    return {"ok": True}


class VoiceBlock(BaseModel):
    speaker: str = Field(..., min_length=1, max_length=24)  # a label like "A", not a name
    condition: str = Field(..., min_length=1, max_length=60)
    execute: bool = False  # False: classify and score only, never act (stop words still halt motion)


class VoicePrompt(BaseModel):
    expected: str = Field(..., max_length=32)
    phrase: str = Field(..., max_length=200)
    slots: dict[str, int | str] = Field(default_factory=dict)


class QuietWindow(BaseModel):
    seconds: float = Field(120.0, ge=10, le=900)


@router.get("/experiments/voice/prompts")
async def voice_prompts():
    from ..voice.trials import PROMPTS

    return {"prompts": PROMPTS}


@router.post("/experiments/voice/block")
async def voice_block(body: VoiceBlock, request: Request):
    return {"ok": True, "block": rt_of(request).voice_trials.start_block(body.speaker, body.condition, body.execute)}


def _voice_call(fn):
    try:
        return fn()
    except ValueError as exc:
        raise HTTPException(409 if "block" in str(exc) else 422, str(exc)) from exc


@router.post("/experiments/voice/prompt")
async def voice_prompt(body: VoicePrompt, request: Request):
    vt = rt_of(request).voice_trials
    return {"ok": True, "prompt": _voice_call(lambda: vt.set_prompt(body.expected, body.phrase, body.slots))}


@router.post("/experiments/voice/miss")
async def voice_miss(request: Request):
    vt = rt_of(request).voice_trials
    return {"ok": True, "record": _voice_call(vt.miss)}


@router.post("/experiments/voice/quiet")
async def voice_quiet(body: QuietWindow, request: Request):
    vt = rt_of(request).voice_trials
    return {"ok": True, **_voice_call(lambda: vt.start_quiet(body.seconds))}


@router.post("/experiments/voice/end")
async def voice_end(request: Request):
    vt = rt_of(request).voice_trials
    return {"ok": True, "block": _voice_call(vt.end_block)}


@router.get("/experiments/voice")
async def voice_summary(request: Request):
    return rt_of(request).voice_trials.snapshot()


# ------------------------------------------------------------------ history / perf
@router.get("/sessions")
async def sessions(request: Request, limit: int = 20):
    return rt_of(request).store.list_sessions(limit=min(limit, 200))


@router.get("/sessions/{session_id}")
async def session_detail(session_id: str, request: Request):
    data = rt_of(request).store.load_session(session_id)
    if data is None:
        raise HTTPException(404, "session not found")
    return data


@router.get("/metrics/perf")
async def perf(request: Request):
    rt = rt_of(request)
    import psutil

    proc = psutil.Process()
    with proc.oneshot():
        cpu = proc.cpu_percent(interval=None)
        rss = proc.memory_info().rss
    return {
        "latency_ms": rt.perf.summary(),
        "process": {"cpu_percent": cpu, "rss_mb": round(rss / 1e6, 1), "threads": proc.num_threads()},
        "vision": rt.vision_status.get("stats"),
        "watchdog": {"fired": rt.watchdog.fired, "loop_max_lag_ms": round(rt.watchdog.max_lag_s * 1000, 1)},
    }
