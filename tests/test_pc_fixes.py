"""Fixes on this computer: modes, permission prompts, restore points and safety, with a fake Windows."""
import asyncio
import json
import os
import sys
import time

import pytest

import app.llm as llm_module
from app import actions
from app.local_gpu import LocalGPU
from app.modes import Modes
from app.pc_actions import PLANS, PcFixes
from test_nightingale import make_agent

FAKE = [sys.executable, os.path.join(os.path.dirname(__file__), "fake_smi.py")]
GPU_INSTANCE = r"PCI\VEN_10DE&DEV_25A2&SUBSYS_1EF21043&REV_A1\4&1234&0&0008"


class FakeWindows:
    """Records every command and answers like Windows would."""

    def __init__(self, decline_admin=False, restore="ok", smi_supported=True):
        self.calls, self.admin_calls, self.opened = [], [], []
        self.decline_admin, self.restore, self.smi_supported = decline_admin, restore, smi_supported
        self.running = {4242}
        self.plan = "balanced"

    def run(self, args, timeout=30):
        self.calls.append(args)
        if args[0] == "tasklist":
            pid = int(args[2].split()[-1])
            return 0, f'"game.exe","{pid}","Console","1","5,800 K"' if pid in self.running else "INFO: No tasks are running"
        if args[0] == "taskkill":
            self.running.discard(int(args[2]))
            return 0, "SUCCESS"
        if args[:2] == ["powercfg", "/list"]:
            return 0, "\n".join(f"Power Scheme GUID: {g}  ({k})" for k, g in PLANS.items() if k != "high_performance")
        if args[:2] == ["powercfg", "/getactivescheme"]:
            return 0, f"Power Scheme GUID: {PLANS[self.plan]}  (Balanced)"
        if args[:2] == ["powercfg", "/setactive"]:
            ok = args[2] != PLANS["high_performance"]
            return (0, "") if ok else (1, "Unable to perform operation. An unexpected error (0x65b) has occurred")
        return 0, "NVIDIA GeForce RTX 3050 Laptop GPU"

    def ps(self, script, timeout=30):
        self.calls.append(["powershell", script])
        if "Get-PnpDevice" in script:
            return 0, GPU_INSTANCE
        if "Get-StartApps" in script:
            return (0, "B9ECED6F.ArmouryCrate_qmba6cd70vzyy!App") if "Armoury" in script else (0, "")
        return 0, ""

    def elevated(self, script, timeout=240):
        self.admin_calls.append(script)
        if self.decline_admin:
            return 1223, "ELEVATION_FAILED: The operation was canceled by the user."
        if "Checkpoint-Computer" in script:
            return {"ok": (0, "RESTORE_OK"),
                    "recent": (0, "WARNING: A new system restore point cannot be created because one has already "
                                  "been created within the past 1440 minutes."),
                    "off": (1, "The service cannot be started, either because it is disabled")}[self.restore]
        if "-lgc" in script or "-pl" in script:
            if not self.smi_supported:
                return 0, "Setting locked GPU clocks is not supported for GPU 00000000:01:00.0.\nTreating as warning."
            return 0, "GPU clocks set to (gpuClkMin 210, gpuClkMax 1650) for GPU 00000000:01:00.0\nAll done."
        return 0, "Device restarted successfully."

    def open(self, target):
        self.opened.append(target)
        return True, ""


def fixes(**kw):
    w = FakeWindows(**kw)
    return PcFixes(smi=["nvidia-smi"], run=w.run, ps=w.ps, elevated=w.elevated, opener=w.open, windows=True), w


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(llm_module.time, "sleep", lambda _s: None)
    monkeypatch.setattr("app.pc_actions.time.sleep", lambda _s: None)


@pytest.fixture
def gpu_state(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text("{}")
    monkeypatch.setenv("FAKE_SMI_STATE", str(path))
    return lambda **kv: path.write_text(json.dumps(kv))


def laptop(pc=None):
    cluster, agent = make_agent(size=256, seed=3)
    src = LocalGPU(command=FAKE, events=False)
    assert src.probe()[0]
    cluster.attach_local(src)
    cluster.pc = pc
    return cluster, agent, src


def test_light_fixes_run_and_report():
    pc, w = fixes()
    ok, msg = pc.execute("pc_close_program", {"pid": 4242, "name": "game.exe"})
    assert ok and "Closed game.exe" in msg and 4242 not in w.running
    ok, msg = pc.execute("pc_power_plan", {"plan": "power_saver", "plan_name": "Power saver"})
    assert ok and "Power saver" in msg
    ok, msg = pc.execute("pc_power_plan", {"plan": "high_performance", "plan_name": "High performance"})
    assert not ok and "Armoury Crate" in msg
    ok, msg = pc.execute("pc_restart_gpu_driver", {})
    assert ok and GPU_INSTANCE in w.admin_calls[-1]


def test_saying_no_to_windows_changes_nothing():
    pc, w = fixes(decline_admin=True)
    ok, msg = pc.execute("pc_restart_gpu_driver", {})
    assert not ok and "said no" in msg
    ok, msg = pc.execute("pc_cap_gpu_clock", {"mhz": 1650})
    assert not ok and "said no" in msg and pc.clock_cap is None


def test_clock_cap_quotes_its_argument_and_reports_unsupported_gpus():
    pc, w = fixes()
    ok, msg = pc.execute("pc_cap_gpu_clock", {"mhz": 1650})
    assert ok and pc.clock_cap == 1650 and "'210,1650'" in w.admin_calls[-1]
    pc, w = fixes(smi_supported=False)
    ok, msg = pc.execute("pc_set_power_limit", {"watts": 60})
    assert not ok and "doesn't allow" in msg


def test_driver_changes_make_a_restore_point_first():
    pc, w = fixes()
    ok, msg = pc.execute("pc_update_driver", {})
    assert ok and "restore point" in msg.lower()
    assert "Checkpoint-Computer" in w.admin_calls[0] and w.opened
    ok, msg = pc.execute("pc_rollback_driver", {})  # within the hour: reuses the restore point
    assert ok and "Roll Back Driver" in msg and len([c for c in w.admin_calls if "Checkpoint" in c]) == 1
    pc, w = fixes(restore="off")
    ok, msg = pc.execute("pc_update_driver", {})
    assert not ok and "System Restore is turned off" in msg and not w.opened, "no driver change without a restore point"
    pc, w = fixes(restore="recent")
    assert pc.execute("pc_rollback_driver", {})[0]


def test_guided_fixes_open_armoury_crate():
    pc, w = fixes()
    ok, msg = pc.execute("pc_performance_mode", {"mode": "Turbo"})
    assert ok and "Turbo" in msg and any("shell:AppsFolder" in " ".join(c) for c in w.calls if c[0] == "explorer.exe")


def test_not_windows_means_nothing_runs():
    pc = PcFixes(windows=False)
    ok, msg = pc.execute("pc_clear_shader_cache", {})
    assert not ok and "Windows only" in msg


def test_validation_protects_windows_and_checks_ranges(gpu_state):
    pc, _ = fixes()
    gpu_state(procs=[{"pid": 4242, "name": "C:\\Games\\game.exe", "mem": 3000},
                     {"pid": 700, "name": "C:\\Windows\\explorer.exe", "mem": 100}])
    cluster, agent, src = laptop(pc)
    src.poll(force_all=True)
    ok, why = actions.normalize({"action": "pc_close_program", "params": {"pid": 4242}}, cluster)
    assert ok and ok["label"] == "Close game.exe" and ok["mode"] == "light"
    assert actions.normalize({"action": "pc_close_program", "params": {"pid": 700}}, cluster)[0] is None
    assert actions.normalize({"action": "pc_close_program", "params": {"pid": 999}}, cluster)[0] is None
    assert actions.normalize({"action": "pc_cap_gpu_clock", "params": {"mhz": 99999}}, cluster)[0] is None
    cap = actions.normalize({"action": "pc_cap_gpu_clock", "params": {"mhz": "1650 MHz"}}, cluster)[0]
    assert cap["mode"] == "serious" and cap["admin"] and cap["params"]["mhz"] == 1650
    assert actions.normalize({"action": "pc_power_plan", "params": {"plan": "High Performance"}}, cluster)[0]


def test_serious_mode_unlocks_fixes_and_switches_itself_off(gpu_state):
    pc, w = fixes()
    gpu_state(temp=88, power=112, util=99, clock=1410, reasons="0x0000000000000044", pstate="P0")
    cluster, agent, src = laptop(pc)

    async def go(ticks):
        for t in range(1, ticks):
            src.poll()
            cluster.tick()
            agent.verify_pending()
            msg = agent.modes.check(agent.incidents)
            if msg:
                await agent.mode_changed()
            if t % 5 == 0:
                await agent.triage_once()
                await asyncio.gather(*list(agent.tasks))
    asyncio.run(go(16))
    inc = agent.incidents[0]
    agent.modes.set("serious", incident=inc["id"])
    asyncio.run(agent.mode_changed())
    assert not any(s["locked"] for s in inc["dry_run"]["steps"])
    agent.approve(inc["id"], run_all=True)
    assert any("-lgc" in c for c in w.admin_calls), "the clock cap ran"
    assert all(a["status"] == "done" for a in inc["actions"])
    gpu_state()  # cooler now
    asyncio.run(go(22))
    assert inc["status"] == "resolved"
    assert agent.modes.mode == "light", "serious mode ends once its incident is fixed"


def test_serious_mode_times_out():
    m = Modes()
    m.set("serious")
    m.until = time.time() - 1  # 30 minutes later
    assert "turned itself off" in m.check([]) and m.mode == "light"


def test_a_declined_fix_can_be_tried_again(gpu_state):
    pc, w = fixes(decline_admin=True)
    cluster, agent, src = laptop(pc)
    gpu_state(lost=True)  # the GPU drops off after connecting

    async def go(ticks):
        for t in range(1, ticks):
            src.poll()
            cluster.tick()
            if t % 5 == 0:
                await agent.triage_once()
                await asyncio.gather(*list(agent.tasks))
    asyncio.run(go(8))
    inc = agent.incidents[0]
    assert inc["actions"][0]["action"] == "pc_restart_gpu_driver"
    agent.approve(inc["id"])
    assert inc["actions"][0]["status"] == "failed" and inc["status"] == "awaiting_approval"
    w.decline_admin = False
    agent.retry_step(inc["id"], 0)
    agent.approve(inc["id"])
    assert inc["actions"][0]["status"] == "done"


def test_the_twin_can_never_run_a_fix():
    pc, w = fixes()
    cluster, agent, src = laptop(pc)
    assert cluster.clone().pc is None
