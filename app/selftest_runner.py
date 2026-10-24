"""Runs the GPU self-tests (app/selftest.py) in a child process and reports their progress.

Results go three places: the dashboard (live progress), the history file, and Nightingale's own
log for this computer, so a failed test is picked up by triage and investigated like any problem.
While a test runs, Nemotron is told the load is intentional. A stress test is stopped at once if
the GPU reaches 95°C.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = {"quick": "Quick check", "memory": "Memory test", "stress": "Stress test"}
STOP_TEMP = 95
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class SelfTests:
    def __init__(self, cluster, store=None, python: str | None = None, extra_env: dict | None = None) -> None:
        self.cluster = cluster
        self.store = store
        self.python = python or sys.executable
        self.extra_env = extra_env or {}
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.state = self._blank()
        self.history: list[dict] = store.selftests() if store is not None and hasattr(store, "selftests") else []

    @staticmethod
    def _blank() -> dict:
        return {"running": False, "test": None, "label": "", "minutes": None, "started_at": None, "pct": 0,
                "message": "", "device": None, "tflops": [], "errors": 0, "result": None, "error": "", "stopped": ""}

    # ------------------------------------------------------------- control
    def start(self, test: str, minutes: float = 3) -> dict:
        if test not in TESTS:
            raise ValueError("test must be quick, memory or stress")
        if self.cluster.local is None:
            raise ValueError("Connect this computer's GPU first.")
        with self.lock:
            if self.state["running"]:
                raise ValueError("A self-test is already running.")
            minutes = max(1.0, min(float(minutes or 3), 30.0)) if test == "stress" else None
            self.state = {**self._blank(), "running": True, "test": test, "label": TESTS[test], "minutes": minutes,
                          "started_at": time.time(), "message": "Starting PyTorch"}
            args = [self.python, "-m", "app.selftest", "--test", test] + (["--minutes", str(minutes)] if minutes else [])
            env = {**os.environ, **self.extra_env, "PYTHONUNBUFFERED": "1"}
            self.proc = subprocess.Popen(args, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True, env=env, creationflags=NO_WINDOW)
        what = f"{TESTS[test]}" + (f" ({minutes:.0f} min)" if minutes else "")
        self.cluster.local_activity = f"a {what.lower()} the person started is running on purpose"
        self.cluster._log("laptop", "selftest", "info", f"{what} started")
        threading.Thread(target=self._read, args=(self.proc,), daemon=True, name="nightingale-selftest").start()
        return self.status()

    def stop(self, reason: str = "Stopped by you.") -> dict:
        with self.lock:
            proc = self.proc
            if not self.state["running"] or proc is None:
                return self.status()
            self.state["stopped"] = reason
        try:
            proc.terminate()
        except OSError:
            pass
        return self.status()

    def check_temperature(self) -> None:
        """Called every tick: a stress test never pushes the GPU past 95°C."""
        if not self.state["running"]:
            return
        node = self.cluster.nodes.get("laptop")
        hot = node and any(not g.lost and g.temp >= STOP_TEMP for g in node.gpus)
        if hot:
            self.stop(f"Stopped automatically: the GPU reached {STOP_TEMP}°C.")

    def status(self) -> dict:
        with self.lock:
            st = dict(self.state)
            st["tflops"] = list(st["tflops"])
        st["history"] = self.history[:10]
        st["elapsed"] = round(time.time() - st["started_at"]) if st["running"] and st["started_at"] else None
        return st

    # ------------------------------------------------------------- reading
    def _read(self, proc: subprocess.Popen) -> None:
        result, error = None, ""
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            with self.lock:
                kind = ev.get("event")
                if kind == "start":
                    self.state["device"] = ev.get("device")
                    self.state["message"] = "Running"
                elif kind == "progress":
                    self.state["pct"] = ev.get("pct", self.state["pct"])
                    self.state["message"] = ev.get("message", "")
                    if "tflops" in ev:
                        self.state["tflops"].append(ev["tflops"])
                    self.state["errors"] = ev.get("errors", self.state["errors"])
                elif kind == "result":
                    result = ev
                elif kind == "error":
                    error = ev.get("message", "The self-test failed.")
        code = proc.wait()
        err_text = (proc.stderr.read() or "").strip().splitlines()[-1:] if proc.stderr else []
        with self.lock:
            stopped = self.state["stopped"]
            if result is None and not error:
                error = stopped or (f"The self-test stopped unexpectedly (exit {code})"
                                    + (f": {err_text[0][:200]}" if err_text else ".") +
                                    " A crash during a GPU test can itself point at a driver or hardware problem.")
            label = self.state["label"]
            entry = {"at": time.time(), "test": self.state["test"], "label": label,
                     "ok": bool(result and result.get("ok")),
                     "summary": result["summary"] if result else error, "details": result.get("details", {}) if result else {},
                     "device": self.state["device"], "stopped": bool(stopped)}
        self.history.insert(0, entry)
        self.cluster.local_activity = ""
        if self.store is not None and hasattr(self.store, "save_selftest") and not stopped:
            self.store.save_selftest(entry)
        self._log(entry, error, stopped)
        with self.lock:  # last, so anyone waiting for the end sees the logs and history too
            self.state.update({"running": False, "pct": 100 if result else self.state["pct"], "result": result,
                               "error": "" if result else error})

    def _log(self, entry: dict, error: str, stopped: str) -> None:
        c = self.cluster
        if stopped:
            c._log("laptop", "selftest", "warn" if "95" in stopped else "info", f"{entry['label']} stopped: {stopped}")
        elif error and "PyTorch" in error:
            c._log("laptop", "selftest", "info", f"{entry['label']} couldn't run: {error}")
        elif entry["ok"]:
            c._log("laptop", "selftest", "info", f"{entry['label']} passed: {entry['summary']}")
        else:
            # A failed test is a finding: logged as an error, triage sees it and Nemotron investigates.
            c._log("laptop", "selftest", "error", f"{entry['label']} FAILED: {entry['summary']}")
        c.selftests = self.history[:5]
