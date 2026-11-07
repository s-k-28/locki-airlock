#!/usr/bin/env python3
"""Read-only Jetson/camera inventory. Does not open cameras or touch GPIO/USB files."""
from __future__ import annotations
import glob
import json
from pathlib import Path
import platform
import shutil
import sys


def read(path: str, limit: int = 1024) -> str | None:
    try:
        with open(path,"rb") as stream:
            return stream.read(limit).decode(errors="replace").replace("\x00", "").strip()
    except (OSError, ValueError):
        return None


def inventory() -> dict:
    memory = read("/proc/meminfo",4096) or ""
    total = next((line.split(":",1)[1].strip() for line in memory.splitlines() if line.startswith("MemTotal:")),None)
    cameras = []
    for node in sorted(glob.glob("/sys/class/video4linux/video*")):
        cameras.append({"node":"/dev/"+Path(node).name,"name":read(node+"/name",256) or "unknown"})
    return {"recorded_by":"AIRLOCK read-only inventory", "machine":platform.machine(),
            "os":platform.platform(),"python":platform.python_version(),
            "jetson_model":read("/proc/device-tree/model") or "UNVERIFIED: no Jetson model file on this host",
            "jetpack_l4t_release":read("/etc/nv_tegra_release") or "UNVERIFIED: L4T release not found",
            "memory_total":total or "UNVERIFIED", "camera_nodes":cameras,
            "opencv_available":__import__("importlib.util",fromlist=["find_spec"]).find_spec("cv2") is not None,
            "tegrastats_available":shutil.which("tegrastats") is not None,
            "v4l2_ctl_available":shutil.which("v4l2-ctl") is not None,
            "power_adapter":"UNVERIFIED: inspect physical label against exact board before any power change",
            "camera_opened":False,"motors_commanded":False,"raw_serials_collected":False}


if __name__ == "__main__":
    json.dump(inventory(),sys.stdout,indent=2); print()
