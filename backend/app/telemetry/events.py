"""Typed events and the in-process event bus.

Events are the observability plane: everything the dashboard timeline, debug view
and JSONL telemetry show comes from here. Control flow (vision -> policy -> safety
-> robot) is done with direct calls in ``runtime.py``; the bus only records and
fans out what happened.

The bus is owned by the asyncio loop. Other threads (camera, microphone) must use
``publish_threadsafe``.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
import uuid
from collections import deque
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class EventType(StrEnum):
    # system
    SYSTEM_STARTED = "system.started"
    SYSTEM_SHUTDOWN = "system.shutdown"
    SYSTEM_ERROR = "system.error"
    SYSTEM_MODE_CHANGED = "system.mode_changed"
    SETTINGS_CHANGED = "settings.changed"
    # state machine
    STATE_CHANGED = "state.changed"
    STATE_TRANSITION_REJECTED = "state.transition_rejected"
    # session
    SESSION_STARTED = "session.started"
    SESSION_PAUSED = "session.paused"
    SESSION_RESUMED = "session.resumed"
    SESSION_ENDED = "session.ended"
    SESSION_PROGRESS = "session.progress"
    SESSION_MILESTONE = "session.milestone"
    # vision
    VISION_STARTED = "vision.started"
    VISION_STOPPED = "vision.stopped"
    VISION_CAMERA_CONNECTED = "vision.camera_connected"
    VISION_CAMERA_DISCONNECTED = "vision.camera_disconnected"
    VISION_HAND_DETECTED = "vision.hand_detected"
    VISION_HAND_LOST = "vision.hand_lost"
    VISION_HAND_APPROACHING = "vision.hand_approaching"
    VISION_REACH_CLEARED = "vision.reach_cleared"
    VISION_PHONE_REACH_CONFIRMED = "vision.phone_reach_confirmed"
    VISION_REACH_SUPPRESSED = "vision.reach_suppressed"
    VISION_FACE_PRESENT = "vision.face_present"
    VISION_FACE_ABSENT = "vision.face_absent"
    VISION_FOCUS_CHANGED = "vision.focus_changed"
    VISION_ZONE_TRACKING = "vision.zone_tracking"
    VISION_ZONE_ATTACHED = "vision.zone_attached"
    # robot
    ROBOT_CONNECTING = "robot.connecting"
    ROBOT_CONNECTED = "robot.connected"
    ROBOT_DISCONNECTED = "robot.disconnected"
    ROBOT_CONNECTION_FAILED = "robot.connection_failed"
    ROBOT_LINK_DEGRADED = "robot.link_degraded"
    ROBOT_MOTION_REQUESTED = "robot.motion_requested"
    ROBOT_MOTION_BLOCKED = "robot.motion_blocked"
    ROBOT_MOTION_SENT = "robot.motion_sent"
    ROBOT_MOTION_ACKNOWLEDGED = "robot.motion_acknowledged"
    ROBOT_MOTION_COMPLETED = "robot.motion_completed"
    ROBOT_MOTION_CONFIRMED = "robot.motion_confirmed"
    ROBOT_MOTION_ABORTED = "robot.motion_aborted"
    ROBOT_STOP_REQUESTED = "robot.stop_requested"
    ROBOT_STOP_SENT = "robot.stop_sent"
    ROBOT_FIRMWARE_DETECTED = "robot.firmware_detected"
    ROBOT_DEVICE_WATCHDOG = "robot.device_watchdog_tripped"
    ROBOT_EXPRESSION = "robot.expression"
    ROBOT_LINK_TEST_STARTED = "robot.link_test_started"
    ROBOT_LINK_FROZEN = "robot.link_frozen"
    ROBOT_LINK_TEST_FINISHED = "robot.link_test_finished"
    # safety
    SAFETY_ARMED = "safety.armed"
    SAFETY_DISARMED = "safety.disarmed"
    SAFETY_AUTONOMY_CHANGED = "safety.autonomy_changed"
    SAFETY_EMERGENCY_STOP = "safety.emergency_stop"
    SAFETY_RESET = "safety.reset"
    SAFETY_MOTION_SUPPRESSED = "safety.motion_suppressed"
    SAFETY_MOTOR_POWER = "safety.motor_power"
    SAFETY_WATCHDOG_FIRED = "safety.watchdog_fired"
    SAFETY_LOOP_STALL = "safety.loop_stall"
    # voice
    VOICE_LISTENING = "voice.listening"
    VOICE_STOPPED = "voice.stopped"
    VOICE_SPEECH_STARTED = "voice.speech_started"
    VOICE_SPEECH_ENDED = "voice.speech_ended"
    VOICE_TRANSCRIBED = "voice.transcribed"
    VOICE_COMMAND_RECOGNIZED = "voice.command_recognized"
    VOICE_COMMAND_REJECTED = "voice.command_rejected"
    VOICE_RESPONSE_GENERATED = "voice.response_generated"
    VOICE_TTS_STARTED = "voice.tts_started"
    VOICE_TTS_FINISHED = "voice.tts_finished"
    VOICE_TURN = "voice.turn"
    VOICE_ERROR = "voice.error"
    # coach / policy
    COACH_DECISION = "coach.decision"
    COACH_INTERVENTION_GENERATED = "coach.intervention_generated"
    COACH_ADAPTATION = "coach.adaptation"

    # Milestone 5: the behaviour engine's speak/stay-silent decisions, typed action requests
    # (from the language model or the behaviour engine) and how they were resolved, and
    # every reply the local language model wrote (spoken or rejected, with why).
    BEHAVIOR_DECISION = "behavior.decision"
    ACTION_REQUESTED = "action.requested"
    ACTION_RESOLVED = "action.resolved"
    DIALOGUE_REPLY = "dialogue.reply"
    # experiments
    DEMO_STEP = "demo.step"
    EXPERIMENT_TRIAL_STARTED = "experiment.trial_started"
    EXPERIMENT_TRIAL_ENDED = "experiment.trial_ended"
    EXPERIMENT_LABEL = "experiment.label"
    EXPERIMENT_VOICE_TRIAL = "experiment.voice_trial"


class EventSource(StrEnum):
    SYSTEM = "system"
    SESSION = "session"
    VISION = "vision"
    ROBOT = "robot"
    SAFETY = "safety"
    VOICE = "voice"
    POLICY = "policy"
    OPERATOR = "operator"
    SIMULATION = "simulation"
    EXPERIMENT = "experiment"


# High-rate or noisy events that are useful live but not worth persisting.
EPHEMERAL_TYPES = frozenset({EventType.VISION_HAND_DETECTED, EventType.VISION_HAND_LOST})


class Event(BaseModel):
    event_id: str
    seq: int
    timestamp: float = Field(description="Unix epoch seconds (wall clock)")
    mono: float = Field(description="time.monotonic() at publish; use for latency, not display")
    event_type: EventType
    source: EventSource
    session_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = None
    # True when the event describes simulated hardware or synthetic input. The UI must
    # label these; nothing simulated may ever be presented as physical.
    simulated: bool = False


class Subscription:
    """Bounded per-consumer queue. When full, the oldest message is dropped so a slow
    consumer (e.g. a backgrounded browser tab) never blocks publishers or grows memory."""

    def __init__(self, bus: EventBus, maxsize: int):
        self._bus = bus
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0

    def offer(self, event: Event) -> None:
        if self.queue.full():
            try:
                self.queue.get_nowait()
                self.dropped += 1
            except asyncio.QueueEmpty:
                pass
        self.queue.put_nowait(event)

    async def get(self) -> Event:
        return await self.queue.get()

    def close(self) -> None:
        self._bus._unsubscribe(self)


class EventBus:
    def __init__(
        self,
        history_size: int = 1000,
        sink: Callable[[Event], None] | None = None,
        session_id_provider: Callable[[], str | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ):
        self._history: deque[Event] = deque(maxlen=history_size)
        self._subs: list[Subscription] = []
        self._listeners: list[Callable[[Event], None]] = []
        self._seq = itertools.count(1)
        self._sink = sink
        self._session_id_provider = session_id_provider or (lambda: None)
        self._clock = clock
        self._wall = wall_clock
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def set_session_id_provider(self, provider: Callable[[], str | None]) -> None:
        self._session_id_provider = provider

    def publish(
        self,
        event_type: EventType,
        source: EventSource,
        payload: dict[str, Any] | None = None,
        *,
        confidence: float | None = None,
        simulated: bool = False,
        session_id: str | None = None,
    ) -> Event:
        event = Event(
            event_id=uuid.uuid4().hex,
            seq=next(self._seq),
            timestamp=self._wall(),
            mono=self._clock(),
            event_type=event_type,
            source=source,
            session_id=session_id if session_id is not None else self._session_id_provider(),
            payload=payload or {},
            confidence=None if confidence is None else round(float(confidence), 4),
            simulated=simulated,
        )
        self._history.append(event)
        if self._sink is not None and event_type not in EPHEMERAL_TYPES:
            try:
                self._sink(event)
            except Exception:  # telemetry must never break control flow
                log.exception("event sink failed")
        for sub in list(self._subs):
            sub.offer(event)
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                log.exception("event listener failed")
        return event

    def publish_threadsafe(self, event_type: EventType, source: EventSource, payload=None, **kw) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(lambda: self.publish(event_type, source, payload, **kw))

    def subscribe(self, maxsize: int = 2000) -> Subscription:
        sub = Subscription(self, maxsize)
        self._subs.append(sub)
        return sub

    def _unsubscribe(self, sub: Subscription) -> None:
        if sub in self._subs:
            self._subs.remove(sub)

    def add_listener(self, fn: Callable[[Event], None]) -> None:
        self._listeners.append(fn)

    def history(self, limit: int = 200, types: set[str] | None = None) -> list[Event]:
        items = [e for e in self._history if types is None or e.event_type in types]
        return items[-limit:]

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)
