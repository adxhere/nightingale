"""A simulated nvidia-smi for the demo laptop (DEMO_PC=1): an RTX 3050 Laptop GPU on a 6-minute cycle.

Two minutes idle, then four minutes of a render job that heats it up until it slows down for heat
at about 87C, so the demo shows a real-looking incident every few minutes. A clock cap set from
the demo's fixes keeps it cooler. State changed by fixes lives in data/demo_gpu.json.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

STATE = Path(os.getenv("DEMO_PC_STATE") or Path(__file__).resolve().parent.parent / "data" / "demo_gpu.json")
CYCLE, IDLE = 360, 120
MAX_CLOCK, LIMIT, MEM = 1740, 80, 4096
RENDER = {"pid": 7124, "name": r"C:\Program Files\Blender Foundation\Blender 4.2\blender.exe", "mem": 3100}
FIELDS_HELP = "\n".join(f'"{f}"' for f in (
    "index", "name", "pci.bus_id", "driver_version", "pstate", "temperature.gpu", "power.draw", "power.limit",
    "power.max_limit", "clocks.sm", "clocks.max.sm", "utilization.gpu", "memory.used", "memory.total", "fan.speed",
    "clocks_event_reasons.active", "clocks_throttle_reasons.active"))


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def readings(now: float | None = None) -> dict:
    st = _state()
    t = (now or time.time()) % CYCLE
    cap = st.get("clock_cap")
    closed = st.get("closed_at", 0) > (now or time.time()) - CYCLE  # the render job was closed this cycle
    busy = t >= IDLE and not closed
    if not busy:
        cool = min(1.0, (t if t < IDLE else 0) / 60)
        temp = 46 + 30 * (1 - cool) if t < 60 else 46
        return {"temp": round(temp), "power": 9.5, "clock": 210, "util": 3, "mem_used": 610, "reasons": "0x0000000000000001",
                "pstate": "P8", "procs": [], "lost": st.get("lost", False)}
    ramp = min(1.0, (t - IDLE) / 150)
    ceiling = 81 if cap else 90
    temp = 62 + (ceiling - 62) * ramp
    hot = temp >= 87
    clock = min(cap or MAX_CLOCK, 1395 if hot else MAX_CLOCK - int(40 * ramp))
    reasons = (0x40 | 0x04) if hot else 0x04
    return {"temp": round(temp), "power": 78.4 if not hot else 71.2, "clock": clock, "util": 98, "mem_used": 3720,
            "reasons": f"0x{reasons:016x}", "pstate": "P0", "procs": [RENDER], "lost": st.get("lost", False)}


def main(argv: list[str]) -> int:
    r = readings()
    if r["lost"]:
        print("No devices were found")
        return 6
    if "--help-query-gpu" in argv:
        print(FIELDS_HELP)
        return 0
    query = next((a for a in argv if a.startswith("--query-gpu=")), None)
    if query:
        values = {"index": 0, "name": "NVIDIA GeForce RTX 3050 Laptop GPU", "pci.bus_id": "00000000:01:00.0",
                  "driver_version": "566.07", "pstate": r["pstate"], "temperature.gpu": r["temp"],
                  "power.draw": r["power"], "power.limit": LIMIT, "power.max_limit": 95, "clocks.sm": r["clock"],
                  "clocks.max.sm": MAX_CLOCK, "utilization.gpu": r["util"], "memory.used": r["mem_used"],
                  "memory.total": MEM, "fan.speed": "[N/A]", "clocks_event_reasons.active": r["reasons"],
                  "clocks_throttle_reasons.active": r["reasons"]}
        fields = query.split("=", 1)[1].split(",")
        print(", ".join(str(values.get(f, "[N/A]")) for f in fields))
        return 0
    if any(a.startswith("--query-compute-apps=") for a in argv):
        for p in r["procs"]:
            print(f"{p['pid']}, {p['name']}, {p['mem']}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
