"""Process configuration (environment) and operator-editable runtime settings.

Two layers:

* ``Settings`` is read once from the environment / ``.env``. It holds things that
  need a restart to change (ports, API keys, model paths).
* ``RuntimeSettings`` is edited live from the dashboard (phone zone, sensitivity,
  personality, robot host, audio devices...) and persisted to
  ``data/runtime_settings.json`` so calibration survives restarts.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_prefix="LOCKEDIN_",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    # 8077/5174 so LOCKED IN can run next to other projects using the 8000/5173 defaults.
    port: int = 8077
    data_dir: Path = PROJECT_ROOT / "data"
    models_dir: Path = PROJECT_ROOT / "backend" / "models"
    cors_origins: list[str] = ["http://localhost:5174", "http://127.0.0.1:5174"]
    log_level: str = "INFO"

    # Robot ------------------------------------------------------------------
    robot_driver: Literal["mock", "zeus"] = "mock"
    # Only the mock driver auto-connects. The real robot always needs an explicit Connect.
    mock_autoconnect: bool = True
    zeus_host: str = "192.168.4.1"
    zeus_port: int = 8765
    # Set to true ONLY after measuring that the robot stops on its own when the laptop
    # link goes silent (see docs/SAFETY.md, test R5). Until then autonomous motion on
    # real hardware is refused by the safety controller.
    zeus_device_watchdog_verified: bool = False
    # Device-side timeout in seconds that was measured/flashed. Stock firmware: 2-4 s.
    zeus_device_watchdog_timeout_s: float = 4.0
    # Real-robot motor commands (docs/SAFETY.md section 7).
    #   disabled: monitor link only; no region frame (motion or Stop) is ever sent. Default.
    #   bench:    control link for wheels-off-ground tests: motion needs the LOCKED IN firmware
    #             patch reported by the robot, LOW_SPEED_TEST / LINK_TEST profiles only, never
    #             autonomous.
    #   enabled:  as bench, plus normal profiles once zeus_device_watchdog_verified (R5) is true.
    zeus_motor_commands: Literal["disabled", "bench", "enabled"] = "disabled"
    # Open the Zeus link at startup and keep reconnecting (make hardware). Never arms or moves.
    zeus_autoconnect: bool = False
    # ESP32-CAM MJPEG stream. None: http://<robot host>:9000/mjpg (camera_server.cpp, port 9000).
    zeus_camera_url: str | None = None
    # Set ONLY after measuring them on the robot (docs/SAFETY.md section 7). Until then the
    # real robot never returns on its own (dead reckoning is not trusted) and never retreats
    # autonomously (its position is not tracked reliably enough).
    zeus_return_calibrated: bool = False
    zeus_tracking_verified: bool = False

    # Vision -----------------------------------------------------------------
    vision_autostart: bool = False
    # Load MediaPipe models at startup (the load blocks Python for ~0.3-0.5 s; better at
    # boot than when the camera is started mid-session).
    vision_preload: bool = True
    camera_width: int = 640
    camera_height: int = 480
    # Startup camera source, overriding the dashboard's saved choice: "zeus" (the ESP32-CAM),
    # a camera index ("0" = this Mac's webcam), an http:// MJPEG URL or a video file.
    camera_source: str | None = None
    # MJPEG streams: "http" = LOCKED IN's multipart reader (socket timeouts, the camera's own
    # frame timestamps); "opencv" = OpenCV's FFmpeg capture. Both decode frames with OpenCV.
    mjpeg_backend: Literal["http", "opencv"] = "http"

    # Voice ------------------------------------------------------------------
    # whisper.cpp (Metal) is ~3.5x faster than faster-whisper here with fewer word errors
    # (docs/MODELS.md); faster-whisper is used automatically if whisper.cpp is unavailable.
    stt_engine: Literal["whisper.cpp", "faster-whisper", "none"] = "whisper.cpp"
    whisper_cpp_gpu: bool = True
    # While whisper.cpp transcribes, the language model worker pauses its prefill (both use the
    # GPU). Off: an interleaved A/B (scripts/latency_check.py, 4 runs) was slower with it on
    # (model turns p50 3.28 s vs 2.62 s, Whisper p50 221 vs 170 ms; MILESTONE5_VERIFICATION.md).
    stt_gpu_priority: bool = False
    # Hands-free speech detection: "silero" (neural, ignores clicks and hums) or "energy" (M4).
    vad_engine: Literal["silero", "energy"] = "silero"
    whisper_model: str = "base.en"
    whisper_compute_type: str = "int8"
    voice_autostart: bool = False
    # "none" silences the process entirely (CI); otherwise the dashboard's voice profile decides.
    tts_engine: Literal["system", "none"] = "system"
    tts_voice: str | None = None
    tts_rate_wpm: int = 190
    # Startup audio output (make hardware SPEAKER="JLab GO Party"); None keeps the dashboard's choice.
    audio_output: str | None = None

    # Fish Audio (https://fish.audio), the optional cloud voice. Backend only: never sent to the
    # dashboard or written to logs. Without it everything works with the local voice.
    fish_audio_api_key: str | None = Field(
        default=None, validation_alias=AliasChoices("LOCKEDIN_FISH_AUDIO_API_KEY", "FISH_AUDIO_API_KEY", "FISH_API_KEY")
    )
    fish_audio_base_url: str = "https://api.fish.audio"

    @field_validator("tts_engine", mode="before")
    @classmethod
    def _legacy_tts_engine(cls, v):
        return "system" if v in ("elevenlabs", "fish_audio") else v

    # Conversation model. "local" (default): Qwen3 on this Mac through MLX, offline, in a worker
    # process (intelligence/local_llm.py); used only if its weights are downloaded. "anthropic":
    # Claude replies for open-ended questions (paid API, needs a key and the internet; text only).
    # "none": the line library only (Milestone 4 behaviour).
    llm_provider: Literal["none", "anthropic", "local"] = "local"
    local_llm_model: str = "Qwen3-4B-Instruct-2507-4bit"
    # A reply that is not complete by then is dropped and a fixed line is spoken instead.
    local_llm_timeout_s: float = 6.0
    # Local neural voice for generated replies (voice/kokoro_tts.py). int8: half the render time
    # of fp32 on this Mac with a near-identical voice (docs/MODELS.md); fp32 is downloaded too.
    kokoro_model: str = "kokoro-v1.0.int8.onnx"
    anthropic_api_key: str | None = Field(
        default=None, validation_alias=AliasChoices("LOCKEDIN_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")
    )
    llm_model: str = "claude-opus-5-5"
    llm_timeout_s: float = 6.0

    # Privacy ----------------------------------------------------------------
    # Raw video is never written to disk unless this is explicitly enabled.
    store_raw_video: bool = False
    # Offline (the robot's Wi-Fi has no internet; make hardware sets it): never contact Fish
    # Audio, Anthropic or model hubs. Voice lines not in the cache use the local macOS voice.
    offline: bool = False


class PhoneZone(BaseModel):
    """Calibrated phone-access region, normalised image coordinates (0..1).

    mode "static": the zone is fixed in camera coordinates.
    mode "marker": the zone is anchored to ArUco markers on the robot and moves with it;
    ``anchor`` holds the zone in each marker's frame (see vision/robot_tracker.py).
    """

    points: list[tuple[float, float]] = Field(
        default_factory=lambda: [(0.62, 0.55), (0.92, 0.55), (0.92, 0.95), (0.62, 0.95)]
    )
    mode: Literal["static", "marker"] = "static"
    anchor: dict | None = None

    @field_validator("points")
    @classmethod
    def _valid_polygon(cls, pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
        if len(pts) < 3:
            raise ValueError("phone zone needs at least 3 points")
        if len(pts) > 16:
            raise ValueError("phone zone supports at most 16 points")
        clean = []
        for x, y in pts:
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                raise ValueError("phone zone points must be normalised to 0..1")
            clean.append((float(x), float(y)))
        return clean


class VisionTuning(BaseModel):
    """Reach-detector thresholds. Defaults were chosen for 640x480 @ ~30 fps."""

    min_hand_confidence: float = Field(0.6, ge=0.0, le=1.0)
    # Distance (normalised image units) from the zone at which a hand counts as near.
    approach_margin: float = Field(0.12, ge=0.0, le=0.5)
    # Minimum speed toward the zone (normalised units / s) for "approaching".
    approach_min_speed: float = Field(0.15, ge=0.0, le=5.0)
    # How long the hand must stay inside the zone before a reach is confirmed.
    confirm_dwell_ms: int = Field(180, ge=0, le=2000)
    # A hand already resting inside the zone needs this long (no approach seen) to confirm.
    static_dwell_ms: int = Field(900, ge=0, le=5000)
    # Hand must leave by this extra margin before another reach can be confirmed.
    exit_margin: float = Field(0.05, ge=0.0, le=0.5)
    reach_cooldown_s: float = Field(6.0, ge=0.0, le=120.0)
    # Hands are dropped if not seen for this long (tolerates brief detection gaps).
    track_timeout_ms: int = Field(300, ge=50, le=3000)
    # Robot tracking: printed ArUco markers on the robot (see docs/HARDWARE_ASSEMBLY.md).
    marker_dictionary: Literal["DICT_4X4_50", "DICT_4X4_100", "DICT_5X5_50", "DICT_6X6_50"] = "DICT_4X4_50"
    marker_ids: list[int] = Field(default_factory=lambda: [7, 8])
    # How long to keep the last robot pose while its marker is hidden and it has not moved.
    marker_hold_s: float = Field(8.0, ge=0.5, le=60.0)


class MotionTuning(BaseModel):
    """Speeds are joystick radius units (0..100) for the Zeus Car, % for the mock."""

    retreat_primitive: Literal["backward", "forward", "strafe_left", "strafe_right"] = "backward"
    retreat_speed: int = Field(55, ge=0, le=100)
    retreat_duration_s: float = Field(0.6, ge=0.05, le=2.0)
    manual_speed: int = Field(50, ge=0, le=100)
    manual_duration_s: float = Field(0.4, ge=0.05, le=2.0)
    autonomous_cooldown_s: float = Field(10.0, ge=0.0, le=600.0)


class VoiceTuning(BaseModel):
    listen_mode: Literal["push_to_talk", "wake_word", "always"] = "push_to_talk"
    input_device: str | int | None = None
    output_device: str | None = None
    min_command_confidence: float = Field(0.45, ge=0.0, le=1.0)
    # Voice intents that would move the robot (e.g. "come back") need this much.
    min_motion_command_confidence: float = Field(0.8, ge=0.0, le=1.0)
    muted: bool = False


class CoachTuning(BaseModel):
    # How the robot talks (voice/personality.py). Unhinged only takes effect with the opt-in.
    personality: Literal["chill", "roast", "unhinged", "judge_demo"] = "roast"
    profanity: Literal["off", "mild", "strong"] = "off"
    tone: Literal["normal", "gentle", "serious"] = "normal"
    unhinged_opt_in: bool = False
    intensity: Literal["gentle", "standard", "strict"] = "standard"
    min_seconds_between_interventions: float = Field(20.0, ge=0.0, le=600.0)
    announce_milestones: bool = True

    @field_validator("personality", mode="before")
    @classmethod
    def _legacy_personality(cls, v):
        # Milestone 1 names. "chaos" maps to roast, never to unhinged: that needs an opt-in.
        return {"coach": "roast", "chaos": "roast"}.get(v, v)


class ConversationTuning(BaseModel):
    """Milestone 5: the local language model. Each switch falls back to the line library."""

    # Replies the deterministic parser does not cover are written by the local model.
    generated_replies: bool = True
    # Unprompted remarks (a confirmed reach, a real focus streak) are written for the moment.
    proactive_generated: bool = True
    # Prepare the next likely remark while idle, so it plays as fast as a cached line.
    anticipate: bool = True


class FishVoiceSettings(BaseModel):
    """What Fish Audio's POST /v1/tts lets you tune (docs.fish.audio)."""

    latency: Literal["low", "normal", "balanced"] = "balanced"
    speed: float = Field(1.0, ge=0.5, le=2.0)
    temperature: float = Field(0.7, ge=0.0, le=1.0)
    top_p: float = Field(0.7, ge=0.0, le=1.0)
    normalize: bool = True
    cues: bool = True  # [bracket] delivery cues on S2 models ("[excited] That's what I'm talking about!")


class VoiceProfile(BaseModel):
    """The active voice. Changing it affects the next line spoken; no restart."""

    provider: Literal["system", "fish_audio", "none"] = "fish_audio"
    reference_id: str = ""
    model: str = "s2.1-pro-free"
    fish: FishVoiceSettings = Field(default_factory=FishVoiceSettings)
    system_voice: str | None = None
    system_rate_wpm: int = Field(190, ge=90, le=360)
    volume: float = Field(1.0, ge=0.0, le=1.5)
    # The offline voice for anything without a cloud clip (generated replies included):
    # "kokoro" = local neural voice (not the Fish Audio voice; labelled), "system" = macOS voice.
    local_voice: Literal["kokoro", "system"] = "kokoro"
    # af_bella: the Kokoro preset closest to the Fish Audio clips by measured pitch and pace.
    kokoro_voice: str = Field("af_bella", pattern=r"^[a-z]{2}_[a-z]+$")
    kokoro_speed: float = Field(1.0, ge=0.7, le=1.4)
    # cache_only: never call the API while speaking (safe on the robot's offline Wi-Fi).
    # cache_then_live: generate uncached lines live, within the daily byte budget.
    cache_behavior: Literal["cache_only", "cache_then_live"] = "cache_only"
    daily_live_bytes: int = Field(5000, ge=0, le=1_000_000)

    @field_validator("reference_id", mode="before")
    @classmethod
    def _voice_id(cls, v):
        from .voice.fish_audio import normalize_reference_id

        return normalize_reference_id(v)

    @field_validator("model")
    @classmethod
    def _model(cls, v):
        from .voice.fish_audio import MODEL_IDS

        if v not in MODEL_IDS:
            raise ValueError(f"unknown Fish Audio model {v!r}; documented: {', '.join(sorted(MODEL_IDS))}")
        return v


class VoicePreset(BaseModel):
    id: str = Field(..., pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")
    name: str = Field(..., min_length=1, max_length=40)
    profile: VoiceProfile = Field(default_factory=VoiceProfile)
    personality: Literal["chill", "roast", "unhinged", "judge_demo"] = "roast"
    profanity: Literal["off", "mild", "strong"] = "off"
    output_device: str | None = None  # None keeps the current device
    builtin: bool = False

    @field_validator("personality", mode="before")
    @classmethod
    def _legacy_personality(cls, v):
        return {"coach": "roast", "chaos": "roast"}.get(v, v)


# The E-girl voice id was chosen by the user (fish.audio/m/ca3007f96ae7499ab87d27ea3599956a,
# a public library voice by twangodev, not a clone of a named person). The other presets
# leave the voice empty for the user to paste their own.
EGIRL_VOICE_ID = "ca3007f96ae7499ab87d27ea3599956a"


def builtin_presets() -> list[VoicePreset]:
    return [
        VoicePreset(id="egirl", name="E-girl (Fish Audio)", builtin=True, profile=VoiceProfile(reference_id=EGIRL_VOICE_ID)),
        VoicePreset(id="original", name="LOCKED IN Original", builtin=True, profile=VoiceProfile(provider="system")),
        VoicePreset(id="sarcastic-bro", name="Sarcastic Bro", builtin=True, profile=VoiceProfile(fish=FishVoiceSettings(temperature=0.8))),
        VoicePreset(
            id="chaotic-coach",
            name="Chaotic Coach",
            builtin=True,
            personality="unhinged",
            profile=VoiceProfile(fish=FishVoiceSettings(speed=1.1, temperature=0.9, top_p=0.8)),
        ),
        VoicePreset(
            id="calm-study-buddy",
            name="Calm Study Buddy",
            builtin=True,
            personality="chill",
            profile=VoiceProfile(fish=FishVoiceSettings(speed=0.95, temperature=0.6)),
        ),
    ]


class RuntimeSettings(BaseModel):
    camera_source: str = "0"
    # Where the camera sits. auto: the Zeus ESP32-CAM rides on the robot, anything else is fixed.
    camera_mount: Literal["auto", "fixed", "robot"] = "auto"
    # The ESP32-CAM module can be mounted rotated; applied to the Zeus stream only.
    zeus_camera_flip: Literal["none", "horizontal", "vertical", "both"] = "none"
    # Reach zone for a robot-mounted camera, in that camera's image: the space just in front of
    # the robot where a reaching hand appears. A starting point only; calibrate it on the robot
    # (docs/REAL_HARDWARE_TESTS.md). The phone itself is never in this camera's view.
    robot_camera_zone: PhoneZone = Field(
        default_factory=lambda: PhoneZone(points=[(0.2, 0.45), (0.8, 0.45), (0.8, 1.0), (0.2, 1.0)])
    )
    vision_enabled: bool = False
    show_video: bool = True
    phone_zone: PhoneZone = Field(default_factory=PhoneZone)
    vision: VisionTuning = Field(default_factory=VisionTuning)
    motion: MotionTuning = Field(default_factory=MotionTuning)
    voice: VoiceTuning = Field(default_factory=VoiceTuning)
    coach: CoachTuning = Field(default_factory=CoachTuning)
    conversation: ConversationTuning = Field(default_factory=ConversationTuning)
    voice_profile: VoiceProfile = Field(default_factory=lambda: VoiceProfile(reference_id=EGIRL_VOICE_ID))
    presets: list[VoicePreset] = Field(default_factory=list)  # the user's own; built-ins live in code
    active_preset: str | None = "egirl"
    robot_host: str | None = None
    default_session_minutes: int = Field(25, ge=1, le=240)


class RuntimeSettingsStore:
    """Load/save ``RuntimeSettings`` atomically. A corrupt file falls back to defaults."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> RuntimeSettings:
        if not self.path.exists():
            return RuntimeSettings()
        try:
            return RuntimeSettings.model_validate_json(self.path.read_text())
        except Exception as exc:  # corrupt or from an older schema
            log.warning("runtime settings at %s unreadable (%s); using defaults", self.path, exc)
            return RuntimeSettings()

    def save(self, settings: RuntimeSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(settings.model_dump(mode="json"), indent=2)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".settings-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise


def merge_settings(current: RuntimeSettings, patch: dict) -> RuntimeSettings:
    """Deep-merge a partial dict from the UI onto the current settings and re-validate."""

    def deep(a: dict, b: dict) -> dict:
        out = dict(a)
        for k, v in b.items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = deep(out[k], v)
            else:
                out[k] = v
        return out

    return RuntimeSettings.model_validate(deep(current.model_dump(mode="json"), patch))
