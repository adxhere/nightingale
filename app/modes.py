"""Light and serious mode for fixes on this computer.

Light is the default. Serious mode unlocks the bigger fixes (clock caps, power limits, driver
rollback and updates, Armoury Crate fan and BIOS work). It switches itself back to light after
30 minutes, or as soon as the incident it was turned on for is fixed, so it is never left on by
accident.
"""
from __future__ import annotations

import time

SERIOUS_MINUTES = 30
FIXED = {"resolved", "waiting_on_technician", "closed"}


class Modes:
    def __init__(self) -> None:
        self.mode = "light"
        self.until: float | None = None
        self.incident: int | None = None
        self.changed_at = time.time()
        self.note = ""

    def set(self, mode: str, incident: int | None = None, minutes: float = SERIOUS_MINUTES) -> dict:
        if mode not in ("light", "serious"):
            raise ValueError("mode must be light or serious")
        self.mode = mode
        self.changed_at = time.time()
        if mode == "serious":
            self.until = time.time() + minutes * 60
            self.incident = incident
            self.note = (f"Serious mode is on for {minutes:.0f} minutes"
                         + (f" or until incident #{incident} is fixed." if incident else "."))
        else:
            self.until, self.incident = None, None
            self.note = "Light mode."
        return self.status()

    def check(self, incidents: list[dict]) -> str:
        """Called every tick. Returns a message when serious mode switches itself off."""
        if self.mode != "serious":
            return ""
        if self.until and time.time() >= self.until:
            self.set("light")
            self.note = f"Serious mode turned itself off after {SERIOUS_MINUTES} minutes."
            return self.note
        if self.incident is not None:
            inc = next((i for i in incidents if i["id"] == self.incident), None)
            if inc is not None and inc["status"] in FIXED:
                n = self.incident
                self.set("light")
                self.note = f"Incident #{n} is fixed, so serious mode switched back to light."
                return self.note
        return ""

    def status(self) -> dict:
        left = max(0, int(self.until - time.time())) if self.until else None
        return {"mode": self.mode, "seconds_left": left, "incident": self.incident, "note": self.note,
                "changed_at": self.changed_at}
