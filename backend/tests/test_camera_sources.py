"""Milestone 3 camera sources: the ESP32-CAM MJPEG stream (against a stand-in that writes the
same bytes as the firmware's camera_server.cpp), timeouts and reconnection, honest status, and
the runtime's camera selection. No real camera or robot is involved."""

from __future__ import annotations

import time

import pytest

from app.vision.mjpeg import FrameClock, MjpegError, MjpegReader, boundary_from
from app.vision.webcam import CameraSource

from .fake_mjpeg import BOUNDARY, FakeEsp32Camera, closed_port


def wait_until(pred, timeout=5.0, step=0.02) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


@pytest.fixture
def cam():
    c = FakeEsp32Camera(fps=20).start()
    yield c
    c.close()


def test_boundary_and_frame_clock_units():
    assert boundary_from(f"multipart/x-mixed-replace;boundary={BOUNDARY}") == BOUNDARY.encode()
    assert boundary_from('multipart/x-mixed-replace; boundary="--abc"') == b"abc"
    assert boundary_from("image/jpeg") is None
    clock = FrameClock()
    t = 100.0
    for i in range(20):
        clock.add(t + i * 0.05, 5.0 + i * 0.05, 1000, arrived_wall=t + i * 0.05)
    clock.add(t + 24 * 0.05, 5.0 + 24 * 0.05, 1000, arrived_wall=t + 24 * 0.05)  # frames 20-23 never arrived
    assert clock.missed == 4
    assert clock.device_fps() == pytest.approx(20, rel=0.05)
    assert clock.delays_ms[-1] == pytest.approx(0, abs=1)
    clock.add(t + 25 * 0.05 + 0.3, 5.0 + 25 * 0.05, 1000, arrived_wall=t + 25 * 0.05 + 0.3)  # 300 ms late
    assert clock.delays_ms[-1] == pytest.approx(300, abs=1)


def test_reader_parses_the_esp32_stream_with_chunked_transfer(cam):
    r = MjpegReader(cam.url)
    r.open()
    try:
        parts = [r.read_part() for _ in range(5)]
    finally:
        r.close()
    assert all(p.jpeg[:2] == b"\xff\xd8" for p in parts)
    ts = [p.device_ts for p in parts]
    assert all(t is not None for t in ts) and all(b > a for a, b in zip(ts, ts[1:], strict=False))
    assert parts[-1].arrived > parts[0].arrived


def test_camera_source_delivers_decoded_frames_with_measured_fps(cam):
    src = CameraSource(cam.url, label="ZEUS ESP32-CAM")
    src.start()
    try:
        assert wait_until(lambda: src.state == "live" and src.frames >= 30, timeout=5)
        st = src.status()
        assert st["label"] == "ZEUS ESP32-CAM" and st["backend"] == "http" and st["kind"] == "url"
        assert st["size"] == (640, 480) and not st["mirror"]  # the robot's camera is not a selfie view
        # The camera's own timestamps and the arrival times tell the same story.
        assert 12 <= st["fps"] <= 22 and st["device_fps"] == pytest.approx(st["fps"], rel=0.2)
        assert st["missed"] == 0 and st["decode_errors"] == 0 and st["frame_kb"] > 1
        assert st["frame_age_ms"] < 500 and st["connects"] == 1 and st["reconnects"] == 0
        item = src.read_latest(0, timeout=1.0)
        assert item is not None and item[1].shape == (480, 640, 3)
    finally:
        src.stop()
    assert src.state == "offline" and src.status()["reason"] == "stopped"


def test_stalled_stream_reconnects_on_its_own(cam):
    states: list[str] = []
    src = CameraSource(cam.url, label="ZEUS ESP32-CAM")
    src.on_state_change = lambda s, r: states.append(s)
    src.start()
    try:
        assert wait_until(lambda: src.state == "live", timeout=5)
        cam.stall()  # socket open, nothing sent: Wi-Fi gone without a close
        assert wait_until(lambda: src.state == "reconnecting", timeout=4)
        assert "no data from the camera" in (src.status()["last_error"] or "")
        cam.stall(False)
        assert wait_until(lambda: src.state == "live" and src.connects >= 2, timeout=6)
        assert src.status()["reconnects"] >= 1 and states[:2] == ["opening", "live"] and "reconnecting" in states
    finally:
        src.stop()


def test_dropped_connection_reconnects(cam):
    src = CameraSource(cam.url, label="ZEUS ESP32-CAM")
    src.start()
    try:
        assert wait_until(lambda: src.state == "live", timeout=5)
        cam.drop()  # camera rebooted
        assert wait_until(lambda: src.connects >= 2 and src.state == "live", timeout=6)
    finally:
        src.stop()


def test_unreachable_camera_reports_why_retries_and_stops_promptly():
    src = CameraSource(f"http://127.0.0.1:{closed_port()}/mjpg", label="ZEUS ESP32-CAM")
    src.start()
    try:
        assert wait_until(lambda: src.attempts >= 2, timeout=5)
        st = src.status()
        assert st["state"] == "reconnecting" and "cannot reach" in st["last_error"]
        assert st["next_retry_in_s"] is not None and st["frames"] == 0
    finally:
        t0 = time.monotonic()
        src.stop()
        assert time.monotonic() - t0 < 1.0  # never waits out a network timeout


@pytest.mark.parametrize(("mode", "message"), [("not_mjpeg", "is not an MJPEG stream"), ("404", "answered HTTP 404")])
def test_wrong_answers_are_explained(cam, mode, message):
    cam.mode = mode
    r = MjpegReader(cam.url)
    with pytest.raises(MjpegError, match=message):
        r.open()
    r.close()


def test_corrupt_jpeg_is_skipped_and_counted(cam):
    src = CameraSource(cam.url)
    src.start()
    try:
        assert wait_until(lambda: src.state == "live", timeout=5)
        cam.corrupt_next(2)
        assert wait_until(lambda: src.decode_errors == 2, timeout=3)
        f = src.frames
        assert wait_until(lambda: src.frames > f + 5, timeout=3) and src.state == "live"
    finally:
        src.stop()


def test_frames_lost_on_the_way_are_counted_from_camera_timestamps(cam):
    src = CameraSource(cam.url)
    src.start()
    try:
        assert wait_until(lambda: src.frames >= 15, timeout=5)
        cam.skip_frames(5)
        assert wait_until(lambda: src.status()["missed"] >= 4, timeout=3)
    finally:
        src.stop()


def test_opencv_backend_reads_the_same_stream(cam):
    src = CameraSource(cam.url, mjpeg_backend="opencv")
    assert src.backend == "opencv-ffmpeg"
    src.start()
    try:
        assert wait_until(lambda: src.state == "live" and src.frames >= 5, timeout=10)
        assert src.status()["size"] == (640, 480)
    finally:
        src.stop()


def test_flip_is_applied_to_the_robot_camera(cam):
    import numpy as np

    plain, flipped = CameraSource(cam.url), CameraSource(cam.url, flip="both")
    for s in (plain, flipped):
        s.start()
    try:
        assert wait_until(lambda: plain.frames > 3 and flipped.frames > 3, timeout=5)
        a, b = plain.read_latest(0)[1], flipped.read_latest(0)[1]
        assert flipped.status()["flip"] == "both" and a.shape == b.shape
        assert not np.array_equal(a, b)
    finally:
        for s in (plain, flipped):
            s.stop()
