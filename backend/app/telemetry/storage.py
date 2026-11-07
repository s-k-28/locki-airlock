"""Append-only JSONL telemetry and per-session summaries.

Only structured events and computed metrics are stored. No audio and no video
frames are ever written here.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

from .events import Event

log = logging.getLogger(__name__)

_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


class TelemetryStore:
    def __init__(self, data_dir: Path):
        self.dir = data_dir / "telemetry"
        self.sessions_dir = data_dir / "sessions"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.path = self.dir / f"run-{stamp}.jsonl"
        self._fh = open(self.path, "a", buffering=1 << 16, encoding="utf-8")
        self._lock = threading.Lock()
        self._unflushed = 0

    def write_event(self, event: Event) -> None:
        line = event.model_dump_json()
        with self._lock:
            if self._fh.closed:
                return
            self._fh.write(line + "\n")
            self._unflushed += 1

    def flush(self) -> None:
        with self._lock:
            if not self._fh.closed and self._unflushed:
                self._fh.flush()
                self._unflushed = 0

    def save_session_summary(self, session_id: str, summary: dict[str, Any]) -> Path:
        path = self.sessions_dir / f"{session_id}.json"
        path.write_text(json.dumps(summary, indent=2, default=str))
        return path

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        out = []
        files = sorted(self.sessions_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files[:limit]:
            try:
                out.append(json.loads(path.read_text()))
            except Exception:
                log.warning("unreadable session summary %s", path)
        return out

    def load_session(self, session_id: str) -> dict[str, Any] | None:
        if not _SAFE_ID.fullmatch(session_id):
            return None
        path = self.sessions_dir / f"{session_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.flush()
                self._fh.close()
