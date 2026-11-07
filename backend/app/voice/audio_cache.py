"""Generated voice clips, keyed by everything that changes the sound.

Key = hash of (provider, voice id, model, voice settings, exact text sent). Change any of
them (a new voice, another model, a different speed) and the old clips simply stop
matching: nothing stale is ever played in the new voice.

Only fixed template lines are written to disk (``persist=True``). Lines that contain
session details (task names, times) or LLM replies stay in memory for this run only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
import wave
from collections import OrderedDict
from pathlib import Path

log = logging.getLogger(__name__)


class AudioCache:
    def __init__(self, root: Path, ram_items: int = 160):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self.ram_items = ram_items
        self._ram: OrderedDict[str, tuple] = OrderedDict()
        self.index: dict[str, dict] = self._load_index()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(provider: str, voice_id: str, model: str, settings: dict, text: str) -> str:
        blob = json.dumps({"p": provider, "v": voice_id, "m": model, "s": settings, "t": text}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:32]

    # ------------------------------------------------------------ lookup
    def has(self, key: str) -> bool:
        return key in self._ram or key in self.index

    def get(self, key: str):
        """(pcm int16 ndarray, sample_rate) or None. Never touches the network."""
        item = self._ram.get(key)
        if item is not None:
            self._ram.move_to_end(key)
            self.hits += 1
            return item
        meta = self.index.get(key)
        if meta is None:
            self.misses += 1
            return None
        path = self.root / f"{key}.wav"
        try:
            import numpy as np

            with wave.open(str(path)) as w:
                item = (np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16), w.getframerate())
        except Exception as exc:
            log.warning("cached clip %s unreadable (%s); dropping it", key, exc)
            self.index.pop(key, None)
            self._save_index()
            self.misses += 1
            return None
        self._remember(key, item)
        self.hits += 1
        return item

    def preload(self, keys: list[str]) -> int:
        """Load clips into memory (blocking: call from a worker thread)."""
        n = 0
        for k in keys:
            if k not in self._ram and k in self.index and self.get(k) is not None:
                n += 1
        return n

    # ------------------------------------------------------------ store
    def put(self, key: str, pcm, sample_rate: int, meta: dict, persist: bool) -> None:
        self._remember(key, (pcm, sample_rate))
        if not persist:
            return
        path = self.root / f"{key}.wav"
        tmp = path.with_suffix(".tmp")
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(pcm.tobytes())
        os.replace(tmp, path)
        self.index[key] = {**meta, "created_at": time.time(), "duration_s": round(len(pcm) / sample_rate, 2)}
        self._save_index()

    def delete(self, keys: list[str]) -> int:
        n = 0
        for k in keys:
            self._ram.pop(k, None)
            if self.index.pop(k, None) is not None:
                n += 1
                try:
                    (self.root / f"{k}.wav").unlink()
                except OSError:
                    pass
        if n:
            self._save_index()
        return n

    def stats(self, keys: list[str]) -> dict:
        cached = sum(1 for k in keys if self.has(k))
        return {"lines": len(keys), "cached": cached, "missing": len(keys) - cached, "files_on_disk": len(self.index)}

    def _remember(self, key: str, item: tuple) -> None:
        self._ram[key] = item
        self._ram.move_to_end(key)
        while len(self._ram) > self.ram_items:
            self._ram.popitem(last=False)

    def _load_index(self) -> dict:
        try:
            return json.loads(self.index_path.read_text())
        except FileNotFoundError:
            return {}
        except Exception as exc:
            log.warning("audio cache index unreadable (%s); starting empty", exc)
            return {}

    def _save_index(self) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".index-", suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump(self.index, fh, indent=1)
        os.replace(tmp, self.index_path)
