"""What Nightingale remembers about this computer across restarts, in one small SQLite file.

  samples    the GPU's readings every 10 seconds, kept for 7 days (history charts)
  fixes      every fix that ran on this computer and what happened
  incidents  this computer's incidents, so the history survives a restart
  memory     fleet memory for this computer ("third overheating this week")

The file lives at data/nightingale.db next to the app (NIGHTINGALE_DB overrides it) and never
leaves the machine.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "nightingale.db"
KEEP_DAYS = 7
RANGES = {"1h": 3600, "24h": 86400, "7d": 7 * 86400}


class Store:
    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = Path(path or os.getenv("NIGHTINGALE_DB") or DEFAULT_PATH)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS samples (ts REAL, gpu INTEGER, temp REAL, power REAL, clock INTEGER,
                max_clock INTEGER, util INTEGER, mem_used INTEGER, mem_total INTEGER, fan INTEGER, heat_slowdown INTEGER);
            CREATE INDEX IF NOT EXISTS samples_ts ON samples (ts);
            CREATE TABLE IF NOT EXISTS fixes (ts REAL, label TEXT, ok INTEGER, result TEXT, mode TEXT, incident INTEGER);
            CREATE TABLE IF NOT EXISTS incidents (id INTEGER PRIMARY KEY, ts REAL, body TEXT);
            CREATE TABLE IF NOT EXISTS memory (ts REAL, gpu INTEGER, title TEXT, fix TEXT);
            CREATE TABLE IF NOT EXISTS selftests (ts REAL, test TEXT, ok INTEGER, body TEXT);
        """)
        self.db.commit()
        self._last_prune = 0.0

    # -------------------------------------------------------------- samples
    def add_sample(self, gpu, ts: float | None = None) -> None:
        ts = ts or time.time()
        with self.lock:
            self.db.execute("INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (ts, gpu.index, round(gpu.temp, 1), round(gpu.power, 1), gpu.clock, gpu.max_clock,
                             gpu.util, gpu.mem_used, gpu.mem_total, gpu.fan,
                             int(any("THERMAL" in r for r in gpu.reasons))))  # idle policy flags are already left out
            if ts - self._last_prune > 3600:
                self.db.execute("DELETE FROM samples WHERE ts < ?", (ts - KEEP_DAYS * 86400,))
                self._last_prune = ts
            self.db.commit()

    def history(self, span: str = "1h", points: int = 180, gpu: int = 0) -> dict:
        seconds = RANGES.get(span, 3600)
        now = time.time()
        bucket = max(10.0, seconds / points)
        with self.lock:
            rows = self.db.execute(
                "SELECT CAST((ts - ?) / ? AS INTEGER) AS b, AVG(temp), MAX(temp), AVG(power), AVG(clock), AVG(util), "
                "AVG(mem_used), MAX(heat_slowdown), MIN(ts) FROM samples WHERE ts >= ? AND gpu = ? GROUP BY b ORDER BY b",
                (now - seconds, bucket, now - seconds, gpu)).fetchall()
        return {"span": span, "bucket_seconds": bucket, "points": [
            {"t": r[8], "temp": round(r[1], 1), "temp_max": round(r[2], 1), "power": round(r[3], 1),
             "clock": round(r[4]), "util": round(r[5]), "mem_used": round(r[6]), "heat_slowdown": bool(r[7])}
            for r in rows]}

    def summary(self, span: str = "24h") -> dict:
        """Hottest temperature and minutes spent slowing down for heat, for the health verdict."""
        seconds = RANGES.get(span, 86400)
        with self.lock:
            r = self.db.execute("SELECT MAX(temp), SUM(heat_slowdown), COUNT(*) FROM samples WHERE ts >= ?",
                                (time.time() - seconds,)).fetchone()
        return {"span": span, "max_temp": r[0], "heat_minutes": round((r[1] or 0) * 10 / 60), "samples": r[2] or 0}

    # ---------------------------------------------------------------- fixes
    def log_fix(self, label: str, ok: bool, result: str, mode: str, incident: int | None = None) -> None:
        with self.lock:
            self.db.execute("INSERT INTO fixes VALUES (?,?,?,?,?,?)", (time.time(), label, int(ok), result[:600], mode,
                                                                    incident))
            self.db.commit()

    def fixes(self, limit: int = 30) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT ts, label, ok, result, mode, incident FROM fixes ORDER BY ts DESC LIMIT ?",
                                   (limit,)).fetchall()
        return [{"at": r[0], "label": r[1], "ok": bool(r[2]), "result": r[3], "mode": r[4], "incident": r[5]}
                for r in rows]

    # ------------------------------------------------------------ incidents
    def save_incidents(self, incidents: list[dict]) -> None:
        with self.lock:
            for inc in incidents:
                if inc.get("target") == "laptop":
                    self.db.execute("INSERT OR REPLACE INTO incidents VALUES (?,?,?)",
                                    (inc["id"], inc.get("opened_at") or time.time(), json.dumps(inc, default=str)))
            self.db.commit()

    def load_incidents(self, limit: int = 40) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT body FROM incidents ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for (body,) in rows:
            try:
                out.append(json.loads(body))
            except ValueError:
                continue
        return out

    # ------------------------------------------------------------- self-tests
    def save_selftest(self, entry: dict) -> None:
        with self.lock:
            self.db.execute("INSERT INTO selftests VALUES (?,?,?,?)",
                            (entry["at"], entry["test"], int(entry["ok"]), json.dumps(entry, default=str)))
            self.db.commit()

    def selftests(self, limit: int = 20) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT body FROM selftests ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    # --------------------------------------------------------------- memory
    def remember(self, gpu: int | None, title: str, fix: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO memory VALUES (?,?,?,?)", (time.time(), gpu, title[:200], fix[:400]))
            self.db.commit()

    def memories(self, days: int = 30) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT ts, gpu, title, fix FROM memory WHERE ts >= ? ORDER BY ts",
                                   (time.time() - days * 86400,)).fetchall()
        return [{"ts": r[0], "gpu": r[1], "title": r[2], "fix": r[3]} for r in rows]
