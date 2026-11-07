#!/usr/bin/env python3
"""Stationary AIRLOCK camera publisher. Capture and publishing are explicit.

OpenCV's built-in HOG detector needs no model download. Motion-only fallback
reports degraded person coverage and never invents a person observation. Images
stay in process memory; only bounded redacted metadata reaches SpacetimeDB.
"""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
import hmac
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import resource
import signal
import socket
import stat
import sys
import threading
import time
from typing import Any
from urllib import error, parse, request
import uuid

EVENT_TTL_MS = 120_000
MAX_PENDING = 128
ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.:-]{1,128}$")


def identifier(value: str) -> str:
    if not ID_PATTERN.fullmatch(value):
        raise ValueError("Identifiers require 1-128 letters, digits, _, ., :, or -")
    return value


def parse_roi(raw: str) -> tuple[float, float, float, float]:
    try:
        values = tuple(float(value) for value in raw.split(","))
    except ValueError as exc:
        raise ValueError("ROI must be x,y,width,height normalized to [0,1]") from exc
    if len(values) != 4 or not all(math.isfinite(v) for v in values):
        raise ValueError("ROI requires four finite numbers")
    x, y, width, height = values
    if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
        raise ValueError("ROI must be a nonempty rectangle within [0,1]")
    return x, y, width, height


def box_in_roi(box: tuple[int, int, int, int], frame_width: int, frame_height: int,
               roi: tuple[float, float, float, float]) -> bool:
    if frame_width <= 0 or frame_height <= 0:
        return False
    bx, by, bw, bh = box
    if bw <= 0 or bh <= 0:
        return False
    cx, cy = (bx + bw / 2) / frame_width, (by + bh / 2) / frame_height
    x, y, width, height = roi
    return x <= cx <= x + width and y <= cy <= y + height


@dataclass
class ZoneLatch:
    """Debounce classification output, then refresh occupied evidence every 5s."""
    enter_frames: int = 3
    clear_frames: int = 5
    refresh_ms: int = 5000
    present: bool = False
    positives: int = 0
    negatives: int = 0
    last_presence_ms: int = 0

    def observe(self, detected: bool, observed_ms: int) -> str | None:
        self.positives = self.positives + 1 if detected else 0
        self.negatives = 0 if detected else self.negatives + 1
        if not self.present and self.positives >= self.enter_frames:
            self.present = True
            self.last_presence_ms = observed_ms
            return "person_in_zone"
        if self.present and self.negatives >= self.clear_frames:
            self.present = False
            return "person_cleared"
        if self.present and detected and observed_ms - self.last_presence_ms >= self.refresh_ms:
            self.last_presence_ms = observed_ms
            return "person_in_zone"
        return None

    def reset(self) -> None:
        self.present = False
        self.positives = self.negatives = self.last_presence_ms = 0


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def acquire_process_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    stream = os.fdopen(fd, "r+")
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        stream.close()
        raise RuntimeError("Another camera publisher already owns this sequence state") from None
    return stream


class SequenceStore:
    """Persist sequence and pending metadata before publication for safe retries."""
    def __init__(self, path: Path, workstation: str, device: str):
        self.path, self.workstation, self.device = path, workstation, device
        self.seq = 0
        self.pending: list[dict[str, Any]] = []
        if path.exists():
            if path.stat().st_size > 1_000_000:
                raise ValueError("Saved publisher state exceeds size limit")
            saved = json.loads(path.read_text())
            if saved.get("workstation") != workstation or saved.get("device") != device:
                raise ValueError("Sequence state belongs to another workstation/device")
            self.seq = saved.get("seq", 0)
            self.pending = saved.get("pending", [])
            if not isinstance(self.seq, int) or isinstance(self.seq, bool) or self.seq < 0:
                raise ValueError("Invalid saved device sequence")
            if not isinstance(self.pending, list) or len(self.pending) > MAX_PENDING:
                raise ValueError("Invalid saved pending queue")
            for event in self.pending:
                if not isinstance(event, dict) or event.get("device_id") != device or event.get("workstation_id") != workstation or not isinstance(event.get("seq"), int) or event["seq"] > self.seq:
                    raise ValueError("Pending event does not belong to sequence state")

    def save(self) -> None:
        atomic_json(self.path, {"workstation": self.workstation, "device": self.device,
                               "seq": self.seq, "pending": self.pending})

    def enqueue(self, session: str, kind: str, observed_ms: int, metadata: dict[str, str],
                confidence: float = 1.0) -> dict[str, Any]:
        if len(self.pending) >= MAX_PENDING:
            raise RuntimeError("Camera metadata queue full; delivery is degraded")
        self.seq += 1
        event = {"schema_version": 1, "workstation_id": self.workstation, "session_id": session,
                 "device_id": self.device, "event_id": "jetson-" + str(uuid.uuid4()),
                 "source": "jetson", "kind": kind, "observed_at": observed_ms, "seq": self.seq,
                 "confidence": max(0.0, min(1.0, confidence)), "metadata": metadata, "simulated": False}
        self.pending.append(event)
        self.save()
        return event

    def prune(self, now_ms: int, session: str) -> int:
        before = len(self.pending)
        self.pending = [event for event in self.pending
                        if event["observed_at"] + EVENT_TTL_MS >= now_ms and event["session_id"] == session]
        if len(self.pending) != before:
            self.save()
        return before - len(self.pending)


def read_secret(path: Path, *, json_token: bool = True) -> str:
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("Credential file must be owner-only (chmod 600)")
    raw = path.read_text().strip()
    secret = json.loads(raw).get("token") if json_token else raw
    if not isinstance(secret, str) or len(secret) < 16 or "\n" in secret:
        raise ValueError("Credential file is missing a valid token")
    return secret


class DatabaseClient:
    def __init__(self, uri: str, database: str, token: str, workstation: str, device: str):
        parsed = parse.urlsplit(uri.replace("ws:", "http:", 1).replace("wss:", "https:", 1))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("Database URI requires HTTP(S) without embedded credentials")
        if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ValueError("Database URI must be a server origin")
        self.base = f"{parsed.scheme}://{parsed.netloc}/v1/database/{parse.quote(database, safe='')}"
        self.token, self.workstation, self.device = token, identifier(workstation), identifier(device)

    def post(self, suffix: str, body: Any, content_type: str = "application/json") -> Any:
        payload = body.encode() if isinstance(body, str) else json.dumps(body, separators=(",", ":")).encode()
        req = request.Request(self.base + suffix, data=payload, method="POST",
                              headers={"Authorization": "Bearer " + self.token, "Content-Type": content_type})
        with request.urlopen(req, timeout=3) as response:
            data = response.read(1_000_000)
        return json.loads(data) if data else None

    def call(self, name: str, args: list[Any]) -> Any:
        return self.post(f"/call/{name}?confirmed=true", args)

    def session(self) -> dict[str, Any] | None:
        result = self.post(f"/sql", f"SELECT * FROM my_workstations WHERE id = '{self.workstation}'", "text/plain")
        stations = sql_rows(result)
        if not stations:
            raise RuntimeError("Device is not enrolled for this workstation")
        session_id = stations[0].get("active_session_id", "")
        if not session_id:
            return None
        identifier(session_id)
        result = self.post("/sql", f"SELECT * FROM my_sessions WHERE id = '{session_id}'", "text/plain")
        sessions = sql_rows(result)
        if not sessions:
            return None
        session = sessions[0]
        if session.get("state") != "armed" or session.get("expires_at_ms", 0) <= time.time_ns() // 1_000_000:
            return None
        return session

    def observation(self, event: dict[str, Any]) -> Any:
        return self.call("ingest_device_event", [event["workstation_id"], event["session_id"], event["device_id"],
                         event["event_id"], event["kind"], event["observed_at"], event["seq"], event["confidence"],
                         json.dumps(event["metadata"], separators=(",", ":")), False, 1])

    def health(self, component: str, status: str, detail: str) -> Any:
        return self.call("report_health", [self.workstation, self.device, component, status, detail[:256]])


def sql_rows(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, list):
        raise ValueError("Unexpected SpacetimeDB SQL response")
    rows = []
    for statement in result:
        columns = [element.get("name", {}).get("some", "") for element in statement.get("schema", {}).get("elements", [])]
        for values in statement.get("rows", []):
            rows.append(values if isinstance(values, dict) else dict(zip(columns, values)))
    return rows


class Metrics:
    def __init__(self, detector: str, publishing: bool):
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.last_frame = 0.0
        self.processing_ms: deque[float] = deque(maxlen=120)
        self.values: dict[str, Any] = {"detector": detector, "publishing": publishing, "camera_status": "unavailable",
            "database_status": "connecting" if publishing else "disabled", "person_classification_available": detector == "hog",
            "frames": 0, "camera_failures": 0, "events_published": 0, "events_dropped": 0,
            "publish_errors": 0, "motion_fraction": 0.0, "pending_events": 0, "session_state": "unknown",
            "last_error": "", "raw_images_persisted": False, "simulated": False}

    def update(self, **values: Any) -> None:
        with self.lock:
            self.values.update(values)

    def increment(self, key: str, amount: int = 1) -> None:
        with self.lock:
            self.values[key] = self.values.get(key, 0) + amount

    def frame(self, elapsed_ms: float) -> None:
        with self.lock:
            self.last_frame = time.monotonic()
            self.processing_ms.append(elapsed_ms)
            self.values["frames"] += 1
            self.values["camera_status"] = "online" if self.values["detector"] == "hog" else "degraded"

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            values = dict(self.values)
            durations = sorted(self.processing_ms)
            age = (time.monotonic() - self.last_frame) * 1000 if self.last_frame else None
        values["uptime_seconds"] = round(time.monotonic() - self.started, 2)
        values["frame_age_ms"] = None if age is None else round(age, 2)
        if age is None or age > 15_000:
            values["camera_status"] = "offline" if age is not None else "unavailable"
        values["processing_p95_ms"] = round(durations[max(0, math.ceil(len(durations) * .95) - 1)], 2) if durations else None
        values["fps_average"] = round(values["frames"] / max(values["uptime_seconds"], .001), 2)
        usage = resource.getrusage(resource.RUSAGE_SELF)
        values["cpu_seconds"] = round(usage.ru_utime + usage.ru_stime, 3)
        values["gpu_utilization"] = "not measured; CPU HOG fallback"
        rss = usage.ru_maxrss
        values["peak_rss_mb"] = round(rss / (1024 * 1024 if sys.platform == "darwin" else 1024), 2)
        values["healthy"] = values["camera_status"] == "online" and (not values["publishing"] or values["database_status"] == "online")
        return values


def metrics_text(values: dict[str, Any]) -> str:
    names = ["frames", "camera_failures", "events_published", "events_dropped", "publish_errors", "pending_events",
             "cpu_seconds", "uptime_seconds", "frame_age_ms", "processing_p95_ms", "fps_average", "peak_rss_mb", "motion_fraction"]
    lines = ["# AIRLOCK camera metrics; no video, identifiers, or credentials are exposed."]
    for key in names:
        value = values.get(key)
        if isinstance(value, (int, float)) and math.isfinite(value):
            lines.append(f"airlock_camera_{key} {value}")
    lines.append(f"airlock_camera_healthy {1 if values['healthy'] else 0}")
    return "\n".join(lines) + "\n"


def bind_requires_auth(bind: str) -> bool:
    if bind == "localhost":
        return False
    try:
        return not ipaddress.ip_address(bind).is_loopback
    except ValueError:
        raise ValueError("Bind must be localhost or an IP address") from None


def serve_metrics(metrics: Metrics, bind: str, port: int, api_token: str | None = None) -> ThreadingHTTPServer:
    if bind_requires_auth(bind) and not api_token:
        raise ValueError("Non-loopback health/metrics binding requires an API token file")
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if api_token and not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + api_token):
                self.send_response(401); self.end_headers(); return
            values = metrics.snapshot()
            if self.path == "/health":
                data, content_type, code = json.dumps(values).encode(), "application/json", 200 if values["healthy"] else 503
            elif self.path == "/metrics":
                data, content_type, code = metrics_text(values).encode(), "text/plain; version=0.0.4", 200
            else:
                self.send_response(404); self.end_headers(); return
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers(); self.wfile.write(data)
        def log_message(self, *_args: Any) -> None:
            return
    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ":" in bind else socket.AF_INET
    server = Server((bind, port), Handler)
    thread = threading.Thread(target=server.serve_forever, name="airlock-camera-metrics", daemon=True)
    thread.start()
    return server


class OpenCVDetector:
    def __init__(self, cv: Any, mode: str, roi: tuple[float,float,float,float], threshold: float):
        self.cv, self.mode, self.roi, self.threshold = cv, mode, roi, threshold
        self.warmup = 0
        if mode == "hog":
            self.model = cv.HOGDescriptor()
            self.model.setSVMDetector(cv.HOGDescriptor_getDefaultPeopleDetector())
        else:
            self.model = cv.createBackgroundSubtractorMOG2(history=200, varThreshold=25, detectShadows=True)

    def detect(self, frame: Any) -> tuple[bool, float, float]:
        height, width = frame.shape[:2]
        if self.mode == "hog":
            if height < 128 or width < 64:
                raise ValueError("HOG capture requires at least 64x128 pixels")
            boxes, scores = self.model.detectMultiScale(frame, winStride=(8,8), padding=(8,8), scale=1.05)
            accepted = [float(score.item() if hasattr(score,"item") else score) for box, score in zip(boxes, scores) if box_in_roi(tuple(box),width,height,self.roi)]
            score = max(accepted, default=0.0)
            # Bounded raw classifier margin, NOT a calibrated probability or accuracy claim.
            return bool(accepted), max(0.0,min(1.0,score)), 0.0
        x, y, rw, rh = self.roi
        crop = frame[int(y*height):int((y+rh)*height),int(x*width):int((x+rw)*width)]
        if not crop.size:
            raise ValueError("ROI is empty at the selected capture resolution")
        mask = self.model.apply(crop)
        _, mask = self.cv.threshold(mask, 200, 255, self.cv.THRESH_BINARY)
        mask = self.cv.morphologyEx(mask,self.cv.MORPH_OPEN,self.cv.getStructuringElement(self.cv.MORPH_ELLIPSE,(3,3)))
        fraction = self.cv.countNonZero(mask) / mask.size
        self.warmup += 1
        return self.warmup >= 30 and fraction >= self.threshold, 0.0, fraction


def run(args: argparse.Namespace) -> None:
    # Importing this module, inventory and tests never imports cv2 or opens a camera.
    import cv2 as cv
    process_lock = acquire_process_lock(args.state_file)
    detector = OpenCVDetector(cv,args.detector,args.roi,args.motion_threshold)
    state = SequenceStore(args.state_file,args.workstation,args.device)
    client = DatabaseClient(args.uri,args.database,read_secret(args.token_file),args.workstation,args.device) if args.publish else None
    metrics = Metrics(args.detector,args.publish)
    api_token = read_secret(args.api_token_file,json_token=False) if args.api_token_file else None
    server = serve_metrics(metrics,args.bind,args.port,api_token)
    stop = threading.Event()
    signal.signal(signal.SIGTERM,lambda *_: stop.set())
    signal.signal(signal.SIGINT,lambda *_: stop.set())
    latch = ZoneLatch(args.enter_frames,args.clear_frames)
    capture = None
    session: dict[str,Any] | None = None
    heartbeat_at = 0.0
    started = time.monotonic()
    camera_online = False
    source: int | str = int(args.camera) if args.camera.isdigit() else args.camera
    print(json.dumps({"adapter":"AIRLOCK stationary camera","detector":args.detector,"publishing":args.publish,
                      "person_classification":args.detector=="hog","raw_video_persisted":False,"simulated":False}),file=sys.stderr)

    def observation(kind: str, detail: str, confidence: float = 1.0) -> None:
        observed = time.time_ns() // 1_000_000
        metadata = {"detail":detail[:256],"captureMode":"opencv_hog" if args.detector=="hog" else "motion_only"}
        # Camera capture is real: never relabel it synthetic to fit a simulated session.
        if client and session and not session.get("simulated"):
            state.enqueue(session["id"],kind,observed,metadata,confidence)
        elif not client:
            print(json.dumps({"kind":kind,"observed_at":observed,"metadata":metadata,"confidence":confidence,"simulated":False}),flush=True)

    def flush_pending() -> None:
        if client:
            while state.pending:
                try:
                    client.observation(state.pending[0])
                except error.HTTPError as exc:
                    metrics.increment("publish_errors")
                    metrics.update(database_status="unavailable",last_error=f"database_http_{exc.code}")
                    if exc.code < 500:
                        state.pending.pop(0); state.save(); metrics.increment("events_dropped")
                    break
                except (error.URLError,TimeoutError) as exc:
                    metrics.increment("publish_errors"); metrics.update(database_status="unavailable",last_error=type(exc).__name__)
                    break
                else:
                    state.pending.pop(0); state.save(); metrics.increment("events_published")

    try:
        while not stop.is_set():
            tick = time.monotonic()
            if args.duration and tick-started >= args.duration:
                break
            if tick-heartbeat_at >= 3:
                heartbeat_at = tick
                if client:
                    try:
                        previous = session.get("id") if session else ""
                        session = client.session()
                        if previous != (session.get("id") if session else ""):
                            latch.reset()
                        metrics.increment("events_dropped",state.prune(time.time_ns()//1_000_000,session["id"] if session else ""))
                        status = metrics.snapshot()["camera_status"]
                        detail = "OpenCV HOG person-classifier feed; scores uncalibrated" if args.detector=="hog" else "Motion-only fallback; no person classification or physical correlation proof"
                        client.health("camera",status,detail)
                        client.health("jetson","online","Stationary camera adapter process heartbeat")
                        metrics.update(database_status="online",session_state="disarmed" if not session else "synthetic-session-skipped" if session.get("simulated") else "armed",last_error="")
                        if session and not session.get("simulated"):
                            observation("heartbeat","Camera adapter heartbeat; sensor status reported separately")
                    except (error.URLError,TimeoutError,ValueError,RuntimeError) as exc:
                        metrics.increment("publish_errors")
                        metrics.update(database_status="unavailable",last_error=type(exc).__name__)
                else:
                    metrics.update(session_state="local-observation-only")
            flush_pending()
            if capture is None:
                capture = cv.VideoCapture(source)
                capture.set(cv.CAP_PROP_FRAME_WIDTH,args.width)
                capture.set(cv.CAP_PROP_FRAME_HEIGHT,args.height)
                capture.set(cv.CAP_PROP_BUFFERSIZE,1)
                if hasattr(cv,"CAP_PROP_READ_TIMEOUT_MSEC"):
                    capture.set(cv.CAP_PROP_READ_TIMEOUT_MSEC,2000)
                if not capture.isOpened():
                    capture.release(); capture=None
                    metrics.increment("camera_failures"); metrics.update(camera_status="offline",last_error="camera_open_failed")
                    stop.wait(2); continue
            frame_started = time.perf_counter()
            ok, frame = capture.read()
            if not ok or frame is None:
                metrics.increment("camera_failures"); metrics.update(camera_status="offline",last_error="camera_frame_failed")
                if camera_online:
                    observation("camera_offline","Camera frame capture failed")
                camera_online=False; latch.reset(); capture.release(); capture=None
                flush_pending()
                stop.wait(1)
                continue
            if not camera_online:
                camera_online=True
                if args.detector == "hog":
                    observation("camera_online","Camera returned a frame; classifier startup is not a performance validation")
            detected,score,fraction = detector.detect(frame)
            # Drop the only frame reference after processing: no video route or recording.
            del frame
            metrics.frame((time.perf_counter()-frame_started)*1000)
            metrics.update(motion_fraction=round(fraction,5),pending_events=len(state.pending))
            if args.detector=="hog":
                kind = latch.observe(detected,time.time_ns()//1_000_000)
                if kind:
                    observation(kind,"OpenCV built-in HOG classification in desk ROI; bounded margin is not a calibrated probability",score)
            elif detected:
                # Deliberately not emitted as person_in_zone: motion does not identify a person.
                metrics.update(last_error="motion_only_person_coverage_degraded")
            flush_pending()
            stop.wait(max(0,1/args.fps-(time.monotonic()-tick)))
    finally:
        if capture is not None:
            capture.release()
        metrics.update(camera_status="offline")
        if client:
            try:
                client.health("camera","offline","Camera adapter stopped")
                client.health("jetson","offline","Camera adapter stopped")
            except (error.URLError,TimeoutError):
                pass
        server.shutdown(); server.server_close(); process_lock.close()
        print(json.dumps({"final_metrics":metrics.snapshot()}),file=sys.stderr)


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture",action="store_true",help="Explicitly open the selected camera")
    parser.add_argument("--publish",action="store_true",help="Send real metadata using an enrolled Jetson token")
    parser.add_argument("--workstation",required=True,type=identifier)
    parser.add_argument("--device",required=True,type=identifier)
    parser.add_argument("--camera",default="0",help="Camera index or /dev/video path; no network video")
    parser.add_argument("--detector",choices=["hog","motion"],default="hog")
    parser.add_argument("--roi",type=parse_roi,default=parse_roi("0.1,0.05,0.8,0.9"))
    parser.add_argument("--fps",type=float,default=3)
    parser.add_argument("--width",type=int,default=640)
    parser.add_argument("--height",type=int,default=480)
    parser.add_argument("--motion-threshold",type=float,default=.03)
    parser.add_argument("--enter-frames",type=int,default=3)
    parser.add_argument("--clear-frames",type=int,default=5)
    parser.add_argument("--duration",type=float,default=0)
    parser.add_argument("--token-file",type=Path)
    parser.add_argument("--state-file",type=Path,required=True)
    parser.add_argument("--api-token-file",type=Path)
    parser.add_argument("--bind",default="127.0.0.1")
    parser.add_argument("--port",type=int,default=8787)
    parser.add_argument("--uri",default=os.environ.get("AIRLOCK_SPACETIME_URI","http://127.0.0.1:3000"))
    parser.add_argument("--database",default=os.environ.get("AIRLOCK_DATABASE","locki-airlock-sid"))
    args=parser.parse_args()
    if not args.capture:
        parser.error("Camera remains closed: pass --capture only with camera/volunteer consent")
    if args.publish and not args.token_file:
        parser.error("--publish requires --token-file for the enrolled Jetson identity")
    if not .5 <= args.fps <= 15 or not 128 <= args.width <= 1920 or not 128 <= args.height <= 1080 or not 0<args.motion_threshold<1:
        parser.error("Use fps0.5–15, width128–1920, height128–1080 and motion threshold0–1")
    if not 1<=args.enter_frames<=30 or not 1<=args.clear_frames<=60 or args.duration<0 or not 1<=args.port<=65535:
        parser.error("Invalid debounce, duration or port")
    if not args.camera.isdigit() and not re.fullmatch(r"/dev/video\d+",args.camera):
        parser.error("Camera must be a local numeric index or /dev/videoN path")
    if bind_requires_auth(args.bind) and not args.api_token_file:
        parser.error("LAN health/metrics binding requires --api-token-file")
    run(args)


if __name__=="__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"Camera adapter unavailable: {type(exc).__name__}: {exc}",file=sys.stderr)
        raise SystemExit(1) from None
