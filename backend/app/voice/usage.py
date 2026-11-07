"""How much text was sent to the cloud voice, per day and purpose.

Fish Audio bills the UTF-8 bytes of the input text, so that is what is counted. This is a
local ledger for the budget; the authoritative numbers are on the provider's dashboard.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path


def utf8_bytes(text: str) -> int:
    return len(text.encode("utf-8"))


class UsageLedger:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self._clock = clock
        try:
            self._data: dict[str, dict[str, int]] = json.loads(self.path.read_text())
        except Exception:
            self._data = {}

    def _day(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(self._clock()))

    def used_today(self, purpose: str | None = None) -> int:
        day = self._data.get(self._day(), {})
        return day.get(purpose, 0) if purpose else sum(day.values())

    def record(self, nbytes: int, purpose: str) -> None:
        day = self._data.setdefault(self._day(), {})
        day[purpose] = day.get(purpose, 0) + int(nbytes)
        for old in sorted(self._data)[:-30]:  # keep a month
            self._data.pop(old, None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".usage-", suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump(self._data, fh, indent=1)
        os.replace(tmp, self.path)

    def today(self) -> dict[str, int]:
        return dict(self._data.get(self._day(), {}))
