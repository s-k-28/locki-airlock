"""A behavioural model of the Zeus Car's ESP32-CAM + Arduino, written from the upstream
sources, for protocol tests. It is NOT the hardware; passing tests here means our frames
match what the published firmware parses, not that a physical car moved.

Mirrors:
* ai-camera-firmware src/ws_server.cpp  handleSunFounderController(), ping/pong, timeouts:
  "modern" = 1.5.3+ (multi-client: closes a client after 3 s without text, answers text
  "ping" itself, one [APPSTOP] after 3 s without data); "legacy" = 1.1-1.4.x and "legacy152"
  = 1.5.2 (single-client: "pong <ms>" on every event, text "ping" -> [APPSTOP], a silent
  client stays open while the Arduino is told "[DISCONNECTED] timeout" every 2 s; 1.5.2 adds
  StaIp/VideoTemplate to the check info)
* the Arduino's sendData() reply to every serial line (not only WS+ frames), relayed by the
  ESP32: to the last active client on single-client firmware, to all clients on 1.5.3+
* zeus-car Zeus_Car/ai_camera.cpp  getStrOf()/getIntOf()/getJoystick()
* zeus-car Zeus_Car/Zeus_Car.ino  onReceive() (F stop, K joystick, Q heading) and loop()
* optionally firmware/optional/locked_in_patch.h v2 (``patched=True``): 400 ms motion
  watchdog that calls stop() and latches until a Stop frame, plus the telemetry reply
"""

from __future__ import annotations

import asyncio
import json
import math
import time

import websockets

REGIONS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _as_string(v) -> str:
    if v is True:
        return "true"
    if v is False:
        return "false"
    if v is None:
        return "null"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def esp32_convert(payload: str) -> str:
    """handleSunFounderController(): JSON -> 'WS+v0;v1;...;v25'."""
    try:
        doc = json.loads(payload)
        if not isinstance(doc, dict):
            doc = {}
    except json.JSONDecodeError:
        doc = {}
    parts = []
    for r in REGIONS:
        v = doc.get(r)
        value = ",".join(_as_string(x) for x in v) if isinstance(v, list) else _as_string(v)
        if value == "true":
            value = "1"
        elif value == "false":
            value = "0"
        parts.append("" if value == "null" else value)
    return "WS+" + ";".join(parts)


def get_str_of(s: str, index: int, divider: str = ";") -> str:
    """ai_camera.cpp getStrOf()."""
    length = len(s)
    if index == 0:
        start = 0
    else:
        start, j = 0, 1
        found = False
        while start < length:
            if s[start] == divider:
                if index == j:
                    start += 1
                    found = True
                    break
                j += 1
            start += 1
        if not found:
            start = length
    end = start
    while end < length and s[end] != divider:
        end += 1
    return s[start:end]


def to_int(s: str) -> int:
    """Arduino String::toInt(): leading integer, else 0."""
    s = s.strip()
    sign, digits = 1, ""
    for i, ch in enumerate(s):
        if i == 0 and ch in "+-":
            sign = -1 if ch == "-" else 1
            continue
        if ch.isdigit():
            digits += ch
        else:
            break
    return sign * int(digits) if digits else 0


def get_joystick(buf: str, region: int) -> tuple[int, int]:
    value = get_str_of(buf, region, ";")
    x = to_int(get_str_of(value, 0, ","))
    y = to_int(get_str_of(value, 1, ","))
    return int(math.atan2(x, y) * 180.0 / math.pi), int(math.sqrt(x * x + y * y))


MODE_NONE, MODE_APP = 0, 7


class FakeArduino:
    """The subset of Zeus_Car.ino that matters for app control."""

    def __init__(self, patched: bool = False, watchdog_s: float = 0.4, patch_version: int = 2):
        self.patched = patched
        self.patch_version = patch_version  # what the telemetry reply reports as "li"
        self.watchdog_s = watchdog_s
        self.last_data = time.monotonic()
        self.trips = 0
        self.latched = False
        self.trip_times: list[float] = []
        self.ignored_while_latched = 0
        self.us_cm: float = 87.5
        self.ir_bits = 0b11  # both IR sensors clear
        self.mode = MODE_NONE
        self.ws_connected = False
        self.ir_or_app = False
        self.remote_angle = self.remote_power = self.remote_heading = 0
        self.app_angle = self.app_power = 0
        self.app_heading = self.app_heading_r = 0
        self.last_echo = 0.0
        self.lines: list[str] = []
        self.history: list[tuple[float, int, int, int, int]] = []
        self.forbidden_regions_seen: list[str] = []

    @property
    def motors_active(self) -> bool:
        return self.mode == MODE_APP and (self.remote_power != 0 or self.remote_heading != 0)

    @property
    def region_lines(self) -> list[str]:
        """Lines the ESP32 built from client frames ("WS+..."): the only input onReceive() acts on."""
        return [ln for ln in self.lines if ln.startswith("WS+")]

    def on_line(self, line: str) -> str | None:
        if line.startswith("[CAM_D]"):
            return None  # readInto() clears camera debug lines
        self.lines.append(line)
        if line.startswith(("[DISCONNECTED]", "[APPSTOP]", "[Init]")):
            self.ws_connected = False
        elif line.startswith("WS+"):
            self.ws_connected = True
            if self.patched:
                # lockedInOnReceive(): refresh the watchdog; while latched only Stop gets through
                self.last_data = time.monotonic()
                if self.latched and not to_int(get_str_of(line[3:], REGIONS.index("F"))):
                    self.ignored_while_latched += 1
                else:
                    self.latched = False
                    self._on_receive(line[3:])
            else:
                self._on_receive(line[3:])
        self._loop()
        now = time.monotonic()
        # AiCamera::loop(): sendData() after any non-empty line, at most every 60 ms.
        if now - self.last_echo > 0.060:
            self.last_echo = now
            return self.reply()
        return None

    def reply(self) -> str:
        """AiCamera::sendData(): the stock send_doc is empty ("null")."""
        if not self.patched:
            return "null"
        doc = {"li": self.patch_version, "us": self.us_cm, "ir": self.ir_bits, "md": self.mode, "wd": self.trips, "lk": int(self.latched), "fr": 487}
        return json.dumps(doc, separators=(",", ":"))

    def motors_stopped_at(self, after: float) -> float | None:
        """First loop pass after ``after`` in which the motors were not driven."""
        for t, mode, _angle, power, heading in self.history:
            if t >= after and not (mode == MODE_APP and (power != 0 or heading != 0)):
                return t
        return None

    def _on_receive(self, buf: str) -> None:
        self.ir_or_app = True
        for name in "CEGIJMNOP":
            if get_str_of(buf, REGIONS.index(name)) not in ("", "0"):
                self.forbidden_regions_seen.append(name)
        if to_int(get_str_of(buf, REGIONS.index("F"))):
            self.mode = MODE_NONE
            self.remote_angle = self.remote_power = self.remote_heading = 0
            return
        angle, power = get_joystick(buf, REGIONS.index("K"))
        if self.app_angle != angle or self.app_power != power or angle != 0 or power != 0:
            self.mode = MODE_APP
            self.app_angle, self.remote_angle = angle, angle
            self.app_power, self.remote_power = power, power
            self.app_heading = self.remote_heading = 0
        ha, hr = get_joystick(buf, REGIONS.index("Q"))
        if self.app_heading != 0 or self.app_heading_r != 0 or self.app_heading != ha or self.app_heading_r != hr:
            self.mode = MODE_APP
            self.remote_angle, self.remote_power = angle, power
            self.app_heading, self.app_heading_r = ha, hr
            self.remote_heading = ha

    def _loop(self) -> None:
        now = time.monotonic()
        if self.patched and self.mode == MODE_APP and now - self.last_data > self.watchdog_s:
            # lockedInLoop(): stop() (MODE_NONE, remote values zeroed), count, latch
            self.mode = MODE_NONE
            self.remote_angle = self.remote_power = self.remote_heading = 0
            self.trips += 1
            self.latched = True
            self.trip_times.append(now)
        if not self.ws_connected and self.ir_or_app:
            self.mode = MODE_NONE
        self.history.append((time.monotonic(), self.mode, self.remote_angle, self.remote_power, self.remote_heading))

    def tick(self) -> None:
        self._loop()


class FakeESP32:
    def __init__(
        self,
        variant: str = "modern",
        data_timeout_s: float = 3.0,
        pingpong_timeout_s: float = 3.0,
        check_period_s: float = 1.0,
        patched: bool = False,
        arduino_loop_s: float = 0.01,
    ):
        assert variant in ("modern", "legacy", "legacy152")
        self.variant = variant
        self.single_client = variant != "modern"
        self.arduino_loop_s = arduino_loop_s
        self._stall_until = 0.0
        self._stalled: list[str] = []
        self.data_timeout_s = data_timeout_s
        self.pingpong_timeout_s = pingpong_timeout_s if variant == "modern" else 2.0
        self._last_pong = 0.0
        self._last_client = None
        self.frozen = False
        self.check_period_s = check_period_s
        self.arduino = FakeArduino(patched=patched)
        self._loop_task: asyncio.Task | None = None
        self.server = None
        self.port = None
        self.received: list[tuple[float, str]] = []
        self.dropped = 0
        self.pings = 0
        self.appstops: list[float] = []
        self._last_forward = 0.0
        self._last_data = time.monotonic()
        self._last_any = time.monotonic()
        self._clients: set = set()
        self._data_timeout_sent = False
        self._watch: asyncio.Task | None = None
        self.stop_mode_at: float | None = None

    async def start(self, port: int = 0):
        self.server = await websockets.serve(self._handler, "127.0.0.1", port)
        self.port = self.server.sockets[0].getsockname()[1]
        self._watch = asyncio.create_task(self._watchdog())
        if self.arduino.patched:
            self._loop_task = asyncio.create_task(self._arduino_loop())
        return self

    async def close(self):
        if self._watch:
            self._watch.cancel()
        if self._loop_task:
            self._loop_task.cancel()
        self.server.close()
        await self.server.wait_closed()

    async def drop_all_clients(self):
        for ws in list(self._clients):
            await ws.close()

    def freeze(self, frozen: bool = True) -> None:
        """Wi-Fi gone without a TCP close: nothing is read (no pongs) and nothing is sent."""
        self.frozen = frozen
        for ws in list(self._clients):
            try:
                (ws.transport.pause_reading if frozen else ws.transport.resume_reading)()
            except Exception:
                pass

    def stall(self, seconds: float) -> None:
        """Wi-Fi stall: frames the client writes are held, then delivered together when the
        stall ends (TCP retransmission), so the Arduino sees silence followed by stale frames."""
        self._stall_until = time.monotonic() + seconds

    async def _arduino_loop(self):
        while True:
            await asyncio.sleep(self.arduino_loop_s)
            self.arduino.tick()

    async def _relay(self, reply: str | None, ws=None) -> None:
        """ESP32 serial handler: a "WS+..." line from the Arduino goes to the client(s)."""
        if reply is None or self.frozen:
            return
        targets = [ws or self._last_client] if self.single_client else list(self._clients)
        for c in targets:
            if c is not None:
                try:
                    await c.send(reply)
                except Exception:
                    pass

    async def _arduino_line(self, line: str, ws=None) -> None:
        await self._relay(self.arduino.on_line(line), ws)

    async def _event_pong(self, ws) -> None:
        """Single-client firmware sends "pong <ms>" at the top of every WebSocket event (<= 1 per 200 ms)."""
        now = time.monotonic()
        if self.single_client and now - self._last_pong > 0.2:
            self._last_pong = now
            await ws.send(f"pong {int(now * 1000)}")

    async def _handler(self, ws):
        self._clients.add(ws)
        self._last_client = ws
        self._last_any = self._last_data = time.monotonic()
        await self._event_pong(ws)
        await self._arduino_line("[CONNECTED] Connected client[0] 127.0.0.1", ws)
        if self.variant == "legacy":
            info = {"Name": "Zeus_Car", "Type": "Zeus_Car", "Check": "SC", "video": "http://192.168.4.1:9000/mjpg"}
            await ws.send(json.dumps(info))
            await ws.send(json.dumps(info))
        else:
            info = {
                "Name": "Zeus_Car", "Type": "Zeus_Car", "Check": "SC", "video": "http://192.168.4.1:9000/mjpg",
                "StaIp": "", "VideoTemplate": "http://ip:9000/mjpg",
            }
            await ws.send(json.dumps(info))
        try:
            async for raw in ws:
                now = time.monotonic()
                self._last_any = now
                self._last_client = ws
                self.received.append((now, raw))
                await self._event_pong(ws)
                if raw == "ping":
                    self.pings += 1
                    if self.variant == "modern":
                        await ws.send(f"pong {int(now * 1000)}")
                    else:
                        self.appstops.append(now)
                        await self._arduino_line("[APPSTOP]", ws)
                    continue
                if raw.startswith("SET+"):
                    raise AssertionError("client must never send SET+ (Wi-Fi reconfiguration)")
                if now < self._stall_until:
                    self._stalled.append(raw)
                    continue
                backlog, self._stalled = self._stalled + [raw], []
                for frame in backlog:
                    self._last_data = now
                    self._data_timeout_sent = False
                    if time.monotonic() - self._last_forward <= 0.020:
                        self.dropped += 1
                        continue
                    self._last_forward = time.monotonic()
                    echo = self.arduino.on_line(esp32_convert(frame))
                    if echo is not None:
                        await ws.send(echo)
        finally:
            self._clients.discard(ws)
            if self._last_client is ws:
                self._last_client = next(iter(self._clients), None)
            await self._arduino_line("[DISCONNECTED] Disconnected client[0]")

    async def _watchdog(self):
        while True:
            await asyncio.sleep(self.check_period_s)
            now = time.monotonic()
            self.arduino.tick()
            if not self._clients:
                continue
            if now - self._last_any > self.pingpong_timeout_s:
                if self.variant == "modern":
                    for ws in list(self._clients):
                        await ws.close()
                else:
                    await self._arduino_line("[DISCONNECTED] timeout")
                    self._last_any = now
            if self.variant == "modern" and now - self._last_data > self.data_timeout_s and not self._data_timeout_sent:
                self._data_timeout_sent = True
                self.appstops.append(now)
                await self._arduino_line("[APPSTOP] Data timeout from all clients")
