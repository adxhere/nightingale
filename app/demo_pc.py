"""A pretend Windows laptop, for the public demo and for development on machines without one.

With DEMO_PC=1, Nightingale reads a simulated RTX laptop GPU (app/demo_smi.py) and runs fixes
against this stand-in instead of Windows. Everything is labelled as a demo in the dashboard.
The stand-in answers the way Windows does, so the whole flow (modes, permission prompts,
restore points, results) can be tried without touching a real computer.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from .demo_smi import readings
from .pc_actions import PLANS, PcFixes

STATE = Path(os.getenv("DEMO_PC_STATE") or Path(__file__).resolve().parent.parent / "data" / "demo_gpu.json")


def demo_smi_command() -> list[str]:
    return [sys.executable, str(Path(__file__).resolve().parent / "demo_smi.py")]


class DemoWindows:
    def __init__(self) -> None:
        self.plan = "balanced"
        self.capped: int | None = None

    def _state(self) -> dict:
        try:
            return json.loads(STATE.read_text())
        except (OSError, ValueError):
            return {}

    def _write(self, **kv) -> None:
        st = self._state()
        st.update(kv)
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(st))

    def run(self, args, timeout=30):
        if args[0] == "tasklist":
            pid = int(args[2].split()[-1])
            return 0, (f'"blender.exe","{pid}"' if any(p["pid"] == pid for p in readings()["procs"]) else "INFO: No tasks")
        if args[0] == "taskkill":
            self._write(closed_at=time.time())  # the demo's render job stops until the next cycle
            return 0, "SUCCESS"
        if args[:2] == ["powercfg", "/list"]:
            return 0, "\n".join(f"Power Scheme GUID: {g}" for g in PLANS.values())
        if args[:2] == ["powercfg", "/getactivescheme"]:
            return 0, f"Power Scheme GUID: {PLANS[self.plan]}  ({self.plan.replace('_', ' ').title()})"
        if args[:2] == ["powercfg", "/setactive"]:
            self.plan = next(k for k, g in PLANS.items() if g == args[2])
            return 0, ""
        return 0, "NVIDIA GeForce RTX 3050 Laptop GPU"

    def ps(self, script, timeout=30):
        if "Get-PnpDevice" in script:
            return 0, r"PCI\VEN_10DE&DEV_25A2&SUBSYS_00000000&REV_A1\DEMO"
        if "Get-StartApps" in script:
            return (0, "DEMO.ArmouryCrate!App") if "Armoury" in script else (0, "")
        if "Win32_VideoController" in script:
            return 0, '{"Name":"NVIDIA GeForce RTX 3050 Laptop GPU","DriverVersion":"32.0.15.6607","DriverDate":"2024-10-14"}'
        return 0, ""

    def elevated(self, script, timeout=240):
        if "Checkpoint-Computer" in script:
            return 0, "RESTORE_OK"
        if "-lgc" in script:
            self._write(clock_cap=int(script.split("210,")[1].split("'")[0]))
            return 0, "All done."
        if "-rgc" in script:
            self._write(clock_cap=None)
            return 0, "All done."
        if "-pl" in script:
            return 0, "Changing power management limit is not supported for GPU 00000000:01:00.0."
        if "pnputil" in script:
            self._write(lost=False, temp=48)
            return 0, "Device restarted successfully."
        return 0, ""

    def open(self, target):
        return True, ""


def make_demo_fixes() -> PcFixes:
    w = DemoWindows()
    return PcFixes(smi=["nvidia-smi"], run=w.run, ps=w.ps, elevated=w.elevated, opener=w.open, windows=True)
