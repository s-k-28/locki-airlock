"""Transport interface every robot driver implements.

Semantics are deliberately narrow:

* ``move(primitive, speed, max_duration_s)`` starts a continuous motion. The
  transport keeps it alive (re-sending frames if the device needs that) and MUST
  stop on its own once ``max_duration_s`` has passed, even if nobody calls
  ``stop()``. This is the last software layer of defence; the safety controller
  normally stops earlier.
* ``stop()`` is always allowed and idempotent.

Transports report what happened through ``LinkCallbacks`` so the runtime can
publish requested / sent / acknowledged / completed as separate facts.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from .commands import MotionPrimitive


class TransportCapabilities(BaseModel):
    simulated: bool
    # True only when the transport receives verified obstacle readings from the robot.
    obstacle_sensing: bool = False
    battery_sensing: bool = False
    # "verified" = measured to stop the motors on its own when the host link goes
    # silent; "unverified" = believed present but not yet measured on this robot.
    device_watchdog: Literal["verified", "unverified", "simulated", "none"] = "none"
    device_watchdog_timeout_s: float | None = None
    # What kind of acknowledgement the device gives for commands.
    motion_ack: Literal["none", "link_echo", "simulated"] = "none"
    camera_stream_url: str | None = None
    # Whether this transport will turn an approved motion into motor commands right now.
    # False on the real robot until the hardware motion gate opens (docs/SAFETY.md section 7).
    motion_permitted: bool = True
    motion_lock_reasons: list[str] = Field(default_factory=list)
    link_mode: Literal["simulated", "monitor", "control"] = "simulated"
    notes: list[str] = Field(default_factory=list)


class RobotTelemetry(BaseModel):
    timestamp: float = Field(default_factory=time.time)
    ultrasonic_cm: float | None = None
    ir_left_blocked: bool | None = None
    ir_right_blocked: bool | None = None
    battery_v: float | None = None
    pose: dict[str, float] | None = None  # simulated robots only
    # Only from the optional LOCKED IN firmware patch (firmware/optional):
    firmware_patch: int | None = None
    device_mode: int | None = None  # Zeus_Car.ino currentMode (0 = MODE_NONE, 7 = app control)
    device_watchdog_trips: int | None = None  # Arduino-side motion watchdog trips since boot
    device_watchdog_latched: bool | None = None  # tripped and waiting for a Stop frame
    device_free_ram: int | None = None  # bytes between heap and stack, measured on the Arduino
    raw: dict[str, Any] | None = None


class RobotStatus(BaseModel):
    transport: str
    simulated: bool
    connected: bool
    link_state: Literal["disconnected", "connecting", "connected", "degraded", "error"] = "disconnected"
    target: str | None = None
    last_error: str | None = None
    connected_since: float | None = None
    last_rx_age_s: float | None = None
    last_tx_age_s: float | None = None
    rtt_ms: float | None = None
    # What rtt_ms measures: the Arduino's relayed echo of a frame, a WebSocket ping to the
    # ESP32, or the simulator's model.
    rtt_kind: Literal["arduino_echo", "ws_ping", "simulated"] | None = None
    frames_sent: int = 0
    frames_received: int = 0
    motion: str = "idle"
    battery_v: float | None = None
    firmware: dict[str, Any] = Field(default_factory=dict)
    last_message: dict[str, Any] | None = None  # the most recent message received from the robot
    message_counts: dict[str, int] = Field(default_factory=dict)
    capabilities: TransportCapabilities


@dataclass
class SendReceipt:
    """What the transport can honestly say about a command it just handled."""

    sent: bool
    sent_at_mono: float
    detail: str = ""


@dataclass
class LinkCallbacks:
    on_connected: Callable[[dict], None] = lambda info: None
    on_disconnected: Callable[[str], None] = lambda reason: None
    on_ack: Callable[[dict], None] = lambda info: None
    on_degraded: Callable[[dict], None] = lambda info: None
    on_motion_ended: Callable[[str, dict], None] = lambda why, info: None
    on_telemetry: Callable[[RobotTelemetry], None] = lambda t: None
    # The device reported that its own motion watchdog stopped the motors.
    on_device_watchdog: Callable[[dict], None] = lambda info: None


class RobotTransport(ABC):
    name: str = "base"

    def __init__(self) -> None:
        self.callbacks = LinkCallbacks()

    def set_callbacks(self, callbacks: LinkCallbacks) -> None:
        self.callbacks = callbacks

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def move(self, primitive: MotionPrimitive, speed: int, max_duration_s: float) -> SendReceipt: ...

    @abstractmethod
    async def stop(self, reason: str = "") -> SendReceipt: ...

    @abstractmethod
    def get_status(self) -> RobotStatus: ...

    @abstractmethod
    def get_telemetry(self) -> RobotTelemetry: ...

    def freeze_tx(self, duration_s: float) -> bool:
        """Link test (R5): stop transmitting for up to ``duration_s`` while the link stays open,
        so the device sees a silent link rather than a closed one.

        ``stop()`` ends the freeze at once and sends stop. If the cap elapses first, the
        transport ends any motion and sends stop. A motion is never resumed after a freeze.
        Transports that cannot do this return False."""
        return False

    @property
    def tx_frozen(self) -> bool:
        return False

    @property
    def connected(self) -> bool:
        return self.get_status().connected

    @property
    def simulated(self) -> bool:
        return self.get_status().simulated

    @property
    def is_moving(self) -> bool:
        return self.get_status().motion != "idle"
