"""Reads the real NVIDIA GPU in this computer.

Every couple of seconds Nightingale runs `nvidia-smi --query-gpu=...` and turns the answer
into the same telemetry the data center produces: temperature, power, clocks, load, memory,
fan and, most usefully, *why* the GPU is slowing itself down (its clock event reasons).
On Windows it also reads the display driver's crash events (nvlddmkm, and Event 4101,
"display driver stopped responding and has recovered") from the System event log.

This module only reads. Fixes on this computer live in pc_actions.py and run only after approval.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque

WANTED = [
    "index", "name", "pci.bus_id", "driver_version", "pstate", "temperature.gpu",
    "power.draw", "power.limit", "power.max_limit", "clocks.sm", "clocks.max.sm",
    "utilization.gpu", "memory.used", "memory.total", "fan.speed",
    "clocks_event_reasons.active", "clocks_throttle_reasons.active",
]
# Used when `--help-query-gpu` can't be read: fields every driver of the last decade knows.
SAFE = ["index", "name", "pci.bus_id", "driver_version", "pstate", "temperature.gpu", "power.draw",
        "power.limit", "clocks.sm", "clocks.max.sm", "utilization.gpu", "memory.used", "memory.total",
        "fan.speed", "clocks_throttle_reasons.active"]

REASONS = [
    (0x0000000000000001, "GPU_IDLE"),
    (0x0000000000000002, "APPLICATIONS_CLOCKS_SETTING"),
    (0x0000000000000004, "SW_POWER_CAP"),
    (0x0000000000000008, "HW_SLOWDOWN"),
    (0x0000000000000010, "SYNC_BOOST"),
    (0x0000000000000020, "SW_THERMAL_SLOWDOWN"),
    (0x0000000000000040, "HW_THERMAL_SLOWDOWN"),
    (0x0000000000000080, "HW_POWER_BRAKE_SLOWDOWN"),
    (0x0000000000000100, "DISPLAY_CLOCK_SETTING"),
]
# The one reason shown when several are active, most serious first.
PRIORITY = ["HW_THERMAL_SLOWDOWN", "SW_THERMAL_SLOWDOWN", "HW_POWER_BRAKE_SLOWDOWN", "HW_SLOWDOWN", "SW_POWER_CAP"]
THERMAL = {"HW_THERMAL_SLOWDOWN", "SW_THERMAL_SLOWDOWN"}
GONE = re.compile(r"no devices were found|unable to determine the device handle|gpu is lost|"
                  r"couldn't communicate with the nvidia driver|failed to initialize nvml", re.I)
_WIN = sys.platform == "win32"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def find_smi() -> list[str] | None:
    """The nvidia-smi command on this computer, or None."""
    env = os.getenv("NVIDIA_SMI", "").strip()
    if env:
        return env.split("|") if "|" in env else [env]
    found = shutil.which("nvidia-smi")
    if found:
        return [found]
    for path in (r"C:\Windows\System32\nvidia-smi.exe",
                 r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
        if os.path.exists(path):
            return [path]
    return None


def _num(value):
    v = str(value).strip()
    if not v or v.startswith("[") or v.upper() in ("N/A", "NOT SUPPORTED"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def decode_reasons(value) -> list[str]:
    v = str(value or "").strip()
    if not v or v.startswith("["):
        return []
    try:
        mask = int(v, 16) if v.lower().startswith("0x") else int(v)
    except ValueError:
        return []
    return [name for bit, name in REASONS if mask & bit]


def main_reason(reasons: list[str]) -> str:
    for r in PRIORITY:
        if r in reasons:
            return r
    return "none"


def parse_query(fields: list[str], text: str) -> list[dict]:
    """Parse `--format=csv,noheader,nounits` output into one dict per GPU."""
    gpus = []
    for line in text.strip().splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) < len(fields):
            continue
        # A GPU name never has a comma, but guard anyway: extra cells join back into the name.
        extra = len(cells) - len(fields)
        if extra and "name" in fields:
            i = fields.index("name")
            cells[i:i + extra + 1] = [", ".join(cells[i:i + extra + 1])]
        row = dict(zip(fields, cells))
        reasons = decode_reasons(row.get("clocks_event_reasons.active") or row.get("clocks_throttle_reasons.active"))
        gpus.append({
            "index": int(_num(row.get("index")) or 0),
            "name": row.get("name") or "NVIDIA GPU",
            "pci": (row.get("pci.bus_id") or "").lower().replace("00000000:", "0000:"),
            "driver": row.get("driver_version") or "",
            "pstate": row.get("pstate") or "",
            "temp": _num(row.get("temperature.gpu")),
            "power": _num(row.get("power.draw")),
            "power_limit": _num(row.get("power.limit")),
            "power_max": _num(row.get("power.max_limit")),
            "clock": _num(row.get("clocks.sm")),
            "max_clock": _num(row.get("clocks.max.sm")),
            "util": _num(row.get("utilization.gpu")),
            "mem_used": _num(row.get("memory.used")),
            "mem_total": _num(row.get("memory.total")),
            "fan": _num(row.get("fan.speed")),
            "reasons": reasons,
        })
    return gpus


PROC_ROW = re.compile(r"^\|\s+(\d+)\s+\S+\s+\S+\s+(\d+)\s+(C\+G|C|G)\s+(.+?)\s+(\d+MiB|N/A)\s*\|$")


def parse_process_table(text: str) -> list[dict]:
    """Fallback for Windows, where --query-compute-apps often lists nothing: read the plain table."""
    out = []
    for line in text.splitlines():
        m = PROC_ROW.match(line.rstrip())
        if m:
            gpu, pid, kind, name, mem = m.groups()
            out.append({"gpu": int(gpu), "pid": int(pid), "type": kind, "name": name.strip().split("\\")[-1],
                        "mem_mib": int(mem[:-3]) if mem.endswith("MiB") else None})
    return out


class LocalGPU:
    """Polls nvidia-smi in a background thread. Every method is safe to call from any thread."""

    def __init__(self, command: list[str] | None = None, interval: float = 2.0, events: bool = True) -> None:
        self.command = command or find_smi()
        self.interval = interval
        self.events_enabled = events and _WIN
        self.lock = threading.Lock()
        self.fields: list[str] = []
        self.state = "off"            # off | ok | lost | error
        self.error = ""
        self.gpus: list[dict] = []
        self.read_at = 0.0
        self.processes: list[dict] = []
        self.events: deque = deque(maxlen=50)   # new driver events since connecting
        self.past_events: list[dict] = []       # driver events in the 24 h before connecting
        self._seen_events: set = set()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_proc = 0.0
        self._last_events = 0.0

    # ------------------------------------------------------------- commands
    def _run(self, args: list[str], timeout: float = 6.0) -> tuple[int, str]:
        if not self.command:
            return 127, "nvidia-smi was not found"
        try:
            proc = subprocess.run(self.command + args, capture_output=True, text=True, timeout=timeout,
                                  creationflags=_NO_WINDOW if _WIN else 0)
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except FileNotFoundError:
            return 127, "nvidia-smi was not found"
        except subprocess.TimeoutExpired:
            return 124, "nvidia-smi did not answer within 6 seconds"
        except OSError as exc:
            return 126, str(exc)

    def _pick_fields(self) -> list[str]:
        code, text = self._run(["--help-query-gpu"])
        known = set(re.findall(r'^"([a-z0-9_.]+)"', text, re.M)) | set(re.findall(r' or "([a-z0-9_.]+)"', text))
        if code != 0 or not known:
            return list(SAFE)
        fields = [f for f in WANTED if f in known]
        if "clocks_event_reasons.active" in fields and "clocks_throttle_reasons.active" in fields:
            fields.remove("clocks_throttle_reasons.active")
        return fields

    def probe(self) -> tuple[bool, str]:
        """Check that a GPU answers. Returns (ok, message) without starting the thread."""
        if not self.command:
            with self.lock:
                self.state, self.error = "error", ("nvidia-smi was not found. Install the NVIDIA driver, "
                                                   "or this computer has no NVIDIA GPU.")
            return False, self.error
        self.fields = self._pick_fields()
        self.poll(force_all=True)
        with self.lock:
            if self.state == "ok":
                names = ", ".join(sorted({g["name"] for g in self.gpus}))
                return True, f"Connected to {names}."
            return False, self.error

    def poll(self, force_all: bool = False) -> None:
        """One reading (and, less often, processes and driver events)."""
        if not self.fields:
            self.fields = self._pick_fields()
        code, text = self._run([f"--query-gpu={','.join(self.fields)}", "--format=csv,noheader,nounits"])
        gpus = parse_query(self.fields, text) if code == 0 else []
        with self.lock:
            self.read_at = time.time()
            if gpus:
                self.gpus, self.state, self.error = gpus, "ok", ""
            elif code == 127 or (code != 0 and not GONE.search(text) and not self.gpus):
                self.state, self.error = "error", (text.strip().splitlines() or ["nvidia-smi failed"])[0][:200]
            else:  # it answered before and now says the GPU is gone
                self.state = "lost"
                self.error = (text.strip().splitlines() or ["No devices were found"])[0][:200]
        now = time.time()
        if force_all or now - self._last_proc >= 10:
            self._last_proc = now
            self._poll_processes()
        if self.events_enabled and (force_all or now - self._last_events >= 30):
            self._last_events = now
            self._poll_events(first=force_all)

    def refresh_processes(self, max_age: float = 3.0) -> None:
        """Re-read the program list if it is older than max_age seconds (used when Ultra asks for it)."""
        if time.time() - self._last_proc > max_age:
            self._last_proc = time.time()
            self._poll_processes()

    def _poll_processes(self) -> None:
        code, text = self._run(["--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader,nounits"])
        procs = []
        if code == 0:
            for line in text.strip().splitlines():
                cells = [c.strip() for c in line.split(",")]
                if len(cells) >= 3 and cells[0].isdigit():
                    mem = _num(cells[2])
                    procs.append({"gpu": 0, "pid": int(cells[0]), "type": "C", "name": cells[1].split("\\")[-1].split("/")[-1],
                                  "mem_mib": int(mem) if mem is not None else None})
        if not procs:
            code, text = self._run([])
            if code == 0:
                procs = parse_process_table(text)
        with self.lock:
            self.processes = procs[:12]

    def _poll_events(self, first: bool) -> None:
        """Windows only: display-driver crashes (nvlddmkm, Display 4101) and hardware errors (WHEA) from the System log."""
        hours = 24 if first else 1
        # One query per provider: a provider missing on this PC must not hide the other's events.
        script = ("$e = @(); foreach ($p in 'nvlddmkm','Display','Microsoft-Windows-WHEA-Logger') { "
                  "$e += Get-WinEvent -FilterHashtable "
                  f"@{{LogName='System'; ProviderName=$p; Level=1,2,3; StartTime=(Get-Date).AddHours(-{hours})}} "
                  "-MaxEvents 30 -ErrorAction SilentlyContinue }; "
                  "$e | Select-Object @{n='t';e={$_.TimeCreated.ToString('o')}},Id,ProviderName,"
                  "@{n='m';e={($_.Message -replace '\\s+',' ')}} | ConvertTo-Json -Compress")
        try:
            proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                  capture_output=True, text=True, timeout=20, creationflags=_NO_WINDOW)
            raw = (proc.stdout or "").strip()
        except (OSError, subprocess.TimeoutExpired):
            return
        if not raw:
            return
        try:
            data = json.loads(raw)
        except ValueError:
            return
        rows = data if isinstance(data, list) else [data]
        fresh = []
        for r in rows:
            key = (r.get("t"), r.get("Id"))
            if key in self._seen_events:
                continue
            self._seen_events.add(key)
            fresh.append({"time": str(r.get("t") or "")[:19].replace("T", " "), "id": r.get("Id"),
                          "provider": r.get("ProviderName") or "", "message": str(r.get("m") or "")[:240]})
        fresh.sort(key=lambda e: e["time"])
        with self.lock:
            if first:
                self.past_events = fresh
            else:
                self.events.extend(fresh)

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="nightingale-local-gpu", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self.lock:
            self.state = "off"

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception as exc:  # never let the reader thread die
                with self.lock:
                    self.state, self.error = "error", f"Reading the GPU failed: {exc}"
            self._stop.wait(self.interval)

    # ----------------------------------------------------------------- read
    def snapshot(self) -> dict:
        with self.lock:
            return {"state": self.state, "error": self.error, "gpus": [dict(g) for g in self.gpus],
                    "read_at": self.read_at, "processes": [dict(p) for p in self.processes],
                    "past_events": list(self.past_events)}

    def take_events(self) -> list[dict]:
        """New driver events since the last call (each is returned once)."""
        with self.lock:
            out = list(self.events)
            self.events.clear()
            return out

    def status(self) -> dict:
        with self.lock:
            return {"state": self.state, "error": self.error,
                    "name": ", ".join(sorted({g["name"] for g in self.gpus})) or "",
                    "driver": self.gpus[0]["driver"] if self.gpus else "",
                    "gpus": len(self.gpus), "read_at": self.read_at,
                    "events_supported": self.events_enabled}
