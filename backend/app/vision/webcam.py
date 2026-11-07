"""Camera capture thread with a single-slot "latest frame" buffer.

If inference is slower than the camera, older frames are overwritten (counted as
``dropped``) instead of queueing up: the pipeline always works on the newest frame,
so latency stays bounded. Sources:

* ``"0"``, ``"1"`` ...  local camera index (AVFoundation on macOS); "0" is the Mac's webcam
* ``http://...``        MJPEG stream, e.g. the Zeus ESP32-CAM at http://192.168.4.1:9000/mjpg,
                        read with LOCKED IN's multipart reader (default) or OpenCV's FFmpeg capture
* a file path           recorded clip, for offline evaluation (paced to its native FPS)

A source that stops delivering is reconnected in the background (state "reconnecting");
it is never silently replaced by another camera. Frames are kept in memory only; nothing is
written to disk.
"""

from __future__ import annotations

import logging
import os
import platform
import threading
import time
from pathlib import Path

import numpy as np

from .mjpeg import FrameClock, MjpegError, MjpegReader

log = logging.getLogger(__name__)

URL_OPEN_TIMEOUT_MS = 3000
URL_READ_TIMEOUT_MS = 2000
STALL_S = 2.0  # no frame for this long: reconnect
RETRY_S = (0.5, 1.0, 2.0, 3.0)
FLIP_CODES = {"horizontal": 1, "vertical": 0, "both": -1}


def source_kind(source: str) -> str:
    s = source.strip()
    if s.isdigit():
        return "index"
    if s.startswith(("http://", "https://", "rtsp://")):
        return "url"
    return "file"


def _open_capture(source: str, width: int, height: int):
    import cv2

    kind = source_kind(source)
    if kind == "index":
        backend = cv2.CAP_AVFOUNDATION if platform.system() == "Darwin" else cv2.CAP_ANY
        cap = cv2.VideoCapture(int(source), backend)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    elif kind == "url":
        # Without these FFmpeg can block for a long time on an unreachable host.
        params = [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, URL_OPEN_TIMEOUT_MS, cv2.CAP_PROP_READ_TIMEOUT_MSEC, URL_READ_TIMEOUT_MS]
        cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG, params)
    else:
        if not Path(source).exists():
            return None, f"file not found: {source}"
        cap = cv2.VideoCapture(source)
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass
    if not cap.isOpened():
        cap.release()
        if kind == "index" and platform.system() == "Darwin":
            return None, (
                f"could not open camera {source}. On macOS, grant Camera access to the app running the "
                "backend (System Settings > Privacy & Security > Camera) and close other apps using it."
            )
        return None, f"could not open {kind} source {source}"
    return cap, ""


class CameraSource:
    def __init__(
        self,
        source: str,
        width: int = 640,
        height: int = 480,
        mirror: bool | None = None,
        *,
        label: str | None = None,
        flip: str = "none",
        mjpeg_backend: str = "http",
    ):
        self.source = str(source)
        self.kind = source_kind(self.source)
        self.label = label or self.source
        self.width = width
        self.height = height
        # Self-view webcams are mirrored so moving your hand right moves it right on screen.
        self.mirror = (self.kind == "index") if mirror is None else mirror
        self.flip = flip if flip in FLIP_CODES else "none"
        if self.kind == "url":
            self.backend = "http" if mjpeg_backend == "http" and self.source.startswith("http://") else "opencv-ffmpeg"
        else:
            self.backend = "opencv"
        self._cond = threading.Condition()
        self._latest: tuple[int, np.ndarray, float] | None = None
        self._consumed_id = 0
        self._frame_id = 0
        self._running = False
        self._thread: threading.Thread | None = None
        self._reader: MjpegReader | None = None
        self.state = "offline"
        self.reason = "not started"
        self.fps = 0.0
        self.dropped = 0
        self.frames = 0
        self.decode_errors = 0
        self.connects = 0  # successful opens
        self.attempts = 0  # failed opens since the last frame
        self.next_retry_at: float | None = None
        self.last_error: str | None = None
        self.actual_size: tuple[int, int] | None = None
        self.opened_at: float | None = None
        self.last_frame_mono: float | None = None
        self.clock = FrameClock()
        self.on_state_change = lambda state, reason: None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._set_state("opening", f"opening {self.label}")
        self._thread = threading.Thread(target=self._run, name=f"camera-{self.source}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        reader = self._reader
        if reader is not None:
            reader.interrupt()  # unblocks a read in progress; the capture thread closes it
        with self._cond:
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._set_state("offline", "stopped")

    def read_latest(self, after_id: int, timeout: float = 0.5) -> tuple[int, np.ndarray, float] | None:
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._running and (self._latest is None or self._latest[0] <= after_id):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
            if self._latest is None or self._latest[0] <= after_id:
                return None
            self._consumed_id = self._latest[0]
            return self._latest

    def status(self) -> dict:
        now = time.monotonic()
        c = self.clock
        arrival = c.arrival_fps(now)
        device_fps = c.device_fps()
        return {
            "state": self.state,
            "reason": self.reason,
            "source": self.source,
            "label": self.label,
            "kind": self.kind,
            "backend": self.backend,
            "mirror": self.mirror,
            "flip": self.flip,
            "fps": round(arrival, 1) if arrival is not None else round(self.fps, 1),
            "device_fps": None if device_fps is None else round(device_fps, 1),
            "frames": self.frames,
            "dropped": self.dropped,
            "missed": c.missed,
            "decode_errors": self.decode_errors,
            "size": self.actual_size,
            "kbps": None if self.opened_at is None or not c.bytes else round(c.bytes * 8 / 1000 / max(1.0, now - self.opened_at)),
            "frame_kb": None if not c.bytes else round(c.bytes / c.frames / 1024, 1),
            "frame_age_ms": None if self.last_frame_mono is None else round((now - self.last_frame_mono) * 1000),
            "delay_above_best_ms_p50": c.delay_ms(0.5),
            "delay_above_best_ms_p95": c.delay_ms(0.95),
            "connects": self.connects,
            "reconnects": max(0, self.connects - 1),
            "attempts": self.attempts,
            "next_retry_in_s": None if self.next_retry_at is None else round(max(0.0, self.next_retry_at - now), 1),
            "last_error": self.last_error,
        }

    def _set_state(self, state: str, reason: str) -> None:
        changed = state != self.state
        self.state, self.reason = state, reason
        if changed:
            try:
                self.on_state_change(state, reason)
            except Exception:
                log.exception("camera state callback failed")

    # ------------------------------------------------------------------ delivery
    def _deliver(self, frame: np.ndarray, now: float) -> None:
        import cv2

        if self.mirror:
            frame = cv2.flip(frame, 1)
        if self.flip != "none":
            frame = cv2.flip(frame, FLIP_CODES[self.flip])
        self.actual_size = (frame.shape[1], frame.shape[0])
        self.last_frame_mono = now
        with self._cond:
            if self._latest is not None and self._latest[0] > self._consumed_id:
                self.dropped += 1
            self._frame_id += 1
            self._latest = (self._frame_id, frame, now)
            self._cond.notify_all()
        self.frames += 1
        if self.state != "live":
            self.attempts = 0
            self.next_retry_at = None
            self._set_state("live", f"{self.label} delivering frames")
        prev = getattr(self, "_prev_t", None)
        if prev is not None and now > prev:
            inst = 1.0 / (now - prev)
            self.fps = inst if self.fps == 0 else 0.9 * self.fps + 0.1 * inst
        self._prev_t = now

    def _wait_retry(self, err: str) -> None:
        """Record the failure and wait before the next attempt, waking early on stop()."""
        self.last_error = err
        self.attempts += 1
        delay = RETRY_S[min(self.attempts - 1, len(RETRY_S) - 1)]
        self.next_retry_at = time.monotonic() + delay
        state = "reconnecting" if self.connects or self.attempts > 1 else "opening"
        self._set_state(state, f"{err}; retrying in {delay:g}s")
        with self._cond:
            self._cond.wait_for(lambda: not self._running, timeout=delay)

    # ------------------------------------------------------------------ threads
    def _run(self) -> None:
        try:
            if self.backend == "http":
                self._run_http()
            else:
                self._run_opencv()
        finally:
            self._running = False

    def _run_http(self) -> None:
        import cv2

        while self._running:
            reader = MjpegReader(self.source, connect_timeout=URL_OPEN_TIMEOUT_MS / 1000, read_timeout=STALL_S)
            self._reader = reader
            try:
                reader.open()
                if not self._running:
                    break
                self.connects += 1
                self.opened_at = time.monotonic()
                self.clock = FrameClock()
                while self._running:
                    part = reader.read_part()
                    frame = cv2.imdecode(np.frombuffer(part.jpeg, np.uint8), cv2.IMREAD_COLOR)
                    if frame is None:
                        self.decode_errors += 1  # a corrupt JPEG: skip it, keep the stream
                        continue
                    self.clock.add(part.arrived, part.device_ts, len(part.jpeg), time.time())
                    self._deliver(frame, part.arrived)
            except MjpegError as exc:
                if not self._running:
                    break
                self._wait_retry(str(exc))
            except Exception as exc:  # never let the capture thread die silently
                if not self._running:
                    break
                log.exception("MJPEG reader failed")
                self._wait_retry(f"{type(exc).__name__}: {exc}")
            finally:
                reader.close()
                self._reader = None

    def _run_opencv(self) -> None:
        import cv2

        cap = None
        last_good = time.monotonic()
        frame_interval = None
        try:
            while self._running:
                if cap is None:
                    cap, err = _open_capture(self.source, self.width, self.height)
                    if cap is None:
                        if self.kind == "file":
                            self._set_state("offline", err)
                            return
                        self._wait_retry(err)
                        continue
                    self.connects += 1
                    self.opened_at = time.monotonic()
                    last_good = time.monotonic()
                    self.clock = FrameClock()
                    if self.kind == "file":
                        native = cap.get(cv2.CAP_PROP_FPS) or 30.0
                        frame_interval = 1.0 / native
                ok, frame = cap.read()
                now = time.monotonic()
                if not ok or frame is None:
                    if self.kind == "file":
                        self._set_state("offline", "end of file")
                        return
                    if now - last_good > STALL_S:
                        cap.release()
                        cap = None
                        self._wait_retry(f"{self.label} stopped delivering frames")
                    else:
                        time.sleep(0.01)
                    continue
                last_good = now
                self.clock.add(now, None, 0)
                self._deliver(frame, now)
                if frame_interval:
                    time.sleep(max(0.0, frame_interval - (time.monotonic() - now)))
        finally:
            if cap is not None:
                cap.release()


def probe_cameras(max_index: int = 4, skip: str | None = None) -> list[dict]:
    """Try local camera indices. Slow on macOS (~0.5 s each); call from a worker thread."""

    out = []
    for i in range(max_index):
        if skip is not None and str(i) == str(skip):
            out.append({"index": i, "available": True, "in_use": True})
            continue
        cap, err = _open_capture(str(i), 640, 480)
        if cap is None:
            out.append({"index": i, "available": False, "reason": err})
            continue
        ok, frame = cap.read()
        out.append({"index": i, "available": bool(ok), "size": None if not ok else [frame.shape[1], frame.shape[0]]})
        cap.release()
    if os.environ.get("LOCKEDIN_EXTRA_CAMERA"):
        out.append({"index": os.environ["LOCKEDIN_EXTRA_CAMERA"], "available": None, "note": "from LOCKEDIN_EXTRA_CAMERA"})
    return out
