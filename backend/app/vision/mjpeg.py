"""MJPEG over HTTP: the Zeus ESP32-CAM stream (and any multipart/x-mixed-replace source).

What the ESP32 sends (ai-camera-firmware camera_server.cpp, port 9000, path /mjpg; same in
1.3.0 and 1.5.4):

    HTTP/1.1 200 OK
    Content-Type: multipart/x-mixed-replace;boundary=123456789000000000000987654321
    Transfer-Encoding: chunked                    (esp_http_server httpd_resp_send_chunk)

    \\r\\n--123456789000000000000987654321\\r\\n
    Content-Type: image/jpeg\\r\\nContent-Length: <n>\\r\\nX-Timestamp: <sec>.<usec>\\r\\n\\r\\n
    <n bytes of JPEG>                             (repeated per frame)

``MjpegReader`` parses that byte stream and hands back each JPEG with the time it arrived and
the camera's own capture timestamp; frames are decoded with OpenCV by the caller. Reads use
socket timeouts, and ``close()`` shuts the socket down from any thread, so stopping or
switching cameras never waits on a dead network. ``FrameClock`` turns the camera timestamps
into frame-rate, missed-frame and delivery-delay figures.
"""

from __future__ import annotations

import http.client
import socket
import statistics
import time
from collections import deque
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_FRAME_BYTES = 2_000_000
MAX_HEADER_BYTES = 4096


class MjpegError(Exception):
    """The stream could not be opened or broke; the message is meant for the dashboard."""


@dataclass
class MjpegPart:
    jpeg: bytes
    arrived: float  # time.monotonic() when the last byte of the part was read
    device_ts: float | None  # X-Timestamp: the camera's capture time (its own clock, seconds)


def boundary_from(content_type: str) -> bytes | None:
    """``multipart/x-mixed-replace;boundary=XYZ`` -> b"XYZ" (quotes and a leading -- tolerated)."""
    ct = content_type or ""
    if "multipart" not in ct.lower():
        return None
    for param in ct.split(";")[1:]:
        key, _, value = param.strip().partition("=")
        if key.strip().lower() == "boundary" and value:
            b = value.strip().strip('"')
            if b.startswith("--"):
                b = b[2:]
            return b.encode("latin-1")
    return None


class MjpegReader:
    def __init__(self, url: str, connect_timeout: float = 2.0, read_timeout: float = 2.0):
        u = urlsplit(url)
        if u.scheme != "http" or not u.hostname:
            raise MjpegError(f"not an http:// MJPEG URL: {url}")
        self.url = url
        self.host = u.hostname
        self.port = u.port or 80
        self.path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self._conn: http.client.HTTPConnection | None = None
        self._resp: http.client.HTTPResponse | None = None
        self._buf = bytearray()
        self._marker = b""
        self._closed = False
        self.bytes_read = 0
        self.content_type = ""

    # ------------------------------------------------------------------ lifecycle
    def open(self) -> None:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.connect_timeout)
        self._conn = conn
        try:
            conn.connect()
        except TimeoutError as exc:
            raise MjpegError(f"no answer from {self.host}:{self.port} within {self.connect_timeout:g}s") from exc
        except OSError as exc:
            raise MjpegError(f"cannot reach {self.host}:{self.port} ({exc.strerror or exc})") from exc
        if self._closed:
            raise MjpegError("closed")
        assert conn.sock is not None
        conn.sock.settimeout(self.read_timeout)
        try:
            conn.request("GET", self.path, headers={"Accept": "multipart/x-mixed-replace, */*", "Connection": "close"})
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            raise MjpegError(f"{self.host}:{self.port} did not answer the request ({exc})") from exc
        if resp.status != 200:
            raise MjpegError(f"{self.url} answered HTTP {resp.status} {resp.reason}")
        self.content_type = resp.getheader("Content-Type", "") or ""
        boundary = boundary_from(self.content_type)
        if boundary is None:
            raise MjpegError(f"{self.url} is not an MJPEG stream (Content-Type {self.content_type or 'missing'})")
        self._resp = resp
        self._marker = b"--" + boundary

    def interrupt(self) -> None:
        """Safe from any thread: a read blocked in the reading thread fails at once. Only shuts
        the socket down; the reading thread closes the connection (http.client is not
        thread-safe)."""
        self._closed = True
        conn = self._conn
        sock = conn.sock if conn is not None else None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def close(self) -> None:
        """Call from the reading thread."""
        self.interrupt()
        conn = self._conn
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ reading
    def read_part(self) -> MjpegPart:
        buf, marker = self._buf, self._marker
        while True:
            start = buf.find(marker)
            if start < 0:
                # Keep a tail in case the marker is split across reads.
                if len(buf) > len(marker):
                    del buf[: len(buf) - len(marker)]
                self._fill()
                continue
            hdr_start = start + len(marker)
            if buf[hdr_start : hdr_start + 2] == b"--":
                raise MjpegError("the camera ended the stream")
            hdr_end = buf.find(b"\r\n\r\n", hdr_start)
            if hdr_end < 0:
                if len(buf) - hdr_start > MAX_HEADER_BYTES:
                    raise MjpegError("malformed MJPEG part (headers too long)")
                self._fill()
                continue
            headers = _parse_headers(bytes(buf[hdr_start:hdr_end]))
            body = hdr_end + 4
            length = headers.get("content-length")
            if length is not None:
                try:
                    n = int(length)
                except ValueError as exc:
                    raise MjpegError(f"malformed Content-Length {length!r}") from exc
                if not 0 < n <= MAX_FRAME_BYTES:
                    raise MjpegError(f"implausible frame size {n} bytes")
                while len(buf) < body + n:
                    self._fill()
                jpeg = bytes(buf[body : body + n])
                del buf[: body + n]
            else:
                nxt = buf.find(marker, body)
                while nxt < 0:
                    if len(buf) - body > MAX_FRAME_BYTES:
                        raise MjpegError("frame without Content-Length exceeds the size limit")
                    self._fill()
                    nxt = buf.find(marker, body)
                jpeg = bytes(buf[body:nxt]).rstrip(b"\r\n")
                del buf[:nxt]
            return MjpegPart(jpeg, time.monotonic(), _timestamp(headers.get("x-timestamp")))

    def _fill(self) -> None:
        if self._closed or self._resp is None:
            raise MjpegError("closed")
        try:
            chunk = self._resp.read1(65536)
        except TimeoutError as exc:
            raise MjpegError(f"no data from the camera for {self.read_timeout:g}s") from exc
        except (OSError, http.client.HTTPException, ValueError, AttributeError) as exc:
            # AttributeError: http.client tidying up after the socket was shut down under it.
            if self._closed:
                raise MjpegError("closed") from exc
            raise MjpegError(f"stream broke ({type(exc).__name__}: {exc})") from exc
        if not chunk:
            raise MjpegError("the camera closed the connection")
        self.bytes_read += len(chunk)
        self._buf += chunk


def _parse_headers(raw: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in raw.decode("latin-1").split("\r\n"):
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip().lower()] = value.strip()
    return out


def _timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        sec, _, usec = value.partition(".")
        return int(sec) + (int(usec) / 1_000_000 if usec else 0.0)
    except ValueError:
        return None


class FrameClock:
    """Per-stream timing: arrival FPS, the camera's own FPS, frames the camera produced that
    never arrived (gaps in X-Timestamp), and delivery delay above the fastest frame seen.

    The camera's clock is not synchronised with the Mac's, so absolute one-way latency cannot
    be measured; ``delay_above_best`` is how much later than the best-case delivery a frame
    arrived (queueing in Wi-Fi / TCP), which is what grows when the link struggles."""

    def __init__(self, window: int = 300):
        self.arrivals: deque[float] = deque(maxlen=90)
        self.device_dts: deque[float] = deque(maxlen=60)
        self.offsets: deque[float] = deque(maxlen=window)
        self.delays_ms: deque[float] = deque(maxlen=120)
        self.last_device_ts: float | None = None
        self.missed = 0
        self.frames = 0
        self.bytes = 0

    def add(self, arrived_mono: float, device_ts: float | None, nbytes: int, arrived_wall: float | None = None) -> None:
        self.frames += 1
        self.bytes += nbytes
        self.arrivals.append(arrived_mono)
        if device_ts is None:
            return
        if self.last_device_ts is not None:
            dt = device_ts - self.last_device_ts
            if dt <= 0 or dt > 30:
                self.offsets.clear()  # camera rebooted or clock jumped: start over
            else:
                if len(self.device_dts) >= 10:
                    med = statistics.median(self.device_dts)
                    if med > 0 and dt > 1.8 * med:
                        self.missed += max(0, round(dt / med) - 1)
                self.device_dts.append(dt)
        self.last_device_ts = device_ts
        offset = (arrived_wall if arrived_wall is not None else arrived_mono) - device_ts
        self.offsets.append(offset)
        self.delays_ms.append((offset - min(self.offsets)) * 1000.0)

    def arrival_fps(self, now: float | None = None) -> float | None:
        a = self.arrivals
        if len(a) < 2:
            return None
        now = time.monotonic() if now is None else now
        if now - a[-1] > 2.0:
            return 0.0
        span = a[-1] - a[0]
        return (len(a) - 1) / span if span > 0 else None

    def device_fps(self) -> float | None:
        if len(self.device_dts) < 5:
            return None
        med = statistics.median(self.device_dts)
        return 1.0 / med if med > 0 else None

    def delay_ms(self, q: float) -> float | None:
        if len(self.delays_ms) < 5:
            return None
        v = sorted(self.delays_ms)
        return round(v[min(len(v) - 1, int(q * len(v)))], 1)
