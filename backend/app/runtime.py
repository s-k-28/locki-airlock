"""LOCKED IN runtime: wires perception, policy, safety, robot, voice and sessions.

Threading model: everything here runs on the asyncio loop. The camera and the
microphone run in their own threads and hand events over with
``loop.call_soon_threadsafe``; robot transports are asyncio-native. Because all
state is mutated on one thread there are no locks around the state machine.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import re
import statistics
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any

from . import __version__
from .config import RuntimeSettings, RuntimeSettingsStore, Settings, merge_settings
from .intelligence.adaptation import AdaptationEngine
from .intelligence.companion import Companion
from .intelligence.expression import ExpressionController
from .intelligence.policy import InterventionPolicy, PolicyContext
from .intelligence.state_machine import MachineContext, State, StateMachine, Trigger
from .robot.base import LinkCallbacks, RobotTelemetry, RobotTransport
from .robot.commands import (
    INVERSE,
    TRANSLATIONS,
    ApprovedMotion,
    MotionOrigin,
    MotionPrimitive,
    MotionProfileName,
    MotionRequest,
)
from .robot.mock import MockRobotTransport
from .safety.controller import SafetyConfig, SafetyController
from .safety.watchdog import MotionWatchdog
from .sessions.manager import SessionError, SessionManager
from .sessions.metrics import FocusAccumulator, FocusState, SessionCounters, compute_metrics
from .telemetry.events import EventBus, EventSource, EventType
from .telemetry.storage import TelemetryStore
from .voice.audio_cache import AudioCache
from .voice.conversation import ResponseComposer, SessionFacts
from .voice.fish_audio import FishAudioVoice
from .voice.intents import IntentClassifier, IntentName
from .voice.kokoro_tts import KokoroTTS
from .voice.personality import MODE_LABELS, SUPPORTIVE_S, Style
from .voice.trials import VoiceTrials
from .voice.tts import (
    PRIORITY_AMBIENT,
    PRIORITY_REPLY,
    AudioOut,
    CachedSystemTTS,
    NullTTS,
    Speaker,
    SystemTTS,
    TTSEngine,
    VoiceRouter,
    system_output_volume,
)
from .voice.usage import UsageLedger

log = logging.getLogger(__name__)

_ACTION_EVENTS = frozenset(
    {
        EventType.ROBOT_MOTION_REQUESTED,
        EventType.ROBOT_MOTION_BLOCKED,
        EventType.ROBOT_MOTION_SENT,
        EventType.ROBOT_MOTION_ACKNOWLEDGED,
        EventType.ROBOT_MOTION_COMPLETED,
        EventType.ROBOT_MOTION_ABORTED,
        EventType.ROBOT_STOP_SENT,
        EventType.SAFETY_EMERGENCY_STOP,
    }
)

# The voice turn being handled in this task, so every line it causes is linked to it.
_TURN: contextvars.ContextVar[dict | None] = contextvars.ContextVar("voice_turn", default=None)
STOP_SUPPRESS_S = 15.0  # after a spoken "stop", no new motion of any kind for this long
REACH_WARNING_COOLDOWN_S = 20.0  # "I see that hand." at most this often
FOCUS_STREAK_S = 600.0  # praise after this long focused without a reach

MANUAL_OK_STATES = frozenset(
    {State.IDLE, State.SESSION_ACTIVE, State.POSSIBLE_PHONE_REACH, State.HOLDING, State.PAUSED, State.RELEASED}
)


@dataclass
class CommandResult:
    ok: bool
    message: str
    state: str = ""
    reply: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "message": self.message, "state": self.state, "reply": self.reply, "data": self.data}


@dataclass
class VisionEvent:
    """Handed from the vision thread to the loop."""

    kind: str
    data: dict = field(default_factory=dict)
    confidence: float | None = None
    frame_mono: float | None = None
    simulated: bool = False


class PerfStats:
    """Rolling latency samples (milliseconds) for the dashboard and docs/PERFORMANCE.md."""

    def __init__(self, size: int = 200):
        self.samples: dict[str, deque[float]] = {}
        self.size = size

    def add(self, name: str, value_ms: float) -> None:
        self.samples.setdefault(name, deque(maxlen=self.size)).append(float(value_ms))

    def summary(self) -> dict[str, dict]:
        out = {}
        for name, values in self.samples.items():
            vals = sorted(values)
            if not vals:
                continue
            out[name] = {
                "n": len(vals),
                "mean": round(statistics.fmean(vals), 2),
                "p50": round(vals[len(vals) // 2], 2),
                "p95": round(vals[min(len(vals) - 1, int(len(vals) * 0.95))], 2),
                "max": round(vals[-1], 2),
                "last": round(values[-1], 2),
            }
        return out


class LockedInRuntime:
    def __init__(
        self,
        settings: Settings,
        *,
        runtime_settings: RuntimeSettings | None = None,
        persist_settings: bool = True,
        tts_engine: TTSEngine | None = None,
        enable_vision: bool = True,
        enable_voice: bool = True,
        enable_llm: bool = True,
        mock_kwargs: dict | None = None,
    ):
        self.settings = settings
        self.enable_llm = enable_llm
        self.started_at = time.time()
        self.run_id = uuid.uuid4().hex[:12]
        self._settings_store = RuntimeSettingsStore(settings.data_dir / "runtime_settings.json") if persist_settings else None
        if runtime_settings is not None:
            self.rs = runtime_settings
        elif self._settings_store is not None:
            self.rs = self._settings_store.load()
        else:
            self.rs = RuntimeSettings()

        if settings.audio_output:  # make hardware SPEAKER="...": startup output device
            self.rs.voice.output_device = settings.audio_output

        self.store = TelemetryStore(settings.data_dir)
        self.bus = EventBus(sink=self.store.write_event)
        self.sm = StateMachine()
        self.sessions = SessionManager()
        self.bus.set_session_id_provider(lambda: self.sessions.current.session_id if self.sessions.current else None)
        self.focus = FocusAccumulator()
        self.counters = SessionCounters()
        self.policy = InterventionPolicy()
        self.adaptation = AdaptationEngine(self.bus)
        self.adaptation.on_change = lambda params: self._apply_vision_tuning()
        self.classifier = IntentClassifier()
        self.composer = ResponseComposer()
        self.perf = PerfStats()

        self.driver = settings.robot_driver
        self.transport: RobotTransport | None = None
        self.robot_telemetry = RobotTelemetry()
        self._mock_kwargs = mock_kwargs or {}
        # Zeus link supervisor: keeps (re)connecting while the Zeus profile is selected.
        self._zeus_wanted = False
        self._zeus_task: asyncio.Task | None = None
        self.robot_link: dict[str, Any] = {"state": "idle", "attempt": 0, "target": None, "last_failure": None, "next_retry_at": None}
        self._last_failure_published: tuple[str, float] | None = None
        # Startup camera choice from the environment (make hardware); the dashboard's own
        # choice wins once the operator picks a source.
        self._camera_override = settings.camera_source
        # Voice turns: heard -> recognised -> intent -> action -> spoken reply, with timings.
        self.voice_turns: deque[dict] = deque(maxlen=12)
        self._turn_index: dict[str, dict] = {}
        # Robot actions as the dashboard shows them: requested -> denied / sent -> ended, each
        # labelled physical or simulated, never claiming more than the host can know.
        self.actions: deque[dict] = deque(maxlen=20)
        self._action_index: dict[str, dict] = {}
        self._last_reach_mono: float | None = None
        self._session_started_mono: float | None = None
        self._last_streak_mono: float | None = None
        self._output_probe: tuple[float, list[str] | None, str | None] | None = None

        self.real_motion_mode = self._real_motion_mode()
        self.safety = SafetyController(
            bus=self.bus,
            get_transport=lambda: self.transport,
            config=SafetyConfig(autonomous_cooldown_s=self.rs.motion.autonomous_cooldown_s, **self._real_motion_config()),
            default_speed=self._default_speed,
            default_duration=self._default_duration,
        )
        self.safety.on_motion_finished = self._on_motion_finished
        self.safety.perception_check = self._perception_check
        self.watchdog = MotionWatchdog(self.safety, self.bus)

        self.fish = FishAudioVoice(
            settings.fish_audio_api_key,
            AudioCache(settings.data_dir / "tts_cache" / "fish_audio"),
            UsageLedger(settings.data_dir / "tts_usage.json"),
            base_url=settings.fish_audio_base_url,
            offline=settings.offline,
        )
        self.kokoro: KokoroTTS | None = None  # the local neural voice (_make_tts)
        engine = tts_engine or self._make_tts()
        self.speaker = Speaker(engine, self.bus, fallback=NullTTS(), voice_label=self._voice_label)
        self.speaker.muted = self.rs.voice.muted
        # Milestone 5: context, behaviour decisions, the local language model, typed actions.
        self.companion = Companion(self)
        self.context = self.companion.context
        self.behavior = self.companion.behavior
        self.expression = ExpressionController(self.bus, led_control=self._led_control)
        self._supportive_until = 0.0
        self._beat_rng = __import__("random").Random()
        self._robot_lost_in_session = False
        self._cache_plans: dict[str, dict] = {}
        self._cache_job = None
        self.cache_job_state: dict | None = None

        self.enable_vision = enable_vision
        self.enable_voice = enable_voice
        self.vision = None  # VisionService, created on demand
        self.voice = None  # VoiceService, created on demand
        self.llm = None
        self.vision_status: dict[str, Any] = {"running": False, "camera": "offline", "reason": "vision not started"}

        # Dead-reckoning log of executed motions since "home" (for the controlled return).
        self.displacement: list[dict] = []
        self.last_decision: dict | None = None
        self.last_intervention_at: float | None = None
        self.last_utterance: dict | None = None
        self.last_reply: dict | None = None
        self.milestones_done: set[str] = set()
        self._sim_perception_used = False
        self._focus_since = time.monotonic()
        self._last_focus_eval = 0.0
        self._last_render_ahead = 0.0
        self._os_volume: dict | None = None  # macOS volume/mute of the default output device
        self._last_volume_check = 0.0
        self._volume_task: asyncio.Task | None = None
        self._render_ahead_task: asyncio.Task | None = None
        self._kokoro_task: asyncio.Task | None = None
        self._follow_up_until = 0.0
        self._latency_marks: dict[str, float] = {}
        self._return_task: asyncio.Task | None = None
        self._tick_task: asyncio.Task | None = None
        self._demo_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.trials: list[dict] = []
        self.active_trial: dict | None = None
        self.link_tests: deque[dict] = deque(maxlen=20)
        self._link_test_task: asyncio.Task | None = None
        self.voice_trials = VoiceTrials(settings.data_dir / "experiments" / "voice_trials.jsonl")
        self.bus.add_listener(self._on_bus_event)
        self._apply_vision_tuning()

    # ================================================================ lifecycle
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.bus.bind_loop(self._loop)
        self.speaker.start()
        self.watchdog.start()
        self._tick_task = asyncio.create_task(self._tick_loop(), name="runtime-tick")
        self.bus.publish(
            EventType.SYSTEM_STARTED,
            EventSource.SYSTEM,
            {"version": __version__, "driver": self.driver, "telemetry_file": str(self.store.path)},
        )
        self._setup_llm()
        self.companion.start()
        if self.kokoro is not None:
            self._kokoro_task = asyncio.create_task(self._load_kokoro(), name="kokoro-load")
        self._schedule_prewarm()
        if self.driver == "mock" and self.settings.mock_autoconnect:
            await self.connect_robot("mock")
        elif self.driver == "zeus" and self.settings.zeus_autoconnect:
            self._start_zeus_supervisor()  # never arms; the dashboard shows progress
        if self.enable_vision and self.settings.vision_preload:
            await self._preload_vision()
        if self.enable_vision and (self.settings.vision_autostart or self.rs.vision_enabled):
            await self.start_vision()
        if self.enable_voice and self.settings.voice_autostart:
            await self.start_voice()

    async def shutdown(self) -> None:
        self.bus.publish(EventType.SYSTEM_SHUTDOWN, EventSource.SYSTEM, {})
        await self._stop_zeus_supervisor()
        system = self._local_tts()
        if system is not None:
            system.prewarm_cancelled = True
        if self.kokoro is not None:
            self.kokoro.prewarm_cancelled = True
        try:
            self.companion.close()
        except Exception:
            log.exception("closing the language model failed")
        try:
            if self.transport is not None and self.transport.connected:
                await self.safety.stop("backend shutting down", source="system")
        except Exception:
            log.exception("stop on shutdown failed")
        for task in (self._tick_task, self._return_task, self._demo_task, self._link_test_task):
            if task:
                task.cancel()
        await self.watchdog.stop()
        if self.vision is not None:
            await asyncio.to_thread(self.vision.stop)
            try:
                await asyncio.wait_for(asyncio.to_thread(self.vision.close_models), timeout=3.0)
            except Exception:
                log.warning("closing the vision models timed out")
        if self.voice is not None:
            await asyncio.to_thread(self.voice.shutdown)
        try:
            if self.transport is not None:
                await self.transport.disconnect()
        except Exception:
            log.exception("disconnect on shutdown failed")
        await self.speaker.close()
        if self.sessions.active:
            try:
                self._finish_session("backend_shutdown", speak=False)
            except Exception:
                log.exception("saving session on shutdown failed")
        self.store.close()

    def _make_tts(self) -> TTSEngine:
        s = self.settings
        if s.tts_engine == "none":
            return NullTTS()
        p = self.rs.voice_profile
        system: TTSEngine = CachedSystemTTS(
            s.data_dir / "tts_cache", voice=p.system_voice or s.tts_voice, rate_wpm=p.system_rate_wpm, output_device=self.rs.voice.output_device
        )
        if not system.available():
            system = NullTTS()
        self.kokoro = KokoroTTS(s.models_dir, s.data_dir / "tts_cache" / "kokoro", voice=p.kokoro_voice, speed=p.kokoro_speed, model_file=s.kokoro_model)
        return VoiceRouter(
            profile=lambda: self.rs.voice_profile,
            system=system,
            cloud=self.fish,
            audio=AudioOut(),
            output_device=lambda: self.rs.voice.output_device,
            neural=self.kokoro,
        )

    async def _load_kokoro(self) -> None:
        """Load the local neural voice, then pre-render the fixed lines that have no cloud
        clip, so even those play in ~10 ms offline."""
        k = self.kokoro
        if k is None:
            return
        if not await asyncio.to_thread(k.load):
            self._schedule_prewarm()  # Kokoro failed: pre-render with the macOS voice instead
            return
        await asyncio.to_thread(k.prewarm, self._kokoro_lines())

    def _kokoro_lines(self) -> list[str]:
        """Fixed lines for the current personality that have no cloud clip, most needed first."""
        from .voice.personality import Style as _Style
        from .voice.personality import cacheable_lines

        cloud = self._cached_line_check()
        lines = cacheable_lines([self.style(), _Style(tone="serious")])
        uncached = {text for _c, text, cue in lines if cloud is None or not cloud(text, cue)}
        return [t for t in self._prewarm_order(lines) if t in uncached]

    def _cached_line_check(self):
        """With the cloud voice selected, prefer lines already cached in it (instant, and the
        robot never switches to the local voice mid-demo)."""
        p = self.rs.voice_profile
        if p.provider != "fish_audio" or not p.reference_id or not self.fish.cache.index:
            return None
        from .voice.tts import SpeechRequest

        def cached(text: str, cue: str | None) -> bool:
            return self.fish.cache.has(self.fish.cache_key(self.fish.text_for(SpeechRequest(text, cue), p), p))

        return cached

    def _voice_label(self) -> str | None:
        p = self.rs.voice_profile
        if p.provider == "fish_audio":
            title = (self.fish.status.get("voice") or {}).get("title")
            return f"Fish Audio · {title or p.reference_id[:8]} · {p.model}"
        return {"system": "Local voice", "none": "Text only"}.get(p.provider)

    def _led_control(self) -> str:
        t = self.transport
        return "simulated" if t is not None and t.connected and t.simulated else "none"

    def style(self) -> Style:
        c = self.rs.coach
        return Style(
            mode=c.personality,
            profanity=c.profanity,
            tone=c.tone,
            unhinged_opt_in=c.unhinged_opt_in,
            supportive=time.monotonic() < self._supportive_until,
        )

    def _local_tts(self) -> CachedSystemTTS | None:
        router = self.speaker.engine if isinstance(self.speaker.engine, VoiceRouter) else None
        system = router.system if router else self.speaker.engine
        return system if isinstance(system, CachedSystemTTS) else None

    def _prewarm_order(self, lines: list[tuple[str, str, str | None]]) -> list[str]:
        """Lines the local voice will really speak come first (no cloud clip cached for them),
        then replies before ambient reactions: a full prewarm takes a couple of minutes."""
        from .voice.personality import INTENTS

        cloud = self._cached_line_check()
        rank: dict[str, tuple[int, int]] = {}
        for category, text, cue in lines:
            intent = INTENTS.get(category)
            key = (int(cloud is not None and cloud(text, cue)), intent.priority if intent else 9)
            rank[text] = min(key, rank.get(text, key))
        return sorted(rank, key=lambda t: (rank[t], t))

    def _schedule_prewarm(self) -> None:
        """Pre-render fixed lines with the local voice and preload cached cloud clips into
        memory, for the current personality, so reactions start in ~10 ms."""
        from .voice.personality import Style as _Style
        from .voice.personality import cacheable_lines

        router = self.speaker.engine if isinstance(self.speaker.engine, VoiceRouter) else None
        style = self.style()
        lines = cacheable_lines([style, _Style(tone="serious")])
        system = self._local_tts()
        kokoro_first = self.kokoro is not None and self.kokoro.state in ("not loaded", "ready") and self.rs.voice_profile.local_voice == "kokoro"
        if system is not None and not kokoro_first:  # with Kokoro, the macOS voice is only the last resort
            texts = self._prewarm_order(lines)
            self._prewarm_task = asyncio.create_task(asyncio.to_thread(system.prewarm, texts), name="tts-prewarm")
        p = self.rs.voice_profile
        if router is not None and p.provider == "fish_audio" and p.reference_id:
            from .voice.tts import SpeechRequest

            keys = [self.fish.cache_key(self.fish.text_for(SpeechRequest(text, cue), p), p) for _c, text, cue in lines]
            asyncio.create_task(asyncio.to_thread(self.fish.cache.preload, keys), name="fish-preload")

    def _setup_llm(self) -> None:
        """The optional cloud replier (provider "anthropic"). The local model is set up by the
        companion (provider "local", intelligence/companion.py)."""
        if self.settings.llm_provider != "anthropic":
            return
        if self.settings.offline:
            log.info("offline mode: the %s LLM is not used (template replies only)", self.settings.llm_provider)
            return
        try:
            from .voice.llm import make_llm

            self.llm = make_llm(self.settings)
        except Exception as exc:
            log.warning("LLM disabled: %s", exc)
            self.llm = None

    # ================================================================ helpers
    def _ctx(self, **override) -> MachineContext:
        base = {
            "robot_connected": bool(self.transport and self.transport.connected),
            "session_active": self.sessions.active,
            "session_paused": self.sessions.paused,
            "robot_displaced": bool(self.displacement),
        }
        base.update(override)
        return MachineContext(**base)

    def _fire(self, trigger: Trigger, reason: str = "", **ctx_override):
        before = self.sm.state
        result = self.sm.fire(trigger, self._ctx(**ctx_override), reason)
        if not result.accepted:
            self.bus.publish(
                EventType.STATE_TRANSITION_REJECTED,
                EventSource.SYSTEM,
                {"trigger": trigger.value, "state": before.value, "reason": result.reason},
            )
        elif result.changed:
            self.bus.publish(
                EventType.STATE_CHANGED,
                EventSource.SYSTEM,
                {"from": before.value, "to": result.to_state.value, "trigger": trigger.value, "reason": reason},
            )
        return result

    def _default_speed(self, origin: MotionOrigin) -> int:
        return self.rs.motion.retreat_speed if origin == MotionOrigin.POLICY else self.rs.motion.manual_speed

    def _default_duration(self, origin: MotionOrigin) -> float:
        return self.rs.motion.retreat_duration_s if origin == MotionOrigin.POLICY else self.rs.motion.manual_duration_s

    def facts(self) -> SessionFacts:
        s = self.sessions.current
        # Reaches are reportable when something was actually watching: the live camera,
        # or (Mode C) the simulator, whose events are labelled SIMULATED in the UI.
        vision_live = (self.vision_live and self.zone_known) or self._sim_perception_used
        note = "marker_lost" if (self.vision_live and not self.zone_known) else "camera_off"
        if s is None:
            return SessionFacts(vision_available=vision_live, vision_note=note)
        m = self.metrics()
        return SessionFacts(
            session_active=True,
            paused=s.paused,
            task=s.task,
            elapsed_s=self.sessions.active_seconds(),
            remaining_s=self.sessions.remaining_seconds(),
            planned_s=s.planned_duration_s,
            completed_units=s.completed_units,
            target_units=s.target_units,
            unit_label=s.unit_label,
            reaches_confirmed=self.counters.reach_confirmed,
            vision_available=vision_live,
            vision_note=note,
            robot_moves=self.counters.robot_moves,
            presence_ratio=m.get("presence_ratio"),
        )

    @property
    def vision_live(self) -> bool:
        return bool(self.vision_status.get("running") and self.vision_status.get("camera") == "live")

    @property
    def zone_known(self) -> bool:
        """False when the zone follows the robot and its marker is currently lost."""
        if self.rs.phone_zone.mode != "marker":
            return True
        return (self.vision_status.get("tracking") or {}).get("state") in ("TRACKING", "HELD")

    def metrics(self) -> dict:
        s = self.sessions.current
        if s is None:
            return {}
        return compute_metrics(
            elapsed_active_s=self.sessions.active_seconds(),
            remaining_s=self.sessions.remaining_seconds(),
            planned_s=s.planned_duration_s,
            completed_units=s.completed_units,
            target_units=s.target_units,
            focus=self.focus.snapshot(),
            counters=self.counters,
            vision_available=self.vision_live,
        )

    def _policy_ctx(self) -> PolicyContext:
        s = self.sessions.current
        since = None if self.last_intervention_at is None else time.monotonic() - self.last_intervention_at
        return PolicyContext(
            state=self.sm.state,
            session_active=self.sessions.active,
            session_paused=self.sessions.paused,
            elapsed_active_s=self.sessions.active_seconds(),
            remaining_s=self.sessions.remaining_seconds(),
            planned_s=s.planned_duration_s if s else 0.0,
            focus_state=self.focus.state,
            robot_connected=bool(self.transport and self.transport.connected),
            robot_can_move=self.safety.armed and self.safety.autonomy_enabled and not self.safety.estop_latched,
            robot_displaced=bool(self.displacement),
            reaches_confirmed=self.counters.reach_confirmed,
            seconds_since_intervention=since,
            base_cooldown_s=self.rs.coach.min_seconds_between_interventions,
            cooldown_multiplier=self.adaptation.params.cooldown_multiplier,
            muted=self.speaker.muted,
            autonomy_suspended=self.adaptation.params.autonomy_suspended,
            intensity=self.rs.coach.intensity,
            retreat_primitive=self.rs.motion.retreat_primitive,
            retreat_speed=self.rs.motion.retreat_speed,
            retreat_duration_s=self.rs.motion.retreat_duration_s,
        )

    def _say(
        self, category: str, *, priority: int | None = None, intervention: bool = False, force: bool = False, beat: bool = False, trace: dict | None = None, **extra
    ) -> str:
        r = self.composer.render(category, self.facts(), self.style(), prefer=self._cached_line_check(), **extra)
        text = r.text
        prio = r.priority if priority is None else priority
        delay = self._beat_rng.uniform(0.25, 0.7) if beat else 0.0  # react like a person, not a buzzer
        turn = _TURN.get()
        if trace is None and turn is not None:
            trace = {"kind": "turn", "id": turn["id"], "t0": turn["heard_mono"]}
            turn["replies"].append({"category": category, "text": text, "cue": r.cue})
        audible = self.speaker.say(text, priority=prio, category=category, force=force, cue=r.cue, cacheable=r.cacheable, delay_s=delay, trace=trace)
        self.expression.for_category(category, simulated=bool(self.transport and self.transport.simulated))
        self.last_reply = {"text": text, "category": category, "at": time.time(), "audible": audible, "priority": prio}
        payload = {"category": category, "text": text, "audible": audible, "intent": r.intent, "priority": f"P{prio}", "pool": r.pool, "profanity": r.profanity, "insult": r.insult}
        if intervention:
            self.counters.interventions += 1
            self.last_intervention_at = time.monotonic()
            self.bus.publish(EventType.COACH_INTERVENTION_GENERATED, EventSource.POLICY, payload)
        else:
            self.bus.publish(EventType.VOICE_RESPONSE_GENERATED, EventSource.VOICE, payload)
        self._follow_up_until = time.monotonic() + 8.0
        return text

    def _result(self, ok: bool, message: str, reply: str | None = None, **data) -> CommandResult:
        return CommandResult(ok, message, self.sm.state.value, reply, data)

    # ================================================================ sessions
    async def start_session(
        self,
        task: str | None = None,
        duration_min: float | None = None,
        target_units: int | None = None,
        unit_label: str | None = None,
        origin: str = "operator",
        speak: bool = True,
    ) -> CommandResult:
        if self.sessions.active:
            reply = self._say("already_active") if speak else None
            return self._result(False, "a session is already active", reply)
        minutes = duration_min if duration_min is not None else self.rs.default_session_minutes
        check = self.sm.resolve(Trigger.START_SESSION, self._ctx(session_active=True, session_paused=False))
        if not check.accepted:
            return self._result(False, f"cannot start a session in state {self.sm.state}: {check.reason}")
        try:
            s = self.sessions.start(task or "Focus session", minutes * 60, target_units, unit_label or "tasks")
        except SessionError as exc:
            return self._result(False, str(exc))
        self.counters = SessionCounters()
        self.focus.reset()
        self.focus.set_counting(True)
        self.adaptation.reset_for_session()
        self.milestones_done.clear()
        self.composer.engine.new_session()
        self._robot_lost_in_session = False
        self._sim_perception_used = False
        self.last_intervention_at = None
        self._session_started_mono = time.monotonic()
        self._last_reach_mono = self._last_streak_mono = None
        self._fire(Trigger.START_SESSION, f"session started by {origin}")
        self.bus.publish(
            EventType.SESSION_STARTED,
            EventSource.SESSION,
            {"task": s.task, "planned_s": s.planned_duration_s, "target_units": s.target_units, "unit_label": s.unit_label, "origin": origin},
        )
        reply = self._say("session_start") if speak else None
        return self._result(True, f"session started: {s.task} for {minutes:g} min", reply, session=self.sessions.snapshot())

    async def pause_session(self, origin: str = "operator", speak: bool = True) -> CommandResult:
        if not self.sessions.active:
            reply = self._say("no_session") if speak else None
            return self._result(False, "no active session", reply)
        if self.sessions.paused:
            return self._result(True, "already paused")
        if self.safety.active is not None:
            await self.safety.stop("session paused", source=origin)
        self.sessions.pause()
        self.focus.set_counting(False)
        self.counters.pauses += 1
        self._fire(Trigger.PAUSE, f"paused by {origin}")
        self.bus.publish(EventType.SESSION_PAUSED, EventSource.SESSION, {"origin": origin, "remaining_s": round(self.sessions.remaining_seconds(), 1)})
        reply = self._say("pause") if speak else None
        return self._result(True, "session paused", reply)

    async def resume_session(self, origin: str = "operator", speak: bool = True) -> CommandResult:
        if not self.sessions.active:
            reply = self._say("no_session") if speak else None
            return self._result(False, "no active session", reply)
        if not self.sessions.paused:
            reply = self._say("not_paused") if speak else None
            return self._result(False, "session is not paused", reply)
        self.sessions.resume()
        self.focus.set_counting(True)
        self._fire(Trigger.RESUME, f"resumed by {origin}")
        self.bus.publish(EventType.SESSION_RESUMED, EventSource.SESSION, {"origin": origin})
        reply = self._say("resume") if speak else None
        return self._result(True, "session resumed", reply)

    async def end_session(self, reason: str = "user", origin: str = "operator", speak: bool = True) -> CommandResult:
        if not self.sessions.active:
            reply = self._say("no_session") if speak else None
            return self._result(False, "no active session", reply)
        if self.safety.active is not None:
            await self.safety.stop("session ended", source=origin)
        category = "end_completed" if reason == "completed" else "end_early"
        reply = self._say(category, priority=PRIORITY_REPLY) if speak else None
        summary = self._finish_session(reason, speak=speak)
        self._fire(Trigger.END_SESSION, f"session ended ({reason})")
        return self._result(True, f"session ended ({reason})", reply, summary=summary)

    def _finish_session(self, reason: str, speak: bool) -> dict:
        metrics = self.metrics()
        s = self.sessions.end(reason)
        self.focus.set_counting(False)
        summary = {
            "session_id": s.session_id,
            "task": s.task,
            "started_at": s.started_at_wall,
            "ended_at": s.ended_at_wall,
            "end_reason": reason,
            "planned_s": s.planned_duration_s,
            "target_units": s.target_units,
            "unit_label": s.unit_label,
            "progress_log": s.progress_log,
            "metrics": metrics,
            "focus_transitions": self.focus.transitions[-200:],
            "adaptation": self.adaptation.snapshot(),
            "robot": {"driver": self.driver, "simulated": bool(self.transport and self.transport.simulated)},
        }
        self.store.save_session_summary(s.session_id, summary)
        self.bus.publish(EventType.SESSION_ENDED, EventSource.SESSION, {"reason": reason, "metrics": metrics}, session_id=s.session_id)
        return summary

    async def add_progress(
        self, units: int = 1, origin: str = "operator", note: str | None = None, speak: bool = True, unit_label: str | None = None
    ) -> CommandResult:
        if not self.sessions.active:
            reply = self._say("no_session") if speak else None
            return self._result(False, "no active session", reply)
        if unit_label:
            self.sessions.adopt_unit_label(unit_label)
        before = self.sessions.current.completed_units if self.sessions.current else 0
        try:
            s = self.sessions.add_progress(units, note)
        except SessionError as exc:
            return self._result(False, str(exc))
        hit_target = bool(s.target_units) and before < s.target_units <= s.completed_units
        if hit_target:
            self.bus.publish(EventType.SESSION_MILESTONE, EventSource.SESSION, {"milestone": "target_reached", "target": s.target_units})
        self.bus.publish(
            EventType.SESSION_PROGRESS,
            EventSource.SESSION,
            {"units": units, "total": s.completed_units, "target": s.target_units, "unit_label": s.unit_label, "origin": origin},
        )
        reply = self._say("target_reached" if hit_target else "progress_ack") if speak else None
        return self._result(True, f"progress: {s.completed_units} {s.unit_label}", reply)

    async def release(self, origin: str = "operator", speak: bool = True) -> CommandResult:
        """User wants the phone: stop immediately, end the session, enter RELEASED."""
        t0 = time.monotonic()
        await self.safety.stop("phone release requested", source=origin)
        self.perf.add("release_stop_ms", (time.monotonic() - t0) * 1000)
        if self._return_task is not None:
            self._return_task.cancel()
        self.speaker.interrupt("release")
        reply = self._say("release") if speak else None
        if self.sessions.active:
            self._finish_session("released", speak=False)
        self._fire(Trigger.RELEASE, f"release requested by {origin}")
        return self._result(True, "released: robot stopped, session ended, phone is yours", reply)

    async def emergency_stop(self, origin: str = "operator", reason: str = "operator e-stop", speak: bool = True) -> CommandResult:
        t0 = time.monotonic()
        await self.safety.emergency_stop(reason, source=origin)
        self.perf.add("estop_ms", (time.monotonic() - t0) * 1000)
        if self._return_task is not None:
            self._return_task.cancel()
        self.speaker.stop_all("emergency stop")
        self._fire(Trigger.EMERGENCY_STOP, reason)
        self.expression.show("estop", simulated=bool(self.transport and self.transport.simulated))
        reply = self._say("estop_ack", force=True) if speak else None
        hw = self.hardware_stop_state()
        return self._result(True, f"EMERGENCY STOP latched; robot disarmed. {hw['summary']}", reply, hardware_stop=hw)

    def hardware_stop_state(self) -> dict:
        """What a stop request did on the robot, never more than the host can know."""
        t = self.transport
        if t is None or not t.connected:
            return {"kind": "no_robot", "summary": "No robot connected: software stop only."}
        if t.simulated:
            return {"kind": "simulated", "summary": "Simulated robot stopped."}
        caps = t.get_status().capabilities
        if caps.link_mode == "monitor":
            return {
                "kind": "software_only",
                "summary": "Software stop only: this monitor link sends no motor commands, so no stop frame went to the robot. "
                "LOCKED IN has not commanded the motors; the robot's power switch is the hardware stop.",
            }
        return {
            "kind": "stop_frame_sent",
            "summary": "Stop frames sent to the robot. Whether the wheels stopped is not measured (no wheel feedback).",
        }

    async def reset(self, origin: str = "operator") -> CommandResult:
        if self.sm.state == State.EMERGENCY_STOP:
            ok, msg = self.safety.reset_estop(source=origin)
            if not ok:
                return self._result(False, msg)
            self._fire(Trigger.RESET, f"e-stop reset by {origin}")
            return self._result(True, msg)
        if self.sm.state == State.RELEASED:
            self._fire(Trigger.RESET, f"acknowledged by {origin}")
            return self._result(True, "ready")
        return self._result(False, f"nothing to reset in state {self.sm.state}")

    # ================================================================ robot link
    def _make_transport(self, driver: str) -> RobotTransport:
        if driver == "mock":
            return MockRobotTransport(**self._mock_kwargs)
        if driver == "zeus":
            from .robot.zeus import ZeusRobotTransport

            host = self.rs.robot_host or self.settings.zeus_host
            return ZeusRobotTransport(
                host=host,
                port=self.settings.zeus_port,
                device_watchdog_verified=self.settings.zeus_device_watchdog_verified,
                device_watchdog_timeout_s=self.settings.zeus_device_watchdog_timeout_s,
                link_mode="monitor" if self.real_motion_mode == "disabled" else "control",
            )
        raise ValueError(f"unknown robot driver {driver!r}")

    def _callbacks(self) -> LinkCallbacks:
        def later(fn, *args):
            if self._loop is not None:
                self._loop.call_soon(fn, *args)

        return LinkCallbacks(
            on_connected=lambda info: later(self._on_robot_connected, info),
            on_disconnected=lambda reason: later(self._on_robot_disconnected, reason),
            on_ack=lambda info: later(self._on_robot_ack, info),
            on_degraded=lambda info: later(self._on_robot_degraded, info),
            on_motion_ended=lambda why, info: later(self.safety.notify_transport_motion_ended, why),
            on_telemetry=lambda tel: setattr(self, "robot_telemetry", tel),
            on_device_watchdog=lambda info: later(self._on_device_watchdog, info),
        )

    async def connect_robot(self, driver: str | None = None, host: str | None = None) -> CommandResult:
        driver = driver or self.driver
        if host:
            self.rs = merge_settings(self.rs, {"robot_host": host})
            self._persist()
        if driver == "zeus":
            return await self._connect_zeus()
        await self._stop_zeus_supervisor()
        if self.transport is not None and self.transport.connected:
            if driver == self.driver:
                return self._result(True, f"{driver} already connected")
            await self.disconnect_robot()
        self.driver = driver
        try:
            self.transport = self._make_transport(driver)
        except Exception as exc:
            return self._result(False, f"cannot create {driver} transport: {exc}")
        self.transport.set_callbacks(self._callbacks())
        target = self.transport.get_status().target
        self.bus.publish(EventType.ROBOT_CONNECTING, EventSource.ROBOT, {"driver": driver, "target": target}, simulated=driver == "mock")
        try:
            await asyncio.wait_for(self.transport.connect(), timeout=8.0)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            self.bus.publish(EventType.ROBOT_CONNECTION_FAILED, EventSource.ROBOT, {"driver": driver, "target": target, "reason": reason})
            return self._result(False, f"could not connect to {driver} at {target}: {reason}")
        await asyncio.sleep(0)  # let the on_connected callback run
        return self._result(True, f"{driver} connected", status=self.transport.get_status().model_dump(mode="json"))

    async def disconnect_robot(self) -> CommandResult:
        was_retrying = self._zeus_wanted
        await self._stop_zeus_supervisor()
        t = self.transport
        if t is None or not t.connected:
            if was_retrying:
                self._publish_mode()
                return self._result(True, "stopped trying to reach the Zeus Car")
            return self._result(True, "robot already disconnected")
        await self.safety.stop("operator disconnect", source="operator")
        await self.safety.disarm(reason="robot disconnecting")
        await t.disconnect()
        await asyncio.sleep(0)
        return self._result(True, "robot disconnected")

    # ---------------------------------------------------------- Zeus supervisor
    def _real_motion_mode(self) -> str:
        """disabled | bench | enabled, from LOCKEDIN_ZEUS_MOTOR_COMMANDS (docs/SAFETY.md section 7).
        "enabled" without a verified device stop (R5) is held at bench."""
        mc = self.settings.zeus_motor_commands
        if mc == "enabled" and self.settings.zeus_device_watchdog_verified:
            return "enabled"
        return "bench" if mc in ("bench", "enabled") else "disabled"

    def _real_motion_config(self) -> dict:
        mode = self._real_motion_mode()
        if mode == "enabled":
            return {"real_profiles": None, "real_autonomy_allowed": True, "real_motion_mode": mode}
        if mode == "bench":
            bench = frozenset({MotionProfileName.LOW_SPEED_TEST, MotionProfileName.LINK_TEST})
            return {"real_profiles": bench, "real_autonomy_allowed": False, "real_motion_mode": mode}
        return {"real_profiles": frozenset(), "real_autonomy_allowed": False, "real_motion_mode": mode}

    async def _connect_zeus(self) -> CommandResult:
        """Select the Zeus profile and start the link supervisor. Returns after the first
        attempt; on failure it keeps retrying in the background (network switch, robot off)."""
        target = f"ws://{self.rs.robot_host or self.settings.zeus_host}:{self.settings.zeus_port}"
        t = self.transport
        running = self._zeus_task is not None and not self._zeus_task.done()
        if running and self.driver == "zeus" and self.robot_link.get("target") == target:
            if t is not None and t.connected:
                return self._result(True, "zeus already connected", status=t.get_status().model_dump(mode="json"))
            return self._result(True, f"still trying to reach the Zeus Car at {target}", link=self.robot_link_snapshot())
        await self._stop_zeus_supervisor()
        if t is not None and t.connected:
            await self.disconnect_robot()
        first = self._start_zeus_supervisor()
        ok, message = await first
        status = self.transport.get_status().model_dump(mode="json") if self.transport is not None else None
        return self._result(ok, message, status=status, link=self.robot_link_snapshot())

    def _start_zeus_supervisor(self) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        first: asyncio.Future = loop.create_future()
        self.driver = "zeus"
        self._zeus_wanted = True
        self._zeus_task = asyncio.create_task(self._zeus_supervisor(first), name="zeus-supervisor")
        self._publish_mode()
        return first

    async def _stop_zeus_supervisor(self) -> None:
        self._zeus_wanted = False
        task, self._zeus_task = self._zeus_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.robot_link.update(state="idle", next_retry_at=None)

    ZEUS_RETRY_S = (1.0, 2.0, 3.0, 5.0)

    async def _zeus_supervisor(self, first: asyncio.Future) -> None:
        attempt = 0
        try:
            while self._zeus_wanted:
                attempt += 1
                transport = self._make_transport("zeus")
                transport.set_callbacks(self._callbacks())
                self.transport = transport
                target = transport.get_status().target
                self.robot_link.update(state="connecting", attempt=attempt, target=target, next_retry_at=None)
                if attempt == 1:
                    self.bus.publish(EventType.ROBOT_CONNECTING, EventSource.ROBOT, {"driver": "zeus", "target": target}, simulated=False)
                    self._publish_mode()
                try:
                    await asyncio.wait_for(transport.connect(), timeout=8.0)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                    delay = self.ZEUS_RETRY_S[min(attempt - 1, len(self.ZEUS_RETRY_S) - 1)]
                    self.robot_link.update(state="retry_wait", last_failure=reason, last_failure_at=time.time(), next_retry_at=time.time() + delay)
                    self._publish_connect_failure(target, reason, attempt)
                    if not first.done():
                        first.set_result((False, f"Zeus Car not reachable at {target} ({reason}). LOCKED IN keeps retrying; join the Zeus_Car Wi-Fi."))
                        self._publish_mode()
                    await asyncio.sleep(delay)
                    continue
                attempt = 0
                self._last_failure_published = None
                self.robot_link.update(state="connected", last_failure=None, connected_at=time.time(), next_retry_at=None)
                if not first.done():
                    link = transport.get_status().capabilities.link_mode
                    first.set_result((True, f"zeus connected ({link} link" + (": no motor commands)" if link == "monitor" else ")")))
                await asyncio.sleep(0)
                while self._zeus_wanted and transport.connected:
                    await asyncio.sleep(0.2)
                if not self._zeus_wanted:
                    break
                lost = transport.get_status().last_error or "link lost"
                self.robot_link.update(state="retry_wait", last_failure=lost, last_failure_at=time.time(), next_retry_at=time.time() + 1.0)
                self._publish_mode()
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("zeus supervisor crashed")
            self.robot_link.update(state="error", last_failure="supervisor crashed (see backend log)")
        finally:
            if not first.done():
                first.set_result((False, "stopped before the Zeus Car answered"))

    def _publish_connect_failure(self, target: str, reason: str, attempt: int) -> None:
        """First failure, a new reason, or every 30 s: the timeline is not a retry log."""
        now = time.monotonic()
        last = self._last_failure_published
        if last is not None and last[0] == reason and now - last[1] < 30.0:
            return
        self._last_failure_published = (reason, now)
        self.bus.publish(
            EventType.ROBOT_CONNECTION_FAILED,
            EventSource.ROBOT,
            {"driver": "zeus", "target": target, "reason": reason, "attempt": attempt, "retrying": True},
        )

    def robot_link_snapshot(self) -> dict:
        link = dict(self.robot_link)
        nra = link.pop("next_retry_at", None)
        link["next_retry_in_s"] = None if nra is None else round(max(0.0, nra - time.time()), 1)
        link["profile"] = "zeus" if self.driver == "zeus" else "sim"
        link["supervising"] = self._zeus_wanted
        return link

    def _perception_check(self) -> tuple[bool, str]:
        """Real robot, autonomous motion only: fail closed unless the robot's position is known."""
        if not self.settings.zeus_tracking_verified:
            return False, "robot position tracking not yet demonstrated on hardware (LOCKEDIN_ZEUS_TRACKING_VERIFIED=false)"
        v = self.vision_status
        if not (v.get("running") and v.get("camera") == "live"):
            return False, "camera not live"
        age = (v.get("link") or {}).get("frame_age_ms")
        if age is not None and age > 1000:
            return False, f"camera frames {age} ms old"
        if v.get("zone_mode") == "marker" and (v.get("tracking") or {}).get("state") not in ("TRACKING", "HELD"):
            return False, "robot marker not tracked"
        return True, "camera live, robot tracked"

    def voice_state(self) -> str:
        """For the dashboard: speaking / hearing / transcribing / thinking / ready / off."""
        v = self.voice
        if self.speaker.speaking:
            return "speaking"
        if v is None or not v.running:
            return "off"
        if getattr(v, "transcribing", False):
            return "transcribing"
        if v._ptt_active or v.vad.in_speech:
            return "hearing"
        cur = getattr(self.companion.llm, "_current", None)
        if cur is not None and not cur[1]:  # the model is writing a reply someone is waiting for
            return "thinking"
        return "ready"

    def audio_output_status(self) -> dict:
        wanted = self.rs.voice.output_device
        now = time.monotonic()
        if self._output_probe is None or now - self._output_probe[0] > 5.0:
            try:
                import sounddevice as sd

                names = sorted({d["name"] for d in sd.query_devices() if d.get("max_output_channels", 0) > 0})
                default = sd.query_devices(kind="output")["name"]
            except Exception:
                names, default = None, None
            self._output_probe = (now, names, default)
        _, names, default = self._output_probe
        listed = None if names is None or not wanted else any(wanted.lower() in n.lower() for n in names)
        # macOS reports volume and mute for the default output only.
        applies = not wanted or (default is not None and wanted.lower() in default.lower())
        vol = self._os_volume
        warning = None
        if vol is not None and applies:
            if vol["muted"]:
                warning = "muted in macOS: the robot's voice plays but nobody hears it (unmute with the volume keys)"
            elif vol["volume"] == 0:
                warning = "macOS output volume is 0: the robot's voice is silent"
        return {
            "device": wanted,
            "label": wanted or "System default output",
            "listed": listed,  # in PortAudio's device list (taken at startup)
            "devices": names,
            "system_volume": None if vol is None else {**vol, "device": default, "applies": applies},
            "warning": warning,
            **self.speaker.output_status(),
        }

    def hardware_gate(self) -> dict:
        """What stands between this robot and physical motion (docs/SAFETY.md section 7)."""
        t = self.transport
        if self.driver != "zeus":
            return {"robot": "simulated", "motion_permitted": True, "motor_commands": "simulated", "checks": []}
        st = t.get_status() if t is not None else None
        caps = st.capabilities if st is not None else None
        connected = bool(st and st.connected)
        fw = st.firmware if st is not None else {}
        mc, mode = self.settings.zeus_motor_commands, self.real_motion_mode
        patched = bool(fw.get("locked_in_patch"))
        rx_age = st.last_rx_age_s if st is not None else None
        checks = [
            {"id": "config", "ok": mode != "disabled", "label": "Motor commands enabled in configuration",
             "detail": f"LOCKEDIN_ZEUS_MOTOR_COMMANDS={mc}" + (" (Milestone 3 ships disabled)" if mc == "disabled" else "")},
            {"id": "link", "ok": bool(caps and caps.link_mode == "control"), "label": "Control link",
             "detail": "monitor link: this connection never sends motor commands" if mode == "disabled" else "control link (stop frames + motion)"},
            {"id": "device_expiry", "ok": patched, "label": "Device-side command expiry on the robot",
             "detail": "LOCKED IN firmware patch v2 reported by the robot" if patched else
             ("not reported: stock firmware 1.4.5 keeps the last command until the ESP32's 2-4 s silence timeout" if connected else "robot not connected")},
            {"id": "r5", "ok": self.settings.zeus_device_watchdog_verified, "label": "Device stop demonstrated on this robot (R5)",
             "detail": "LOCKEDIN_ZEUS_DEVICE_WATCHDOG_VERIFIED=" + str(self.settings.zeus_device_watchdog_verified).lower(),
             "blocking": mode != "bench"},
            {"id": "link_fresh", "ok": connected and rx_age is not None and rx_age < 1.5, "label": "Link alive",
             "detail": "not connected" if not connected else f"last message {rx_age:.1f}s ago" if rx_age is not None else "no message yet"},
            {"id": "motor_power", "ok": self.safety.motor_power_valid(), "label": "Motor power checked by the operator",
             "detail": "battery charged (its LEDs light with the power switch on), power switch on, wheels clear; "
             f"USB alone powers only the Arduino. Valid {round(self.safety.config.motor_power_ttl_s / 60)} min, cleared when the link drops"},
            {"id": "estop", "ok": not self.safety.estop_latched, "label": "E-stop clear",
             "detail": self.safety.estop_reason or "not latched"},
            {"id": "exclusive", "ok": None, "label": "Only LOCKED IN controls the robot", "blocking": False,
             "detail": "cannot be verified: the ESP32 accepts several clients and the IR remote always works; close the SunFounder app, keep the remote away"},
        ]
        for c in checks:
            c.setdefault("blocking", True)
        permitted = bool(caps and caps.motion_permitted) and all(c["ok"] for c in checks if c["blocking"])
        seen, why = self._perception_check()
        autonomy = [
            {"id": "mode", "ok": mode == "enabled", "label": "Motor commands enabled (R5 verified)", "detail": f"effective mode: {mode}"},
            {"id": "tracking", "ok": seen, "label": "Robot position tracked", "detail": why},
            {"id": "clear_path", "ok": self.safety.clear_path_valid(), "label": "Operator confirmed a clear retreat path", "detail": "valid 15 min once confirmed"},
        ]
        return {
            "autonomy_permitted": permitted and all(c["ok"] for c in autonomy),
            "autonomy_checks": autonomy,
            "robot": "zeus",
            "motor_commands": mc,
            "effective": mode,
            "motion_permitted": permitted,
            "link_mode": caps.link_mode if caps else None,
            "allowed_profiles": None if self.safety.config.real_profiles is None else sorted(p.value for p in self.safety.config.real_profiles),
            "checks": checks,
        }

    def _on_robot_connected(self, info: dict) -> None:
        t = self.transport
        sim = bool(t and t.simulated)
        self.bus.publish(EventType.ROBOT_CONNECTED, EventSource.ROBOT, {"driver": self.driver, **info}, simulated=sim)
        if info.get("firmware"):
            self.bus.publish(EventType.ROBOT_FIRMWARE_DETECTED, EventSource.ROBOT, {"firmware": info["firmware"]}, simulated=sim)
        self._fire(Trigger.ROBOT_CONNECTED, f"{self.driver} connected")
        self._publish_mode()
        if self._robot_lost_in_session and self.sessions.active:
            self._robot_lost_in_session = False
            self.behavior.essential("robot_back", "robot_back", "The robot link is back after a loss mid-session.")
            self._say("robot_back")

    def _on_robot_disconnected(self, reason: str) -> None:
        # Motion is impossible now; require an explicit re-arm after reconnecting.
        self.safety.armed = False
        self.safety.autonomy_enabled = False
        self.safety.clear_path_confirmed_at = None
        self.safety.clear_motor_power("robot link lost: it may have been switched off")
        self.safety.notify_transport_motion_ended("disconnect")
        self.bus.publish(EventType.ROBOT_DISCONNECTED, EventSource.ROBOT, {"driver": self.driver, "reason": reason})
        if self.sessions.active and reason != "operator disconnect":  # a real drop, not a click
            self._robot_lost_in_session = True
            self.behavior.essential("robot_lost", "robot_lost", "Robot link lost mid-session: the user must know the robot cannot act.")
            self._say("robot_lost")
        if self.sm.state in (State.RETREATING, State.RETURNING):
            self._fire(Trigger.MOTION_ABORTED, "robot link lost during motion", robot_connected=False)
        self._fire(Trigger.ROBOT_DISCONNECTED, reason)
        self._publish_mode()

    def _on_robot_ack(self, info: dict) -> None:
        active = self.safety.active
        payload = {**info, "request_id": active.motion.request_id if active else None}
        if active is not None:
            sent_mark = self._latency_marks.get(f"sent:{active.motion.request_id}")
            if sent_mark is not None:
                ack_ms = (time.monotonic() - sent_mark) * 1000
                payload["sent_to_ack_ms"] = round(ack_ms, 1)
                self.perf.add("motion_sent_to_ack_ms", ack_ms)
        self.bus.publish(EventType.ROBOT_MOTION_ACKNOWLEDGED, EventSource.ROBOT, payload, simulated=bool(self.transport and self.transport.simulated))

    def _on_robot_degraded(self, info: dict) -> None:
        self.bus.publish(EventType.ROBOT_LINK_DEGRADED, EventSource.ROBOT, info)

    def _on_device_watchdog(self, info: dict) -> None:
        sim = bool(self.transport and self.transport.simulated)
        self.bus.publish(
            EventType.ROBOT_DEVICE_WATCHDOG,
            EventSource.ROBOT,
            {**info, "note": "the robot's own firmware stopped the motors after a silent link"},
            simulated=sim,
        )

    async def arm(self, origin: str = "operator") -> CommandResult:
        ok, msg = self.safety.arm(source=origin)
        return self._result(ok, msg)

    async def disarm(self, origin: str = "operator") -> CommandResult:
        await self.safety.disarm(source=origin)
        return self._result(True, "disarmed")

    async def confirm_motor_power(self, origin: str = "operator") -> CommandResult:
        """The operator's physical check before any real motion (docs/SAFETY.md section 7)."""
        t = self.transport
        if self.driver != "zeus" or t is None or t.simulated:
            return self._result(False, "only the real robot needs a motor power check")
        if not t.connected:
            return self._result(False, "connect the robot first: the check is cleared whenever the link drops")
        self.safety.confirm_motor_power(source=origin)
        mins = round(self.safety.config.motor_power_ttl_s / 60)
        return self._result(True, f"motor power checked; valid {mins} min or until the robot disconnects")

    async def set_autonomy(self, enabled: bool, clear_path_confirmed: bool = False, origin: str = "operator") -> CommandResult:
        ok, msg = self.safety.set_autonomy(enabled, clear_path_confirmed, source=origin)
        if ok and enabled and self.adaptation.params.autonomy_suspended:
            self.adaptation.resume_autonomy()
        return self._result(ok, msg)

    async def manual_move(
        self,
        primitive: str,
        speed: float | None = None,
        duration_s: float | None = None,
        origin: str = "operator",
        test_profile: bool = False,
    ) -> CommandResult:
        try:
            prim = MotionPrimitive(primitive)
        except ValueError:
            return self._result(False, f"unknown motion primitive {primitive!r}")
        if prim == MotionPrimitive.STOP:
            return await self.stop_robot(origin)
        motion_origin = MotionOrigin.VOICE if origin == "voice" else (MotionOrigin.TEST if test_profile else MotionOrigin.OPERATOR)
        req = MotionRequest(
            primitive=prim,
            speed=speed,
            duration_s=duration_s,
            profile=MotionProfileName.LOW_SPEED_TEST if test_profile else MotionProfileName.NORMAL_CONTROL,
            origin=motion_origin,
            reason=f"manual {prim.value} from {origin}",
        )
        decision = await self.safety.request(req, state_allows=self.sm.state in MANUAL_OK_STATES, state_name=self.sm.state.value)
        if not decision.approved:
            return self._result(False, decision.reason, reason_code=decision.reason_code, checks=[c.model_dump() for c in decision.checks])
        return self._result(True, f"{prim.value} approved", motion=decision.motion.model_dump(mode="json"))

    async def stop_robot(self, origin: str = "operator", speak: bool = False) -> CommandResult:
        t0 = time.monotonic()
        if self._return_task is not None:
            self._return_task.cancel()
        sent = await self.safety.stop(f"stop requested by {origin}", source=origin)
        self.perf.add("stop_call_ms", (time.monotonic() - t0) * 1000)
        self.speaker.interrupt("stop")
        reply = self._say("stop_ack", force=True) if speak else None
        return self._result(True, "stop sent" if sent else "stop requested (robot not connected)", reply)

    # ---------------------------------------------------------- R5 link test
    LINK_TEST_MAX_FREEZE_S = {"wheels_off_ground": 5.0, "on_floor": 1.5}

    async def run_link_test(
        self, setup: str, primitive: str = "forward", speed: int = 30, lead_s: float = 0.5, freeze_s: float = 2.0
    ) -> CommandResult:
        """R5 (docs/HARDWARE_TESTS.md): drive slowly, then stop transmitting with the link left
        open, as a hung laptop or stalled Wi-Fi would, and see whether the robot stops on its
        own. The host cannot see the wheels; the physical stop time comes from video. This
        records what the host does know: timestamps, frames written during the freeze, whether
        the link stayed open, and the patched firmware's own watchdog counter."""
        limit = self.LINK_TEST_MAX_FREEZE_S.get(setup)
        if limit is None:
            return self._result(False, "say how the robot is set up: wheels_off_ground or on_floor")
        try:
            prim = MotionPrimitive(primitive)
        except ValueError:
            return self._result(False, f"unknown motion primitive {primitive!r}")
        if prim not in TRANSLATIONS:
            return self._result(False, "the link test uses translations only")
        if not 0.3 <= lead_s <= 1.0:
            return self._result(False, "lead time must be 0.3-1.0 s")
        if not 0.5 <= freeze_s <= limit:
            return self._result(False, f"freeze must be 0.5-{limit:g} s with the robot {setup.replace('_', ' ')}")
        if self._link_test_task is not None and not self._link_test_task.done():
            return self._result(False, "a link test is already running")
        t = self.transport
        if t is None or not t.connected:
            return self._result(False, "robot is not connected")
        if self.sessions.current is not None or self.sm.state != State.IDLE:
            return self._result(False, f"run link tests from IDLE with no session (state {self.sm.state.value})")
        test_id = uuid.uuid4().hex[:8]
        req = MotionRequest(
            primitive=prim,
            speed=speed,
            duration_s=lead_s + freeze_s + 1.0,  # the test ends it with stop() when the freeze is over
            profile=MotionProfileName.LINK_TEST,
            origin=MotionOrigin.TEST,
            reason=f"R5 link test {test_id} ({setup})",
        )
        decision = await self.safety.request(req, state_allows=self.sm.state == State.IDLE, state_name=self.sm.state.value)
        if not decision.approved:
            return self._result(False, decision.reason, reason_code=decision.reason_code, checks=[c.model_dump() for c in decision.checks])
        active = self.safety.active
        motion = decision.motion
        assert motion is not None and active is not None
        before = self.robot_telemetry.model_copy()
        self.bus.publish(
            EventType.ROBOT_LINK_TEST_STARTED,
            EventSource.SAFETY,
            {"test_id": test_id, "setup": setup, "primitive": prim.value, "speed": motion.speed, "lead_s": lead_s, "freeze_s": freeze_s},
            simulated=t.simulated,
        )
        self._link_test_task = asyncio.create_task(self._link_test(test_id, t, active, setup, lead_s, freeze_s, before))
        result = await asyncio.shield(self._link_test_task)
        return self._result(result["outcome"] == "completed", result["summary"], result=result)

    async def _link_test(self, test_id, t: RobotTransport, active, setup: str, lead_s: float, freeze_s: float, before: RobotTelemetry) -> dict:
        motion = active.motion
        sim = t.simulated
        notes: list[str] = []
        outcome = "completed"
        freeze_at = None
        frozen_for = None
        link_closed_after = None
        frames_during = None

        t0 = time.monotonic()
        while not active.sent and self.safety.active is active and time.monotonic() - t0 < 1.0:
            await asyncio.sleep(0.005)
        if not active.sent:
            outcome = "not_sent"
            notes.append("the motion frame was never written; nothing was tested")
        else:
            await asyncio.sleep(lead_s)
            if self.safety.active is not active:
                outcome = "interrupted"
                notes.append("motion ended before the freeze (stop, e-stop or disconnect)")
            else:
                frames0 = t.get_status().frames_sent
                if not t.freeze_tx(freeze_s + 0.5):
                    outcome = "unsupported"
                    notes.append(f"{t.name} transport cannot pause transmission")
                else:
                    freeze_at = time.monotonic()
                    self.bus.publish(
                        EventType.ROBOT_LINK_FROZEN,
                        EventSource.ROBOT,
                        {"test_id": test_id, "freeze_s": freeze_s, "primitive": motion.primitive.value, "speed": motion.speed, "setup": setup},
                        simulated=sim,
                    )
                    while (now := time.monotonic()) - freeze_at < freeze_s:
                        if not t.connected:
                            link_closed_after = round(now - freeze_at, 3)
                            outcome = "link_closed"
                            notes.append(f"the robot side closed the connection {link_closed_after:.2f}s into the freeze")
                            break
                        if self.safety.estop_latched or self.safety.active is not active or not t.tx_frozen:
                            outcome = "interrupted"
                            notes.append("freeze ended early by stop / e-stop")
                            break
                        await asyncio.sleep(0.005)
                    frozen_for = round(time.monotonic() - freeze_at, 3)
                    if t.connected:
                        frames_during = t.get_status().frames_sent - frames0
        if self.safety.active is active:
            await self.safety.stop("link test: freeze over" if outcome == "completed" else f"link test {outcome}", source="test")
        stopped_at, stopped_wall = time.monotonic(), time.time()

        after = None  # first telemetry the robot sends once we transmit again
        while t.connected and time.monotonic() - stopped_at < 1.0:
            tel = self.robot_telemetry
            if tel.timestamp > stopped_wall:
                after = tel
                break
            await asyncio.sleep(0.02)

        result: dict[str, Any] = {
            "test_id": test_id,
            "at": time.time(),
            "simulated": sim,
            "transport": t.name,
            "setup": setup,
            "primitive": motion.primitive.value,
            "speed": motion.speed,
            "lead_s": lead_s,
            "freeze_s": freeze_s,
            "outcome": outcome,
            "frozen_for_s": frozen_for,
            "frames_written_during_freeze": frames_during,
            "link_closed_after_s": link_closed_after,
            "stop_sent_after_freeze_s": None if freeze_at is None else round(stopped_at - freeze_at, 3),
            "device_watchdog": self._link_test_device_evidence(t, before, after, active, freeze_at),
        }
        if isinstance(t, MockRobotTransport):
            notes.append("simulated robot: no wheels moved")
        else:
            notes.append(
                "the host cannot see the wheels: read the stop time from the video (wheels vs the dashboard freeze timer); "
                "pass = wheels stop within 0.5 s of the freeze, every run"
            )
            if setup == "on_floor":
                notes.append("put the robot back where it started and press Set home before driving it again")
        result["notes"] = notes
        ev = result["device_watchdog"]
        result["summary"] = (
            f"link test {outcome}: froze {frozen_for if frozen_for is not None else 0:.2f}s; "
            + (ev["summary"] if ev else "no device evidence")
        )
        self.link_tests.append(result)
        self.bus.publish(EventType.ROBOT_LINK_TEST_FINISHED, EventSource.ROBOT, result, simulated=sim)
        return result

    @staticmethod
    def _link_test_device_evidence(t: RobotTransport, before: RobotTelemetry, after: RobotTelemetry | None, active, freeze_at: float | None) -> dict:
        if isinstance(t, MockRobotTransport):
            stop = next((e for e in reversed(t.travel_log) if e.get("stopped_at") and e["stopped_at"] >= active.started_mono), None)
            if stop is None or freeze_at is None:
                return {"source": "simulator", "tripped": None, "summary": "simulated wheels: no stop recorded"}
            after_s = round(stop["stopped_at"] - freeze_at, 3)
            tripped = stop["why"] == "device_watchdog"
            how = "simulated device watchdog" if tripped else f"host stop ({stop['why']})"
            return {
                "source": "simulator",
                "tripped": tripped,
                "wheels_stopped_after_s": after_s,
                "watchdog_timeout_s": t.device_watchdog_s,
                "summary": f"simulated wheels stopped {after_s:.2f}s after the freeze by {how}",
            }
        b, a = before.device_watchdog_trips, None if after is None else after.device_watchdog_trips
        if b is None and a is None:
            return {
                "source": "none",
                "tripped": None,
                "summary": "no telemetry (stock firmware reports nothing): only the video can tell whether the wheels stopped",
            }
        tripped = a is not None and b is not None and a > b
        return {
            "source": "firmware_patch",
            "tripped": tripped if a is not None and b is not None else None,
            "trips_before": b,
            "trips_after": a,
            "latched_after": None if after is None else after.device_watchdog_latched,
            "free_ram_after": None if after is None else after.device_free_ram,
            "summary": (
                "the Arduino reports its watchdog stopped the motors during the freeze"
                if tripped
                else "the Arduino did NOT report a watchdog trip"
                if a is not None and b is not None
                else "no telemetry after the freeze"
            ),
        }

    async def set_home(self, origin: str = "operator") -> CommandResult:
        if self.sm.state in (State.RETREATING, State.RETURNING) or self.safety.active is not None:
            return self._result(False, "cannot set home while the robot is moving")
        self.displacement.clear()
        self._fire(Trigger.HOME_SET, f"home set by {origin}")
        if isinstance(self.transport, MockRobotTransport):
            self.transport.reset_pose()
        return self._result(True, "current position is now home")

    async def return_home(self, origin: str = "operator") -> CommandResult:
        t = self.transport
        if t is not None and not t.simulated and not self.settings.zeus_return_calibrated:
            return self._result(
                False,
                "return refused on the real robot: no calibrated, verified return procedure yet "
                "(dead reckoning is not trusted; LOCKEDIN_ZEUS_RETURN_CALIBRATED=false)",
                reason_code="return_uncalibrated",
            )
        if not self.displacement:
            return self._result(False, "robot is already at home (no recorded displacement)")
        if self._return_task is not None and not self._return_task.done():
            return self._result(False, "return already in progress")
        if not self.safety.armed:
            return self._result(False, "robot is disarmed; arm it to allow the return")
        result = self._fire(Trigger.RETURN_REQUESTED, f"return requested by {origin}")
        if not result.accepted:
            return self._result(False, result.reason)
        self._return_task = asyncio.create_task(self._run_return(origin), name="return-home")
        return self._result(True, "returning (approximate dead reckoning, no localisation)")

    async def _run_return(self, origin: str) -> None:
        aborted = False
        try:
            while self.displacement:
                step = self.displacement[-1]
                req = MotionRequest(
                    primitive=INVERSE[MotionPrimitive(step["primitive"])],
                    speed=step["speed"],
                    duration_s=step["ran_s"],
                    profile=MotionProfileName.NORMAL_CONTROL,
                    origin=MotionOrigin.RETURN,
                    reason=f"return: undo {step['primitive']} {step['ran_s']:.2f}s",
                )
                decision = await self.safety.request(req, state_allows=self.sm.state == State.RETURNING, state_name=self.sm.state.value)
                if not decision.approved:
                    aborted = True
                    break
                task = self.safety.active.task if self.safety.active else None
                if task is not None:
                    await asyncio.shield(task)
                if self._last_motion_outcome != "completed":
                    aborted = True
                    break
                self.displacement.pop()
        except asyncio.CancelledError:
            aborted = True
        finally:
            if self.sm.state == State.RETURNING:
                self._fire(Trigger.MOTION_ABORTED if aborted else Trigger.MOTION_COMPLETED, "return finished" if not aborted else "return aborted")

    _last_motion_outcome: str = ""

    def _on_motion_finished(self, motion: ApprovedMotion, outcome: str, info: dict) -> None:
        self._last_motion_outcome = outcome
        ran = float(info.get("ran_s") or 0.0)
        sent = bool(info.get("sent"))
        moved = sent and ran > 0.02
        t = self.transport
        if moved:
            self.counters.robot_moves += 1
            if isinstance(t, MockRobotTransport):
                tel = t.get_telemetry()
                self.bus.publish(
                    EventType.ROBOT_MOTION_CONFIRMED,
                    EventSource.ROBOT,
                    {"request_id": motion.request_id, "pose": tel.pose, "note": "simulated odometry"},
                    simulated=True,
                )
        if motion.profile == MotionProfileName.LINK_TEST:
            pass  # travel depends on what the firmware did during the frozen link; nothing to replay
        elif motion.origin != MotionOrigin.RETURN and moved:
            self.displacement.append(
                {"primitive": motion.primitive.value, "speed": motion.speed, "ran_s": round(min(ran, motion.duration_s), 3), "origin": motion.origin.value}
            )
        if motion.origin == MotionOrigin.POLICY and self.sm.state == State.RETREATING:
            if outcome == "completed":
                self._fire(Trigger.MOTION_COMPLETED, "retreat window finished")
            else:
                self._fire(Trigger.MOTION_ABORTED, f"retreat {outcome}")

    # ================================================================ vision
    async def _preload_vision(self) -> None:
        try:
            from .vision.pipeline import VisionService

            if self.vision is None:
                self.vision = VisionService(
                    models_dir=self.settings.models_dir,
                    emit=self._vision_emit,
                    width=self.settings.camera_width,
                    height=self.settings.camera_height,
                )
            t0 = time.monotonic()
            await asyncio.to_thread(self.vision.load_models)
            self.perf.add("vision_model_load_ms", (time.monotonic() - t0) * 1000)
        except Exception as exc:
            log.warning("vision preload skipped: %s", exc)

    ZEUS_CAMERA_LABEL = "ZEUS ESP32-CAM"

    def zeus_camera_url(self) -> str:
        if self.settings.zeus_camera_url:
            return self.settings.zeus_camera_url
        return f"http://{self.rs.robot_host or self.settings.zeus_host}:9000/mjpg"

    def resolve_camera(self, source: str) -> dict:
        """Dashboard / startup name -> what to open, how to label it, where it is mounted."""
        s = (source or "").strip()
        zeus_url = self.zeus_camera_url()
        if s.lower() == "zeus" or s == zeus_url:
            target, label, mount = zeus_url, self.ZEUS_CAMERA_LABEL, "robot"
        elif s.isdigit():
            target, label, mount = s, "MAC WEBCAM" if s == "0" else f"CAMERA {s}", "fixed"
        elif s.startswith(("http://", "https://", "rtsp://")):
            from urllib.parse import urlsplit

            target, label, mount = s, f"STREAM {urlsplit(s).netloc}", "fixed"
        else:
            from pathlib import Path

            target, label, mount = s, f"FILE {Path(s).name}", "fixed"
        if self.rs.camera_mount != "auto":
            mount = self.rs.camera_mount
        flip = self.rs.zeus_camera_flip if label == self.ZEUS_CAMERA_LABEL else "none"
        return {"source": s, "target": target, "label": label, "mount": mount, "flip": flip}

    @property
    def camera_choice(self) -> str:
        return self._camera_override or self.rs.camera_source

    async def start_vision(self, source: str | None = None) -> CommandResult:
        """Start the camera, or switch to another one: the MediaPipe models stay loaded and the
        old source is released first. The chosen source is used as is; if it cannot deliver,
        the dashboard says so and it keeps reconnecting, it is never swapped for another camera."""
        if not self.enable_vision:
            return self._result(False, "vision disabled for this process")
        if source is not None:
            self._camera_override = None  # the operator's choice wins over the startup default
            self.rs = merge_settings(self.rs, {"camera_source": source})
        try:
            from .vision.pipeline import VisionService
        except Exception as exc:
            self.vision_status = {"running": False, "camera": "offline", "reason": f"vision unavailable: {exc}"}
            return self._result(False, self.vision_status["reason"])
        if self.vision is None:
            self.vision = VisionService(
                models_dir=self.settings.models_dir,
                emit=self._vision_emit,
                width=self.settings.camera_width,
                height=self.settings.camera_height,
            )
        cam = self.resolve_camera(self.camera_choice)
        try:
            await asyncio.to_thread(
                self.vision.start,
                cam["target"],
                label=cam["label"],
                mount=cam["mount"],
                flip=cam["flip"],
                mjpeg_backend=self.settings.mjpeg_backend,
            )
        except Exception as exc:
            self.vision_status = {"running": False, "camera": "offline", "reason": str(exc)}
            return self._result(False, f"vision failed to start: {exc}")
        self._apply_vision_tuning()
        self.rs = merge_settings(self.rs, {"vision_enabled": True})
        self._persist()
        self.vision_status = self.vision.snapshot()
        self.bus.publish(EventType.VISION_STARTED, EventSource.VISION, {"source": cam["target"], "label": cam["label"], "mount": cam["mount"]})
        self._publish_mode()
        return self._result(True, f"vision started on {cam['label']} ({cam['target']})", camera=cam)

    async def stop_vision(self) -> CommandResult:
        if self.vision is not None:
            await asyncio.to_thread(self.vision.stop)
        self.vision_status = {"running": False, "camera": "offline", "reason": "stopped by operator"}
        self.focus.set_state(FocusState.UNKNOWN)
        self.rs = merge_settings(self.rs, {"vision_enabled": False})
        self._persist()
        self.bus.publish(EventType.VISION_STOPPED, EventSource.VISION, {})
        self._publish_mode()
        return self._result(True, "vision stopped")

    @property
    def zone_setting_key(self) -> str:
        """Zones are per camera geometry: the robot-mounted camera has its own."""
        return "robot_camera_zone" if self.vision is not None and self.vision.mount == "robot" else "phone_zone"

    async def attach_zone(self, points: list | None = None) -> CommandResult:
        """Anchor the phone zone to the robot markers visible right now."""
        if self.vision is None or not self.vision_live:
            return self._result(False, "start the camera first, with the robot's marker in view")
        if self.vision.mount == "robot":
            return self._result(False, "the robot-mounted camera cannot see the robot's own markers; its zone is already robot-relative")
        pts = [tuple(p) for p in (points or self.rs.phone_zone.points)]
        try:
            anchor = await asyncio.to_thread(self.vision.attach_zone, pts)
        except ValueError as exc:
            return self._result(False, str(exc))
        data = self.rs.model_dump(mode="json")
        data["phone_zone"] = {"points": [list(p) for p in pts], "mode": "marker", "anchor": anchor}
        self.rs = RuntimeSettings.model_validate(data)
        self._persist()
        markers = sorted(int(k) for k in anchor["marker_points"])
        self.bus.publish(EventType.VISION_ZONE_ATTACHED, EventSource.OPERATOR, {"markers": markers, "points": data["phone_zone"]["points"]})
        return self._result(True, f"phone zone now follows marker{'s' if len(markers) > 1 else ''} {', '.join(map(str, markers))}")

    async def detach_zone(self) -> CommandResult:
        data = self.rs.model_dump(mode="json")
        data["phone_zone"]["mode"] = "static"
        data["phone_zone"]["anchor"] = None
        self.rs = RuntimeSettings.model_validate(data)
        self._apply_vision_tuning()
        self._persist()
        self.bus.publish(EventType.SETTINGS_CHANGED, EventSource.OPERATOR, {"patch": {"phone_zone": {"mode": "static"}}})
        return self._result(True, "phone zone fixed in camera coordinates")

    def _vision_emit(self, ev: VisionEvent) -> None:
        """Called from the vision thread."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.handle_vision_event(ev)))

    def _apply_vision_tuning(self) -> None:
        tuning = self.rs.vision.model_copy()
        p = self.adaptation.params
        tuning.confirm_dwell_ms = min(2000, tuning.confirm_dwell_ms + p.confirm_dwell_bonus_ms)
        tuning.min_hand_confidence = min(0.95, tuning.min_hand_confidence + p.min_confidence_bonus)
        self.effective_vision_tuning = tuning
        if self.vision is not None:
            z = self.rs.phone_zone
            robot_cam = self.vision.mount == "robot"
            self.vision.configure(
                zone=self.rs.robot_camera_zone.points if robot_cam else z.points,
                tuning=tuning,
                zone_mode=z.mode,
                anchor=z.anchor,
                marker_ids=self.rs.vision.marker_ids,
                marker_dictionary=self.rs.vision.marker_dictionary,
                hold_s=self.rs.vision.marker_hold_s,
            )
            self.vision.show_video = self.rs.show_video

    async def handle_vision_event(self, ev: VisionEvent) -> None:
        k = ev.kind
        sim = ev.simulated
        if sim and k in ("approaching", "confirmed", "cleared", "focus"):
            self._sim_perception_used = True
        if k == "status":
            was_live = self.vision_live
            self.vision_status = ev.data
            if was_live != self.vision_live:
                self._publish_mode()
            return
        if k == "camera_connected":
            self.bus.publish(EventType.VISION_CAMERA_CONNECTED, EventSource.VISION, ev.data, simulated=sim)
            d = self.behavior.camera_back(running=self.sessions.active and not self.sessions.paused)
            if d is not None and d.speak:
                self.companion.react(d)
            return
        if k == "camera_disconnected":
            self.focus.set_state(FocusState.UNKNOWN)
            self.bus.publish(EventType.VISION_CAMERA_DISCONNECTED, EventSource.VISION, ev.data, simulated=sim)
            # Announced only if it stays down (behavior.py B3): a blip is not news.
            self.behavior.camera_lost()
            return
        if k == "hand_detected":
            self.bus.publish(EventType.VISION_HAND_DETECTED, EventSource.VISION, ev.data, confidence=ev.confidence, simulated=sim)
            return
        if k == "hand_lost":
            self.bus.publish(EventType.VISION_HAND_LOST, EventSource.VISION, ev.data, simulated=sim)
            return
        if k == "approaching":
            self.bus.publish(EventType.VISION_HAND_APPROACHING, EventSource.VISION, ev.data, confidence=ev.confidence, simulated=sim)
            if self.sessions.active and not self.sessions.paused:
                self.counters.reach_suspected += 1
            self._fire(Trigger.REACH_SUSPECTED, "hand approaching phone zone")
            self._maybe_warn_reach(ev)
            return
        if k == "cleared":
            self.bus.publish(EventType.VISION_REACH_CLEARED, EventSource.VISION, ev.data, simulated=sim)
            # Only genuine "came close, then left" episodes count as uncertain movement;
            # cooldown suppressions and lost tracks do not.
            if self.sm.state == State.POSSIBLE_PHONE_REACH and ev.data.get("reason") in (None, "left_zone_unconfirmed"):
                self.adaptation.record_uncertain()
            self._fire(Trigger.REACH_CLEARED, "hand left before confirmation")
            return
        if k == "suppressed":
            self.bus.publish(EventType.VISION_REACH_SUPPRESSED, EventSource.VISION, ev.data, confidence=ev.confidence, simulated=sim)
            if self.sessions.active:
                self.counters.reach_suppressed += 1
            return
        if k == "confirmed":
            await self._handle_confirmed_reach(ev)
            return
        if k == "focus":
            await self._handle_focus(FocusState(ev.data["state"]), ev)
            return
        if k == "face_present":
            self.bus.publish(EventType.VISION_FACE_PRESENT, EventSource.VISION, ev.data, confidence=ev.confidence, simulated=sim)
            return
        if k == "face_absent":
            self.bus.publish(EventType.VISION_FACE_ABSENT, EventSource.VISION, ev.data, simulated=sim)
            return
        if k == "zone_tracking":
            self.bus.publish(EventType.VISION_ZONE_TRACKING, EventSource.VISION, ev.data, simulated=sim)
            if self.vision is not None:
                self.vision_status = self.vision.snapshot()
            return

    @property
    def _last_reach_warning(self) -> float:
        return self.behavior.last_heads_up

    @_last_reach_warning.setter
    def _last_reach_warning(self, value: float) -> None:
        self.behavior.last_heads_up = value

    def _maybe_warn_reach(self, ev: VisionEvent) -> None:
        """A hand heading for the phone is a *possible* reach: at most a light heads-up (a beat
        late, so a confirmation right after replaces it), never counted, never motion. The
        behaviour engine decides and records why (behavior.py, rule B1)."""
        d = self.behavior.possible_reach(
            running=self.sessions.active and not self.sessions.paused,
            monitoring=self.sm.state in (State.SESSION_ACTIVE, State.POSSIBLE_PHONE_REACH),
            confidence=ev.confidence,
            last_intervention=self.last_intervention_at,
        )
        if d.speak:
            self.companion.react(d, beat=True, trace={"kind": "approach", "t0": ev.frame_mono or time.monotonic()})

    async def _handle_confirmed_reach(self, ev: VisionEvent) -> None:
        now = time.monotonic()
        conf = ev.confidence if ev.confidence is not None else 0.0
        payload = dict(ev.data)
        if ev.frame_mono is not None:
            payload["frame_to_event_ms"] = round((now - ev.frame_mono) * 1000, 1)
            self.perf.add("frame_to_reach_event_ms", (now - ev.frame_mono) * 1000)
        if self.sessions.active:
            payload["session_active_s"] = round(self.sessions.active_seconds(), 1)
        before = self.sm.state
        payload["state"] = before.value
        self.bus.publish(EventType.VISION_PHONE_REACH_CONFIRMED, EventSource.VISION, payload, confidence=conf, simulated=ev.simulated)
        if self.active_trial is not None:
            self.active_trial.setdefault("confirmations", []).append(round(now - self.active_trial["mono"], 3))

        result = self._fire(Trigger.REACH_CONFIRMED, f"reach confirmed (confidence {conf:.2f})")
        counting = self.sessions.active and not self.sessions.paused
        counted = False
        if before == State.HOLDING:
            # The phone is already away; noted, and the policy stays quiet or speaks only.
            if counting:
                self.counters.reach_while_holding += 1
        elif result.changed and result.to_state == State.PHONE_REACH_CONFIRMED:
            if counting:
                self.counters.reach_confirmed += 1
                self.adaptation.record_confirmed_reach()
                counted = True
        elif before == State.DISCONNECTED and counting:
            # Robot offline mid-session: still a reach; the policy falls back to voice.
            self.counters.reach_confirmed += 1
            self.adaptation.record_confirmed_reach()
            counted = True
        else:
            # Already handling one (confirmed / retreating / returning), no session, paused or
            # e-stop: the event above records it, nothing reacts to it.
            return

        decision = self.policy.decide_reach(self._policy_ctx(), conf)
        self.last_decision = {**decision.to_payload(), "at": time.time()}
        decision_ms = (time.monotonic() - (ev.frame_mono or now)) * 1000
        self.perf.add("frame_to_decision_ms", decision_ms)
        self.bus.publish(
            EventType.COACH_DECISION,
            EventSource.POLICY,
            {**decision.to_payload(), "frame_to_decision_ms": round(decision_ms, 1)},
            simulated=ev.simulated,
        )

        moving = False
        say_as: str | None = None
        physical: str | None = None
        if decision.action == "retreat" and decision.motion is not None:
            if ev.frame_mono is not None:
                self._latency_marks[decision.motion.request_id] = ev.frame_mono
            sd = await self.safety.request(
                decision.motion, state_allows=self.sm.state == State.PHONE_REACH_CONFIRMED, state_name=self.sm.state.value
            )
            if sd.approved:
                moving = True
                physical = "retreat requested; the safety controller approved it"
                self._fire(Trigger.RETREAT_STARTED, "safety approved retreat")
            else:
                self.counters.motions_blocked += 1
                physical = f"retreat requested; the safety controller refused: {sd.reason}"
                if sd.reason_code not in ("cooldown", "rate", "busy"):  # it really could not move
                    say_as = "retreat_blocked"
        if not moving and self.sm.state == State.PHONE_REACH_CONFIRMED:
            self._fire(Trigger.REACH_HANDLED, f"decision: {decision.action}")
        self._last_reach_mono = time.monotonic()
        bd = self.behavior.confirmed_reach(policy=decision, counted=counted, elapsed_s=self.sessions.active_seconds(), physical=physical)
        if bd.speak:
            self.companion.react(bd, say_as=say_as, intervention=True, beat=True, trace={"kind": "reach", "t0": ev.frame_mono or now})

    async def _handle_focus(self, state: FocusState, ev: VisionEvent) -> None:
        previous = self.focus.state
        if state == previous:
            return
        held = time.monotonic() - self._focus_since
        self.focus.set_state(state)
        self._focus_since = time.monotonic()
        self.bus.publish(
            EventType.VISION_FOCUS_CHANGED,
            EventSource.VISION,
            {"from": previous.value, "to": state.value, **ev.data},
            confidence=ev.confidence,
            simulated=ev.simulated,
        )
        if state == FocusState.FOCUSED and previous == FocusState.AWAY and held > 60:
            decision = self.policy.decide_focus(self._policy_ctx(), previous, 0.0)
            if decision.speak and decision.category:
                self._say(decision.category, priority=PRIORITY_AMBIENT, intervention=True)

    def _evaluate_focus_nudge(self) -> None:
        if self.focus.state != FocusState.POSSIBLY_DISTRACTED:
            return
        held = time.monotonic() - self._focus_since
        decision = self.policy.decide_focus(self._policy_ctx(), self.focus.state, held)
        if decision.action == "voice_only" and decision.category:
            self.last_decision = {**decision.to_payload(), "at": time.time()}
            self.bus.publish(EventType.COACH_DECISION, EventSource.POLICY, decision.to_payload())
            if decision.speak:
                self._say(decision.category, priority=PRIORITY_AMBIENT, intervention=True)

    def _evaluate_focus_streak(self) -> None:
        """Praise a real streak: the camera sees a focused user and no reach for FOCUS_STREAK_S.
        Without a live camera focus is unknown, so there is no praise either."""
        if self.focus.state != FocusState.FOCUSED or self._session_started_mono is None:
            return
        now = time.monotonic()
        since = max(self._session_started_mono, self._last_reach_mono or 0.0)
        if now - max(since, self._last_streak_mono or 0.0) < FOCUS_STREAK_S:
            return
        self._last_streak_mono = now
        d = self.behavior.focus_streak(running=self.sessions.active and not self.sessions.paused, streak_s=now - since)
        if d is None or not d.speak:
            return
        if self.companion.proactive_available:
            asyncio.create_task(self.companion.react_generated(d, priority=PRIORITY_AMBIENT), name="praise")
        else:
            self.companion.react(d, priority=PRIORITY_AMBIENT, slots={"streak": f"{int((now - since) // 60)} minutes"})

    # ================================================================ voice
    async def start_voice(self) -> CommandResult:
        if not self.enable_voice:
            return self._result(False, "voice disabled for this process")
        try:
            from .voice.service import VoiceService
        except Exception as exc:
            return self._result(False, f"voice unavailable: {exc}")
        if self.voice is None:
            self.voice = VoiceService(settings=self.settings, emit=self._voice_emit, speaker=self.speaker, on_transcribing=self._stt_busy)
        try:
            await asyncio.to_thread(self.voice.start, self.rs.voice.input_device, self.rs.voice.listen_mode)
        except Exception as exc:
            self.bus.publish(EventType.VOICE_ERROR, EventSource.VOICE, {"error": str(exc)})
            return self._result(False, f"microphone failed to start: {exc}")
        self.bus.publish(EventType.VOICE_LISTENING, EventSource.VOICE, {"mode": self.rs.voice.listen_mode, "device": self.rs.voice.input_device})
        return self._result(True, f"listening ({self.rs.voice.listen_mode})")

    async def stop_voice(self) -> CommandResult:
        if self.voice is not None:
            await asyncio.to_thread(self.voice.stop)
        self.bus.publish(EventType.VOICE_STOPPED, EventSource.VOICE, {})
        return self._result(True, "voice stopped")

    def _stt_busy(self, busy: bool) -> None:
        """Called from the STT thread: the language model worker yields the GPU meanwhile."""
        llm = self.companion.llm
        if self.settings.stt_gpu_priority and llm is not None and hasattr(llm, "speech_recognition"):
            llm.speech_recognition(busy)

    def _voice_emit(self, kind: str, data: dict) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        if kind == "utterance":
            # The user stopped talking about one transcription ago: the start of the response clock.
            heard = time.monotonic() - float(data.get("latency_ms") or 0.0) / 1000.0
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(
                    self.handle_utterance(
                        data["text"],
                        source="voice",
                        stt_confidence=data.get("confidence", 0.0),
                        stt_ms=data.get("latency_ms"),
                        during_playback=bool(data.get("during_playback")),
                        heard_mono=heard,
                    )
                )
            )
        elif kind == "speech_started":
            loop.call_soon_threadsafe(lambda: self.bus.publish(EventType.VOICE_SPEECH_STARTED, EventSource.VOICE, data))
        elif kind == "speech_ended":
            loop.call_soon_threadsafe(lambda: self.bus.publish(EventType.VOICE_SPEECH_ENDED, EventSource.VOICE, data))
        elif kind == "error":
            loop.call_soon_threadsafe(lambda: self.bus.publish(EventType.VOICE_ERROR, EventSource.VOICE, data))
        elif kind == "rejected":
            loop.call_soon_threadsafe(lambda: self.bus.publish(EventType.VOICE_COMMAND_REJECTED, EventSource.VOICE, data))
        elif kind == "recovered":
            loop.call_soon_threadsafe(lambda: self.bus.publish(EventType.VOICE_LISTENING, EventSource.VOICE, {"mode": self.rs.voice.listen_mode, **data}))

    async def handle_utterance(
        self,
        text: str,
        source: str = "typed",
        stt_confidence: float = 1.0,
        stt_ms: float | None = None,
        during_playback: bool = False,
        heard_mono: float | None = None,
    ) -> CommandResult:
        """One voice turn: recognised text -> intent -> gate -> action -> spoken reply. Every
        turn is recorded (``voice_turns``) with what was done and, once the reply starts to
        play, how long after the user stopped talking."""
        turn = {
            "id": uuid.uuid4().hex[:8],
            "at": time.time(),
            "source": source,
            "text": (text or "").strip()[:300],
            "stt_confidence": round(stt_confidence, 3),
            "stt_ms": stt_ms,
            "during_playback": during_playback,
            "heard_mono": heard_mono if heard_mono is not None else time.monotonic(),
            "intent": None,
            "replies": [],
            "reply_source": None,
            "reply_to_audio_ms": None,
        }
        self._turn_index[turn["id"]] = turn
        if len(self._turn_index) > 60:
            for old in list(self._turn_index)[:-30]:
                self._turn_index.pop(old, None)
        token = _TURN.set(turn)
        t0 = time.monotonic()
        try:
            result = await self._handle_utterance(text, source, stt_confidence, stt_ms, during_playback)
        finally:
            _TURN.reset(token)
        turn.update(ok=result.ok, action=result.message, reply=result.reply, handled_ms=round((time.monotonic() - t0) * 1000, 1))
        self.voice_turns.append(turn)
        self.bus.publish(EventType.VOICE_TURN, EventSource.VOICE, self._turn_view(turn))
        return result

    @staticmethod
    def _turn_view(turn: dict) -> dict:
        return {k: v for k, v in turn.items() if k != "heard_mono"}

    async def _handle_utterance(
        self, text: str, source: str, stt_confidence: float, stt_ms: float | None, during_playback: bool
    ) -> CommandResult:
        t0 = time.monotonic()
        text = (text or "").strip()[:300]
        intent = self.classifier.classify(text, stt_confidence)
        turn = _TURN.get()
        if turn is not None:
            turn["intent"] = intent.to_payload()
        if source == "voice":
            # The robot's own voice coming back through the mic: while it talks, or just after (a
            # Bluetooth speaker keeps sounding after the stream ends). Echo is never answered;
            # words the user added on top of it are taken on their own.
            from .voice.intents import BARGE_IN_INTENTS

            echo, residual = self.speaker.echo_split(text, within_s=3.0)
            if echo >= (0.6 if during_playback else 0.8):
                rest = self.classifier.classify(residual, stt_confidence) if residual else None
                allowed = rest is not None and rest.name != IntentName.UNKNOWN and (
                    not during_playback or rest.name in BARGE_IN_INTENTS or rest.stop_word
                )
                if not allowed:
                    self.bus.publish(
                        EventType.VOICE_COMMAND_REJECTED,
                        EventSource.VOICE,
                        {"text": text, "reason": "echo of the robot's own speech", "echo": round(echo, 2), "during_playback": during_playback},
                    )
                    return self._result(False, "ignored: echo of the robot's own speech")
                text, intent = residual, rest
                if turn is not None:
                    turn["intent"] = intent.to_payload()
                    turn["echo_removed"] = round(echo, 2)
        if during_playback:
            # Heard while the robot was talking (the user talking over it): only barge-in commands count.
            from .voice.intents import BARGE_IN_INTENTS

            if intent.name not in BARGE_IN_INTENTS and not intent.stop_word:
                self.bus.publish(
                    EventType.VOICE_COMMAND_REJECTED,
                    EventSource.VOICE,
                    {"text": text, "reason": "said while the robot was talking: only stop, mute, release, pause, 'be serious' and 'stop roasting me' are taken"},
                )
                return self._result(False, "ignored: robot was talking")
        self.last_utterance = {"text": text, "source": source, "confidence": round(stt_confidence, 3), "at": time.time(), "intent": intent.name.value}
        if source == "voice" and stt_ms is not None:
            self.perf.add("stt_ms", stt_ms)
        # Typed commands go through exactly the same pipeline; the source is recorded.
        self.bus.publish(
            EventType.VOICE_TRANSCRIBED,
            EventSource.VOICE if source == "voice" else EventSource.OPERATOR,
            {"text": text, "source": source, "latency_ms": stt_ms},
            confidence=stt_confidence,
        )

        # Fail-safe first: a stop word halts motion before any interpretation, then speech.
        if intent.stop_word and (self.safety.active is not None or (self.transport and self.transport.is_moving)):
            await self.safety.stop(f"stop word heard ({source})", source=source)
        if intent.stop_word:
            self.speaker.interrupt("stop word")

        gate, reason = self._utterance_gate(intent, source)
        if source == "voice" and self.voice_trials.block is not None:
            rec = self.voice_trials.record(text=text, stt_confidence=stt_confidence, stt_ms=stt_ms, intent=intent, rejected_reason=reason)
            if rec is not None:
                self.bus.publish(EventType.EXPERIMENT_VOICE_TRIAL, EventSource.EXPERIMENT, rec)
            if not self.voice_trials.block["execute"]:
                outcome = rec["outcome"] if rec else "between prompts, not scored"
                return self._result(bool(rec) and outcome in ("correct", "correct_reject"), f"voice trial (dry run): {outcome}", trial=rec)

        if gate == "wake_word":
            self.bus.publish(EventType.VOICE_COMMAND_REJECTED, EventSource.VOICE, {"text": text, "reason": reason})
            return self._result(False, "ignored: wake word not heard")
        route = self.companion.route(intent, text, gate, source)
        if turn is not None:
            turn["route"] = route
        if route == "converse":
            # Milestone 5: the local model interprets the sentence and writes the reply; its
            # action request is validated and executed like any other (companion.py).
            if self.sessions.active:
                self.counters.voice_commands += 1
            result = await self.companion.converse(text, source, intent, turn, gate=gate)
            self.perf.add("command_to_reply_ms", (time.monotonic() - t0) * 1000)
            result.data["intent"] = intent.to_payload()
            return result
        if gate in ("unknown", "low_confidence"):
            self.bus.publish(EventType.VOICE_COMMAND_REJECTED, EventSource.VOICE, {**intent.to_payload(), "reason": reason})
            if source == "voice" and self.rs.voice.listen_mode != "push_to_talk":
                # Hands-free: the mic also hears the room (measured: nearby conversation is
                # transcribed). Unrecognised speech gets no spoken answer, or it would talk to everyone.
                return self._result(False, f"{reason} (hands-free: not answered)", intent=intent.to_payload())
            reply = None
            if gate == "unknown" and self.llm is not None and intent.confidence >= 0.0 and text:
                reply = await self._llm_reply(text)
            if reply is None:
                reply = self._say("unknown" if gate == "unknown" else "low_confidence")
            return self._result(False, reason, reply, intent=intent.to_payload())
        if gate in ("motion_confidence", "conflict"):
            self.bus.publish(EventType.VOICE_COMMAND_REJECTED, EventSource.VOICE, {**intent.to_payload(), "reason": reason})
            reply = self._say("low_confidence")
            return self._result(False, reason or "motion command rejected", reply, intent=intent.to_payload())

        self.bus.publish(EventType.VOICE_COMMAND_RECOGNIZED, EventSource.VOICE, {**intent.to_payload(), "source": source}, confidence=intent.confidence)
        if self.sessions.active:
            self.counters.voice_commands += 1
        result = await self._dispatch_intent(intent, source)
        self.perf.add("command_to_reply_ms", (time.monotonic() - t0) * 1000)
        result.data["intent"] = intent.to_payload()
        return result

    def _utterance_gate(self, intent, source: str) -> tuple[str | None, str | None]:
        """Whether an utterance may act: (None, None) or (gate, human-readable reason)."""
        voice_cfg = self.rs.voice
        if source == "voice" and voice_cfg.listen_mode == "wake_word":
            in_follow_up = time.monotonic() < self._follow_up_until
            if not intent.addressed and not in_follow_up and not intent.safety_critical:
                return "wake_word", "not addressed to LOCKED IN"
        if intent.name == IntentName.UNKNOWN:
            return "unknown", "unrecognised"
        floor = 0.2 if intent.name == IntentName.STOP_MOTION else voice_cfg.min_command_confidence
        if intent.confidence < floor:
            return "low_confidence", f"low confidence {intent.confidence:.2f} < {floor:.2f}"
        if intent.moves_robot and intent.conflict:
            return "conflict", f"conflicting movement command ({intent.conflict}): nothing moved"
        if intent.moves_robot and intent.confidence < voice_cfg.min_motion_command_confidence:
            return "motion_confidence", f"motion command needs confidence >= {voice_cfg.min_motion_command_confidence}"
        return None, None

    async def _dispatch_intent(self, intent, origin: str) -> CommandResult:
        n = intent.name
        s = intent.slots
        if n == IntentName.EMERGENCY_STOP:
            return await self.emergency_stop(origin=origin, reason=f"voice: {intent.text}")
        if n == IntentName.STOP_MOTION:
            if self._demo_task is not None and not self._demo_task.done():
                self._demo_task.cancel()  # a scripted demo is a pending action too
            r = await self.stop_robot(origin=origin, speak=True)
            self.safety.suppress_motion(STOP_SUPPRESS_S, f"'{intent.text or 'stop'}' ({origin})")
            r.message = f"{r.message}; new motion refused for {STOP_SUPPRESS_S:.0f} s"
            r.data["hardware_stop"] = self.hardware_stop_state()
            return r
        if n == IntentName.RELEASE:
            return await self.release(origin=origin)
        if n == IntentName.PAUSE:
            return await self.pause_session(origin=origin)
        if n == IntentName.RESUME:
            if not self.sessions.active:
                return await self.start_session(origin=origin)
            return await self.resume_session(origin=origin)
        if n == IntentName.END_SESSION:
            return await self.end_session(reason="user", origin=origin)
        if n == IntentName.START_SESSION:
            if self.sessions.active and self.sessions.paused:
                return await self.resume_session(origin=origin)
            r = await self.start_session(
                task=s.get("task"),
                duration_min=s.get("duration_min"),
                target_units=s.get("target_units"),
                unit_label=s.get("unit_label"),
                origin=origin,
            )
            if r.ok and origin == "voice":
                r.data["monitoring"] = await self._ensure_monitoring()
            return r
        if n == IntentName.PROGRESS:
            return await self.add_progress(int(s.get("units", 1)), origin=origin, unit_label=s.get("unit_label"))
        if n == IntentName.TIME_LEFT:
            return self._result(True, "time left", self._say("time_left"))
        if n == IntentName.STATUS:
            return self._result(True, "status", self._say("status"))
        if n == IntentName.NEXT_STEP:
            return self._result(True, "next step", self._say("next_step"))
        if n == IntentName.HARDER:
            order = ["gentle", "standard", "strict"]
            idx = min(len(order) - 1, order.index(self.rs.coach.intensity) + 1)
            patch = {"coach": {"intensity": order[idx], "tone": "normal"}}
            if idx == len(order) - 1 and self.rs.coach.personality == "chill":
                patch["coach"]["personality"] = "roast"
            self.update_settings(patch, publish=True)
            self.adaptation.record_user_request("harder")
            return self._result(True, f"intensity {order[idx]}", self._say("harder"))
        if n == IntentName.TONE_DOWN:
            self.update_settings({"coach": {"tone": "gentle", "intensity": "gentle"}}, publish=True)
            self.adaptation.record_user_request("easier")
            return self._result(True, "toned down: no roasting, no swearing", self._say("tone_down"))
        if n == IntentName.SERIOUS:
            self.update_settings({"coach": {"tone": "serious"}}, publish=True)
            return self._result(True, "serious tone", self._say("serious_ack"))
        if n == IntentName.RESET_TONE:
            self.update_settings({"coach": {"tone": "normal"}}, publish=True)
            return self._result(True, "normal tone", self._say("tone_reset"))
        if n == IntentName.MUTE:
            self.set_muted(True)
            self.bus.publish(EventType.VOICE_RESPONSE_GENERATED, EventSource.VOICE, {"category": "mute", "text": "Going quiet.", "audible": False})
            return self._result(True, "muted: speech stopped and queue cleared", "Going quiet.")
        if n == IntentName.UNMUTE:
            self.set_muted(False)
            self.context.end_quiet()
            return self._result(True, "unmuted", self._say("unmute"))
        if n == IntentName.SET_PERSONALITY:
            mode = s.get("personality", "roast")
            patch: dict = {"coach": {"personality": mode, "tone": "normal"}}
            if mode == "unhinged":
                patch["coach"]["unhinged_opt_in"] = True  # saying "unhinged mode" is the opt-in
            self.update_settings(patch, publish=True)
            if s.get("roast_me"):
                return self._result(True, "personality roast (asked to be roasted)", self._say("roast_me"))
            return self._result(True, f"personality {mode}", self._say("personality_set", new_personality=MODE_LABELS.get(mode, mode)))
        if n == IntentName.SET_PROFANITY:
            level = s.get("profanity", "off")
            self.update_settings({"coach": {"profanity": level}}, publish=True)
            return self._result(True, f"profanity {level}", self._say(f"profanity_{level}"))
        if n == IntentName.HELP:
            self._supportive_until = time.monotonic() + SUPPORTIVE_S
            reply = None
            if self.llm is not None:
                reply = await self._llm_reply(intent.text or "I need help", purpose="help")
            if reply is None:
                reply = self._say("help")
            return self._result(True, "help (no roasting for 5 minutes)", reply)
        if n == IntentName.RETURN_ROBOT:
            r = await self.return_home(origin=origin)
            r.reply = self._say("return_ack") if r.ok else self._say("return_refused", reason=r.message)
            return r
        if n == IntentName.CAMERA_OFF:
            r = await self.stop_vision()
            self.context.observe("camera_off", "the user turned the camera off: not watching")
            return self._result(r.ok, f"camera off by voice ({r.message})", self._say("camera_off_ack"))
        if n == IntentName.CAMERA_ON:
            if not self.enable_vision:
                return self._result(False, "no camera in this process", self._say("cant_do"))
            asyncio.create_task(self.start_vision(), name="camera-on")
            return self._result(True, "camera starting by voice", self._say("camera_on_ack"))
        if n == IntentName.MIC_OFF:
            reply = self._say("mic_off_ack", priority=PRIORITY_REPLY)
            # After the line is queued: the microphone stops; the dashboard turns it back on.
            asyncio.create_task(self.stop_voice(), name="mic-off")
            return self._result(True, "microphone off by voice (turn it back on in the dashboard)", reply)
        if n == IntentName.GREETING:
            return self._result(True, "greeting", self._say("greeting"))
        if n == IntentName.THANKS:
            return self._result(True, "thanks", self._say("thanks"))
        if n == IntentName.ABOUT:
            return self._result(True, "about", self._say("about"))
        return self._result(False, "unhandled intent", self._say("unknown"))

    async def _ensure_monitoring(self) -> str:
        """"Lock me in" also turns the robot's eyes on, if this process has vision."""
        if not self.enable_vision:
            return "no camera in this process"
        if self.vision_status.get("running"):
            return "camera already on"
        asyncio.create_task(self.start_vision(), name="monitoring-on")
        return "camera starting"

    async def _llm_reply(self, text: str, purpose: str = "reply") -> str | None:
        """Optional LLM reply. Text only: it is spoken, never parsed for actions, and it is
        dropped if it cites numbers the session does not have or breaks the style rules."""
        from .voice.content_guard import check as guard_check
        from .voice.conversation import grounding_violations

        facts = self.facts()
        style = self.style()
        if purpose == "help":
            style = Style(mode=style.mode, profanity=style.profanity, tone=style.tone, unhinged_opt_in=style.unhinged_opt_in, supportive=True)
        try:
            t0 = time.monotonic()
            reply = await asyncio.wait_for(self.llm.reply(text, facts, style), timeout=self.settings.llm_timeout_s)
            self.perf.add("llm_ms", (time.monotonic() - t0) * 1000)
        except Exception as exc:
            log.info("LLM reply unavailable: %s", exc)
            return None
        if not reply:
            return None
        sentences = re.split(r"(?<=[.!?])\s+", reply.strip())
        reply = " ".join(sentences[:2])
        bad = grounding_violations(reply, facts)
        if bad:
            self.bus.publish(EventType.VOICE_COMMAND_REJECTED, EventSource.VOICE, {"reason": "LLM reply cited numbers not in session state", "numbers": bad, "reply": reply})
            return None
        verdict = guard_check(reply, max_profanity=style.max_profanity, allow_insults=style.insults_allowed)
        if not verdict.ok:
            self.bus.publish(EventType.VOICE_COMMAND_REJECTED, EventSource.VOICE, {"reason": f"LLM reply broke the style rules: {verdict.reason}", "reply": reply})
            return None
        self.speaker.say(reply, priority=PRIORITY_REPLY, category="llm")
        self.last_reply = {"text": reply, "category": "llm", "at": time.time()}
        self.bus.publish(EventType.VOICE_RESPONSE_GENERATED, EventSource.VOICE, {"category": "llm", "text": reply, "grounded": True})
        return reply

    def set_muted(self, muted: bool) -> None:
        if muted:
            self.speaker.stop_all("mute")
        self.speaker.muted = muted
        self.update_settings({"voice": {"muted": muted}}, publish=True)


    # ================================================================ voice + personality
    def _apply_voice_profile(self, patch: dict) -> None:
        """React to voice / personality changes: new line pool, new local voice, new clips."""
        if not ({"voice_profile", "coach", "active_preset"} & set(patch)):
            return
        router = self.speaker.engine if isinstance(self.speaker.engine, VoiceRouter) else None
        system = router.system if router else None
        p = self.rs.voice_profile
        if isinstance(system, CachedSystemTTS):
            voice = p.system_voice or self.settings.tts_voice
            if (system.voice, system.rate_wpm) != (voice, p.system_rate_wpm):
                system.voice, system.rate_wpm = voice, p.system_rate_wpm
                system._pcm.clear()
        if self.kokoro is not None:
            self.kokoro.set_voice(p.kokoro_voice, p.kokoro_speed)
        if self._loop is not None:
            self._schedule_prewarm()
            if self.kokoro is not None and self.kokoro.available() and (self._kokoro_task is None or self._kokoro_task.done()):
                self._kokoro_task = asyncio.create_task(asyncio.to_thread(self.kokoro.prewarm, self._kokoro_lines()), name="kokoro-prewarm")

    def personality_snapshot(self) -> dict:
        st = self.style()
        c = self.rs.coach
        return {
            "mode": c.personality,
            "effective_mode": st.effective_mode,
            "label": MODE_LABELS.get(st.effective_mode, st.effective_mode),
            "unhinged_opt_in": c.unhinged_opt_in,
            "profanity": c.profanity,
            "max_profanity": ["off", "mild", "strong"][st.max_profanity],
            "tone": c.tone,
            "supportive_s_left": max(0.0, round(self._supportive_until - time.monotonic(), 1)),
            "pool": st.pool,
            "insults_allowed": st.insults_allowed,
        }

    def set_personality(self, mode: str | None = None, profanity: str | None = None, tone: str | None = None, unhinged_opt_in: bool | None = None) -> CommandResult:
        coach: dict = {}
        if mode is not None:
            coach["personality"] = mode
        if profanity is not None:
            coach["profanity"] = profanity
        if tone is not None:
            coach["tone"] = tone
        if unhinged_opt_in is not None:
            coach["unhinged_opt_in"] = unhinged_opt_in
        if not coach:
            return self._result(False, "nothing to change")
        self.update_settings({"coach": coach, "active_preset": None} if mode or profanity else {"coach": coach}, publish=True)
        return self._result(True, "personality updated", personality=self.personality_snapshot())

    def presets(self) -> list[dict]:
        from .config import builtin_presets

        return [p.model_dump(mode="json") for p in builtin_presets() + list(self.rs.presets)]

    def apply_preset(self, preset_id: str) -> CommandResult:
        from .config import builtin_presets

        preset = next((p for p in builtin_presets() + list(self.rs.presets) if p.id == preset_id), None)
        if preset is None:
            return self._result(False, f"no preset {preset_id!r}")
        patch: dict = {
            "voice_profile": preset.profile.model_dump(mode="json"),
            "coach": {"personality": preset.personality, "profanity": preset.profanity, "tone": "normal"},
            "active_preset": preset.id,
        }
        if preset.output_device is not None:
            patch["voice"] = {"output_device": preset.output_device}
        self.update_settings(patch, publish=True)
        note = ""
        if preset.personality == "unhinged" and not self.rs.coach.unhinged_opt_in:
            note = " (Unhinged needs your opt-in; speaking as Roast until then)"
        return self._result(True, f"preset {preset.name} applied{note}", personality=self.personality_snapshot())

    def save_preset(self, name: str, preset_id: str | None = None) -> CommandResult:
        import re as _re

        from .config import VoicePreset, builtin_presets

        pid = preset_id or _re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "preset"
        if any(p.id == pid for p in builtin_presets()):
            return self._result(False, f"{pid!r} is a built-in preset; choose another name")
        preset = VoicePreset(
            id=pid,
            name=name.strip()[:40],
            profile=self.rs.voice_profile,
            personality=self.rs.coach.personality,
            profanity=self.rs.coach.profanity,
            output_device=self.rs.voice.output_device,
        )
        others = [p.model_dump(mode="json") for p in self.rs.presets if p.id != pid]
        self.update_settings({"presets": others + [preset.model_dump(mode="json")], "active_preset": pid}, publish=True)
        return self._result(True, f"saved preset {preset.name}", preset=preset.model_dump(mode="json"))

    def delete_preset(self, preset_id: str) -> CommandResult:
        keep = [p.model_dump(mode="json") for p in self.rs.presets if p.id != preset_id]
        if len(keep) == len(self.rs.presets):
            return self._result(False, "only your own presets can be deleted")
        patch: dict = {"presets": keep}
        if self.rs.active_preset == preset_id:
            patch["active_preset"] = None
        self.update_settings(patch, publish=True)
        return self._result(True, "preset deleted")

    def update_voice_profile(self, patch: dict) -> CommandResult:
        self.update_settings({"voice_profile": patch, "active_preset": None}, publish=True)
        return self._result(True, "voice updated", profile=self.rs.voice_profile.model_dump(mode="json"))

    def _cache_keys_for(self, styles=None, lines=None) -> list[tuple]:
        from .voice.personality import cacheable_lines
        from .voice.tts import SpeechRequest
        from .voice.usage import utf8_bytes

        p = self.rs.voice_profile
        out = []
        seen: set[str] = set()
        for category, text, cue in lines if lines is not None else cacheable_lines(styles):
            tts_text = self.fish.text_for(SpeechRequest(text, cue), p)
            key = self.fish.cache_key(tts_text, p)
            if key in seen:  # the same line reused in another category is one clip, counted once
                continue
            seen.add(key)
            out.append((category, text, tts_text, key, utf8_bytes(tts_text)))
        return out

    def _cache_styles(self, scope: str) -> list[Style]:
        st = self.style()
        safety = Style(tone="serious")
        if scope == "all_modes":
            return [Style(mode=m, profanity=st.profanity, unhinged_opt_in=True) for m in ("chill", "roast", "unhinged", "judge_demo")] + [safety]
        return [Style(mode=st.mode, profanity=st.profanity, unhinged_opt_in=st.unhinged_opt_in), safety]

    def voice_config(self) -> dict:
        from .voice.fish_audio import MODELS
        from .voice.personality import voice_pack

        p = self.rs.voice_profile
        lines = self._cache_keys_for(self._cache_styles("current")) if p.reference_id else []
        cached = sum(1 for *_x, key, _n in lines if self.fish.cache.has(key))
        pack = self._cache_keys_for(lines=voice_pack()) if p.reference_id else []
        pack_cached = sum(1 for *_x, key, _n in pack if self.fish.cache.has(key))
        return {
            "profile": p.model_dump(mode="json"),
            "active_preset": self.rs.active_preset,
            "presets": self.presets(),
            "personality": self.personality_snapshot(),
            "fish": {
                **self.fish.describe(),
                "models": [{"id": m.id, "label": m.label, "usd_per_million_bytes": m.usd_per_million_bytes, "bracket_cues": m.bracket_cues, "note": m.note} for m in MODELS],
                "daily_live_bytes": p.daily_live_bytes,
                "live_bytes_today": self.fish.usage.used_today("live"),
            },
            "cache": {
                "lines": len(lines),
                "cached": cached,
                "missing": len(lines) - cached,
                "pack": {"lines": len(pack), "cached": pack_cached},
                "job": self.cache_job_state,
            },
            "speaking_now": self.speaker.snapshot().get("current"),
        }

    async def check_cloud_voice(self) -> dict:
        return await self.fish.check(self.rs.voice_profile)

    async def cloud_voices(self, mine: bool = True, query: str | None = None) -> list[dict]:
        return await self.fish.voices(mine=mine, query=query)

    async def test_voice(self, live: bool = False, text: str | None = None) -> CommandResult:
        """Speak a test line. ``live`` explicitly asks Fish Audio for a fresh clip (the click is
        the approval for that one request); otherwise the normal cache / local route is used."""
        from .voice.fish_audio import FishAudioError
        from .voice.tts import SpeechRequest

        r = self.composer.render("voice_test", self.facts(), self.style())
        line = (text or r.text).strip()[:200]
        p = self.rs.voice_profile
        if live:
            if p.provider != "fish_audio":
                return self._result(False, "select Fish Audio as the provider first")
            if not self.fish.configured:
                return self._result(False, "add FISH_AUDIO_API_KEY to .env and restart the backend")
            if not p.reference_id:
                return self._result(False, "paste a Fish Audio voice id first")
            tts_text = self.fish.text_for(SpeechRequest(line, r.cue if text is None else None), p)
            if self.fish.cached(tts_text, p) is None:
                try:
                    await self.fish.generate(tts_text, p, persist=text is None, purpose="test", category="voice_test")
                except FishAudioError as exc:
                    return self._result(False, exc.message, status=self.fish.status)
        self.speaker.say(line, priority=PRIORITY_REPLY, category="voice_test", cue=r.cue if text is None else None, cacheable=text is None)
        return self._result(True, "speaking test line", text=line)

    def speak_sample(self, category: str = "reach_first") -> CommandResult:
        from .voice.personality import LIBRARY

        if category not in LIBRARY:
            return self._result(False, f"unknown sample {category!r}")
        facts = self.facts()
        if not facts.session_active:  # sample lines need some numbers; make them clearly fictional
            done = 5 if category in ("target_reached", "end_completed") else 2
            facts = SessionFacts(session_active=True, task="the sample task", elapsed_s=120, remaining_s=1380, planned_s=1500, completed_units=done, target_units=5, unit_label="problems", reaches_confirmed=3)
        r = self.composer.render(category, facts, self.style(), prefer=self._cached_line_check())
        self.speaker.say(r.text, priority=PRIORITY_REPLY, category=f"sample:{category}", cue=r.cue, cacheable=r.cacheable)
        return self._result(True, "speaking sample", text=r.text, pool=r.pool)

    def plan_cache(self, scope: str = "current") -> CommandResult:
        from .voice.fish_audio import estimate_usd

        p = self.rs.voice_profile
        if p.provider != "fish_audio" or not p.reference_id:
            return self._result(False, "select Fish Audio and a voice id first")
        if scope in ("pack", "pack_chill"):
            from .voice.personality import voice_pack

            items = self._cache_keys_for(lines=voice_pack(include_chill=scope == "pack_chill"))
        else:
            items = self._cache_keys_for(self._cache_styles(scope))
        missing = [it for it in items if not self.fish.cache.has(it[3])]
        nbytes = sum(it[4] for it in missing)
        plan_id = uuid.uuid4().hex[:10]
        self._cache_plans = {plan_id: {"items": missing, "profile": p.model_copy(deep=True), "bytes": nbytes, "created": time.monotonic()}}
        return self._result(
            True,
            f"{len(missing)} of {len(items)} lines need generating ({nbytes} bytes of text)",
            plan={
                "plan_id": plan_id,
                "scope": scope,
                "lines": len(items),
                "missing": len(missing),
                "bytes": nbytes,
                "estimated_usd": estimate_usd(nbytes, p.model),
                "model": p.model,
                "voice": p.reference_id,
                "configured": self.fish.configured,
                "sample": [it[2] for it in missing[:8]],
            },
        )

    def start_cache_job(self, plan_id: str, confirm: bool, max_bytes: int) -> CommandResult:
        from .voice.fish_audio import CacheJob, CachePlanItem

        plan = self._cache_plans.get(plan_id)
        if plan is None or time.monotonic() - plan["created"] > 900:
            return self._result(False, "plan expired; make a new one")
        if not confirm:
            return self._result(False, "generation needs your explicit confirmation")
        if max_bytes < plan["bytes"]:
            return self._result(False, f"the plan needs {plan['bytes']} bytes; you approved {max_bytes}")
        if self.settings.offline:
            return self._result(False, "offline mode: generate the voice cache while this Mac has internet (make hardware runs offline)")
        if not self.fish.configured:
            return self._result(False, "add FISH_AUDIO_API_KEY to .env and restart the backend")
        if self._cache_job is not None and self._cache_job.task is not None and not self._cache_job.task.done():
            return self._result(False, "a cache job is already running")
        items = [CachePlanItem(c, t, tt, k, n) for c, t, tt, k, n in plan["items"]]

        def progress(state: dict) -> None:
            self.cache_job_state = state

        job = CacheJob(self.fish, items, plan["profile"], on_progress=progress)
        self.cache_job_state = dict(job.state)
        job.task = asyncio.create_task(job.run(), name="fish-cache-job")
        self._cache_job = job
        self._cache_plans.pop(plan_id, None)
        return self._result(True, f"generating {len(items)} clips", job=self.cache_job_state)

    def cancel_cache_job(self) -> CommandResult:
        job = self._cache_job
        if job is None or job.task is None or job.task.done():
            return self._result(False, "no cache job running")
        job.task.cancel()
        return self._result(True, "cache job cancelled")

    def ptt(self, active: bool) -> None:
        """Push-to-talk: pressing it cuts answers and jokes so the user can talk over the robot."""
        if active:
            self.speaker.barge_in()

    # ================================================================ settings
    def update_settings(self, patch: dict, publish: bool = True) -> RuntimeSettings:
        self.rs = merge_settings(self.rs, patch)
        self.safety.config.autonomous_cooldown_s = self.rs.motion.autonomous_cooldown_s
        if self.rs.voice.muted and not self.speaker.muted:
            self.speaker.stop_all("mute")
        self.speaker.muted = self.rs.voice.muted
        if isinstance(self.speaker.engine, SystemTTS):
            self.speaker.engine.output_device = self.rs.voice.output_device
        self._apply_voice_profile(patch)
        self._apply_vision_tuning()
        if self.voice is not None and "voice" in patch:
            self.voice.set_mode(self.rs.voice.listen_mode)
        if self.vision is not None and self.vision_status.get("running") and ({"camera_mount", "zeus_camera_flip"} & set(patch)):
            with contextlib.suppress(RuntimeError):  # no running loop: nothing to re-apply now
                asyncio.get_running_loop().create_task(self.start_vision(), name="camera-reapply")  # mount/flip apply at start
        self._persist()
        if publish:
            self.bus.publish(EventType.SETTINGS_CHANGED, EventSource.OPERATOR, {"patch": patch})
        return self.rs

    def _persist(self) -> None:
        if self._settings_store is not None:
            try:
                self._settings_store.save(self.rs)
            except Exception:
                log.exception("saving runtime settings failed")

    # ================================================================ mode label
    def mode(self) -> dict:
        """Headline mode. With the Zeus profile selected it never reads as simulation: an
        unreachable robot is ZEUS DISCONNECTED, not a fallback to the simulator."""
        t = self.transport
        robot_real = bool(t and t.connected and not t.simulated)
        robot_sim = bool(t and t.connected and t.simulated)
        cam = self.vision_live
        camera_label = self.vision_status.get("source_label") if cam else None
        profile = "zeus" if self.driver == "zeus" else "sim"
        motion = "simulated"
        if profile == "zeus":
            gate = t.get_status().capabilities if t is not None else None
            motion = "permitted" if gate is not None and gate.motion_permitted and robot_real else "locked"
            if robot_real:
                robot_state = "connected"
                code = "A" if cam else "A-"
                label = "ZEUS · " + ("MOTION LOCKED" if motion == "locked" else f"MOTION {self.real_motion_mode.upper()}")
            else:
                link = self.robot_link.get("state")
                robot_state = "connecting" if link == "connecting" and self.robot_link.get("attempt", 0) <= 1 else "disconnected"
                code, label = "Z", "ZEUS CONNECTING" if robot_state == "connecting" else "ZEUS DISCONNECTED"
        else:
            robot_state = "connected" if robot_sim else "disconnected"
            if robot_sim and cam:
                code, label = "B", "LIVE PERCEPTION · SIMULATED ROBOT"
            elif robot_sim:
                code, label = "C", "FULL SIMULATION"
            else:
                code, label = "-", "ROBOT OFFLINE"
        return {
            "code": code,
            "label": label,
            "robot_profile": profile,
            "robot_state": robot_state,
            "robot_simulated": profile == "sim",
            "robot_connected": robot_real or robot_sim,
            "camera_live": cam,
            "camera_label": camera_label,
            "physical_motion": motion,
            "offline": self.settings.offline,
        }

    def _publish_mode(self) -> None:
        self.bus.publish(EventType.SYSTEM_MODE_CHANGED, EventSource.SYSTEM, self.mode())

    # ================================================================ experiments
    def start_trial(self, kind: str, note: str = "") -> dict:
        if kind not in ("reach", "non_reach"):
            raise ValueError("trial kind must be 'reach' or 'non_reach'")
        if self.active_trial is not None:
            self.end_trial()
        self.active_trial = {"kind": kind, "note": note, "mono": time.monotonic(), "started_at": time.time(), "confirmations": []}
        self.bus.publish(EventType.EXPERIMENT_TRIAL_STARTED, EventSource.EXPERIMENT, {"kind": kind, "note": note})
        return self.active_trial

    def end_trial(self) -> dict | None:
        t = self.active_trial
        if t is None:
            return None
        self.active_trial = None
        t["duration_s"] = round(time.monotonic() - t["mono"], 3)
        t["detected"] = bool(t["confirmations"])
        t["outcome"] = {
            ("reach", True): "TP",
            ("reach", False): "FN",
            ("non_reach", True): "FP",
            ("non_reach", False): "TN",
        }[(t["kind"], t["detected"])]
        t.pop("mono", None)
        self.trials.append(t)
        path = self.settings.data_dir / "experiments"
        path.mkdir(parents=True, exist_ok=True)
        with open(path / f"trials-{time.strftime('%Y%m%d')}.jsonl", "a") as fh:
            import json

            fh.write(json.dumps(t) + "\n")
        self.bus.publish(EventType.EXPERIMENT_TRIAL_ENDED, EventSource.EXPERIMENT, t)
        return t

    def trial_summary(self) -> dict:
        tp = sum(1 for t in self.trials if t["outcome"] == "TP")
        fp = sum(1 for t in self.trials if t["outcome"] == "FP")
        fn = sum(1 for t in self.trials if t["outcome"] == "FN")
        tn = sum(1 for t in self.trials if t["outcome"] == "TN")
        return {
            "trials": len(self.trials),
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "tn": tn,
            "precision": round(tp / (tp + fp), 3) if tp + fp else None,
            "recall": round(tp / (tp + fn), 3) if tp + fn else None,
            "false_positive_rate": round(fp / (fp + tn), 3) if fp + tn else None,
            "first_confirmation_s": [t["confirmations"][0] for t in self.trials if t["confirmations"]],
            "active": self.active_trial is not None,
            "items": self.trials[-50:],
        }

    def label_false_trigger(self) -> None:
        self.counters.false_trigger_labels += 1
        self.adaptation.record_false_trigger()
        self.bus.publish(EventType.EXPERIMENT_LABEL, EventSource.OPERATOR, {"label": "false_trigger"})

    # ================================================================ loop
    def _on_bus_event(self, event) -> None:
        et = event.event_type
        if et == EventType.VOICE_TTS_FINISHED:
            self._note_reply_audio(event.payload)
        elif et in _ACTION_EVENTS:
            self._note_action(event)
        self._on_motion_event(event)

    def _note_reply_audio(self, p: dict) -> None:
        tr = p.get("trace") or {}
        ms = p.get("to_audio_ms")
        if not tr.get("kind") or ms is None:
            return
        self.perf.add(f"voice_{tr['kind']}_to_audio_ms", ms)
        turn = self._turn_index.get(tr.get("id") or "")
        if turn is not None and turn.get("reply_to_audio_ms") is None:  # the first line of the reply
            turn["reply_to_audio_ms"] = ms
            turn["reply_source"] = p.get("source_label")
            turn["output_latency_ms"] = p.get("output_latency_ms")

    def _note_action(self, event) -> None:
        p = event.payload
        et = event.event_type
        physical = not event.simulated and bool(self.transport is not None and not self.transport.simulated)
        if et == EventType.ROBOT_MOTION_REQUESTED:
            a = {
                "id": p.get("request_id"), "at": event.timestamp, "kind": "motion", "what": p.get("primitive"),
                "origin": p.get("origin"), "profile": p.get("profile"), "status": "requested", "physical": physical,
                "detail": p.get("reason") or "",
            }
            self._action_index[a["id"]] = a
            self.actions.append(a)
            if len(self._action_index) > 60:
                for old in list(self._action_index)[:-30]:
                    self._action_index.pop(old, None)
            return
        if et in (EventType.ROBOT_STOP_SENT, EventType.SAFETY_EMERGENCY_STOP):
            hw = self.hardware_stop_state()
            if et == EventType.SAFETY_EMERGENCY_STOP:
                status, detail = "e-stop latched", f"{p.get('reason', '')}. {hw['summary']}"
            elif p.get("sent"):
                status, detail = "stop sent", hw["summary"]
            else:
                status, detail = "software stop", p.get("detail") or hw["summary"]
            self.actions.append(
                {"id": event.event_id[:12], "at": event.timestamp, "kind": "stop", "what": "stop", "origin": p.get("source"),
                 "status": status, "physical": physical and bool(p.get("sent")), "detail": detail}
            )
            return
        a = self._action_index.get(p.get("request_id") or "")
        if a is None:
            return
        if et == EventType.ROBOT_MOTION_BLOCKED:
            a.update(status="denied", reason_code=p.get("reason_code"), detail=p.get("reason") or "")
        elif et == EventType.ROBOT_MOTION_SENT:
            a.update(status="sent" if physical else "simulated", detail="frames written to the robot; no wheel feedback" if physical else "simulated robot")
        elif et == EventType.ROBOT_MOTION_ACKNOWLEDGED:
            a.update(ack="Arduino replied after the frame (link echo, not wheel motion)")
        elif et == EventType.ROBOT_MOTION_COMPLETED:
            a.update(status="ended" if physical else "simulated, ended", detail="commanded window over and stop sent; wheels not measured" if physical else "simulated robot")
        elif et == EventType.ROBOT_MOTION_ABORTED:
            a.update(status="aborted", detail=str(p.get("outcome") or p.get("reason") or ""))

    def _on_motion_event(self, event) -> None:
        if self.vision is not None and event.event_type in (
            EventType.ROBOT_MOTION_SENT,
            EventType.ROBOT_MOTION_COMPLETED,
            EventType.ROBOT_MOTION_ABORTED,
        ):
            # The zone tracker must know when the robot moves: a pose seen before a motion
            # is not trusted after it unless the marker is seen again.
            self.vision.set_robot_moving(event.event_type == EventType.ROBOT_MOTION_SENT)
        if event.event_type == EventType.ROBOT_MOTION_SENT:
            rid = event.payload.get("request_id")
            self._latency_marks[f"sent:{rid}"] = event.mono
            frame = self._latency_marks.pop(rid, None)
            if frame is not None:
                self.perf.add("frame_to_motion_sent_ms", (event.mono - frame) * 1000)
            if len(self._latency_marks) > 200:
                self._latency_marks.clear()

    async def _tick_loop(self) -> None:
        last_flush = time.monotonic()
        while True:
            await asyncio.sleep(0.2)
            try:
                now = time.monotonic()
                if self.sessions.active and not self.sessions.paused:
                    if self.sessions.is_due():
                        await self.end_session(reason="completed", origin="timer")
                    else:
                        self._check_milestones()
                        if now - self._last_focus_eval > 5.0:
                            self._last_focus_eval = now
                            self._evaluate_focus_nudge()
                            self._evaluate_focus_streak()
                            self.adaptation.tick(self.focus.state == FocusState.FOCUSED)
                self.companion.tick()
                if now - self._last_volume_check > 10.0:
                    self._last_volume_check = now
                    self._check_output_volume()
                if self.sessions.active and now - self._last_render_ahead > 10.0:
                    self._last_render_ahead = now
                    self._render_ahead()
                if self.vision is not None:
                    status = self.vision.snapshot()
                    was_live = self.vision_live
                    self.vision_status = status
                    if was_live != self.vision_live:
                        self._publish_mode()
                if now - last_flush > 2.0:
                    self.store.flush()
                    last_flush = now
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("runtime tick failed")

    def _check_output_volume(self) -> None:
        """Real audio only (not in tests): a muted speaker makes the robot silently mute."""
        if self._local_tts() is None or (self._volume_task is not None and not self._volume_task.done()):
            return

        async def probe() -> None:
            vol = await system_output_volume()
            self._os_volume = None if vol is None else {**vol, "checked_at": time.time()}

        self._volume_task = asyncio.create_task(probe(), name="output-volume")

    def _render_ahead(self) -> None:
        """Render this minute's and the next minute's "N minutes left." with the local voice,
        so "how much longer?" is answered at once offline (a live `say` takes 1.4-2 s)."""
        system = self._local_tts()
        kokoro = self.kokoro if self.kokoro is not None and self.kokoro.available() and self.rs.voice_profile.local_voice == "kokoro" else None
        engine = kokoro or system
        if engine is None or not self.sessions.active:
            return
        if self._render_ahead_task is not None and not self._render_ahead_task.done():
            return
        f = self.facts()
        texts = list(dict.fromkeys(self.composer.time_left(replace(f, remaining_s=max(0.0, f.remaining_s - ahead))) for ahead in (0.0, 60.0)))
        self._render_ahead_task = asyncio.create_task(asyncio.to_thread(engine.ensure, texts), name="tts-render-ahead")

    def _check_milestones(self) -> None:
        s = self.sessions.current
        if s is None or not self.rs.coach.announce_milestones:
            return
        elapsed = self.sessions.active_seconds()
        remaining = self.sessions.remaining_seconds()
        candidates = []
        if s.planned_duration_s >= 600 and elapsed >= s.planned_duration_s / 2:
            candidates.append("halfway")
        if s.planned_duration_s >= 900 and remaining <= 300:
            candidates.append("five_left")
        for m in candidates:
            if m in self.milestones_done:
                continue
            self.milestones_done.add(m)
            self.bus.publish(EventType.SESSION_MILESTONE, EventSource.SESSION, {"milestone": m})
            decision = self.policy.decide_milestone(self._policy_ctx(), m)
            bd = self.behavior.milestone(m, policy=decision)
            if bd.speak and bd.category:
                self._say(bd.category, priority=PRIORITY_AMBIENT)

    # ================================================================ snapshot
    def snapshot(self) -> dict:
        t = self.transport
        status = t.get_status().model_dump(mode="json") if t else None
        tel = self.robot_telemetry.model_dump(mode="json") if t and t.connected else None
        focus_durations = self.focus.snapshot()
        return {
            "server_time": time.time(),
            "run_id": self.run_id,
            "version": __version__,
            "uptime_s": round(time.time() - self.started_at, 1),
            "mode": self.mode(),
            "state": {
                "name": self.sm.state.value,
                "since_s": round(self.sm.time_in_state(), 2),
                "history": [r.__dict__ for r in list(self.sm.history)[-12:]],
            },
            "session": self.sessions.snapshot(),
            "last_session": None
            if self.sessions.last is None
            else {"session_id": self.sessions.last.session_id, "task": self.sessions.last.task, "end_reason": self.sessions.last.end_reason},
            "metrics": self.metrics(),
            "focus": {"state": self.focus.state.value, "since_s": round(time.monotonic() - self._focus_since, 1), "durations": focus_durations},
            "robot": {
                "driver": self.driver,
                "profile": "zeus" if self.driver == "zeus" else "sim",
                "link": self.robot_link_snapshot(),
                "gate": self.hardware_gate(),
                "actions": list(self.actions)[-12:][::-1],
                "hardware_stop": self.hardware_stop_state() if self.safety.estop_latched else None,
                "status": status,
                "telemetry": tel,
                "displacement": self.displacement,
                "displaced": bool(self.displacement),
                "host": self.rs.robot_host or self.settings.zeus_host,
                "link_test_running": self._link_test_task is not None and not self._link_test_task.done(),
                "link_tests": list(self.link_tests)[-5:],
            },
            "safety": {**self.safety.snapshot(), "watchdog_fired": self.watchdog.fired, "loop_max_lag_ms": round(self.watchdog.max_lag_s * 1000, 1)},
            "vision": {
                **self.vision_status,
                "tuning": self.effective_vision_tuning.model_dump(mode="json"),
                "choice": self.camera_choice,  # "zeus", "0", a URL...: what the source picker shows
                "zeus_camera_url": self.zeus_camera_url(),
            },
            "voice": {
                "tts": self.speaker.snapshot(),
                "input": self.voice.snapshot() if self.voice is not None else {"running": False, "reason": "voice input not started"},
                "last_utterance": self.last_utterance,
                "last_reply": self.last_reply,
                "llm": None if self.llm is None else self.llm.describe(),
                "personality": self.personality_snapshot(),
                "profile": {k: v for k, v in self.rs.voice_profile.model_dump(mode="json").items()},
                "active_preset": self.rs.active_preset,
                "cloud": self.fish.describe(),
                "cache_job": self.cache_job_state,
                "state": self.voice_state(),
                "turns": [self._turn_view(t) for t in list(self.voice_turns)[-6:]][::-1],
                "output": self.audio_output_status(),
            },
            "expression": self.expression.snapshot(),
            "mind": self.companion.snapshot(),
            "coach": {
                "personality": self.rs.coach.personality,
                "intensity": self.rs.coach.intensity,
                "muted": self.speaker.muted,
                "adaptation": self.adaptation.snapshot(),
                "last_decision": self.last_decision,
                "cooldown_s": round(self._policy_ctx().cooldown_s, 1),
            },
            "experiment": {
                "active": self.active_trial is not None,
                "summary": {k: v for k, v in self.trial_summary().items() if k != "items"},
                "voice": self.voice_trials.snapshot(),
            },
            "settings": self.rs.model_dump(mode="json"),
            "perf": self.perf.summary(),
            "demo_running": self._demo_task is not None and not self._demo_task.done(),
            "offline": {
                "enabled": self.settings.offline,
                "fish_audio": "cached clips only" if self.settings.offline else "per voice profile",
                "llm": "local (on this Mac)" if self.companion.llm is not None else ("off" if self.settings.offline or self.llm is None else self.settings.llm_provider),
                "fish_refusals": self.fish.offline_refusals,
            },
        }
