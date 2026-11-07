"""Builds firmware/test/patch_harness.cpp, which compiles the real firmware/optional/
locked_in_patch.h for this machine against stubbed Arduino APIs, and runs it: 400 ms expiry,
latch until Stop, no trips in stock modes, millis() wraparound, telemetry. Logic only; the
timing on the robot itself is test R5."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

FIRMWARE = Path(__file__).resolve().parents[2] / "firmware"
CXX = shutil.which("c++") or shutil.which("clang++") or shutil.which("g++")


@pytest.mark.skipif(CXX is None, reason="no C++ compiler on this machine")
def test_patch_logic_on_the_real_header(tmp_path):
    exe = tmp_path / "patch_harness"
    build = subprocess.run(
        [CXX, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", str(FIRMWARE / "test" / "stubs"),
         str(FIRMWARE / "test" / "patch_harness.cpp"), "-o", str(exe)],
        capture_output=True, text=True, timeout=120,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "ALL PASSED (locked_in_patch.h v2, timeout 400 ms)" in run.stdout
