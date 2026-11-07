"""SunFounder Zeus Car driver (stock firmware, WebSocket via the ESP32-CAM).

Everything here is derived from the upstream sources; see docs/HARDWARE_PROTOCOL.md
for the line-by-line references. Summary of the verified path:

    laptop --WebSocket text JSON--> ESP32-CAM (ai-camera-firmware, port 8765)
           ESP32 converts {"A".."Z"} to "WS+v0;v1;...;v25" and writes it to UART
    UART 115200 --> Arduino (Zeus_Car.ino) parses region values in onReceive()

Frames we send (only these, nothing else):

* translation:  {"K": [x, y]}  joystick, x = right, y = forward, |r| <= 100
* rotation:     {"Q": [+-100, 0]}  heading joystick; the Arduino's magnetometer
                PD loop turns toward +-90 deg relative to the heading at mode entry.
                Rotation *speed* is not controllable with stock firmware.
* stop:         {"F": true}  the app's Stop button -> MODE_NONE -> carStop()

We never send "SET+..." (Wi-Fi reconfiguration), E (compass calibration spins the car
for ~1 min), I (speech commands drive at power 60), or the autonomous mode switches M/N/O/P.

Two link modes:

* ``monitor`` (default): no region frame is ever written, not even Stop. The link is kept
  open and measured with WebSocket protocol pings, which the ESP32's WebSocket library
  answers itself. On camera firmware 1.5.3+ (which closes clients that send no text for
  3 s) a text "ping" is added; that firmware answers "pong <ms>" and forwards nothing to
  the Arduino. Older firmware (1.1-1.5.2) turns a text "ping" into [APPSTOP] for the
  Arduino, so it never gets one. move() and stop() write nothing and say so.
* ``control``: the Milestone 2 driver (stop burst on connect, Stop keepalive every 500 ms,
  10 Hz motion refresh). Only used when the hardware motion gate allows it
  (docs/SAFETY.md section 7); with stock firmware it never does.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Literal

from .base import RobotStatus, RobotTelemetry, RobotTransport, SendReceipt, TransportCapabilities
from .commands import MotionPrimitive

log = logging.getLogger(__name__)

# Timing derived from firmware (docs/HARDWARE_PROTOCOL.md section 5).
MIN_FRAME_GAP_S = 0.045  # ESP32 drops frames <20 ms apart; Arduino reads ~1 char/ms into a 64-byte buffer
# The first stop frame may follow a motion frame sooner: >20 ms clears the ESP32's drop
# window and one ~33-char motion line + one ~29-char stop line still fit the Uno's
# 64-byte RX buffer. The rest of the burst keeps the normal spacing.
STOP_FIRST_GAP_S = 0.025
MOTION_REFRESH_S = 0.10  # 10 Hz while moving
IDLE_KEEPALIVE_S = 0.50  # stop frame every 500 ms holds MODE_NONE and feeds the 2 s / 3 s firmware timeouts
STOP_BURST = 3  # stop frames sent back-to-back (MIN_FRAME_GAP_S apart) to survive a dropped frame
LINK_DEGRADED_S = 1.2
LINK_LOST_S = 2.5
CHECK_INFO_TIMEOUT_S = 2.5
FRAME_LOG = 60
LINK_TEST_MAX_FREEZE_S = 6.0  # cap for freeze_tx(); the R5 test asks for <= 5 s
MONITOR_PING_S = 1.0  # monitor link: protocol ping (all firmware) + text "ping" (1.5.3+ only)
MONITOR_PING_TIMEOUT_S = 2.0

LinkMode = Literal["monitor", "control"]
# Firmware patch versions this build was reviewed and tested against (firmware/optional).
# Any other "li" value is reported but never trusted to expire commands on the device.
SUPPORTED_PATCH_VERSIONS = frozenset({2})
MONITOR_NO_MOTION = "monitor link: motor commands are disabled, nothing was sent to the robot"
MONITOR_NO_STOP = "monitor link: no stop frame sent (motor commands are disabled; LOCKED IN has not commanded the motors)"


def classify_camera_firmware(unprompted_pong: bool, check_info: dict) -> tuple[str, str]:
    """(family, label) from what the ESP32 sent on connect (docs/HARDWARE_PROTOCOL.md section 3).

    single_client: ai-camera-firmware 1.1.0-1.5.2. Sends "pong <ms>" on every WebSocket event,
        including the connect itself; keeps a silent client open; turns text "ping" into
        [APPSTOP] for the Arduino. 1.5.2 already adds StaIp/VideoTemplate to the check info.
    multi_client: 1.5.3+. No unprompted pong; check info with StaIp; closes a client that sends
        no text for 3 s; answers text "ping" with "pong <ms>" without involving the Arduino.
    """
    modern_info = "StaIp" in check_info or "VideoTemplate" in check_info
    if unprompted_pong:
        return "single_client", "1.5.2 (single-client)" if modern_info else "1.1-1.4.x (single-client)"
    if modern_info:
        return "multi_client", "1.5.3+ (multi-client)"
    return "unknown", "unknown"


def translation_vector(primitive: MotionPrimitive, speed: int) -> tuple[int, int]:
    s = max(0, min(100, int(speed)))
    return {
        MotionPrimitive.FORWARD: (0, s),
        MotionPrimitive.BACKWARD: (0, -s),
        MotionPrimitive.STRAFE_LEFT: (-s, 0),
        MotionPrimitive.STRAFE_RIGHT: (s, 0),
    }[primitive]


def encode_motion(primitive: MotionPrimitive, speed: int) -> str:
    """JSON text for one motion refresh frame."""
    if primitive in (MotionPrimitive.ROTATE_LEFT, MotionPrimitive.ROTATE_RIGHT):
        x = -100 if primitive == MotionPrimitive.ROTATE_LEFT else 100
        return json.dumps({"Q": [x, 0]}, separators=(",", ":"))
    if primitive == MotionPrimitive.STOP:
        return encode_stop()
    x, y = translation_vector(primitive, speed)
    return json.dumps({"K": [x, y]}, separators=(",", ":"))


def encode_stop() -> str:
    return json.dumps({"F": True}, separators=(",", ":"))


def arduino_joystick(x: int, y: int) -> tuple[int, int]:
    """What Zeus_Car.ino computes from a K/Q value: (angle deg, radius)."""
    angle = int(math.atan2(x, y) * 180.0 / math.pi)
    radius = int(math.sqrt(x * x + y * y))
    return angle, radius


@dataclass
class Incoming:
    kind: str  # check_info | pong | arduino | config_reply | text
    data: dict


def parse_incoming(text: str) -> Incoming:
    t = text.strip()
    if t.startswith("pong ") or t == "pong":
        parts = t.split()
        return Incoming("pong", {"esp_ms": int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None})
    if t == "null":
        return Incoming("arduino", {})
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        return Incoming("text", {"text": t[:200]})
    if isinstance(obj, dict):
        if obj.get("Check") == "SC" or "Name" in obj and "Type" in obj:
            return Incoming("check_info", obj)
        if "state" in obj and ("errors" in obj or obj.get("state") in ("OK", "ERROR")):
            return Incoming("config_reply", obj)
        return Incoming("arduino", obj)
    return Incoming("text", {"text": t[:200]})


@dataclass
class _Motion:
    primitive: MotionPrimitive
    speed: int
    frame: str
    started: float
    deadline: float
    first_sent: asyncio.Future
    acked: bool = False


class ZeusRobotTransport(RobotTransport):
    name = "zeus"

    def __init__(
        self,
        host: str = "192.168.4.1",
        port: int = 8765,
        *,
        device_watchdog_verified: bool = False,
        device_watchdog_timeout_s: float = 4.0,
        connect_timeout_s: float = 4.0,
        link_mode: LinkMode = "monitor",
    ):
        super().__init__()
        if link_mode not in ("monitor", "control"):
            raise ValueError(f"unknown link mode {link_mode!r}")
        self.host = host
        self.port = port
        self.link_mode: LinkMode = link_mode
        self.device_watchdog_verified = device_watchdog_verified
        self.device_watchdog_timeout_s = device_watchdog_timeout_s
        self.connect_timeout_s = connect_timeout_s
        self._ws = None
        self._connected = False
        self._link_state = "disconnected"
        self._connected_since: float | None = None
        self._last_error: str | None = None
        self._tx_task: asyncio.Task | None = None
        self._rx_task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._motion: _Motion | None = None
        self._stop_burst = 0
        self._stop_waiters: list[asyncio.Future] = []
        self._last_tx = 0.0
        self._last_rx: float | None = None
        self._last_arduino_rx: float | None = None
        self._last_esp_rx: float | None = None
        self._frames_sent = 0
        self._frames_received = 0
        self._arduino_msgs = 0
        self._echo_ms: deque[float] = deque(maxlen=30)
        self._pending_echo_from: float | None = None
        self._degraded = False
        self.check_info: dict = {}
        self.firmware: dict = {}
        self.frame_log: deque[dict] = deque(maxlen=FRAME_LOG)
        self._telemetry = RobotTelemetry()
        self._patched = False
        self._closing = False
        self._tx_frozen_until = 0.0  # link test: no frames at all until then (or until stop())
        self._resumed_at = 0.0
        self._wd_trips: int | None = None
        # Monitor link
        self._family = "unknown"
        self._monitor_task: asyncio.Task | None = None
        self._ping_task: asyncio.Task | None = None
        self._last_pong_rx: float | None = None  # WebSocket protocol pong
        self._ping_rtt_ms: deque[float] = deque(maxlen=30)
        self._ping_timeouts = 0
        self._last_proto_ping = 0.0
        self._last_text_ping = 0.0
        self._text_ping_at: float | None = None
        self._app_rtt_ms: deque[float] = deque(maxlen=30)
        self._msg_counts: dict[str, int] = {}
        self._last_msg: dict | None = None
        self._unknown_patch: int | None = None

    # ---------------------------------------------------------------- connect
    async def connect(self) -> None:
        import websockets

        if self._connected:
            return
        self._closing = False
        self._tx_frozen_until = 0.0
        self._resumed_at = 0.0
        self._link_state = "connecting"
        uri = f"ws://{self.host}:{self.port}"
        try:
            self._ws = await websockets.connect(
                uri, open_timeout=self.connect_timeout_s, ping_interval=None, close_timeout=1.0, max_size=2**16
            )
        except Exception as exc:
            self._link_state = "error"
            self._last_error = f"cannot open {uri}: {exc}"
            raise ConnectionError(self._last_error) from exc

        info_msgs: list[Incoming] = []
        try:
            deadline = time.monotonic() + CHECK_INFO_TIMEOUT_S
            while time.monotonic() < deadline:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=max(0.05, deadline - time.monotonic()))
                msg = parse_incoming(raw if isinstance(raw, str) else raw.decode(errors="ignore"))
                info_msgs.append(msg)
                if msg.kind == "check_info":
                    self.check_info = msg.data
                    break
        except TimeoutError:
            pass
        except Exception as exc:
            await self._abort(f"connection dropped during handshake: {exc}")
            raise ConnectionError(self._last_error) from exc

        unprompted_pong = any(m.kind == "pong" for m in info_msgs)
        for m in info_msgs:
            self._count(m.kind, m)
        self._family, generation = classify_camera_firmware(unprompted_pong, self.check_info)
        self.firmware = {
            "name": self.check_info.get("Name"),
            "type": self.check_info.get("Type"),
            "check": self.check_info.get("Check"),
            "video_url": self.check_info.get("video") or None,
            "sta_ip": self.check_info.get("StaIp") or None,
            "camera_firmware_generation": generation,
            "camera_firmware_family": self._family,
            "unprompted_pong": unprompted_pong,
            "identified": self.check_info.get("Check") == "SC",
        }
        if not self.check_info:
            log.warning("no SunFounder check_info received from %s; continuing but marking unidentified", uri)

        self._connected = True
        self._link_state = "connected"
        self._connected_since = time.time()
        self._last_rx = time.monotonic()
        self._rx_task = asyncio.create_task(self._rx_loop(), name="zeus-rx")
        if self.link_mode == "control":
            self._tx_task = asyncio.create_task(self._tx_loop(), name="zeus-tx")
            # Put the Arduino in MODE_NONE first; this also cancels any autonomous mode left on by the app.
            self._stop_burst = STOP_BURST
            self._wake.set()
        else:
            self._monitor_task = asyncio.create_task(self._monitor_loop(), name="zeus-monitor")
        self.callbacks.on_connected(
            {"transport": "zeus", "simulated": False, "target": uri, "firmware": self.firmware, "link_mode": self.link_mode}
        )

    async def disconnect(self) -> None:
        if not self._connected:
            return
        if self.link_mode == "monitor":
            # Nothing to stop: a monitor link never commanded the motors. Closing makes the
            # ESP32 report [DISCONNECTED] to the Arduino, as with any client.
            await self._teardown("operator disconnect", notify=True)
            return
        # Best effort: stop burst before closing (closing also makes the ESP32 report
        # [DISCONNECTED], which stops the car on stock firmware).
        try:
            await asyncio.wait_for(self.stop("disconnect"), timeout=0.6)
            await asyncio.sleep(MIN_FRAME_GAP_S * STOP_BURST)
        except Exception:
            pass
        await self._teardown("operator disconnect", notify=True)

    async def _abort(self, reason: str) -> None:
        self._last_error = reason
        self._link_state = "error"
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        self._ws = None

    async def _teardown(self, reason: str, notify: bool) -> None:
        if self._closing:
            return
        self._closing = True
        was_connected = self._connected
        self._connected = False
        self._link_state = "disconnected" if reason == "operator disconnect" else "error"
        if reason != "operator disconnect":
            self._last_error = reason
        if self._motion is not None:
            m = self._motion
            self._motion = None
            if not m.first_sent.done():
                m.first_sent.set_result(False)
            self.callbacks.on_motion_ended("link_lost" if reason != "operator disconnect" else "disconnect", {"primitive": m.primitive.value})
        for w in self._stop_waiters:
            if not w.done():
                w.set_result(False)
        self._stop_waiters.clear()
        current = asyncio.current_task()
        for task in (self._tx_task, self._rx_task, self._monitor_task, self._ping_task):
            if task is not None and task is not current:
                task.cancel()
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        self._ws = None
        if was_connected and notify:
            self.callbacks.on_disconnected(reason)

    # ---------------------------------------------------------------- motion
    async def move(self, primitive: MotionPrimitive, speed: int, max_duration_s: float) -> SendReceipt:
        now = time.monotonic()
        if not self._connected:
            return SendReceipt(False, now, "not connected")
        if self.link_mode == "monitor":
            return SendReceipt(False, now, MONITOR_NO_MOTION)
        if primitive == MotionPrimitive.STOP:
            return await self.stop("move(stop)")
        if self.tx_frozen:
            return SendReceipt(False, now, "transmit paused by the link test")
        loop = asyncio.get_running_loop()
        prev = self._motion
        if prev is not None and not prev.first_sent.done():
            prev.first_sent.set_result(False)
        self._stop_burst = 0
        self._motion = _Motion(primitive, int(speed), encode_motion(primitive, speed), now, now + max_duration_s, loop.create_future())
        self._wake.set()
        try:
            sent = await asyncio.wait_for(asyncio.shield(self._motion.first_sent), timeout=0.5)
        except TimeoutError:
            sent = False
        frame = self._motion.frame if self._motion else ""
        return SendReceipt(bool(sent), time.monotonic(), f"frame {frame}" if sent else "first frame not written within 500 ms")

    async def stop(self, reason: str = "") -> SendReceipt:
        now = time.monotonic()
        if not self._connected:
            return SendReceipt(False, now, "not connected")
        if self.link_mode == "monitor":
            return SendReceipt(False, now, MONITOR_NO_STOP)
        if self._tx_frozen_until:
            self._resume_tx(now, f"stop: {reason}" if reason else "stop")
        m = self._motion
        if m is not None:
            self._motion = None
            if not m.first_sent.done():
                m.first_sent.set_result(False)
            self.callbacks.on_motion_ended("stopped", {"primitive": m.primitive.value, "ran_s": round(now - m.started, 3)})
        self._stop_burst = STOP_BURST
        waiter = asyncio.get_running_loop().create_future()
        self._stop_waiters.append(waiter)
        self._wake.set()
        try:
            sent = await asyncio.wait_for(asyncio.shield(waiter), timeout=0.5)
        except TimeoutError:
            sent = False
        return SendReceipt(bool(sent), time.monotonic(), f"stop burst x{STOP_BURST} {encode_stop()}" if sent else "stop not written within 500 ms")

    # ------------------------------------------------------------- link test
    def freeze_tx(self, duration_s: float) -> bool:
        """R5: write nothing (no motion refresh, no keepalive) for up to ``duration_s`` while
        the WebSocket stays open, as a hung laptop or stalled Wi-Fi would. The car then
        only stops if its own firmware does. stop() ends the freeze immediately."""
        if not self._connected or self.link_mode == "monitor":
            return False
        d = max(0.0, min(LINK_TEST_MAX_FREEZE_S, float(duration_s)))
        self._tx_frozen_until = time.monotonic() + d
        self.frame_log.append({"t": round(time.time(), 3), "dir": "tx", "kind": "freeze", "frame": f"transmit paused for up to {d:.2f}s (link test)"})
        return True

    @property
    def tx_frozen(self) -> bool:
        return self._tx_frozen_until > 0.0

    def _resume_tx(self, now: float, why: str) -> None:
        self._tx_frozen_until = 0.0
        self._resumed_at = now
        self.frame_log.append({"t": round(time.time(), 3), "dir": "tx", "kind": "freeze", "frame": f"transmit resumed ({why})"})

    # ---------------------------------------------------------------- loops
    async def _send(self, text: str, kind: str, region: bool = True) -> bool:
        """Write one text message. ``region`` frames reach the Arduino (which then echoes);
        the monitor link's text "ping" does not."""
        ws = self._ws
        if ws is None:
            return False
        try:
            await ws.send(text)
        except Exception as exc:
            await self._teardown(f"send failed: {exc}", notify=True)
            return False
        now = time.monotonic()
        self._last_tx = now
        self._frames_sent += 1
        if region:
            self._pending_echo_from = now
        self.frame_log.append({"t": round(time.time(), 3), "dir": "tx", "kind": kind, "frame": text})
        return True

    async def _tx_loop(self) -> None:
        try:
            while self._connected:
                now = time.monotonic()
                gap_ok = now - self._last_tx >= MIN_FRAME_GAP_S
                m = self._motion
                if m is not None and now >= m.deadline:
                    # Transport-level hard cap: nobody stopped us in time.
                    self._motion = None
                    self._stop_burst = STOP_BURST
                    self.callbacks.on_motion_ended("deadline", {"primitive": m.primitive.value, "ran_s": round(now - m.started, 3)})
                    m = None
                if self._tx_frozen_until:
                    if now < self._tx_frozen_until:
                        self._check_link(now)
                        self._wake.clear()
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(self._wake.wait(), timeout=0.02)
                        continue
                    # Nobody called stop() before the cap: end the motion, never resume it.
                    self._resume_tx(now, "freeze cap elapsed")
                    if m is not None:
                        self._motion = None
                        if not m.first_sent.done():
                            m.first_sent.set_result(False)
                        self.callbacks.on_motion_ended("freeze_timeout", {"primitive": m.primitive.value, "ran_s": round(now - m.started, 3)})
                        m = None
                    self._stop_burst = STOP_BURST
                if self._stop_burst > 0:
                    first = self._stop_burst == STOP_BURST
                    stop_gap_ok = now - self._last_tx >= (STOP_FIRST_GAP_S if first else MIN_FRAME_GAP_S)
                    if stop_gap_ok and await self._send(encode_stop(), "stop"):
                        self._stop_burst -= 1
                        for w in self._stop_waiters:
                            if not w.done():
                                w.set_result(True)
                        self._stop_waiters.clear()
                elif m is not None:
                    due = not m.first_sent.done() or now - self._last_tx >= MOTION_REFRESH_S
                    if gap_ok and due and await self._send(m.frame, "motion"):
                        if not m.first_sent.done():
                            m.first_sent.set_result(True)
                elif now - self._last_tx >= IDLE_KEEPALIVE_S:
                    await self._send(encode_stop(), "keepalive")
                self._check_link(now)
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=0.02)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("zeus tx loop crashed")
            await self._teardown("tx loop crashed", notify=True)

    async def _monitor_loop(self) -> None:
        """Keep a monitor link open and measured without writing anything the Arduino acts on."""
        try:
            while self._connected:
                now = time.monotonic()
                if self._family == "multi_client" and now - self._last_text_ping >= MONITOR_PING_S:
                    # 1.5.3+ closes clients that send no text for 3 s; it answers "ping" itself.
                    self._last_text_ping = now
                    if await self._send("ping", "keepalive", region=False):
                        self._text_ping_at = now
                if now - self._last_proto_ping >= MONITOR_PING_S and (self._ping_task is None or self._ping_task.done()):
                    self._last_proto_ping = now
                    self._ping_task = asyncio.create_task(self._protocol_ping(), name="zeus-ping")
                self._check_link(now)
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("zeus monitor loop crashed")
            await self._teardown("monitor loop crashed", notify=True)

    async def _protocol_ping(self) -> None:
        """RFC 6455 ping; the ESP32's WebSocket library replies with a pong on its own."""
        ws = self._ws
        if ws is None:
            return
        try:
            waiter = await ws.ping()
            latency_s = await asyncio.wait_for(waiter, timeout=MONITOR_PING_TIMEOUT_S)
        except TimeoutError:
            self._ping_timeouts += 1
            return
        except Exception:
            return  # connection closed; the rx loop reports it
        self._last_pong_rx = time.monotonic()
        self._ping_rtt_ms.append(float(latency_s) * 1000.0)

    def _check_link(self, now: float) -> None:
        if self._last_rx is None:
            return
        if self._tx_frozen_until:
            return  # the Arduino only answers what we send; silence during a freeze is ours
        gap = now - max(self._last_rx, self._last_pong_rx or 0.0, self._resumed_at)
        if gap > LINK_LOST_S:
            asyncio.ensure_future(self._teardown(f"link lost: nothing from the robot for {gap:.1f}s", notify=True))
        elif gap > LINK_DEGRADED_S and not self._degraded:
            self._degraded = True
            self._link_state = "degraded"
            self.callbacks.on_degraded({"rx_gap_s": round(gap, 2)})
        elif gap <= LINK_DEGRADED_S and self._degraded:
            self._degraded = False
            self._link_state = "connected"

    async def _rx_loop(self) -> None:
        ws = self._ws
        try:
            async for raw in ws:
                now = time.monotonic()
                self._last_rx = now
                self._frames_received += 1
                if not isinstance(raw, str):
                    # The ESP32 never sends binary to clients; record it, never interpret it.
                    msg = Incoming("binary", {"bytes": len(raw)})
                    self._count("binary", msg, f"<{len(raw)} bytes binary>")
                    self.frame_log.append({"t": round(time.time(), 3), "dir": "rx", "kind": "binary", "frame": f"<{len(raw)} bytes>"})
                    continue
                text = raw
                msg = parse_incoming(text)
                self._count(msg.kind, msg, text)
                if len(self.frame_log) < FRAME_LOG or msg.kind != "arduino":
                    self.frame_log.append({"t": round(time.time(), 3), "dir": "rx", "kind": msg.kind, "frame": text[:120]})
                if msg.kind == "pong":
                    self._last_esp_rx = now
                    if self._text_ping_at is not None:
                        self._app_rtt_ms.append((now - self._text_ping_at) * 1000.0)
                        self._text_ping_at = None
                elif msg.kind == "check_info":
                    self.check_info = msg.data
                elif msg.kind == "arduino":
                    self._on_arduino(msg.data, now)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._connected:
                await self._teardown(f"connection closed: {exc}", notify=True)
            return
        if self._connected:
            await self._teardown("connection closed by robot", notify=True)

    def _count(self, kind: str, msg: Incoming, text: str | None = None) -> None:
        self._msg_counts[kind] = self._msg_counts.get(kind, 0) + 1
        shown = text if text is not None else json.dumps(msg.data, separators=(",", ":"))[:120]
        self._last_msg = {"kind": kind, "text": shown[:120], "at": time.time(), "mono": time.monotonic()}

    def _on_arduino(self, data: dict, now: float) -> None:
        self._last_arduino_rx = now
        self._last_esp_rx = now
        self._arduino_msgs += 1
        if self._pending_echo_from is not None:
            self._echo_ms.append((now - self._pending_echo_from) * 1000)
            echo = self._echo_ms[-1]
            self._pending_echo_from = None
            m = self._motion
            if m is not None and not m.acked and m.first_sent.done():
                m.acked = True
                self.callbacks.on_ack(
                    {
                        "ack": "link_echo",
                        "latency_ms": round(echo, 1),
                        "note": "Arduino processed a serial line after the motion frame; not proof of wheel motion",
                    }
                )
        if data:
            li = data.get("li")
            marked = isinstance(li, int) and not isinstance(li, bool)
            self._patched = marked and li in SUPPORTED_PATCH_VERSIONS
            self._unknown_patch = li if marked and li not in SUPPORTED_PATCH_VERSIONS else None
            if not self._patched:
                # Stock replies carry only the speech toggle "I". Anything else without the
                # patch marker is kept as raw text, never read as sensor values.
                self._telemetry = RobotTelemetry(raw=data)
                self.callbacks.on_telemetry(self._telemetry)
                return
            us = data.get("us")  # -1 no echo within ~2 m, -2 not measured in this mode
            ir = data.get("ir")

            def num(key: str) -> int | None:
                v = data.get(key)
                return int(v) if isinstance(v, int | float) and not isinstance(v, bool) else None

            self._telemetry = RobotTelemetry(
                ultrasonic_cm=float(us) if isinstance(us, int | float) and us >= 0 else None,
                ir_left_blocked=None if ir is None else not bool(int(ir) & 0b01),
                ir_right_blocked=None if ir is None else not bool(int(ir) & 0b10),
                firmware_patch=num("li"),
                device_mode=num("md"),
                device_watchdog_trips=num("wd"),
                device_watchdog_latched=None if num("lk") is None else bool(num("lk")),
                device_free_ram=num("fr"),
                raw=data,
            )
            wd = self._telemetry.device_watchdog_trips
            if wd is not None:
                prev, self._wd_trips = self._wd_trips, wd
                if prev is not None and wd > prev:
                    self._on_device_watchdog(prev, wd, now)
            self.callbacks.on_telemetry(self._telemetry)

    def _on_device_watchdog(self, prev: int, count: int, now: float) -> None:
        """The Arduino says its watchdog stopped the motors (no data for 400 ms). If we still
        think a motion is running, end it: the next refresh frame would restart the wheels
        after a stall the host never noticed."""
        m = self._motion
        self.frame_log.append({"t": round(time.time(), 3), "dir": "rx", "kind": "device_watchdog", "frame": f"Arduino watchdog trips {prev} -> {count}"})
        info = {"trips": count, "new_trips": count - prev, "during_motion": m is not None, "during_link_test": self.tx_frozen}
        if m is not None:
            self._motion = None
            if not m.first_sent.done():
                m.first_sent.set_result(False)
            self._stop_burst = STOP_BURST
            self._wake.set()
            info["primitive"] = m.primitive.value
            self.callbacks.on_motion_ended("device_watchdog", {"primitive": m.primitive.value, "ran_s": round(now - m.started, 3)})
        self.callbacks.on_device_watchdog(info)

    # ---------------------------------------------------------------- status
    @property
    def motion_permitted(self) -> bool:
        """Control link and the firmware reported its device-side command expiry (patch v2)."""
        return self.link_mode == "control" and self._connected and self._patched

    def motion_lock_reasons(self) -> list[str]:
        if self.link_mode == "monitor":
            return ["monitor link: this connection never sends motor commands"]
        reasons = []
        if self._unknown_patch is not None:
            reasons.append(f"the robot reports firmware patch version {self._unknown_patch}; this build only trusts version 2")
        elif not self._patched:
            reasons.append(
                "no device-side command expiry reported by the robot (stock firmware keeps the last command until the ESP32's 2-4 s silence timeout)"
            )
        if not self._connected:
            reasons.append("not connected")
        return reasons

    def get_status(self) -> RobotStatus:
        now = time.monotonic()
        m = self._motion
        monitor = self.link_mode == "monitor"
        if monitor:
            rtts = sorted(self._ping_rtt_ms)
            rtt_kind = "ws_ping"
        else:
            rtts = sorted(self._echo_ms)
            rtt_kind = "arduino_echo"
        notes = [
            "Stock firmware stops on [DISCONNECTED]/[APPSTOP]/[Init] from the ESP32 (client gone, or 2-4 s without data).",
            "No Arduino-side command timeout without the optional firmware patch (firmware/optional).",
            "Rotation is magnetometer-closed-loop; keep magnets (phone, speaker, laptop) away from the compass.",
        ]
        if monitor:
            notes.insert(0, "Monitor link: no region frame (motion or Stop) is ever sent. Latency is a WebSocket ping to the ESP32.")
            if self._family == "single_client":
                notes.append("Camera firmware 1.1-1.5.2: the ESP32 itself tells the Arduino [DISCONNECTED] every 2 s while no app sends; its reply proves the Arduino is running.")
            elif self._family == "multi_client":
                notes.append("Camera firmware 1.5.3+: LOCKED IN sends a text \"ping\" each second, which the ESP32 answers and never forwards to the Arduino.")
        if not self._patched:
            notes.append("No sensor telemetry from stock firmware: ultrasonic/IR/battery unavailable.")
        last = None
        if self._last_msg is not None:
            last = {k: v for k, v in self._last_msg.items() if k != "mono"}
            last["age_s"] = round(now - self._last_msg["mono"], 2)
        return RobotStatus(
            transport="zeus",
            simulated=False,
            connected=self._connected,
            link_state=self._link_state,  # type: ignore[arg-type]
            target=f"ws://{self.host}:{self.port}",
            last_error=self._last_error,
            connected_since=self._connected_since if self._connected else None,
            # Anything received from the robot counts, WebSocket pongs included (a monitor link's
            # data messages may be seconds apart).
            last_rx_age_s=None if self._last_rx is None else round(now - max(self._last_rx, self._last_pong_rx or 0.0), 3),
            last_tx_age_s=None if not self._last_tx else round(now - self._last_tx, 3),
            rtt_ms=round(rtts[len(rtts) // 2], 1) if rtts else None,
            rtt_kind=rtt_kind,  # type: ignore[arg-type]
            frames_sent=self._frames_sent,
            frames_received=self._frames_received,
            motion="idle" if m is None else f"{m.primitive.value}@{m.speed}",
            battery_v=None,
            firmware={
                **self.firmware,
                "arduino_messages": self._arduino_msgs,
                "arduino_last_age_s": None if self._last_arduino_rx is None else round(now - self._last_arduino_rx, 3),
                "esp32_last_age_s": None if self._last_esp_rx is None else round(now - self._last_esp_rx, 3),
                "ping_rtt_ms": round(sorted(self._ping_rtt_ms)[len(self._ping_rtt_ms) // 2], 1) if self._ping_rtt_ms else None,
                "ping_timeouts": self._ping_timeouts,
                "text_ping_rtt_ms": round(sorted(self._app_rtt_ms)[len(self._app_rtt_ms) // 2], 1) if self._app_rtt_ms else None,
                "locked_in_patch": self._patched,
                "unknown_patch_version": self._unknown_patch,
                "locked_in_patch_version": self._telemetry.firmware_patch,
                "device_mode": self._telemetry.device_mode,
                "device_watchdog_trips": self._telemetry.device_watchdog_trips,
                "device_free_ram": self._telemetry.device_free_ram,
                "tx_frozen": self.tx_frozen,
                "recent_frames": list(self.frame_log)[-12:],
            },
            last_message=last,
            message_counts=dict(self._msg_counts),
            capabilities=TransportCapabilities(
                simulated=False,
                obstacle_sensing=self._patched and self._telemetry.ultrasonic_cm is not None,
                battery_sensing=False,
                device_watchdog="verified" if self.device_watchdog_verified else "unverified",
                device_watchdog_timeout_s=self.device_watchdog_timeout_s,
                motion_ack="none" if monitor else "link_echo",
                camera_stream_url=self.firmware.get("video_url"),
                motion_permitted=self.motion_permitted,
                motion_lock_reasons=self.motion_lock_reasons(),
                link_mode=self.link_mode,
                notes=notes,
            ),
        )

    def get_telemetry(self) -> RobotTelemetry:
        return self._telemetry
