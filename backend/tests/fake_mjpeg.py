"""A stand-in for the Zeus ESP32-CAM's MJPEG server, for camera tests. It writes the same bytes
as ai-camera-firmware camera_server.cpp (port 9000, /mjpg): HTTP/1.1 chunked transfer,
``multipart/x-mixed-replace;boundary=123456789000000000000987654321``, and per frame
``\\r\\n--<boundary>\\r\\nContent-Type: image/jpeg\\r\\nContent-Length: N\\r\\nX-Timestamp: s.us\\r\\n\\r\\n``
followed by the JPEG. Frames are synthetic (a numbered test card), never a real camera.

Faults for tests: ``stall()`` keeps the socket open and sends nothing, ``drop()`` closes every
stream, ``skip_frames(n)`` advances the camera clock as if n frames were lost, ``corrupt_next()``
sends one broken JPEG, ``mode`` = "not_mjpeg" / "404" answers wrongly.
"""

from __future__ import annotations

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

BOUNDARY = "123456789000000000000987654321"


def test_card(n: int, w: int = 640, h: int = 480) -> bytes:
    img = np.full((h, w, 3), (40, 40 + (n * 7) % 200, 90), np.uint8)
    cv2.putText(img, f"FAKE ESP32 {n}", (20, h // 2), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 2)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
    assert ok
    return buf.tobytes()


class FakeEsp32Camera:
    def __init__(self, fps: float = 20.0, size: tuple[int, int] = (640, 480), port: int = 0):
        self.fps = fps
        self.size = size
        self.mode = "ok"  # ok | not_mjpeg | 404
        self._stall = threading.Event()
        self._drop_gen = 0
        self._skip = 0
        self._corrupt = 0
        self.frames_sent = 0
        self.connections = 0
        self.boot = time.monotonic()
        self._cards = [test_card(i, *size) for i in range(8)]
        cam = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # quiet
                pass

            def do_GET(self):
                cam.connections += 1
                if cam.mode == "404" or self.path != "/mjpg":
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                ctype = "text/html" if cam.mode == "not_mjpeg" else f"multipart/x-mixed-replace;boundary={BOUNDARY}"
                self.send_header("Content-Type", ctype)
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                if cam.mode == "not_mjpeg":
                    self._chunk(b"<html>hello</html>")
                    self._chunk(b"")
                    return
                gen = cam._drop_gen
                n = 0
                lost_s = 0.0  # camera-clock time of frames captured but never sent
                period = 1.0 / cam.fps
                try:
                    while gen == cam._drop_gen:
                        if cam._stall.is_set():
                            time.sleep(0.02)
                            continue
                        if cam._skip:
                            n += cam._skip  # frames the camera "captured" that never go out
                            lost_s += cam._skip * period
                            cam._skip = 0
                        jpeg = cam._cards[n % len(cam._cards)]
                        if cam._corrupt:
                            cam._corrupt -= 1
                            jpeg = b"\xff\xd8" + b"\x00" * 500  # not a decodable JPEG
                        # The camera's own clock at capture time, like esp_timer on the ESP32.
                        ts = (time.monotonic() - cam.boot) + 1.0 + lost_s
                        sec, usec = int(ts), int((ts % 1) * 1_000_000)
                        self._chunk(f"\r\n--{BOUNDARY}\r\n".encode())
                        self._chunk(f"Content-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\nX-Timestamp: {sec}.{usec:06d}\r\n\r\n".encode())
                        self._chunk(jpeg)
                        cam.frames_sent += 1
                        n += 1
                        time.sleep(period)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

            def _chunk(self, data: bytes) -> None:
                self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/mjpg"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> FakeEsp32Camera:
        self._thread.start()
        return self

    def close(self) -> None:
        self.drop()
        self._server.shutdown()
        self._server.server_close()

    # faults
    def stall(self, on: bool = True) -> None:
        (self._stall.set if on else self._stall.clear)()

    def drop(self) -> None:
        self._drop_gen += 1

    def skip_frames(self, n: int) -> None:
        self._skip = n

    def corrupt_next(self, n: int = 1) -> None:
        self._corrupt = n


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
