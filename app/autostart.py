"""Start Nightingale with Windows: a shortcut in the Startup folder that runs the tray app without a window."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "Nightingale.lnk"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def startup_dir() -> Path | None:
    appdata = os.environ.get("APPDATA")
    return Path(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup") if appdata else None


def pythonw() -> str:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def is_enabled() -> bool:
    d = startup_dir()
    return bool(d and (d / NAME).exists())


def set_enabled(on: bool) -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, "Starting with Windows only works on Windows."
    d = startup_dir()
    if d is None:
        return False, "Couldn't find the Windows Startup folder."
    link = d / NAME
    if not on:
        try:
            link.unlink()
        except FileNotFoundError:
            pass
        return True, "Nightingale won't start with Windows any more."
    script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut('" + str(link).replace("'", "''") + "'); "
              f"$s.TargetPath = '{pythonw()}'; $s.Arguments = '-m app.tray'; "
              f"$s.WorkingDirectory = '{str(ROOT).replace(chr(39), chr(39) * 2)}'; "
              "$s.Description = 'Nightingale: watches your GPU and fixes it with your approval'; $s.Save()")
    proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
    if proc.returncode != 0 or not link.exists():
        return False, f"Windows couldn't create the startup shortcut: {(proc.stderr or proc.stdout)[:200]}"
    return True, "Nightingale will start quietly in the tray when you sign in to Windows."
