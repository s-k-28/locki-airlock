"""Narrow, factual AIRLOCK announcements using the existing P0 Speaker queue.

The input is committed incident metadata supplied by the authenticated local
SpacetimeDB adapter, never model prose or recognized speech. No security reducer,
robot command, microphone, model, notification, or screen lock is invoked here.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import deque
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..telemetry.events import EventSource, EventType
from ..voice.tts import P0_SAFETY, Speaker


class IncidentAnnouncement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    workstation_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")
    session_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")
    incident_id: str = Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z0-9_.:-]+$")
    severity: Literal["medium", "high"]
    rule_id: Literal["unknown_usb", "correlated_usb_physical", "unapproved_app", "owner_panic"]
    simulated: bool
    updated_at_ms: int = Field(ge=0)

    @property
    def warning_id(self) -> str:
        content = [self.workstation_id, self.session_id, self.incident_id, self.severity, self.rule_id, self.simulated]
        return hashlib.sha256(json.dumps(content, separators=(",", ":")).encode()).hexdigest()


LINES = {
    (False, "unknown_usb"): "Security alert. A new unapproved USB device was observed while this workstation was armed. Verify the device before resuming.",
    (False, "correlated_usb_physical"): "Security alert. A new unapproved USB device and desk zone activity were observed close in time. Verify the device and workstation.",
    (False, "unapproved_app"): "Security alert. An unapproved visible application launch was observed while the workstation was armed. Review the application.",
    (False, "owner_panic"): "Security alert. The workstation owner requested a manual panic response. Review the workstation and follow the configured response policy.",
    (True, "unknown_usb"): "Rehearsal. A synthetic unapproved USB observation opened a test incident. No real intrusion or screen lock is being claimed.",
    (True, "correlated_usb_physical"): "Rehearsal. Synthetic USB and desk zone observations were correlated. No real intrusion or screen lock is being claimed.",
    (True, "unapproved_app"): "Rehearsal. A synthetic application observation opened a test incident. No real intrusion or screen lock is being claimed.",
    (True, "owner_panic"): "Rehearsal. A synthetic owner panic request opened a test incident. This manual request contains no sensor inference.",
}


class AnnouncementRejected(ValueError):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


class IncidentAnnouncer:
    """Persist an at-most-once queue claim, not a promise of exactly-once audio.

    A crash after the claim but before playback leaves an uncertain silent warning;
    reconnect never replays an old incident as a new audible intrusion. The visible
    database incident remains available independently.
    """

    def __init__(self, speaker: Speaker, ledger: Path, *, audio_enabled: bool = False, clock=time.time):
        self.speaker = speaker
        self.audio_enabled = audio_enabled
        self.clock = clock
        self.recent: deque[float] = deque(maxlen=12)
        self.last_by_station: dict[str, tuple[float, str]] = {}
        ledger.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(ledger)
        self.db.execute("CREATE TABLE IF NOT EXISTS warnings (id TEXT PRIMARY KEY, incident_id TEXT, at REAL, result TEXT)")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def announce(self, incident: IncidentAnnouncement) -> dict:
        known = self.db.execute("SELECT result FROM warnings WHERE id = ?", (incident.warning_id,)).fetchone()
        if known:
            return {**json.loads(known[0]), "duplicate": True, "queued": False, "audio_enabled": self.audio_enabled, "status": "duplicate; no new playback"}
        now = self.clock()
        if incident.updated_at_ms > int(now * 1000) + 5000 or incident.updated_at_ms < int(now * 1000) - 30_000:
            raise AnnouncementRejected("Incident update is stale or clock-skewed; no warning replay", 409)
        expected_severity = "high" if incident.rule_id in {"correlated_usb_physical", "owner_panic"} else "medium"
        if incident.severity != expected_severity:
            raise AnnouncementRejected(f"Rule {incident.rule_id} requires the committed {expected_severity} severity")
        while self.recent and self.recent[0] < now - 60:
            self.recent.popleft()
        if len(self.recent) >= 10 or self.speaker.snapshot()["queue"] >= 2:
            raise AnnouncementRejected("Warning queue is bounded; retry while the incident update is fresh", 429)
        previous = self.last_by_station.get(incident.workstation_id)
        if previous and now - previous[0] < 2 and not (previous[1] == "medium" and incident.severity == "high"):
            raise AnnouncementRejected("Workstation warning cooldown; retry shortly", 429)
        text = LINES[(incident.simulated, incident.rule_id)]
        result = {
            "ok": True, "warning_id": incident.warning_id, "incident_id": incident.incident_id,
            "simulated": incident.simulated, "text": text, "priority": "P0",
            "audio_enabled": self.audio_enabled, "queued": False, "duplicate": False,
            "status": "claimed; playback not yet confirmed",
        }
        # Claim before queueing. Two local requests cannot create two queued lines,
        # and a reconnect cannot treat an uncertain crash as a new authorization.
        self.db.execute("INSERT INTO warnings VALUES (?, ?, ?, ?)", (incident.warning_id, incident.incident_id, now, json.dumps(result)))
        self.db.execute("DELETE FROM warnings WHERE id IN (SELECT id FROM warnings ORDER BY at DESC LIMIT -1 OFFSET 4096)")
        self.db.commit()
        queued = self.speaker.say(text, priority=P0_SAFETY, category="airlock_rehearsal" if incident.simulated else "airlock_security", cacheable=True, force=False, max_age_s=5)
        result.update(queued=queued, status="queued; not proof of audible output" if queued else "muted or duplicate; not spoken")
        self.db.execute("UPDATE warnings SET result = ? WHERE id = ?", (json.dumps(result), incident.warning_id))
        self.db.commit()
        self.recent.append(now)
        self.last_by_station[incident.workstation_id] = (now, incident.severity)
        if len(self.last_by_station) > 256:
            oldest = min(self.last_by_station, key=lambda key: self.last_by_station[key][0])
            self.last_by_station.pop(oldest)
        self.speaker.bus.publish(EventType.VOICE_RESPONSE_GENERATED, EventSource.VOICE, {
            "text": text, "category": "airlock_rehearsal" if incident.simulated else "airlock_security",
            "priority": "P0", "incident_id": incident.incident_id, "simulated": incident.simulated,
            "source": "AIRLOCK fixed factual announcement; no model inference",
        })
        return result
