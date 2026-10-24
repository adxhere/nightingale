"""Fixes Nightingale can run on this computer, in two modes.

Light mode: quick, reversible fixes (close a program stuck on the GPU, switch the power plan,
clear the shader cache, restart the GPU driver).
Serious mode: everything in light mode, plus clock caps, power limits, driver rollback and
updates, and fan/BIOS work through Armoury Crate. Serious mode turns itself off after 30 minutes.

Every fix runs only after the person approves it. Fixes that need administrator rights ask
Windows, which shows its own yes/no box; if the person says no, nothing changes. Driver changes
always make a System Restore point first. Some fixes are "guided": Nightingale prepares
everything and opens the right window, and the person makes the final click (Windows has no
command for rolling back a driver, and BIOS updates belong to the laptop maker's own tool).

The low-level runners (`run`, `ps`, `elevated`, `open`) can be swapped for fakes, so the
logic here is tested without Windows.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WIN = sys.platform == "win32"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

PLANS = {"balanced": "381b4222-f694-41f0-9685-ff5bb260df2e",
         "high_performance": "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
         "power_saver": "a1841308-3541-4fab-bc81-f71556f20b4a"}
PLAN_NAMES = {"balanced": "Balanced", "high_performance": "High performance", "power_saver": "Power saver"}
PLAN_ALIASES = {"high": "high_performance", "performance": "high_performance", "high performance": "high_performance",
                "saver": "power_saver", "power saver": "power_saver", "battery": "power_saver"}
AC_MODES = {"silent": "Silent", "quiet": "Silent", "performance": "Performance", "balanced": "Performance",
            "turbo": "Turbo", "max": "Turbo", "manual": "Manual"}
# Programs that keep Windows itself running: never closed, whatever is proposed.
NEVER_CLOSE = {"system", "idle", "registry", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe",
               "smss.exe", "dwm.exe", "explorer.exe", "svchost.exe", "fontdrvhost.exe", "nvcontainer.exe",
               "nvdisplay.container.exe", "python.exe", "pythonw.exe", "py.exe", "searchhost.exe",
               "shellexperiencehost.exe", "startmenuexperiencehost.exe", "textinputhost.exe", "ctfmon.exe",
               "sihost.exe", "taskhostw.exe", "runtimebroker.exe", "applicationframehost.exe", "lockapp.exe",
               "audiodg.exe", "msmpeng.exe", "securityhealthservice.exe"}
SHADER_DIRS = [("LOCALAPPDATA", "NVIDIA", "DXCache"), ("LOCALAPPDATA", "NVIDIA", "GLCache"),
               ("LOCALAPPDATA", "D3DSCache"), ("LOCALAPPDATA", "NVIDIA Corporation", "NV_Cache")]
NVIDIA_APPS = [r"C:\Program Files\NVIDIA Corporation\NVIDIA App\CEF\NVIDIA App.exe",
               r"C:\Program Files\NVIDIA Corporation\NVIDIA GeForce Experience\NVIDIA GeForce Experience.exe"]
DRIVER_PAGE = "https://www.nvidia.com/en-us/drivers/"

PC_CATALOG: dict[str, dict] = {
    "pc_close_program": {
        "params": ["pid", "name"], "mode": "light", "risk": "medium", "admin": False,
        "desc": "Close one program that is using the GPU (pid and name exactly as gpu_processes lists them). "
                "Unsaved work in it may be lost. Never for Windows' own processes."},
    "pc_power_plan": {
        "params": ["plan"], "mode": "light", "risk": "low", "admin": False,
        "desc": "Switch the Windows power plan: balanced, high_performance or power_saver. Easy to switch back."},
    "pc_clear_shader_cache": {
        "params": [], "mode": "light", "risk": "low", "admin": False,
        "desc": "Delete NVIDIA's and DirectX's shader caches. Fixes stutter and some crashes after driver "
                "updates; games rebuild the cache on their next launch."},
    "pc_restart_gpu_driver": {
        "params": [], "mode": "light", "risk": "medium", "admin": True,
        "desc": "Restart the NVIDIA GPU device in Windows. The screen may go black for a few seconds and "
                "programs using the GPU may close. Recovers a GPU that stopped answering or a hung driver."},
    "pc_uncap_gpu_clock": {
        "params": [], "mode": "light", "risk": "low", "admin": True,
        "desc": "Remove a GPU clock cap and return to normal clock behaviour."},
    "pc_create_restore_point": {
        "params": [], "mode": "light", "risk": "low", "admin": True,
        "desc": "Make a Windows System Restore point (Windows allows one per 24 hours)."},
    "pc_cap_gpu_clock": {
        "params": ["mhz"], "mode": "serious", "risk": "medium", "admin": True,
        "desc": "Cap the GPU's top clock (MHz) so it makes less heat: the laptop-safe alternative to an "
                "undervolt. Lasts until removed or the laptop restarts. Use about 75-90% of the max clock."},
    "pc_set_power_limit": {
        "params": ["watts"], "mode": "serious", "risk": "high", "admin": True,
        "desc": "Change the GPU's power limit (watts). Most laptop GPUs refuse this; Nightingale reports it."},
    "pc_performance_mode": {
        "params": ["mode"], "mode": "serious", "risk": "low", "admin": False, "guided": True,
        "desc": "Open Armoury Crate to switch the fan and performance mode (Silent, Performance or Turbo). "
                "The person picks the mode in the app."},
    "pc_update_driver": {
        "params": [], "mode": "serious", "risk": "high", "admin": True, "guided": True,
        "desc": "Make a restore point, then open the NVIDIA App (or nvidia.com) to install the latest "
                "official driver with a clean installation."},
    "pc_rollback_driver": {
        "params": [], "mode": "serious", "risk": "high", "admin": True, "guided": True,
        "desc": "Make a restore point, then open the GPU's driver page in Device Manager, where the person "
                "clicks Roll Back Driver. For crashes that started after a driver update."},
    "pc_bios_update": {
        "params": [], "mode": "serious", "risk": "high", "admin": False, "guided": True,
        "desc": "Open Armoury Crate (or MyASUS) to check for BIOS and firmware updates. Charger plugged in."},
}
LABELS = {
    "pc_close_program": "Close {name}",
    "pc_power_plan": "Switch Windows to the {plan_name} power plan",
    "pc_clear_shader_cache": "Clear the NVIDIA shader cache",
    "pc_restart_gpu_driver": "Restart the GPU driver",
    "pc_uncap_gpu_clock": "Remove the GPU clock cap",
    "pc_create_restore_point": "Make a Windows restore point",
    "pc_cap_gpu_clock": "Cap the GPU clock at {mhz} MHz",
    "pc_set_power_limit": "Set the GPU power limit to {watts} W",
    "pc_performance_mode": "Switch Armoury Crate to {mode} mode",
    "pc_update_driver": "Update the NVIDIA driver (restore point first)",
    "pc_rollback_driver": "Roll back the NVIDIA driver (restore point first)",
    "pc_bios_update": "Check for BIOS and firmware updates",
}
RESTORE_FIRST = {"pc_update_driver", "pc_rollback_driver"}


def catalog_text() -> str:
    lines = []
    for name, spec in PC_CATALOG.items():
        tags = [spec["mode"] + " mode"] + (["Windows asks for permission"] if spec["admin"] else []) + \
               (["guided: the person makes the final click"] if spec.get("guided") else [])
        lines.append(f"- {name}({', '.join(spec['params'])}) [{'; '.join(tags)}]: {spec['desc']}")
    return "\n".join(lines)


def _int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    m = re.search(r"-?\d+", str(value or ""))
    return int(m.group()) if m else None


def normalize(name: str, given: dict, cluster) -> tuple[dict | None, str | None]:
    """Validate one proposed fix for this computer. Returns (params, None) or (None, why not)."""
    spec = PC_CATALOG[name]
    node = cluster.nodes.get("laptop") if cluster is not None else None
    if node is None or getattr(cluster, "local", None) is None:
        return None, "this computer's GPU isn't connected"
    g = node.gpus[0] if node.gpus else None
    p: dict = {}
    if name == "pc_close_program":
        pid = _int(given.get("pid"))
        procs = cluster.local.snapshot().get("processes", [])
        match = next((x for x in procs if x["pid"] == pid), None)
        if match is None:
            want = str(given.get("name") or "").strip().lower()
            match = next((x for x in procs if want and x["name"].lower() == want), None)
        if match is None:
            return None, "that program isn't using the GPU right now (pick one that gpu_processes lists)"
        if match["name"].lower() in NEVER_CLOSE:
            return None, f"{match['name']} is part of Windows and is never closed"
        p = {"pid": match["pid"], "name": match["name"]}
    elif name == "pc_power_plan":
        plan = str(given.get("plan") or "").strip().lower().replace("-", "_")
        plan = PLAN_ALIASES.get(plan.replace("_", " "), PLAN_ALIASES.get(plan, plan))
        if plan not in PLANS:
            return None, "plan must be balanced, high_performance or power_saver"
        p = {"plan": plan, "plan_name": PLAN_NAMES[plan]}
    elif name == "pc_cap_gpu_clock":
        mhz = _int(given.get("mhz"))
        top = g.max_clock if g else 3000
        if mhz is None or not 300 <= mhz <= top:
            return None, f"mhz must be between 300 and this GPU's max clock ({top:,} MHz)"
        p = {"mhz": mhz}
    elif name == "pc_set_power_limit":
        watts = _int(given.get("watts"))
        top = (g.power_max or int((g.power_limit or 100) * 1.5)) if g else 150
        if watts is None or not 5 <= watts <= top:
            return None, f"watts must be between 5 and {top}"
        p = {"watts": watts}
    elif name == "pc_performance_mode":
        mode = AC_MODES.get(str(given.get("mode") or "performance").strip().lower(), "Performance")
        p = {"mode": mode}
    p["node"] = "laptop"
    return p, None


def label(name: str, params: dict) -> str:
    try:
        return LABELS[name].format(**params)
    except (KeyError, IndexError):
        return name


# ---------------------------------------------------------------------- runners
def _run(args: list[str], timeout: float = 30) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              creationflags=NO_WINDOW if WIN else 0)
        return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()
    except FileNotFoundError:
        return 127, f"{args[0]} was not found"
    except subprocess.TimeoutExpired:
        return 124, f"{args[0]} did not finish within {timeout:.0f} seconds"
    except OSError as exc:
        return 126, str(exc)


def _ps(script: str, timeout: float = 30) -> tuple[int, str]:
    return _run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                timeout)


def _encode(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _elevated(script: str, timeout: float = 240) -> tuple[int, str]:
    """Run a PowerShell script as administrator. Windows shows its permission box; the person decides."""
    out = Path(tempfile.gettempdir()) / f"nightingale-{os.getpid()}-{time.time_ns()}.txt"
    inner = (f"$code = 0\n"
             f"try {{ $r = & {{ {script} }} 2>&1 | Out-String; if ($LASTEXITCODE) {{ $code = $LASTEXITCODE }} }} "
             f"catch {{ $r = $_ | Out-String; $code = 1 }}\n"
             f"Set-Content -LiteralPath '{out}' -Value $r -Encoding UTF8\n"
             f"exit $code")
    outer = ("try { $p = Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-NonInteractive',"
             "'-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-EncodedCommand','" + _encode(inner) + "') "
             "-Verb RunAs -WindowStyle Hidden -Wait -PassThru; exit $p.ExitCode } "
             "catch { Write-Output ('ELEVATION_FAILED: ' + $_.Exception.Message); exit 1223 }")
    code, text = _ps(outer, timeout)
    body = ""
    try:
        body = out.read_text(encoding="utf-8-sig").strip()
        out.unlink()
    except OSError:
        pass
    return code, (body or text).strip()


def _open(target: str) -> tuple[bool, str]:
    try:
        if WIN:
            os.startfile(target)  # noqa: S606 - opening a known app or the official driver page
        else:
            subprocess.Popen(["xdg-open", target])
        return True, ""
    except OSError as exc:
        return False, str(exc)


def _declined(text: str) -> bool:
    return bool(re.search(r"canceled by the user|cancelled by the user|ELEVATION_FAILED", text, re.I))


# ----------------------------------------------------------------------- fixes
class PcFixes:
    """Runs fixes on this computer. On anything but Windows, every fix explains that it is Windows-only."""

    def __init__(self, smi: list[str] | None = None, run=None, ps=None, elevated=None, opener=None,
                 windows: bool | None = None) -> None:
        self.smi = smi or ["nvidia-smi"]
        self.run = run or _run
        self.ps = ps or _ps
        self.elevated = elevated or _elevated
        self.open = opener or _open
        self.windows = WIN if windows is None else windows
        self.restore_made_at = 0.0
        self.clock_cap: int | None = None

    # ------------------------------------------------------------ helpers
    def _gpu_instance(self) -> str:
        code, out = self.ps("Get-PnpDevice -Class Display -PresentOnly | Where-Object { $_.FriendlyName -like "
                            "'*NVIDIA*' } | Select-Object -First 1 -ExpandProperty InstanceId", 20)
        return out.strip().splitlines()[0].strip() if code == 0 and out.strip() else ""

    def _start_app(self, *names: str) -> str:
        for n in names:
            code, out = self.ps(f"Get-StartApps | Where-Object {{ $_.Name -like '*{n}*' }} | "
                                "Select-Object -First 1 -ExpandProperty AppID", 20)
            app_id = out.strip().splitlines()[0].strip() if code == 0 and out.strip() else ""
            if app_id:
                self.run(["explorer.exe", f"shell:AppsFolder\\{app_id}"], 15)
                return n
        return ""

    def _restore_point(self, why: str) -> tuple[bool, str]:
        script = (f"Checkpoint-Computer -Description 'Nightingale: before {why}' -RestorePointType "
                  "'MODIFY_SETTINGS' -WarningVariable w -WarningAction SilentlyContinue; "
                  "if ($w) { Write-Output ('WARNING: ' + ($w -join ' ')) } else { Write-Output 'RESTORE_OK' }")
        code, out = self.elevated(script)
        if _declined(out):
            return False, "You said no to the Windows permission box, so no restore point was made and nothing changed."
        if re.search(r"1440 minutes|within the past", out, re.I):
            self.restore_made_at = time.time()
            return True, ("Windows skipped a new restore point because one was made in the last 24 hours; "
                          "that one can still undo this change.")
        if "RESTORE_OK" in out and code == 0:
            self.restore_made_at = time.time()
            return True, "Made a Windows restore point."
        if re.search(r"disabled|turned off|service cannot be started", out, re.I):
            return False, ("System Restore is turned off on this PC, so no restore point could be made. Turn it on "
                           "in Windows (search 'Create a restore point', select drive C:, Configure, Turn on) and try again.")
        return False, f"Windows couldn't make a restore point: {out[:200] or 'unknown error'}"

    # ----------------------------------------------------------- dispatch
    def execute(self, name: str, p: dict) -> tuple[bool, str]:
        if not self.windows:
            return False, "Fixes on this computer work on Windows only for now. Nothing changed."
        fn = getattr(self, "_do_" + name[3:], None)
        if fn is None:
            return False, f"Unknown fix {name}."
        try:
            return fn(p)
        except Exception as exc:  # a fix must never take Nightingale down
            return False, f"The fix failed: {exc}"

    def precheck(self, name: str, p: dict) -> tuple[bool, str]:
        """Check a fix can run, without changing anything."""
        spec = PC_CATALOG[name]
        if not self.windows:
            return False, "Fixes on this computer work on Windows only for now."
        note = " Windows will ask for permission." if spec["admin"] else ""
        if name == "pc_close_program":
            code, out = self.run(["tasklist", "/FI", f"PID eq {p['pid']}", "/FO", "CSV", "/NH"], 15)
            if str(p["pid"]) not in out:
                return False, f"{p['name']} (pid {p['pid']}) is no longer running."
            return True, f"{p['name']} is running and can be closed. Save your work in it first."
        if name == "pc_power_plan":
            code, out = self.run(["powercfg", "/list"], 15)
            if PLANS[p["plan"]] not in out.lower():
                return False, (f"The {p['plan_name']} plan isn't available on this laptop (many laptops hide it). "
                               "Use Armoury Crate's operating mode instead.")
            return True, f"The {p['plan_name']} plan is available."
        if name == "pc_clear_shader_cache":
            size = sum(f.stat().st_size for d in self._shader_dirs() for f in d.rglob("*") if f.is_file())
            return True, f"About {size / 1e6:.0f} MB of shader cache to clear."
        if name in ("pc_restart_gpu_driver", "pc_rollback_driver"):
            if not self._gpu_instance():
                return False, "Windows doesn't list an NVIDIA display device right now."
            return True, ("The NVIDIA GPU was found in Windows." + note)
        if name in ("pc_cap_gpu_clock", "pc_uncap_gpu_clock", "pc_set_power_limit"):
            code, out = self.run(self.smi + ["--query-gpu=name", "--format=csv,noheader"], 15)
            if code != 0:
                return False, "nvidia-smi isn't answering."
            extra = " Most laptop GPUs refuse power-limit changes; Nightingale will say if yours does." \
                if name == "pc_set_power_limit" else ""
            return True, "nvidia-smi is ready." + note + extra
        if name == "pc_update_driver":
            app = next((a for a in NVIDIA_APPS if os.path.exists(a)), None)
            return True, ("Will make a restore point, then open " + ("the NVIDIA App." if app else "nvidia.com's driver page.") + note)
        if name in ("pc_performance_mode", "pc_bios_update"):
            return True, "Will open Armoury Crate (or MyASUS)."
        return True, "Ready." + note

    # -------------------------------------------------------------- fixes
    def _do_close_program(self, p) -> tuple[bool, str]:
        pid, name = str(p["pid"]), p["name"]
        self.run(["taskkill", "/PID", pid, "/T"], 15)  # ask it to close, like clicking X
        for _ in range(10):
            time.sleep(0.5)
            code, out = self.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], 15)
            if pid not in out:
                return True, f"Closed {name}. Its GPU memory is free again."
        code, out = self.run(["taskkill", "/PID", pid, "/T", "/F"], 15)
        if code == 0:
            return True, f"{name} didn't close when asked, so Nightingale ended it. Its GPU memory is free again."
        if re.search(r"access is denied", out, re.I):
            code, out = self.elevated(f"taskkill /PID {pid} /T /F")
            if _declined(out):
                return False, f"{name} needs administrator rights to close, and you said no. Nothing changed."
            if code == 0:
                return True, f"Ended {name} with administrator rights."
        return False, f"Couldn't close {name}: {out[:160]}"

    def _do_power_plan(self, p) -> tuple[bool, str]:
        code, before = self.run(["powercfg", "/getactivescheme"], 15)
        was = next((PLAN_NAMES[k] for k, guid in PLANS.items() if guid in before.lower()), None)
        code, out = self.run(["powercfg", "/setactive", PLANS[p["plan"]]], 15)
        if code != 0:
            return False, (f"Windows wouldn't switch to {p['plan_name']}: {out[:140] or 'plan not available'}. "
                           "On many laptops Armoury Crate controls this instead.")
        return True, f"Switched to the {p['plan_name']} power plan" + (f" (it was {was})." if was and was != p["plan_name"] else ".")

    def _shader_dirs(self) -> list[Path]:
        out = []
        for env, *parts in SHADER_DIRS:
            base = os.environ.get(env)
            if base and Path(base, *parts).is_dir():
                out.append(Path(base, *parts))
        return out

    def _do_clear_shader_cache(self, p) -> tuple[bool, str]:
        freed = skipped = 0
        for d in self._shader_dirs():
            for f in sorted(d.rglob("*"), key=lambda x: -len(x.parts)):
                try:
                    if f.is_file():
                        size = f.stat().st_size
                        f.unlink()
                        freed += size
                    elif f.is_dir():
                        f.rmdir()
                except OSError:
                    skipped += 1
        if not freed and not skipped:
            return True, "The shader caches were already empty."
        return True, (f"Cleared {freed / 1e6:.0f} MB of shader cache." +
                      (f" {skipped} files were in use and were left alone; close games and try again to clear them."
                       if skipped else "") + " Games rebuild the cache on their next launch.")

    def _do_restart_gpu_driver(self, p) -> tuple[bool, str]:
        inst = self._gpu_instance()
        if not inst:
            return False, "Windows doesn't list an NVIDIA display device, so there's nothing to restart."
        script = (f"pnputil /restart-device '{inst}'; if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 3010) {{ "
                  f"Disable-PnpDevice -InstanceId '{inst}' -Confirm:$false; Start-Sleep -Seconds 2; "
                  f"Enable-PnpDevice -InstanceId '{inst}' -Confirm:$false; Write-Output 'RESTARTED_BY_TOGGLE' }}")
        code, out = self.elevated(script, 180)
        if _declined(out):
            return False, "You said no to the Windows permission box, so the driver wasn't restarted."
        if code == 3010 or re.search(r"reboot is (needed|required)", out, re.I):
            return True, "Windows started the GPU restart but needs a computer restart to finish it."
        if code == 0 or re.search(r"restarted|RESTARTED_BY_TOGGLE", out, re.I):
            return True, "Restarted the NVIDIA GPU. If the screen flickered, that was it."
        return False, f"Windows couldn't restart the GPU: {out[:200]}"

    def _smi_admin(self, args: list[str], ok_text: str, what: str) -> tuple[bool, str]:
        # Quote every argument: PowerShell would split an unquoted "210,1500" into two.
        cmd = " ".join("'" + a.replace("'", "''") + "'" for a in self.smi + args)
        code, out = self.elevated(f"& {cmd}", 120)
        if _declined(out):
            return False, f"You said no to the Windows permission box, so the {what} didn't change."
        if re.search(r"not supported|insufficient permissions|not permitted|terminating early", out, re.I):
            return False, f"This GPU doesn't allow changing the {what} from software ({out.strip().splitlines()[-1][:160] if out.strip() else 'not supported'})."
        if code == 0:
            return True, ok_text
        return False, f"nvidia-smi couldn't change the {what}: {out[:200]}"

    def _do_cap_gpu_clock(self, p) -> tuple[bool, str]:
        ok, msg = self._smi_admin(["-lgc", f"210,{p['mhz']}"],
                                  f"Capped the GPU clock at {p['mhz']:,} MHz. It runs cooler and a little slower; "
                                  "the cap lasts until you remove it or restart.", "clock limit")
        if ok:
            self.clock_cap = p["mhz"]
        return ok, msg

    def _do_uncap_gpu_clock(self, p) -> tuple[bool, str]:
        ok, msg = self._smi_admin(["-rgc"], "Removed the GPU clock cap. Clocks behave normally again.", "clock limit")
        if ok:
            self.clock_cap = None
        return ok, msg

    def _do_set_power_limit(self, p) -> tuple[bool, str]:
        return self._smi_admin(["-pl", str(p["watts"])], f"Set the GPU power limit to {p['watts']} W.", "power limit")

    def _do_create_restore_point(self, p) -> tuple[bool, str]:
        return self._restore_point("a change")

    def _ensure_restore_point(self, why: str) -> tuple[bool, str]:
        if time.time() - self.restore_made_at < 3600:
            return True, "A restore point from the last hour covers this change."
        return self._restore_point(why)

    def _do_update_driver(self, p) -> tuple[bool, str]:
        ok, rp = self._ensure_restore_point("driver update")
        if not ok:
            return False, rp
        app = next((a for a in NVIDIA_APPS if os.path.exists(a)), None)
        opened, err = self.open(app or DRIVER_PAGE)
        where = ("the NVIDIA App: open Drivers, download the latest Game Ready or Studio driver, and choose a clean "
                 "installation" if app else "nvidia.com's driver page: pick your GPU, download the driver, run it, "
                 "and choose Custom > Perform a clean installation")
        if not opened:
            return False, f"{rp} But Nightingale couldn't open {where.split(':')[0]}: {err}"
        return True, f"{rp} Opened {where}. Nightingale re-checks the GPU afterwards."

    def _do_rollback_driver(self, p) -> tuple[bool, str]:
        ok, rp = self._ensure_restore_point("driver rollback")
        if not ok:
            return False, rp
        inst = self._gpu_instance()
        if not inst:
            return False, f"{rp} But Windows doesn't list the NVIDIA GPU right now, so Device Manager can't open it."
        self.run(["rundll32.exe", "devmgr.dll,DeviceProperties_RunDLL", "/DeviceID", inst], 20)
        return True, (f"{rp} Opened your GPU's properties: go to the Driver tab and click Roll Back Driver. If it's "
                      "greyed out, Windows didn't keep the previous driver; use the driver update step instead.")

    def _do_performance_mode(self, p) -> tuple[bool, str]:
        app = self._start_app("Armoury Crate", "MyASUS")
        if not app:
            return False, "Couldn't find Armoury Crate or MyASUS. Open it from the Start menu and pick " + p["mode"] + " mode."
        return True, (f"Opened {app}: under Operating Mode, pick {p['mode']}. Nightingale re-checks temperatures "
                      "afterwards.")

    def _do_bios_update(self, p) -> tuple[bool, str]:
        app = self._start_app("Armoury Crate", "MyASUS")
        if not app:
            return False, "Couldn't find Armoury Crate or MyASUS. Install one from the Microsoft Store to get ASUS updates."
        return True, (f"Opened {app}: go to its Update Center (Armoury Crate) or Customer Support > Live Update "
                      "(MyASUS) and install the BIOS and firmware updates it lists. Keep the charger plugged in and "
                      "don't close the lid while it updates.")

    # ---------------------------------------------------------------- info
    def driver_info(self) -> dict:
        if not self.windows:
            return {}
        code, out = self.ps("Get-CimInstance Win32_VideoController | Where-Object { $_.Name -like '*NVIDIA*' } | "
                            "Select-Object -First 1 Name,DriverVersion,"
                            "@{n='DriverDate';e={$_.DriverDate.ToString('yyyy-MM-dd')}} | ConvertTo-Json -Compress", 20)
        try:
            d = json.loads(out) if code == 0 and out.strip().startswith("{") else {}
        except ValueError:
            d = {}
        return {"name": d.get("Name") or "", "windows_version": d.get("DriverVersion") or "",
                "date": d.get("DriverDate") or ""}

    def active_plan(self) -> str:
        if not self.windows:
            return ""
        code, out = self.run(["powercfg", "/getactivescheme"], 15)
        m = re.search(r"\(([^)]+)\)\s*$", out.strip())
        return m.group(1) if m else ""


def make_default(smi: list[str] | None = None) -> PcFixes:
    found = smi or ([shutil.which("nvidia-smi")] if shutil.which("nvidia-smi") else None)
    return PcFixes(smi=found)
