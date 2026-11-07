"""Vision service: camera thread -> processing thread -> events to the runtime.

The processing thread always takes the newest frame (stale frames are dropped by
the camera buffer), runs hand landmarks every frame and face detection every third
frame, updates the reach detector and focus estimator, and renders an annotated
JPEG only when someone is watching the stream. Raw frames never leave memory.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

import numpy as np

from ..config import PhoneZone, VisionTuning
from .focus_observation import FaceObs, FocusEstimator
from .hands import HAND_CONNECTIONS, FaceObserver, HandTracker
from .reach import HandObs, ReachDetector, ReachEvent
from .robot_tracker import (
    DISABLED,
    HELD,
    LOST,
    TRACKING,
    MarkerDetector,
    MarkerObservation,
    RobotTracker,
    ZoneAnchor,
)
from .webcam import CameraSource

log = logging.getLogger(__name__)

CYAN = (255, 240, 0)  # BGR for #00F0FF
MAGENTA = (214, 43, 255)  # BGR for #FF2BD6
AMBER = (0, 190, 255)
WHITE = (240, 240, 240)
FACE_EVERY_N = 3
STATIC_MARKER_SCAN_EVERY_N = 5
EGO_SETTLE_S = 0.6  # robot-mounted camera: reach stays paused this long after a motion ends


def _pct(values, q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return round(v[min(len(v) - 1, int(len(v) * q))], 2)


class VisionService:
    def __init__(self, models_dir: Path, emit: Callable, width: int = 640, height: int = 480):
        self.models_dir = Path(models_dir)
        self.emit = emit
        self.width = width
        self.height = height
        self._lock = threading.Lock()
        self.static_zone = list(PhoneZone().points)
        self.zone = list(self.static_zone)  # the zone in effect this frame (static or tracked)
        self.tuning = VisionTuning()
        self.detector = ReachDetector(self.zone, self.tuning)
        # Robot tracking (ArUco): the zone follows markers on the robot when anchored.
        self.zone_mode = "static"
        self.marker_ids: list[int] = [7, 8]
        self.marker_dictionary = "DICT_4X4_50"
        self.marker_detector: MarkerDetector | None = None
        self.tracker = RobotTracker(None)
        self._anchor_dict: dict | None = None
        self._last_frame: tuple[np.ndarray, bool] | None = None
        self.last_markers: list[MarkerObservation] = []
        self.last_markers_t = 0.0
        self._marker_ms: deque[float] = deque(maxlen=150)
        self.focus = FocusEstimator()
        self.camera: CameraSource | None = None
        self.hands_model: HandTracker | None = None
        self.face_model: FaceObserver | None = None
        self.models_error: str | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._jpeg: tuple[int, bytes] | None = None
        self.stream_clients = 0
        self.show_video = True
        self.last_hands: list[HandObs] = []
        self.last_face: FaceObs | None = None
        self.last_reach: dict | None = None
        self._hand_ms: deque[float] = deque(maxlen=150)
        self._face_ms: deque[float] = deque(maxlen=60)
        self._total_ms: deque[float] = deque(maxlen=150)
        self._latency_ms: deque[float] = deque(maxlen=150)
        self._proc_fps = 0.0
        self._processed = 0
        self._frames_seen = 0
        self._last_ts_ms = 0
        self._stage: dict[str, deque[float]] = {k: deque(maxlen=150) for k in ("markers", "to_rgb", "hands", "reach", "face", "render")}
        self._last_proc_t: float | None = None
        self._camera_was_live = False
        # Where the camera sits. "robot": it rides on the robot (the Zeus ESP32-CAM), so the
        # robot's own markers are never in view and every robot motion moves the whole image.
        self.mount = "fixed"
        self._robot_moving = False
        self._ego_settle_until = 0.0
        self._ego_paused = False

    # ------------------------------------------------------------------ control
    def load_models(self) -> None:
        """Loading the MediaPipe graphs holds the GIL for a few hundred ms; the runtime calls
        this at startup so that stall never lands during a session or a motion."""
        if self.hands_model is None:
            self.hands_model = HandTracker(self.models_dir)
        if self.face_model is None:
            try:
                self.face_model = FaceObserver(self.models_dir)
            except Exception as exc:  # focus is optional; reach detection is not
                log.warning("face model unavailable: %s", exc)
                self.models_error = f"face model unavailable: {exc}"

    def close_models(self) -> None:
        """Release the MediaPipe graphs. Must run while MediaPipe's dispatcher is alive (at runtime
        shutdown): left to the garbage collector at interpreter exit, HandLandmarker.__del__
        waits on a dispatcher that is already gone and the process never exits."""
        for attr in ("hands_model", "face_model"):
            model = getattr(self, attr)
            setattr(self, attr, None)
            if model is not None:
                try:
                    model.close()
                except Exception:
                    log.debug("closing %s failed", attr, exc_info=True)

    def start(self, source: str, *, label: str | None = None, mount: str = "fixed", flip: str = "none", mjpeg_backend: str = "http") -> None:
        self.stop()
        try:
            self.load_models()
        except Exception as exc:
            self.models_error = str(exc)
            raise
        self.mount = mount if mount in ("fixed", "robot") else "fixed"
        self._ego_paused = False
        self.camera = CameraSource(source, self.width, self.height, label=label, flip=flip, mjpeg_backend=mjpeg_backend)
        self.camera.on_state_change = self._on_camera_state
        self.detector = ReachDetector(self.zone, self.tuning)
        self.focus.reset()
        self._running = True
        self.camera.start()
        self._thread = threading.Thread(target=self._run, name="vision-processing", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._running and self.camera is None:
            return
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        if self.camera is not None:
            self.camera.stop()
            self.camera = None
        for ev in self.detector.reset("vision stopped"):
            self._emit_reach(ev, None)
        self._jpeg = None
        self._camera_was_live = False

    def configure(
        self,
        zone=None,
        tuning: VisionTuning | None = None,
        zone_mode: str | None = None,
        anchor: dict | None = None,
        marker_ids: list[int] | None = None,
        marker_dictionary: str | None = None,
        hold_s: float | None = None,
    ) -> None:
        with self._lock:
            if zone is not None:
                self.static_zone = [tuple(p) for p in zone]
            if tuning is not None:
                self.tuning = tuning
            if marker_ids is not None or marker_dictionary is not None:
                self.marker_ids = list(marker_ids if marker_ids is not None else self.marker_ids)
                self.marker_dictionary = marker_dictionary or self.marker_dictionary
                self.marker_detector = None  # rebuilt lazily with the new settings
            if hold_s is not None:
                self.tracker.cfg.hold_s = hold_s
            if zone_mode is not None:
                wanted = anchor if zone_mode == "marker" else None
                # Settings are re-applied whenever anything changes (e.g. adaptation during a
                # session); only a genuinely new anchor may reset tracking.
                if zone_mode != self.zone_mode or wanted != self._anchor_dict:
                    self.zone_mode = zone_mode
                    self._anchor_dict = wanted
                    self.tracker.set_anchor(ZoneAnchor.from_dict(wanted) if wanted else None)
                    if not self._ego_paused:
                        self.detector.set_paused(self.effective_zone_mode == "marker")
            if self.effective_zone_mode != "marker":
                self.zone = list(self.static_zone)
                if not self._ego_paused:
                    self.detector.set_paused(False)
            self.detector.configure(self.zone, self.tuning)

    def set_robot_moving(self, moving: bool) -> None:
        """Called by the runtime when a motion starts or ends (from any thread)."""
        with self._lock:
            self.tracker.set_robot_moving(moving)
            if self._robot_moving and not moving:
                self._ego_settle_until = time.monotonic() + EGO_SETTLE_S
            self._robot_moving = moving

    @property
    def effective_zone_mode(self) -> str:
        """A robot-mounted camera cannot see the robot's markers: its zone is fixed in the
        image, which is already robot-relative (the space in front of the robot)."""
        return "static" if self.mount == "robot" else self.zone_mode

    def attach_zone(self, zone_points) -> dict:
        """Express ``zone_points`` (display coordinates) relative to the markers visible now."""
        with self._lock:
            fresh = self.last_markers if time.monotonic() - self.last_markers_t < 0.5 else []
        if not fresh and self._last_frame is not None:
            fresh = self._markers(*self._last_frame)
        with self._lock:
            anchor = ZoneAnchor.attach(zone_points, fresh)
            self.static_zone = [tuple(p) for p in zone_points]
            self.zone_mode = "marker"
            self._anchor_dict = anchor.to_dict()
            self.tracker.set_anchor(anchor)
            self.detector.set_paused(True)
        return self._anchor_dict

    def _markers(self, bgr: np.ndarray, mirrored: bool) -> list[MarkerObservation]:
        import cv2

        if self.marker_detector is None:
            self.marker_detector = MarkerDetector(self.marker_dictionary, self.marker_ids)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        return self.marker_detector.detect(gray, mirrored)

    # ------------------------------------------------------------------ outputs
    def latest_jpeg(self) -> bytes | None:
        item = self._jpeg
        return None if item is None else item[1]

    def latest_jpeg_with_id(self) -> tuple[int, bytes] | None:
        return self._jpeg

    def snapshot(self) -> dict:
        cam = self.camera.status() if self.camera is not None else None
        running = self._running and self.camera is not None
        camera_state = "offline" if cam is None else ("live" if cam["state"] == "live" else cam["state"])
        face = self.last_face
        return {
            "running": running,
            "camera": camera_state,
            "reason": None if cam is None else cam["reason"],
            "source": None if cam is None else cam["source"],
            "source_label": None if cam is None else cam["label"],
            "source_kind": None if cam is None else cam["kind"],
            "backend": None if cam is None else cam["backend"],
            "mount": self.mount,
            "mirror": None if cam is None else cam["mirror"],
            "flip": None if cam is None else cam["flip"],
            "frame_size": None if cam is None else cam["size"],
            "link": None if cam is None else {k: cam[k] for k in (
                "connects", "reconnects", "attempts", "next_retry_in_s", "last_error", "frame_age_ms",
                "device_fps", "missed", "decode_errors", "kbps", "frame_kb",
                "delay_above_best_ms_p50", "delay_above_best_ms_p95",
            )},
            "ego_motion_paused": self._ego_paused,
            "stats": {
                "capture_fps": None if cam is None else cam["fps"],
                "processing_fps": round(self._proc_fps, 1),
                "frames_processed": self._processed,
                "frames_dropped": None if cam is None else cam["dropped"],
                "hand_ms_p50": _pct(self._hand_ms, 0.5),
                "hand_ms_p95": _pct(self._hand_ms, 0.95),
                "face_ms_p50": _pct(self._face_ms, 0.5),
                "total_ms_p50": _pct(self._total_ms, 0.5),
                "total_ms_p95": _pct(self._total_ms, 0.95),
                "capture_to_result_ms_p50": _pct(self._latency_ms, 0.5),
                "capture_to_result_ms_p95": _pct(self._latency_ms, 0.95),
                "marker_ms_p50": _pct(self._marker_ms, 0.5),
                "stages": self.stage_stats(),
            },
            "hands": len(self.last_hands),
            "detector": self.detector.snapshot(),
            "focus": {
                "state": self.focus.state.value,
                "presence_p": round(self.focus.p, 3),
                "face_present": bool(face and face.present),
                "yaw_ratio": None if not face or face.yaw_ratio is None else round(face.yaw_ratio, 3),
            },
            "last_reach": self.last_reach,
            "models": {"hands": self.hands_model is not None, "face": self.face_model is not None, "error": self.models_error},
            "stream_clients": self.stream_clients,
            "zone": self.zone if (self.effective_zone_mode != "marker" or self.tracker.state in (TRACKING, HELD)) else None,
            "zone_mode": self.effective_zone_mode,
            "zone_mode_setting": self.zone_mode,
            "tracking": {
                **self.tracker.snapshot(),
                "visible_markers": [o.marker_id for o in self.last_markers] if time.monotonic() - self.last_markers_t < 0.5 else [],
                "marker_ms_p50": _pct(self._marker_ms, 0.5),
                "dictionary": self.marker_dictionary,
                "ids": self.marker_ids,
            },
        }

    # ------------------------------------------------------------------ internals
    def _emit(self, kind: str, data: dict | None = None, confidence: float | None = None, frame_mono: float | None = None) -> None:
        from ..runtime import VisionEvent

        try:
            self.emit(VisionEvent(kind, data or {}, confidence, frame_mono))
        except Exception:
            log.exception("vision emit failed")

    def _emit_reach(self, ev: ReachEvent, frame_mono: float | None) -> None:
        if ev.kind in ("confirmed", "suppressed", "approaching"):
            self.last_reach = {"kind": ev.kind, "confidence": ev.confidence, "at": time.time(), **ev.data}
        self._emit(ev.kind, {**ev.data, "track": ev.track_id}, ev.confidence, frame_mono)

    def _on_camera_state(self, state: str, reason: str) -> None:
        if state == "live" and not self._camera_was_live:
            self._camera_was_live = True
            self._emit("camera_connected", {"source": self.camera.source if self.camera else None, "reason": reason})
        elif state != "live" and self._camera_was_live:
            self._camera_was_live = False
            self._emit("camera_disconnected", {"reason": reason})
            for ev in self.detector.reset("camera lost"):
                self._emit_reach(ev, None)
            change = self.focus.update(time.monotonic(), None, camera_ok=False)
            if change:
                self._emit("focus", {"state": change[0].value, "reason": change[2]}, change[1])

    def _run(self) -> None:
        last_id = 0
        while self._running:
            cam = self.camera
            if cam is None:
                break
            item = cam.read_latest(last_id, timeout=0.5)
            if item is None:
                with self._lock:
                    events = self.detector.tick(time.monotonic())
                for ev in events:
                    self._emit_reach(ev, None)
                continue
            fid, bgr, cap_t = item
            last_id = fid
            try:
                self.process_frame(fid, bgr, cap_t, cam.mirror)
            except Exception:
                log.exception("vision processing failed on frame %s", fid)

    def process_frame(self, fid: int, bgr: np.ndarray, cap_t: float, mirrored: bool) -> None:
        """One frame through markers -> tracker -> hands -> reach -> face -> render.
        Called by the processing thread; tests and offline evaluation call it directly."""
        import cv2

        self._frames_seen += 1
        n = self._frames_seen
        ts_ms = max(self._last_ts_ms + 1, int(cap_t * 1000))
        self._last_ts_ms = ts_ms
        st = self._stage
        t_start = time.perf_counter()

        tracking_events: list[dict] = []
        pre_events: list[ReachEvent] = []
        if self.mount == "robot":
            pre_events += self._ego_motion_gate()
        if self.effective_zone_mode == "marker":
            t = time.perf_counter()
            markers = self._markers(bgr, mirrored)
            st["markers"].append((time.perf_counter() - t) * 1000)
            self._marker_ms.append(st["markers"][-1])
            with self._lock:
                self.last_markers, self.last_markers_t = markers, time.monotonic()
                upd = self.tracker.update(cap_t, markers)
                tracking_events = upd.events
                if upd.zone is not None:
                    self.zone = upd.zone
                    self.detector.configure(zone=upd.zone)
                    if upd.moved:
                        self.detector.rebase()
                    pre_events = self.detector.set_paused(False)
                else:
                    pre_events = self.detector.set_paused(True, "zone_unknown")
        elif self.mount != "robot" and n % STATIC_MARKER_SCAN_EVERY_N == 0:
            # Fixed zone: still look for robot markers now and then, so the dashboard can show
            # that the robot is in view and the zone can be attached to it.
            t = time.perf_counter()
            markers = self._markers(bgr, mirrored)
            st["markers"].append((time.perf_counter() - t) * 1000)
            with self._lock:
                self.last_markers, self.last_markers_t = markers, time.monotonic()
        self._last_frame = (bgr, mirrored)

        t = time.perf_counter()
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        st["to_rgb"].append((time.perf_counter() - t) * 1000)

        t = time.perf_counter()
        hands = self.hands_model.detect(rgb, ts_ms) if self.hands_model else []
        st["hands"].append((time.perf_counter() - t) * 1000)
        self._hand_ms.append(st["hands"][-1])

        t = time.perf_counter()
        with self._lock:
            events = pre_events + self.detector.update(cap_t, hands)
        st["reach"].append((time.perf_counter() - t) * 1000)

        face_change = None
        if self.face_model is not None and n % FACE_EVERY_N == 0:
            t = time.perf_counter()
            face = self.face_model.observe(rgb, ts_ms)
            st["face"].append((time.perf_counter() - t) * 1000)
            self._face_ms.append(st["face"][-1])
            self.last_face = face
            if self.mount == "robot" and not face.present:
                # The robot camera points where the robot points; no face there is not "away".
                face_change = self.focus.update(cap_t, None, camera_ok=False, unknown_reason="robot camera: no face in view")
            else:
                face_change = self.focus.update(cap_t, face, camera_ok=True)

        self.last_hands = hands
        for te in tracking_events:
            self._emit("zone_tracking", {**te, "markers": self.tracker.used_markers}, None, cap_t)
        for ev in events:
            self._emit_reach(ev, cap_t)
        if face_change:
            state, conf, reason = face_change
            self._emit("focus", {"state": state.value, "reason": reason}, conf, cap_t)
            self._emit("face_present" if self.last_face and self.last_face.present else "face_absent", {"state": state.value})

        now = time.monotonic()
        self._total_ms.append((time.perf_counter() - t_start) * 1000)
        self._latency_ms.append((now - cap_t) * 1000)
        self._processed += 1
        if self._last_proc_t is not None and now > self._last_proc_t:
            inst = 1.0 / (now - self._last_proc_t)
            self._proc_fps = inst if self._proc_fps == 0 else 0.9 * self._proc_fps + 0.1 * inst
        self._last_proc_t = now
        if self.stream_clients > 0 or n % 15 == 0:
            try:
                t = time.perf_counter()
                self._jpeg = (fid, self._render(bgr))
                st["render"].append((time.perf_counter() - t) * 1000)
            except Exception:
                log.exception("overlay render failed")

    def _ego_motion_gate(self) -> list[ReachEvent]:
        """Robot-mounted camera: while the robot moves (and briefly after), the whole image moves,
        so hand speed toward the zone means nothing. Reach confirmation pauses, then restarts
        from the new view."""
        with self._lock:
            moving = self._robot_moving or time.monotonic() < self._ego_settle_until
            if moving and not self._ego_paused:
                self._ego_paused = True
                return self.detector.set_paused(True, "camera_moving")
            if not moving and self._ego_paused:
                self._ego_paused = False
                self.detector.rebase()
                return self.detector.set_paused(False)
        return []

    def stage_stats(self) -> dict:
        return {k: {"p50": _pct(v, 0.5), "p95": _pct(v, 0.95), "n": len(v)} for k, v in self._stage.items() if v}

    def _render(self, bgr: np.ndarray) -> bytes:
        import cv2

        h, w = bgr.shape[:2]
        canvas = bgr.copy() if self.show_video else np.zeros_like(bgr)
        with self._lock:
            zone = list(self.zone)
            det = self.detector.snapshot()
            markers = list(self.last_markers) if time.monotonic() - self.last_markers_t < 0.5 else []
            tstate = self.tracker.state if self.effective_zone_mode == "marker" else DISABLED
            ego_paused = self._ego_paused
        tracks = det["tracks"]
        for m in markers:
            q = m.corners.astype(np.int32)
            cv2.polylines(canvas, [q], True, AMBER, 2, cv2.LINE_AA)
            cv2.putText(canvas, f"id {m.marker_id}", (int(q[0][0]), int(q[0][1]) - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.4, AMBER, 1, cv2.LINE_AA)
        inside = any(t["inside"] for t in tracks)
        approaching = any(t["episode"] for t in tracks)
        color = MAGENTA if inside else (AMBER if approaching else CYAN)
        if tstate == LOST or (tstate != DISABLED and tstate not in (TRACKING, HELD)):
            label = "PHONE ZONE UNKNOWN · robot marker " + ("lost" if tstate == LOST else "not found yet")
            cv2.putText(canvas, label, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (90, 90, 255), 1, cv2.LINE_AA)
            zone = []
        if zone:
            pts = np.array([[int(x * w), int(y * h)] for x, y in zone], dtype=np.int32)
            overlay = canvas.copy()
            cv2.fillPoly(overlay, [pts], color)
            canvas = cv2.addWeighted(overlay, 0.16 if inside or approaching else 0.08, canvas, 0.84 if inside or approaching else 0.92, 0)
            cv2.polylines(canvas, [pts], True, color, 2, cv2.LINE_AA)
            label = "PHONE ZONE" + (" · HAND INSIDE" if inside else (" · APPROACH" if approaching else ""))
            if tstate in (TRACKING, HELD):
                label += f" · {tstate.lower()}"
            x0, y0 = pts[:, 0].min(), pts[:, 1].min()
            cv2.putText(canvas, label, (int(x0) + 4, max(14, int(y0) - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.42, color, 1, cv2.LINE_AA)

        for hand in self.last_hands:
            p = [(int(x * w), int(y * h)) for x, y in hand.landmarks]
            for a, b in HAND_CONNECTIONS:
                cv2.line(canvas, p[a], p[b], CYAN, 2, cv2.LINE_AA)
            for q in p:
                cv2.circle(canvas, q, 3, WHITE, -1, cv2.LINE_AA)
        for t in tracks:
            rx, ry = t["reach_point"]
            cv2.circle(canvas, (int(rx * w), int(ry * h)), 8, MAGENTA if t["inside"] else CYAN, 2, cv2.LINE_AA)

        face = self.last_face
        if face and face.present and face.box:
            bx, by, bw, bh = face.box
            cv2.rectangle(canvas, (int(bx * w), int(by * h)), (int((bx + bw) * w), int((by + bh) * h)), (160, 160, 160), 1, cv2.LINE_AA)
            cv2.putText(canvas, self.focus.state.value, (int(bx * w), max(12, int(by * h) - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (200, 200, 200), 1, cv2.LINE_AA)

        if ego_paused:
            cv2.putText(canvas, "REACH PAUSED: camera moving with the robot", (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, AMBER, 1, cv2.LINE_AA)
        cam = self.camera
        if cam is not None:
            # The frame says where it came from, wherever it is viewed.
            tag = f"{cam.label}  {w}x{h}"
            cv2.putText(canvas, tag, (8, h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 3, cv2.LINE_AA)
            cv2.putText(canvas, tag, (8, h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.45, WHITE, 1, cv2.LINE_AA)

        return self._encode(canvas, h)

    def _encode(self, canvas: np.ndarray, h: int) -> bytes:
        import cv2

        hud = f"{self._proc_fps:4.0f} FPS  hands {len(self.last_hands)}  {(_pct(self._hand_ms, 0.5) or 0):.0f} ms"
        cv2.putText(canvas, hud, (8, h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (210, 210, 210), 1, cv2.LINE_AA)
        if not self.show_video:
            cv2.putText(canvas, "VIDEO HIDDEN (privacy)", (8, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (150, 150, 150), 1, cv2.LINE_AA)
        ok, buf = cv2.imencode(".jpg", canvas, [cv2.IMWRITE_JPEG_QUALITY, 72])
        if not ok:
            raise RuntimeError("jpeg encode failed")
        return buf.tobytes()
