"""Alerts: short messages about things that need the person, shown as Windows notifications by the tray app.

Alerts come from incidents on this computer (and, if turned on, the simulated data center), from
serious mode switching itself off, from finished self-tests, and from fixes that failed. Each event
alerts once.
"""
from __future__ import annotations

import time
from collections import deque

# Windows notifications refuse longer text (63 characters of title, 255 of message).
TITLE_MAX, MESSAGE_MAX = 63, 255


def clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip(" ,.:;") + "…"


class Alerts:
    def __init__(self) -> None:
        self.enabled = True
        self.datacenter = False      # alerts for the simulated data center too (off: it's for drills)
        self.queue: deque = deque(maxlen=20)
        self.seen: set = set()
        self.recent: deque = deque(maxlen=30)

    def push(self, key: str, title: str, message: str, link: str = "#/pc") -> None:
        if key in self.seen:
            return
        self.seen.add(key)
        title, message = clip(title, TITLE_MAX), clip(message, MESSAGE_MAX)
        item = {"at": time.time(), "title": title, "message": message, "link": link}
        self.recent.appendleft(item)
        if self.enabled:
            self.queue.append(item)

    def take(self) -> list[dict]:
        out = list(self.queue)
        self.queue.clear()
        return out

    def scan(self, incidents: list[dict], mode_note: str = "") -> None:
        """Called every tick by the monitor loop."""
        for inc in incidents:
            mine = inc["target"] == "laptop"
            if not mine and not self.datacenter:
                continue
            title = inc["title"] or inc["trigger"]["signal"]
            where = "Your GPU" if mine else inc["target"]
            link = f"#/incidents/{inc['id']}"
            if inc["status"] == "awaiting_approval":
                nxt = next((a for a in inc["actions"] if a["status"] == "pending"), None)
                step = f" Next: {nxt['label']}." if nxt else ""
                self.push(f"wait-{inc['id']}-{len([a for a in inc['actions'] if a['status'] != 'pending'])}",
                          f"{where}: {title}", f"Nemotron has a fix plan ready for your approval.{step}", link)
            elif inc["status"] == "needs_attention":
                self.push(f"attn-{inc['id']}-{len(inc['timeline'])}", f"{where} needs attention",
                          (inc["error"] or inc["verification"] or title)[:180], link)
            elif inc["status"] == "resolved" and mine:
                self.push(f"done-{inc['id']}", "Fixed", f"{title}: verified, the problem is gone.", link)
        if mode_note:
            self.push(f"mode-{int(time.time() // 60)}", "Back to light mode", mode_note)
