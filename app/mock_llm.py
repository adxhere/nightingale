"""Offline stand-in for the Nemotron models (demo mode).

Used when NEBIUS_API_KEY is not set, so the whole app runs end to end without
credits. It reads the same inputs the real models receive (the triage window, the
answers of the investigation tools, pasted logs) and returns the same JSON shapes,
using simple hand-written rules. The real product uses Nemotron on Nebius Token
Factory; this file exists for development, tests and offline demos.
"""
from __future__ import annotations

import re

ANOMALY_RE = re.compile(
    r"^\s+(dgx-\d+) \[([A-H]-\d+), (\w+)\] g(\d+) temp=(\d+)C power=\d+W/(\d+)W clock=(\d+)MHz "
    r"throttle=(\S+) sbe=(\d+)\(\+(\d+)\) dbe=(\d+)\(\+(\d+)\) nvlink_crc=(\d+)\(\+(\d+)\)")
LOST_RE = re.compile(r"^\s+(dgx-\d+) \[([A-H]-\d+), (\w+)\] g(\d+) LOST")
EVENT_RE = re.compile(r"^\s+\d\d:\d\d:\d\d (\S+) (\S+): (.*)$")
XID_RE = re.compile(r"Xid \(PCI:([0-9a-fA-F:.]+)\): (\d+)")
RACK_RE = re.compile(r"rack ([A-H]-\d+): busy GPUs average (\d+)C vs fleet (\d+)C")
TARGET_RE = re.compile(r"\b(dgx-\d{4}|[A-H]-\d{2}|laptop)\b")
LOCAL_RE = re.compile(r"^\s+(laptop) \[this computer\] g(\d+) .+? temp=(\d+)C power=\d+W/\d+W "
                      r"clock=(\d+)/(\d+)MHz util=(\d+)% vram=(\d+)/(\d+)MiB throttle=(\S+)")
LOCAL_LOST_RE = re.compile(r"^\s+(laptop) \[this computer\] g(\d+) LOST")
REAL_LINE_RE = re.compile(r"Real servers \(.*?\): \d+ servers, \d+ GPUs: (.*?); cordoned: (.*?); NotReady: (.*?); "
                          r"not reporting: (.*?)\. ")
REAL_ROW_RE = re.compile(r"^\s+(\S+) \[real server, rack (R-\d+), (\w+)(, NotReady)?\] g(\d+) (.*)$")
REAL_VALS_RE = re.compile(r"temp=(\d+)C .*?util=(\d+)% throttle=(\S+) sbe=(\d+)\(\+(\d+)\) dbe=(\d+)\(\+(\d+)\) "
                          r"nvlink_crc=(\d+)\(\+(\d+)\) remap_pending=(yes|no) xid=(none|\d+)")


def _names(text: str) -> set:
    return {x.strip() for x in re.sub(r" and \d+ more", "", text).split(",") if x.strip() and x.strip() != "none"}


# --------------------------------------------------------------------------- triage
def triage(digest: str) -> dict:
    skip = set()
    real: set = set()
    rm = REAL_LINE_RE.search(digest)
    if rm:
        real = _names(rm.group(1))
    for label in ("Known issues already handled", "Open incidents"):
        m = re.search(label + r"[^:]*: (.*)", digest)
        if m:
            skip |= set(TARGET_RE.findall(m.group(1)))
            skip |= {n for n in real if re.search(r"(^|[\s(,])" + re.escape(n) + r"([\s(,]|$)", m.group(1))}
    hot_racks = {}
    for rid, avg, fleet in RACK_RE.findall(digest):
        hot_racks[rid] = int(avg) - int(fleet)
    findings: dict[str, dict] = {}
    order = {"watch": 1, "incident": 2}

    def add(target, gpu, severity, signal, evidence):
        if not target or target in skip:
            return
        cur = findings.get(target)
        if cur is None or order[severity] > order[cur["severity"]]:
            findings[target] = {"target": target, "gpu": gpu, "severity": severity, "signal": signal,
                                "evidence": [evidence.strip()] + (cur["evidence"][:2] if cur else [])}
        elif len(cur["evidence"]) < 3:
            cur["evidence"].append(evidence.strip())

    for rid, gap in hot_racks.items():
        if rid not in skip:
            add(rid, None, "incident" if gap >= 8 else "watch", "Rack running hotter than the fleet",
                f"rack {rid} busy GPUs {gap}C above the fleet average")
    if rm:
        for n in _names(rm.group(3)):
            add(n, None, "incident", "Kubernetes node is NotReady", f"{n} NotReady")
    rack_of: dict[str, str] = {}
    for line in digest.splitlines():
        m = REAL_ROW_RE.match(line)
        if m:
            node, _rack, _st, _nr, g, rest = m.groups()
            g = int(g)
            if rest.startswith("NOT REPORTING"):
                add(node, g, "incident", "A real GPU stopped reporting to DCGM", line)
                continue
            v = REAL_VALS_RE.search(rest)
            if not v:
                continue
            temp, util, thr, _sbe, dsbe, dbe, ddbe, _nvl, dnvl, remap, xid = v.groups()
            temp, util, dsbe, ddbe, dnvl = int(temp), int(util), int(dsbe), int(ddbe), int(dnvl)
            if xid != "none":
                add(node, g, "incident", f"Driver reported Xid {xid} on a real GPU", line)
            elif ddbe > 0 or int(dbe) > 0:
                add(node, g, "incident", "Uncorrectable (double-bit) ECC error", line)
            elif remap == "yes":
                add(node, g, "incident", "Pending memory row remap", line)
            elif dnvl > 0:
                add(node, g, "incident", "NVLink error counter is climbing", line)
            elif "HW_THERMAL" in thr or ("THERMAL" in thr and util >= 30):
                add(node, g, "incident", "Real GPU slowing down for heat under load", line)
            elif dsbe >= 10:
                add(node, g, "watch", "Single-bit ECC errors climbing fast", line)
            elif temp >= 83:
                add(node, g, "watch", "Real GPU temperature trending high", line)
            continue
        m = LOCAL_LOST_RE.match(line)
        if m:
            add("laptop", int(m.group(2)), "incident", "This computer's GPU stopped answering the driver", line)
            continue
        m = LOCAL_RE.match(line)
        if m:
            _n, g, temp, _clk, _max, util, used, total, thr = m.groups()
            g, temp, util, used, total = int(g), int(temp), int(util), int(used), int(total)
            if "THERMAL" in thr:
                add("laptop", g, "incident" if "HW_THERMAL" in thr or util >= 50 else "watch",
                    "This computer's GPU is cutting its clocks to stay cool", line)
            elif total and used / total >= 0.95:
                add("laptop", g, "watch", "This computer's GPU memory is almost full", line)
            elif temp >= 90:
                add("laptop", g, "watch", "This computer's GPU is running very hot", line)
            continue
        m = LOST_RE.match(line)
        if m:
            node, rack, _st, g = m.groups()
            rack_of[node] = rack
            if rack not in skip:
                add(node, int(g), "incident", "GPU is no longer visible on the PCIe bus", line)
            continue
        m = ANOMALY_RE.match(line)
        if m:
            node, rack, _st, g, temp, _lim, _clk, thr, _sbe, dsbe, _dbe, ddbe, _nvl, dnvl = m.groups()
            rack_of[node] = rack
            if rack in skip:
                continue
            g, temp, dsbe, ddbe, dnvl = int(g), int(temp), int(dsbe), int(ddbe), int(dnvl)
            if ddbe > 0:
                add(node, g, "incident", "Uncorrectable (double-bit) ECC error", line)
            elif dnvl > 0:
                add(node, g, "incident", "NVLink error counter is climbing", line)
            elif rack in hot_racks:
                continue  # part of a rack-wide problem, reported once for the rack
            elif thr == "HW_THERMAL_SLOWDOWN":
                add(node, g, "incident", "GPU is thermally throttling", line)
            elif dsbe >= 50:
                add(node, g, "watch", "Single-bit ECC errors climbing fast", line)
            elif temp >= 82:
                add(node, g, "watch", "GPU temperature trending high", line)
            continue
        m = EVENT_RE.match(line)
        if not m:
            continue
        where, src, text = m.groups()
        if where in real:
            xid = XID_RE.search(text)
            if xid:
                add(where, None, "incident", f"Driver reported Xid {xid.group(2)} on a real GPU", line)
            elif "stopped reporting" in text:
                add(where, None, "incident", "A real GPU stopped reporting to DCGM", line)
            elif "NotReady" in text:
                add(where, None, "incident", "Kubernetes node is NotReady", line)
            elif src == "kubernetes" and re.search(r"restarted|CrashLoopBackOff|is now crashed|is Failed|OOMKill", text):
                add(where, None, "incident", "A GPU workload is crashing", line)
            elif "double-bit" in text or "row remap" in text:
                add(where, None, "incident", "Memory errors on a real GPU", line)
            continue
        if where == "laptop":
            if src == "windows":
                add("laptop", None, "incident", "The display driver on this computer reported errors", line)
            elif src == "selftest" and "FAILED" in text:
                add("laptop", None, "incident", "A GPU self-test failed on this computer", line)
            continue
        if src == "bms":
            rm = re.search(r"feeds rack ([A-H]-\d+)", text)
            if rm:
                add(rm.group(1), None, "incident", "Coolant loop degraded", line)
            continue
        if not where.startswith("dgx-") or rack_of.get(where) in skip:
            continue
        xid = XID_RE.search(text)
        if xid:
            add(where, None, "incident", f"Driver reported Xid {xid.group(2)}", line)
        elif "single-bit ECC" in text:
            add(where, None, "watch", "Single-bit ECC errors climbing fast", line)
        elif src == "trainer" and "[rank" in text and re.search(r"error|timeout|fail", text, re.I):
            add(where, None, "incident", "Training job crashed", line)
        elif src == "dcgm" and ("HW_THERMAL_SLOWDOWN" in text or "and rising" in text) and rack_of.get(where) not in hot_racks:
            add(where, None, "watch", "GPU running hot", line)
    return {"findings": list(findings.values())}


# --------------------------------------------------------------- investigation
def _classify_local(cluster, node, gpu, signal: str):
    g = node.gpus[gpu] if gpu is not None and gpu < len(node.gpus) else max(node.gpus, key=lambda x: x.temp)
    lost = [x for x in node.gpus if x.lost]
    if lost or "answer" in signal:
        return "local_lost", (lost[0].index if lost else g.index)
    if "self-test" in signal:
        failed = next((t for t in cluster.selftests if not t["ok"]), None)
        if failed and failed["test"] == "memory":
            return "local_vram", g.index
        if failed:
            return "local_unstable", g.index
    if "driver" in signal:
        return "local_driver", g.index
    if "memory" in signal or (g.mem_total and g.mem_used / g.mem_total >= 0.95):
        return "local_memory", g.index
    if any("THERMAL" in r for r in g.reasons) or g.temp >= 90 or "cool" in signal or "hot" in signal:
        return "local_thermal", g.index
    return "local_unknown", g.index


def _classify(cluster, target: str, gpu, signal: str = ""):
    """Decide what to look at from what the telemetry and logs show (as an engineer would)."""
    if target in cluster.racks:
        return "cooling", None
    node = cluster.nodes[target]
    if node.local:
        return _classify_local(cluster, node, gpu, signal)
    if node.real:
        return _classify_real(cluster, node, gpu, signal)
    logs = [l["text"] for l in cluster.logs if l["node"] == target][-80:]
    joined = "\n".join(logs)
    lost = [g for g in node.gpus if g.lost]
    if lost:
        kind = "overheat_lost" if "thermal shutdown" in joined else "bus"
        return kind, lost[0].index
    for g in node.gpus:
        h = list(g.history)
        if g.remap_pending or g.dbe or (len(h) >= 3 and h[-1][5] > h[-3][5]):
            return "ecc", g.index
    for g in node.gpus:
        h = list(g.history)
        if len(h) >= 3 and h[-1][7] > h[-3][7]:
            return "nvlink", g.index
    if re.search(r"\): 31,|illegal memory access", joined):
        m = XID_RE.search(joined[joined.find("): 31,") - 40:] if "): 31," in joined else "")
        g = next((x.index for x in node.gpus if m and x.pci == m.group(1)), gpu if gpu is not None else 0)
        return "app_bug", g
    hot = max(node.gpus, key=lambda x: x.temp)
    if hot.temp >= 80 or hot.throttle == "HW_THERMAL_SLOWDOWN":
        return "thermal", hot.index
    for g in node.gpus:
        if g.nvlink_crc:
            return "nvlink", g.index
    return "unknown", gpu if gpu is not None else 0


XID_KIND = {"gpu_lost": "real_lost", "memory": "real_memory", "interconnect": "real_nvlink",
            "firmware": "real_firmware", "software": "real_app"}


def _classify_real(cluster, node, gpu, signal: str):
    import time  # noqa: PLC0415

    from .dcgm import XID  # noqa: PLC0415
    lost = [g for g in node.gpus if g.lost]
    if lost:
        return "real_lost", lost[0].index
    recent = [g for g in node.gpus if g.xid_at and time.time() - g.xid_at < 300]
    if recent:
        g = max(recent, key=lambda x: x.xid_at)
        return XID_KIND.get(XID.get(g.xid, ("", "software"))[1], "real_app"), g.index
    for g in node.gpus:
        h = list(g.history)
        if g.dbe or g.remap_pending or (len(h) >= 3 and h[-1][5] > h[-3][5]):
            return "real_memory", g.index
    for g in node.gpus:
        h = list(g.history)
        if len(h) >= 3 and h[-1][7] > h[-3][7]:
            return "real_nvlink", g.index
    if not node.ready:
        return "real_notready", gpu if gpu is not None else 0
    if any(j.real and node.name in j.nodes and j.status == "crashed" for j in cluster.jobs.values()) or "crash" in signal:
        return "real_crash", gpu if gpu is not None else 0
    hot = max(node.gpus, key=lambda x: x.temp) if node.gpus else None
    if hot is not None and (hot.temp >= 80 or "HW_THERMAL_SLOWDOWN" in hot.reasons
                            or (any("THERMAL" in r for r in hot.reasons) and hot.util >= 30)):
        return "real_thermal", hot.index
    return "real_unknown", gpu if gpu is not None else 0


def _plan_real(kind: str, n: str, g, job: str | None) -> list[tuple]:
    steps = [(f"Read every GPU on real server {n} as DCGM reports it now.", "node_status", {"node": n})]
    if kind == "real_lost":
        steps += [("Find what DCGM and Kubernetes logged when the GPU went quiet.", "search_logs", {"query": "", "node": n})]
        if job:
            steps += [(f"See what this did to {job}.", "job_info", {"job": job})]
        steps += [(f"Check fleet memory: has {n} lost a GPU before?", "incident_history", {"target": n}),
                  ("See where Kubernetes could run the work instead.", "find_spares", {"count": 1, "avoid_rack": n})]
    elif kind == "real_memory":
        steps += [("Find the memory errors DCGM reported.", "search_logs", {"query": "ECC", "node": n}),
                  (f"See how GPU {g}'s error counters moved.", "gpu_history", {"node": n, "gpu": g}),
                  (f"Check fleet memory for earlier trouble on {n}.", "incident_history", {"target": n})]
        if job:
            steps += [("See what runs on the GPU before planning a reset.", "job_info", {"job": job})]
    elif kind in ("real_nvlink", "real_firmware"):
        steps += [("Find the driver messages.", "search_logs", {"query": "Xid" if kind == "real_firmware" else "NVLink", "node": n}),
                  (f"See how GPU {g} has behaved.", "gpu_history", {"node": n, "gpu": g}),
                  (f"Check fleet memory for earlier trouble on {n}.", "incident_history", {"target": n})]
        if job:
            steps += [("See which workload depends on it.", "job_info", {"job": job})]
    elif kind == "real_app":
        steps += [("Find the Xid and what ran at the time.", "search_logs", {"query": "Xid", "node": n})]
        if job:
            steps += [(f"Check {job}'s pods and last error.", "job_info", {"job": job})]
        steps += [(f"See whether GPU {g}'s telemetry looks healthy.", "gpu_history", {"node": n, "gpu": g})]
    elif kind == "real_crash":
        if job:
            steps += [(f"Read {job}'s pods, restarts and last error.", "job_info", {"job": job})]
        steps += [("Find the crash in the Kubernetes log lines.", "search_logs", {"query": "pod", "node": n}),
                  (f"Rule out the hardware: how did GPU {g} behave?", "gpu_history", {"node": n, "gpu": g})]
    elif kind == "real_thermal":
        steps += [(f"See how GPU {g}'s temperature and clocks moved.", "gpu_history", {"node": n, "gpu": g}),
                  ("Compare with the other real servers.", "compare_rack", {"rack": n}),
                  ("Look for slowdown messages.", "search_logs", {"query": "clocks reduced", "node": n})]
    elif kind == "real_notready":
        steps += [("Find what Kubernetes said about the node.", "search_logs", {"query": "Ready", "node": n})]
        if job:
            steps += [(f"See what this does to {job}.", "job_info", {"job": job})]
    else:
        steps += [("Look for recent warnings and errors.", "search_logs", {"query": "", "node": n})]
    return steps


def _plan(kind: str, target: str, g, job: str | None, rack: str) -> list[tuple]:
    n = target
    if kind.startswith("real_"):
        return _plan_real(kind, n, g, job)
    if kind == "local_thermal":
        return [("Read every value the GPU reports right now, including why it is slowing down.", "node_status", {"node": n}),
                (f"See how GPU {g}'s temperature, clocks and load moved over the last half minute.", "gpu_history", {"node": n, "gpu": g}),
                ("Find out which program is loading the GPU.", "gpu_processes", {"node": n}),
                ("Check how often the driver has been cutting clocks for heat.", "search_logs", {"query": "clocks reduced", "node": n}),
                ("Check whether this computer has overheated before.", "incident_history", {"target": n})]
    if kind == "local_memory":
        return [("Read the GPU's memory use and load.", "node_status", {"node": n}),
                ("Find out which program holds the memory.", "gpu_processes", {"node": n}),
                (f"See whether GPU {g}'s load is steady or climbing.", "gpu_history", {"node": n, "gpu": g})]
    if kind == "local_driver":
        return [("Read the display driver's error events.", "search_logs", {"query": "event", "node": n}),
                ("Check whether the GPU looks healthy right now.", "node_status", {"node": n}),
                ("See which programs were using the GPU.", "gpu_processes", {"node": n}),
                ("Check fleet memory for earlier driver trouble here.", "incident_history", {"target": n})]
    if kind == "local_lost":
        return [("Find the last messages before the GPU stopped answering.", "search_logs", {"query": "", "node": n}),
                ("Check whether this has happened before.", "incident_history", {"target": n})]
    if kind in ("local_vram", "local_unstable"):
        return [("Read what the self-test found.", "selftest_results", {"node": n}),
                ("Check whether Windows logged driver or hardware errors around it.", "search_logs", {"query": "event", "node": n}),
                ("Read the GPU's state now.", "node_status", {"node": n}),
                ("Check fleet memory for earlier trouble on this GPU.", "incident_history", {"target": n})]
    if kind == "local_unknown":
        return [("Read the GPU's current state.", "node_status", {"node": n}),
                (f"See how GPU {g} has behaved recently.", "gpu_history", {"node": n, "gpu": g})]
    if kind == "cooling":
        return [("Several servers may be hot at once. Compare every server in the rack with the fleet.",
                 "compare_rack", {"rack": rack}),
                ("If the whole rack runs hot at the same load, check the coolant loop that feeds it.",
                 "facility_sensors", {"rack": rack}),
                ("Look for facility and throttling messages from this rack.", "search_logs", {"query": "", "node": rack}),
                ("Check whether this rack has had cooling trouble before.", "incident_history", {"target": rack}),
                ("See how many spare servers could take this rack's work.", "find_spares", {"count": 8, "avoid_rack": rack})]
    steps = [(f"Start with the current state of every GPU on {n}.", "node_status", {"node": n})]
    if kind == "ecc":
        steps += [(f"Confirm the memory errors in the driver and DCGM logs.", "search_logs", {"query": "ECC", "node": n}),
                  (f"See how fast GPU {g}'s corrected errors are rising.", "gpu_history", {"node": n, "gpu": g}),
                  ("Check the rest of the rack, to rule out a rack-wide cause.", "compare_rack", {"rack": n}),
                  (f"Check fleet memory for earlier trouble on {n}.", "incident_history", {"target": n}),
                  ("Find a healthy spare to move the job's ranks to before anything crashes.",
                   "find_spares", {"count": 1, "avoid_rack": rack})]
    elif kind == "thermal":
        steps += [(f"See how GPU {g}'s temperature and clocks have moved.", "gpu_history", {"node": n, "gpu": g}),
                  ("Compare with the other servers in the rack: one GPU or the whole rack?", "compare_rack", {"rack": n}),
                  ("Look for throttling messages.", "search_logs", {"query": "THERMAL", "node": n}),
                  (f"Check fleet memory for earlier trouble on {n}.", "incident_history", {"target": n}),
                  ("Find a spare in case the job should move.", "find_spares", {"count": 1, "avoid_rack": rack})]
    elif kind in ("bus", "overheat_lost"):
        steps += [("Find the driver errors around the failure.", "search_logs", {"query": "Xid", "node": n})]
        if job:
            steps += [(f"See what the failure did to {job}.", "job_info", {"job": job})]
        steps += [(f"Check fleet memory: has {n} lost a GPU before?", "incident_history", {"target": n}),
                  ("Find a healthy spare for the job.", "find_spares", {"count": 1, "avoid_rack": rack})]
    elif kind == "nvlink":
        steps += [("Find the NVLink errors in the logs.", "search_logs", {"query": "NVLink", "node": n})]
        if job:
            steps += [(f"See how the link errors affect {job}.", "job_info", {"job": job})]
        steps += [(f"Check how fast GPU {g}'s link errors are rising.", "gpu_history", {"node": n, "gpu": g}),
                  ("Find a healthy spare for the job.", "find_spares", {"count": 1, "avoid_rack": rack})]
    elif kind == "app_bug":
        steps += [("Find the crash in the logs.", "search_logs", {"query": "illegal memory", "node": n})]
        if job:
            steps += [(f"Check {job}'s state and last error.", "job_info", {"job": job})]
        steps += [("Compare with the rest of the rack, to rule out hardware.", "compare_rack", {"rack": n})]
    else:
        steps += [("Look for recent warnings and errors.", "search_logs", {"query": "", "node": n})]
    return steps


def investigate_next(state: dict) -> dict:
    with state["cluster"].lock:
        return _investigate_next(state)


def _investigate_next(state: dict) -> dict:
    cluster = state["cluster"]
    target = state["target"]
    steps = state.get("steps", [])
    if "kind" not in state:
        state["kind"], state["gpu_found"] = _classify(cluster, target, state.get("gpu"), state.get("signal", "").lower())
    kind, g = state["kind"], state["gpu_found"]
    if target in cluster.racks:
        rack, job = target, sorted(cluster.jobs_on(target))[0] if cluster.jobs_on(target) else None
    else:
        rack = cluster.nodes[target].rack
        gj = cluster.nodes[target].gpus[g].job if g is not None else None
        job = gj or next((j.id for j in cluster.jobs.values() if target in j.nodes), None)
        if kind == "app_bug":
            job = next((j.id for j in cluster.jobs.values() if target in j.nodes and j.status == "failed"), job)
    plan = _plan(kind, target, g, job, rack)
    if len(steps) < len(plan):
        thought, tool, args = plan[len(steps)]
        return {"thought": thought, "tool": tool, "args": args}
    return {"thought": "I have enough to explain it and plan a safe fix.",
            "final": _final(kind, target, g, job, rack, steps, cluster, state.get("revise"))}


def _from_steps(steps, tool, pattern):
    for s in steps:
        if s["tool"] == tool:
            m = re.search(pattern, s["result"])
            if m:
                return m
    return None


def _evidence_lines(steps, patterns, limit=4):
    out = []
    for s in steps:
        for line in s["result"].splitlines():
            if any(re.search(p, line) for p in patterns) and line.strip() not in out:
                out.append(line.strip())
    return out[:limit]


def _final_local(kind, n, g, steps, cluster) -> dict:
    node = cluster.nodes[n]
    gpu = node.gpus[g if g is not None and g < len(node.gpus) else 0]
    proc_m = _from_steps(steps, "gpu_processes", r"pid (\d+) (\S+)")
    top = proc_m.group(2) if proc_m else ""
    top_pid = int(proc_m.group(1)) if proc_m else None
    hist_m = _from_steps(steps, "incident_history", r"(\d+) incidents on")
    prior = int(hist_m.group(1)) if hist_m else 0
    model = node.model
    base = {"needs_technician": False, "technician_note": "", "scope": "gpu",
            "affected": {"target": n, "gpus": [gpu.index], "jobs": []}}

    def tip(msg, why):
        return {"action": "recommend", "params": {"node": n, "message": msg}, "reason": why}

    def fix(name, why, **params):
        return {"action": name, "params": params, "reason": why}

    cap = int(round(gpu.max_clock * 0.85 / 15) * 15) if gpu.max_clock else 1500

    if kind == "local_thermal":
        reasons = "+".join(r for r in gpu.reasons if "THERMAL" in r) or "high temperature"
        actions = [tip("Put the laptop on a hard, flat surface and raise the back so the vents can breathe.",
                       "Laptop fans pull air from underneath; a bed, lap or desk blocking the vents is the most common cause."),
                   fix("pc_performance_mode", "Turbo spins the fans up, so the GPU stays under its temperature limit.",
                       mode="Turbo"),
                   fix("pc_cap_gpu_clock", f"Capping the top clock at about 85% ({cap:,} MHz) cuts heat a lot for a "
                       "small loss of speed, and stops the up-and-down throttling.", mhz=cap)]
        if top:
            actions.insert(1, tip(f"If {top} doesn't need full speed, cap its frame rate or lower its settings.",
                                  f"{top} is what is loading the GPU; less work means less heat."))
        if prior >= 1:
            actions.append(tip("Clean the fans and vents, or have the thermal paste replaced.",
                               f"This has happened {prior + 1} times now, which points at dust or worn thermal paste."))
        else:
            actions.append(tip("If it keeps happening at normal load, clean the fans and vents.",
                               "Dust is the usual reason a laptop that used to run cool starts throttling."))
        return {**base, "title": "This laptop's GPU is slowing down to stay cool", "category": "thermal",
                "severity": "medium", "confidence": 0.82,
                "summary": (f"Your {model} reached {gpu.temp:.0f}°C at {gpu.util}% load and is cutting its clock to "
                            f"{gpu.clock:,} of {gpu.max_clock:,} MHz to protect itself ({reasons}). Nothing is broken, "
                            f"but you are losing speed" + (f" in {top}" if top else "") + ". Laptops share one set of "
                            "fans between the CPU and the GPU, so blocked vents, dust or a quiet fan mode push them "
                            "over the limit."),
                "root_cause": "Not enough cooling for the load: the GPU hit its temperature limit and the driver is cutting clocks.",
                "evidence": _evidence_lines(steps, [r"slowdown_reasons=\S*THERMAL", r"clocks reduced", r"throttle=\S*THERMAL"]),
                "actions": actions}
    if kind == "local_memory":
        pct = round(100 * gpu.mem_used / gpu.mem_total) if gpu.mem_total else 0
        actions = ([fix("pc_close_program", f"{top} holds the most GPU memory; closing it frees the memory at once. "
                        "Save anything open in it first.", pid=top_pid, name=top)] if top_pid else
                   [tip("Close programs you aren't using that hold GPU memory.",
                        "Freeing memory is the quickest fix and changes nothing permanent.")]) + [
                   tip("For training or image generation, lower the batch size or resolution.",
                       "Memory use grows with batch size; a smaller one fits and runs a little slower.")]
        return {**base, "title": "GPU memory on this laptop is almost full", "category": "memory",
                "severity": "medium", "confidence": 0.78,
                "summary": (f"{gpu.mem_used / 1024:.1f} of {gpu.mem_total / 1024:.1f} GB of GPU memory is in use ({pct}%). "
                            "When it fills, programs either fall back to much slower shared memory or stop with an "
                            "out-of-memory error" + (f". {top} is the program holding it." if top else ".")),
                "root_cause": "The programs on the GPU need more memory than it has free.",
                "evidence": _evidence_lines(steps, [r"memory=\d+/\d+MiB", r"MiB in use", r"pid \d+"]),
                "actions": actions}
    if kind == "local_driver":
        healthy = not any("THERMAL" in r for r in gpu.reasons) and not gpu.lost
        return {**base, "title": "The display driver on this laptop has been crashing", "category": "software",
                "scope": "server", "severity": "medium", "confidence": 0.7,
                "summary": ("Windows logged display-driver errors on this laptop: the NVIDIA driver stopped responding "
                            "and had to recover. The usual causes are an outdated or damaged driver, an overclock or "
                            "undervolt, or one program pushing the GPU past what it handles. "
                            + ("The GPU's readings look normal right now, which points at the driver rather than the hardware."
                               if healthy else "The GPU is also running hot, which can trigger these crashes.")),
                "root_cause": "The NVIDIA display driver timed out and was reset by Windows.",
                "evidence": _evidence_lines(steps, [r"event \d+", r"display-driver"]),
                "actions": [fix("pc_clear_shader_cache", "A corrupt shader cache is a common cause of driver "
                                "crashes after updates; it rebuilds itself."),
                            fix("pc_restart_gpu_driver", "Starts the driver fresh, which clears a half-recovered state."),
                            fix("pc_update_driver", "If crashes continue, a clean install of the latest official "
                                "driver fixes most of them. A restore point is made first."),
                            tip("If it happens again, note which program was open when the screen froze.",
                                "That tells whether it is one program or the GPU itself.")]}
    if kind == "local_vram":
        return {**base, "title": "The memory test found errors in this laptop's GPU memory", "category": "memory",
                "severity": "high", "confidence": 0.8, "needs_technician": True,
                "technician_note": "GPU memory (VRAM) fails a write-and-read-back test. Laptop GPU memory is soldered "
                                   "to the board, so the fix is a board repair or replacement under warranty.",
                "summary": ("Nightingale's memory test wrote patterns to the GPU's memory and some came back changed. "
                            "Memory that doesn't hold its value causes crashes, artefacts on screen, and wrong results "
                            "in AI work. It can't be repaired in software. Before blaming the hardware, make sure the "
                            "GPU isn't overclocked and rerun the test once at stock settings."),
                "root_cause": "Faulty GPU memory (VRAM) cells.",
                "evidence": _evidence_lines(steps, [r"FAILED", r"memory errors"]),
                "actions": [fix("pc_uncap_gpu_clock", "Return clocks to normal before retesting, so an overclock "
                                "or cap isn't mistaken for a fault."),
                            tip("Run the memory test again from My computer.",
                                "Two failures in a row confirm it's the hardware, not a one-off."),
                            tip("If it fails again, contact ASUS support for a warranty repair and mention the "
                                "memory test result.", "GPU memory on laptops is soldered to the board.")]}
    if kind == "local_unstable":
        return {**base, "title": "This laptop's GPU gave wrong results under load", "category": "unknown",
                "severity": "high", "confidence": 0.65,
                "summary": ("During the stress test the GPU either crashed or gave results that didn't match, which "
                            "means it isn't stable at full load. The usual causes are heat, an overclock or "
                            "undervolt, a damaged driver, or failing hardware."),
                "root_cause": "The GPU isn't stable at full load.",
                "evidence": _evidence_lines(steps, [r"FAILED", r"wrong results", r"stopped"]),
                "actions": [fix("pc_restart_gpu_driver", "Start the driver fresh before retesting."),
                            fix("pc_cap_gpu_clock", "A lower top clock makes the GPU cooler and more stable; if the "
                                "stress test passes with the cap, heat or clocks were the cause.", mhz=cap),
                            fix("pc_update_driver", "A clean driver install rules out a damaged driver. A restore "
                                "point is made first."),
                            tip("Run the stress test again after each step.", "That shows which step fixed it.")]}
    if kind == "local_lost":
        return {**base, "title": "This laptop's GPU stopped answering the driver", "category": "gpu_lost",
                "severity": "high", "confidence": 0.75,
                "summary": ("nvidia-smi can no longer reach the GPU, so anything using it has most likely crashed. On "
                            "laptops this usually follows a driver crash, a switch between graphics modes, or a "
                            "power-saving mode powering the GPU down."),
                "root_cause": "The GPU dropped off the driver.",
                "evidence": _evidence_lines(steps, [r"stopped answering", r"No devices", r"event \d+"]),
                "actions": [fix("pc_restart_gpu_driver", "Restarting the GPU device reconnects it without "
                                "restarting the whole laptop."),
                            tip("If the GPU is still missing, save your work and restart the laptop.",
                                "A restart reconnects the GPU in almost every case."),
                            fix("pc_update_driver", "Repeated drop-outs usually come from the driver; a clean "
                                "install of the official one fixes them. A restore point is made first."),
                            tip("Keep the charger plugged in and turn off battery-saver modes while you use the GPU.",
                                "Some power-saving modes switch the GPU off under load.")]}
    return {**base, "title": "Something looked off on this laptop's GPU", "category": "unknown", "severity": "low",
            "confidence": 0.3, "summary": "The readings moved, but they do not point at a clear cause yet.",
            "root_cause": "Not enough evidence.", "evidence": [], "actions": []}


def _final_real(kind, n, g, job, steps, cluster, revise=None) -> dict:
    from .dcgm import xid_text  # noqa: PLC0415
    node = cluster.nodes[n]
    gpu = node.gpus[g if g is not None and g < len(node.gpus) else 0]
    gt = f"GPU {gpu.index}"
    hist_m = _from_steps(steps, "incident_history", r"(\d+) incidents on")
    prior = int(hist_m.group(1)) if hist_m else 0
    j = cluster.jobs.get(job) if job else None
    xid = xid_text(gpu.xid) if gpu.xid else ""
    base = {"needs_technician": False, "technician_note": "", "scope": "gpu",
            "affected": {"target": n, "gpus": [gpu.index], "jobs": [job] if job else []}}

    def act(name, why, **params):
        return {"action": name, "params": params, "reason": why}

    move = ([act("restart_job_from_checkpoint", f"Evict {job}'s pods so Kubernetes starts them on a healthy "
                 "server; the job resumes from its own checkpoint.", job=job)] if j and j.status != "crashed" else [])
    drain = act("drain_node", f"Cordon {n} so Kubernetes places no new work on it.", node=n)
    ev = _evidence_lines(steps, [r"Xid", r"NOT REPORTING", r"stopped reporting", r"dbe=[1-9]", r"remap_pending=yes",
                                 r"NotReady", r"restarted|CrashLoopBackOff|crashed", r"HW_THERMAL|clocks reduced"])
    if kind == "real_lost":
        gone = gpu.lost
        d = {**base, "title": (f"{gt} on {n} stopped reporting to DCGM" if gone else
                               f"{gt} on {n} fell off the bus (Xid {gpu.xid})"),
             "category": "gpu_lost", "severity": "critical", "confidence": 0.86,
             "summary": ((f"{gt} on real server {n} no longer answers DCGM" + (f" after {xid}" if gpu.xid_at else "") + ". ")
                         if gone else
                         (f"DCGM reported {xid} on {gt} of real server {n}. A GPU that has dropped off the bus can't be "
                          "trusted until the server is rebooted or the GPU reseated, even if it answers again. ")) +
                        "Cordon the server, move " + (f"{job}" if j else "its work") +
                        " off it, and have the server rebooted or the GPU reseated.",
             "root_cause": f"{gt} on {n} dropped off the bus or its driver lost it.",
             "evidence": ev, "needs_technician": True,
             "technician_note": f"Reboot {n} through its BMC or cloud console; reseat or replace {gt} if it happens again.",
             "actions": [drain] + move + [act("open_ticket", "A reboot or reseat needs hands on the server.", node=n,
                                              gpu=gpu.index, summary=(f"{gt} stopped reporting on {n}" if gone else
                                                                      f"{gt} on {n}: {xid}")
                                              + "; reboot the server, reseat the GPU if it happens again")]}
    elif kind in ("real_memory", "real_nvlink", "real_firmware"):
        what = {"real_memory": "memory errors", "real_nvlink": "NVLink errors", "real_firmware": "a firmware (GSP) error"}[kind]
        tech = kind == "real_nvlink" or gpu.dbe > 0 or prior >= 1
        acts = [drain] + move + [act("reset_gpu", "With the work moved, a reset clears the error state"
                                     + (" and applies the pending row remap." if gpu.remap_pending else "."), node=n, gpu=gpu.index)]
        acts.append(act("open_ticket", "It keeps happening or the link is physical; replace or inspect the part.",
                        node=n, gpu=gpu.index, summary=f"{gt} on {n}: {what}" + (f" ({xid})" if xid else ""))
                    if tech else act("undrain_node", "Return the server once the reset is clean.", node=n))
        d = {**base, "title": f"{gt} on {n}: {what}", "category": "interconnect" if kind == "real_nvlink" else "memory"
             if kind == "real_memory" else "unknown", "severity": "high", "confidence": 0.8,
             "summary": (f"DCGM shows {what} on {gt} of real server {n}" + (f" ({xid})" if gpu.xid_at else "") +
                         ". Running work there risks a crash or wrong results. Cordon the server, move "
                         + (job if j else "its work") + ", then reset the GPU" + (" and send it for repair." if tech else ".")),
             "root_cause": f"{what.capitalize()} on {gt} of {n}.", "evidence": ev, "needs_technician": tech,
             "technician_note": f"Inspect or replace {gt} on {n}." if tech else "", "actions": acts}
    elif kind == "real_app":
        d = {**base, "title": f"{job or 'A workload'} hit {xid or 'a driver error'} on {n}", "category": "software",
             "scope": "job", "severity": "medium", "confidence": 0.75,
             "summary": (f"{xid or 'The Xid'} on {gt} of {n} usually comes from the program on the GPU, not the "
                         "hardware, and the GPU's telemetry looks healthy. Tell the workload's owners; moving it "
                         "would hit the same error."),
             "root_cause": "An error in the workload's own code or framework.", "evidence": ev,
             "actions": [act("notify_owner", "The error comes from their code.", job=job,
                             message=f"{job} hit {xid or 'a GPU driver error'} on {n} {gt}; telemetry is healthy, "
                                     "so please check the code or framework before restarting.")] if j else []}
    elif kind == "real_crash":
        err = j.last_error if j else ""
        oom = "OOMKilled" in err
        d = {**base, "title": f"{job or 'A GPU workload'} keeps crashing on {n}", "category": "software", "scope": "job",
             "severity": "medium", "confidence": 0.72,
             "summary": (f"{job or 'The workload'}'s pods on {n} are restarting ({err or 'see the pod logs'}). The GPUs "
                         "report no hardware errors, so the cause is in the workload" +
                         (": it ran out of memory." if oom else ".") + " Tell its owners."),
             "root_cause": "The workload runs out of memory." if oom else "The workload's own code or settings fail.",
             "evidence": ev,
             "actions": [act("notify_owner", "The crash comes from the workload itself.", job=job,
                             message=f"{job} keeps crashing on {n}: {err or 'see its pod logs'}. GPUs are healthy.")] if j else []}
    elif kind == "real_thermal":
        lim = gpu.power_limit or 0
        cap = max(gpu.power_min or 0, int(round(lim * 0.85 / 5) * 5)) if lim else 0
        acts = ([act("set_power_limit", "Less power means less heat, so it stops throttling while cooling is checked.",
                     node=n, gpu=gpu.index, watts=cap)] if cap else [])
        acts.append(act("open_ticket", "The cooling of this GPU needs a physical check.", node=n, gpu=gpu.index,
                        summary=f"{gt} on {n} slowing down for heat at {gpu.temp:.0f}C under load; check airflow and fans"))
        d = {**base, "title": f"{gt} on {n} is slowing down for heat", "category": "thermal", "severity": "high",
             "confidence": 0.78,
             "summary": (f"{gt} on real server {n} is at {gpu.temp:.0f}C and cutting its clocks under load "
                         f"({'+'.join(gpu.reasons) or 'thermal'}). Cap its power a little to stop the throttling and "
                         "have its cooling checked."),
             "root_cause": f"Not enough cooling for {gt} on {n} at this load.", "evidence": ev, "needs_technician": True,
             "technician_note": f"Check airflow, fans and heatsink contact for {gt} on {n}.", "actions": acts}
    elif kind == "real_notready":
        d = {**base, "title": f"Kubernetes node {n} is NotReady", "category": "unknown", "scope": "server",
             "severity": "high", "confidence": 0.7,
             "summary": (f"Kubernetes reports {n} NotReady ({node.ready_reason or 'no reason given'}), so its pods can't "
                         "be managed. Cordon it, move " + (job if j else "its work") + ", and have the server checked."),
             "root_cause": f"The kubelet on {n} stopped reporting or the node is unhealthy.", "evidence": ev,
             "needs_technician": True, "technician_note": f"Check {n}: kubelet, network and power.",
             "actions": [drain] + move + [act("open_ticket", "The server needs someone to look at it.", node=n,
                                              gpu=None, summary=f"{n} NotReady: {node.ready_reason or 'unknown'}")]}
    else:
        d = {**base, "title": f"Unclear problem on {n}", "category": "unknown", "scope": "server", "severity": "low",
             "confidence": 0.3, "summary": "Something looked off on this real server, but the data does not point to a cause yet.",
             "root_cause": "Not enough evidence.", "evidence": ev, "actions": []}
    if revise is not None:  # keep the steps the rehearsal accepted, and always the ticket
        ok = [s["ok"] for s in revise.get("steps", [])]
        d["actions"] = [a for i, a in enumerate(d["actions"])
                        if (i < len(ok) and ok[i]) or a["action"] in ("open_ticket", "notify_owner")]
        if not any(a["action"] == "open_ticket" for a in d["actions"]):
            d["actions"].append(act("open_ticket", "The rehearsal showed Nightingale can't act here; hand it over.",
                                    node=n, gpu=gpu.index, summary=f"{d['title']}: needs a person"))
        d["summary"] += " (Revised after the rehearsal: only the steps Kubernetes accepted are left.)"
    return d


def _final(kind, target, g, job, rack, steps, cluster, revise=None) -> dict:
    n = target
    if kind.startswith("local_"):
        return _final_local(kind, n, g, steps, cluster)
    if kind.startswith("real_"):
        return _final_real(kind, n, g, job, steps, cluster, revise)
    spare_m = _from_steps(steps, "find_spares", r"first \d+: (dgx-\d+)")
    spare = spare_m.group(1) if spare_m else "a spare"
    hist_m = _from_steps(steps, "incident_history", r"(\d+) incidents on")
    prior = int(hist_m.group(1)) if hist_m else 0
    servers = len(cluster.jobs[job].nodes) if job in cluster.jobs else 0
    gt = f"GPU {g}" if g is not None else "a GPU"
    base = {"confidence": 0.85, "needs_technician": False, "technician_note": "",
            "affected": {"target": n, "gpus": [g] if g is not None else [], "jobs": [job] if job else []}}

    if kind == "cooling":
        cdu = cluster.racks[rack].cdu
        flow_m = _from_steps(steps, "facility_sensors", r"flow (\d+)%")
        jobs = sorted(cluster.jobs_on(rack))
        servers = len(cluster.racks[rack].nodes)
        common = {**base, "category": "cooling", "scope": "rack", "severity": "high", "confidence": 0.84,
                  "root_cause": f"Reduced coolant flow in {cdu}, which cools only rack {rack}.",
                  "evidence": _evidence_lines(steps, [r"flow \d+%", r"HW_THERMAL_SLOWDOWN", r"avg (8\d|9\d)C"]),
                  "needs_technician": True,
                  "technician_note": f"Inspect {cdu}: pump, valves and filters.",
                  "affected": {"target": rack, "gpus": [], "jobs": jobs}}
        ticket = {"action": "open_facility_ticket", "params": {"rack": rack, "summary":
                  f"{cdu} coolant flow low, rack {rack} running hot. Check pump and valves."},
                  "reason": "The coolant loop needs facilities to repair it."}
        flow = flow_m.group(1) if flow_m else "well below normal"
        if revise is None:
            moves = [{"action": "restart_job_from_checkpoint", "params": {"job": j},
                      "reason": f"Move {j}'s ranks off rack {rack} onto spares elsewhere."} for j in jobs]
            return {**common, "title": f"Coolant loop failing on rack {rack}",
                    "summary": (f"Servers across rack {rack} run 8-20C hotter than identical servers elsewhere at the "
                                f"same load, and the rack's coolant flow is down to {flow}%. The cause is the shared "
                                f"cooling, not the GPUs. Moving the work to spare servers keeps every job at full "
                                f"speed while facilities fixes the loop."),
                    "actions": [{"action": "drain_rack", "params": {"rack": rack},
                                 "reason": "Stop new work landing on the overheating rack."}] + moves + [ticket]}
        return {**common, "title": f"Coolant loop failing on rack {rack}",
                "summary": (f"Servers across rack {rack} run hot because its coolant flow is down to {flow}%. "
                            f"There are not enough spare servers to move all {servers} of them, so cap the rack's "
                            f"power instead: jobs there slow a little but stay out of thermal shutdown until "
                            f"facilities fixes the loop."),
                "technician_note": f"Inspect {cdu}: pump, valves and filters. Remove the power cap after the fix.",
                "actions": [{"action": "set_rack_power_limit", "params": {"rack": rack, "watts": 550},
                             "reason": "Less power means less heat while cooling is degraded."}, ticket]}

    if kind == "ecc":
        crashed = job in cluster.jobs and cluster.jobs[job].status == "crashed"
        actions = [{"action": "drain_node", "params": {"node": n}, "reason": "Keep new work off the failing GPU."}]
        if job:
            actions.append({"action": "restart_job_from_checkpoint", "params": {"job": job},
                            "reason": f"Move {job}'s ranks from {n} to {spare} " +
                                      ("and resume from the last checkpoint." if crashed else "before an uncorrectable error crashes it.")})
        actions.append({"action": "reset_gpu", "params": {"node": n, "gpu": g},
                        "reason": "With the job gone, a reset retires the weak memory rows."})
        if prior >= 2:
            actions.append({"action": "open_ticket", "params": {"node": n, "gpu": g, "summary":
                            f"{gt} has had {prior + 1} memory incidents this month; replace it."},
                            "reason": "It keeps coming back, so replace it instead of resetting again."})
        else:
            actions.append({"action": "undrain_node", "params": {"node": n},
                            "reason": "Return the server to the spare pool once the reset is clean."})
        return {**base, "title": (f"Uncorrectable memory error on {n} {gt}" if crashed else
                                  f"Memory errors climbing on {n} {gt}"),
                "category": "memory", "scope": "gpu", "severity": "high", "confidence": 0.88,
                "summary": (f"{gt} on {n} is logging hundreds of corrected memory errors a minute" +
                            (", and one uncorrectable error has already crashed " + job + ". " if crashed else
                             ". That pattern usually ends in an uncorrectable error that crashes the run. ") +
                            f"The rest of rack {rack} is clean, so it is this GPU. Move the job to {spare}, then reset the GPU."),
                "root_cause": f"Degrading HBM memory on {gt} of {n}.",
                "evidence": _evidence_lines(steps, [r"\): 92,", r"\): 48,", r"single-bit ECC", r"sbe=\d{3,}"]),
                "actions": actions, "needs_technician": prior >= 2,
                "technician_note": f"Replace {gt} on {n}." if prior >= 2 else ""}

    if kind == "thermal":
        servers = len(cluster.jobs[job].nodes) if job in cluster.jobs else 0
        actions = [{"action": "set_power_limit", "params": {"node": n, "gpu": g, "watts": 500},
                    "reason": "Immediate safety: less power means less heat, so it stays below shutdown."}]
        if job:
            actions += [{"action": "drain_node", "params": {"node": n}, "reason": "Keep new work off this server."},
                        {"action": "restart_job_from_checkpoint", "params": {"job": job},
                         "reason": f"A capped GPU would hold all {servers * 8:,} GPUs of {job} to its speed; "
                                   f"moving to {spare} restores full speed for a few minutes of redone steps."}]
        actions.append({"action": "open_ticket", "params": {"node": n, "gpu": g, "summary":
                        f"{gt} overheating under normal load: check fans, heatsink contact and coolant flow"},
                        "reason": "The GPU's cooling needs a physical check."})
        return {**base, "title": f"{gt} on {n} is overheating", "category": "thermal", "scope": "gpu",
                "severity": "high", "confidence": 0.82,
                "summary": (f"{gt} on {n} keeps heating up while its neighbours and the rest of rack {rack} stay normal, "
                            "so its own cooling is failing. It throttles, and because every rank waits for the slowest "
                            f"one, it slows all of {job or 'its job'}. Cap it now, move the job to {spare}, and send a "
                            "technician."),
                "root_cause": f"Degraded cooling on {gt} of {n} (one GPU, not the rack).",
                "evidence": _evidence_lines(steps, [r"HW_THERMAL_SLOWDOWN", r"and rising", r"temp=(8\d|9\d)C"]),
                "actions": actions, "needs_technician": True,
                "technician_note": f"Inspect the fans, heatsink and cold plate of {gt} on {n}."}

    if kind in ("bus", "overheat_lost"):
        overheated = kind == "overheat_lost"
        tech = overheated or prior >= 1
        actions = [{"action": "drain_node", "params": {"node": n}, "reason": "Keep new work off the broken server."}]
        if job:
            actions.append({"action": "restart_job_from_checkpoint", "params": {"job": job},
                            "reason": f"Get {job} running again with {spare} in place of {n}."})
        actions.append({"action": "reboot_node", "params": {"node": n},
                        "reason": "A reboot usually brings a GPU back onto the PCIe bus."})
        if tech:
            actions.append({"action": "open_ticket", "params": {"node": n, "gpu": g, "summary":
                            (f"{gt} shut down from overheating; inspect its cooling" if overheated else
                             f"{gt} fell off the bus again ({prior + 1} times); reseat or replace the GPU and riser")},
                            "reason": "This needs hands-on work."})
        return {**base, "title": f"{gt} on {n} " + ("shut down after overheating" if overheated else "fell off the bus"),
                "category": "gpu_lost", "scope": "gpu", "severity": "critical", "confidence": 0.9,
                "summary": (f"{gt} on {n} disappeared from the PCIe bus (Xid 79)" +
                            (f", which crashed {job} on all {servers * 8:,} GPUs of its {servers} servers. " if job else ". ") +
                            f"Move the job to {spare}, then reboot {n} to recover the GPU."),
                "root_cause": f"{gt} on {n} dropped off the PCIe bus" + (" after hitting its thermal shutdown limit." if overheated else "."),
                "evidence": _evidence_lines(steps, [r"\): 79,", r"GPU_IS_LOST", r"thermal shutdown", r"last error"]),
                "actions": actions, "needs_technician": tech,
                "technician_note": ("Check the cooling of this GPU." if overheated else
                                    "Repeated Xid 79: reseat the GPU and check the riser and power." if tech else "")}

    if kind == "nvlink":
        return {**base, "title": f"NVLink failing on {n} {gt}", "category": "interconnect", "scope": "gpu",
                "severity": "high", "confidence": 0.8,
                "summary": (f"NVLink errors on {gt} of {n} cut all-reduce bandwidth for its ranks, and every rank of "
                            f"{job} waits for them. If it keeps going, NCCL times out and the run crashes. Move the job "
                            f"to {spare}, try a GPU reset, and send a technician if the link stays down."),
                "root_cause": f"A degraded NVLink connection on {gt} (Xid 74, rising CRC errors).",
                "evidence": _evidence_lines(steps, [r"\): 74,", r"bus bandwidth", r"nvlink_crc=[1-9]"]),
                "actions": [
                    {"action": "drain_node", "params": {"node": n}, "reason": "Stop routing training traffic over the bad link."},
                    {"action": "restart_job_from_checkpoint", "params": {"job": job},
                     "reason": f"Resume training with {spare} in place of {n}."},
                    {"action": "reset_gpu", "params": {"node": n, "gpu": g},
                     "reason": "A reset retrains the link and clears transient faults; cheap to try."},
                    {"action": "open_ticket", "params": {"node": n, "gpu": g, "summary":
                        f"NVLink errors on {gt}: inspect NVLink cabling and NVSwitch"},
                     "reason": "If the link does not recover, it is a physical fault."}],
                "needs_technician": True, "technician_note": "Inspect the NVLink cabling and NVSwitch for this GPU."}

    if kind == "app_bug":
        return {**base, "title": f"Bug in user code crashed {job}", "category": "software", "scope": "job",
                "severity": "medium", "confidence": 0.9,
                "summary": ("The job read memory it does not own (Xid 31, illegal memory access in train.py line 214). "
                            f"Temperatures, memory errors and links on {n} and the rest of rack {rack} are clean, so the "
                            "hardware is fine. Restarting would crash again; the owners need to fix the code."),
                "root_cause": "An out-of-range index in the job's own code (mine_hard_negatives, train.py line 214).",
                "evidence": _evidence_lines(steps, [r"\): 31,", r"illegal memory access", r"train\.py"]),
                "actions": [{"action": "notify_owner", "params": {"job": job, "message":
                             f"{job} crashed with an illegal memory access in train.py line 214 (mine_hard_negatives). "
                             "The GPUs are healthy; please check the indexing there before restarting."},
                             "reason": "The crash comes from the job's code, so its owners need to fix it."}]}

    return {**base, "title": f"Unclear problem on {n}", "category": "unknown", "scope": "server", "severity": "low",
            "confidence": 0.3, "summary": "Something looked off, but the data does not point to a clear cause yet.",
            "root_cause": "Not enough evidence.", "evidence": [], "actions": []}


# ------------------------------------------------------------ pasted logs
def _evidence(ctx: str, patterns: list[str], limit: int = 4) -> list[str]:
    out = []
    for line in ctx.splitlines():
        if any(re.search(p, line) for p in patterns) and line.strip() not in out:
            out.append(line.strip())
    return out[-limit:]


def diagnose(ctx: str, external: bool = False) -> dict:
    full = ctx
    ctx = ctx.split("Related warnings from other nodes")[0]  # judge the node on its own evidence
    nm = re.search(r"node=(dgx-\d+)", ctx) or re.search(r"\b(dgx-\d+|[a-z]+-node-\d+|node\d+)\b", ctx)
    node = nm.group(1) if nm else "unknown"
    gm = re.search(r"node=\S+ gpu=(\d+)", ctx)
    gpu = int(gm.group(1)) if gm else None
    if gpu is None:  # map the Xid PCI address back to a GPU index
        xid = XID_RE.search(ctx)
        pci_map = dict((p, int(i)) for i, p in re.findall(r"g(\d+)=([0-9a-f:]+)", ctx))
        if xid and xid.group(1) in pci_map:
            gpu = pci_map[xid.group(1)]
        else:
            gm = re.search(r"GPU (\d+)", ctx)
            gpu = int(gm.group(1)) if gm else None
    jm = re.search(r"^Job (\S+): owner=", full, re.M)
    job = jm.group(1) if jm else None
    pm = re.search(r"Previous incidents on this node \((\d+)\)", ctx)
    prior = int(pm.group(1)) if pm else 0
    g_txt = f"GPU {gpu}" if gpu is not None else "a GPU"

    if re.search(r"\): 79,|state=LOST|fallen off the bus|DCGM_ST_GPU_IS_LOST", ctx):
        overheated = "thermal shutdown" in ctx
        actions = [{"action": "drain_node", "params": {"node": node},
                    "reason": "Keep new work off the node while it is broken."}]
        if job:
            actions.append({"action": "restart_job_from_checkpoint", "params": {"job": job},
                            "reason": "Get training running again on a healthy spare node."})
        actions.append({"action": "reboot_node", "params": {"node": node},
                        "reason": "A reboot usually brings a GPU back onto the PCIe bus."})
        tech = overheated or prior > 0
        if tech:
            actions.append({"action": "open_ticket", "params": {
                "node": node, "gpu": gpu,
                "summary": (f"{g_txt} shut down from overheating; inspect cooling" if overheated else
                            f"{g_txt} fell off the bus again; reseat or replace the GPU/riser")},
                "reason": "This needs hands-on inspection."})
        return {
            "title": f"{g_txt} on {node} " + ("shut down after overheating" if overheated else "fell off the bus"),
            "category": "gpu_lost", "severity": "critical", "confidence": 0.86,
            "affected": {"target": node, "gpus": [gpu] if gpu is not None else [], "jobs": [job] if job else []},
            "summary": (f"{g_txt} on {node} disappeared from the PCIe bus (Xid 79), which crashed the "
                        "training job on every rank. Move the job to a spare node, then reboot this one "
                        "to recover the GPU."),
            "root_cause": f"{g_txt} on {node} dropped off the PCIe bus" +
                          (" after hitting its thermal shutdown limit." if overheated else "."),
            "evidence": _evidence(ctx, [r"\): 79,", r"GPU_IS_LOST", r"device handle", r"worker group failed"]),
            "actions": actions, "needs_technician": tech,
            "technician_note": ("Check the cooling loop for this GPU." if overheated else
                                "Repeated Xid 79: reseat the GPU and check the riser/power." if tech else ""),
        }

    if not re.search(r"\): 48,|dbe=[1-9]|Row Remapper|uncorrectable ECC", ctx) and \
            re.search(r"\): 92,|single-bit ECC", ctx):
        actions = [{"action": "drain_node", "params": {"node": node},
                    "reason": "Keep new work off the GPU before it fails."}]
        if job:
            actions.append({"action": "restart_job_from_checkpoint", "params": {"job": job},
                            "reason": "Move training to a healthy spare now, before an uncorrectable "
                                      "error crashes it."})
        actions += [
            {"action": "reset_gpu", "params": {"node": node, "gpu": gpu},
             "reason": "Once the job is off, a reset retires the weak memory rows."},
            {"action": "undrain_node", "params": {"node": node},
             "reason": "Return the node as a spare after the reset."},
        ]
        return {
            "title": f"Memory errors climbing on {node} {g_txt}, crash likely soon",
            "category": "memory", "severity": "high", "confidence": 0.78,
            "affected": {"target": node, "gpus": [gpu] if gpu is not None else [], "jobs": [job] if job else []},
            "summary": (f"{g_txt} on {node} is logging hundreds of corrected memory errors per minute "
                        "(Xid 92). That pattern usually ends in an uncorrectable error that kills the job. "
                        "Moving the job to a spare now avoids the crash."),
            "root_cause": f"Degrading HBM memory on {g_txt}.",
            "evidence": _evidence(ctx, [r"\): 92,", r"single-bit ECC"]),
            "actions": actions, "needs_technician": False, "technician_note": "",
        }

    if re.search(r"\): 48,|dbe=[1-9]|Row Remapper|uncorrectable ECC", ctx):
        actions = [{"action": "drain_node", "params": {"node": node},
                    "reason": "Stop new work from landing on the faulty GPU."}]
        if job:
            actions.append({"action": "restart_job_from_checkpoint", "params": {"job": job},
                            "reason": "Resume training from the last checkpoint on a healthy spare."})
        actions += [
            {"action": "reset_gpu", "params": {"node": node, "gpu": gpu},
             "reason": "Activates the pending row remap so the bad memory row is retired."},
            {"action": "undrain_node", "params": {"node": node},
             "reason": "Return the node to the spare pool once the reset succeeds."},
        ]
        return {
            "title": f"Uncorrectable memory error on {node} {g_txt}",
            "category": "memory", "severity": "high", "confidence": 0.9,
            "affected": {"target": node, "gpus": [gpu] if gpu is not None else [], "jobs": [job] if job else []},
            "summary": (f"{g_txt} on {node} hit a double-bit ECC memory error (Xid 48) after a burst of "
                        "single-bit errors, which crashed training. The GPU has marked the bad memory row "
                        "for remapping; a GPU reset activates the fix."),
            "root_cause": f"A failing HBM memory row on {g_txt}, now pending remap.",
            "evidence": _evidence(ctx, [r"\): 48,", r"\): 63,", r"\): 92,", r"uncorrectable ECC"]),
            "actions": actions, "needs_technician": False, "technician_note": "",
        }

    if re.search(r"\): 74,|NVLink|nvlink_crc=[1-9]", ctx):
        actions = [{"action": "drain_node", "params": {"node": node},
                    "reason": "Stop routing training traffic over the bad link."}]
        if job:
            actions.append({"action": "restart_job_from_checkpoint", "params": {"job": job},
                            "reason": "Resume training on a healthy spare node."})
        actions += [
            {"action": "reset_gpu", "params": {"node": node, "gpu": gpu},
             "reason": "A reset retrains the link and clears transient faults; cheap to try first."},
            {"action": "open_ticket", "params": {"node": node, "gpu": gpu,
                                                  "summary": f"NVLink errors on {g_txt}: inspect NVLink cabling and NVSwitch"},
             "reason": "If the link doesn't recover, it is a physical fault."},
        ]
        return {
            "title": f"NVLink failure on {node} {g_txt}",
            "category": "interconnect", "severity": "high", "confidence": 0.8,
            "affected": {"target": node, "gpus": [gpu] if gpu is not None else [], "jobs": [job] if job else []},
            "summary": (f"NVLink errors on {g_txt} cut all-reduce bandwidth on {node}'s ranks and then "
                        "stalled training until NCCL timed out. Move the job off, try a GPU reset, and "
                        "send a technician if the link stays down."),
            "root_cause": f"A degraded NVLink connection on {g_txt} (Xid 74, rising CRC errors).",
            "evidence": _evidence(ctx, [r"\): 74,", r"bus bandwidth", r"watchdog timeout"]),
            "actions": actions, "needs_technician": True,
            "technician_note": "Inspect NVLink cabling/NVSwitch for this GPU if a reset doesn't clear the errors.",
        }

    if re.search(r"\): (31|13|43),|illegal memory access|MMU Fault", ctx):
        actions = []
        if job:
            actions.append({"action": "notify_owner", "params": {
                "job": job,
                "message": (f"{job} crashed with an illegal memory access in train.py line 214 "
                            "(mine_hard_negatives). The GPU is healthy; please check the indexing there "
                            "before restarting.")},
                "reason": "The crash comes from the job's code, so the owners need to fix it."})
        return {
            "title": f"Bug in user code crashed {job or 'a job'}",
            "category": "software", "severity": "medium", "confidence": 0.88,
            "affected": {"target": node, "gpus": [gpu] if gpu is not None else [], "jobs": [job] if job else []},
            "summary": ("The job read memory it doesn't own (Xid 31, illegal memory access in train.py "
                        "line 214). Temperatures, ECC and NVLink are all clean, so the hardware is fine. "
                        "Restarting would just crash again; the code needs a fix."),
            "root_cause": "An out-of-range index in the job's own code (mine_hard_negatives).",
            "evidence": _evidence(ctx, [r"\): 31,", r"illegal memory access", r"train\.py"]),
            "actions": actions, "needs_technician": False, "technician_note": "",
        }

    if re.search(r"HW_THERMAL_SLOWDOWN|temperature \d+C and rising|slowest rank", ctx):
        return {
            "title": f"{g_txt} on {node} is overheating",
            "category": "thermal", "severity": "high", "confidence": 0.82,
            "affected": {"target": node, "gpus": [gpu] if gpu is not None else [], "jobs": [job] if job else []},
            "summary": (f"{g_txt} on {node} keeps heating up while its neighbours stay normal, so its "
                        "cooling is failing. It is throttling, which slows the whole training job, and it "
                        "will shut down if it keeps climbing. Capping its power buys time; the cooling "
                        "needs a technician."),
            "root_cause": f"Degraded cooling on {g_txt} (not workload: other GPUs at the same load are normal).",
            "evidence": _evidence(ctx, [r"HW_THERMAL_SLOWDOWN", r"and rising", r"slowest rank"]),
            "actions": [
                {"action": "set_power_limit", "params": {"node": node, "gpu": gpu, "watts": 500},
                 "reason": "Less power means less heat; keeps the GPU below its slowdown limit."},
                {"action": "open_ticket", "params": {"node": node, "gpu": gpu,
                                                      "summary": f"{g_txt} overheating under normal load: check fans, heatsink and coolant flow"},
                 "reason": "The cooling hardware needs a physical check."},
            ],
            "needs_technician": True,
            "technician_note": "Inspect fans, heatsink contact and coolant flow for this GPU.",
        }

    return {
        "title": f"Unclear problem on {node}", "category": "unknown", "severity": "low",
        "confidence": 0.3, "affected": {"target": node, "gpus": [], "jobs": [job] if job else []},
        "summary": "Something looked off, but the logs don't point to a clear cause yet. Keep watching.",
        "root_cause": "Not enough evidence.", "evidence": [], "actions": [],
        "needs_technician": False, "technician_note": "",
    }


def extract(raw: str) -> dict:
    keep = [l for l in raw.splitlines()
            if re.search(r"xid|error|fail|warn|ecc|nvlink|thrott|timeout|lost|temperature|nccl|fallen",
                         l, re.I)]
    return {"relevant_lines": keep[:60], "note": f"Kept {min(len(keep), 60)} lines that mention faults."}


# ------------------------------------------------------------- ask your data center
LAPTOP_WORDS = re.compile(r"\b(laptop|my gpu|my pc|this computer|this pc|my computer|rtx|geforce|local gpu)\b", re.I)
SERVER_RE = re.compile(r"\bdgx[-_ ]?0*(\d{1,4})\b", re.I)
RACK_Q_RE = re.compile(r"\b(?:rack\s+)?([A-Ha-h])-?(\d{1,2})\b")


def ask_next(state: dict) -> dict:
    """Demo-mode answers for "Ask your data center": pick tools by keyword, then answer from the data."""
    cluster = state["cluster"]
    with cluster.lock:
        return _ask_next(state)


def _ask_plan(q: str, c, state) -> tuple[str, list[tuple]]:
    ql = q.lower()
    server = SERVER_RE.search(q)
    server = c.canonical_node(f"dgx-{int(server.group(1)):04d}") if server else None
    rack = None
    for m in RACK_Q_RE.finditer(q):
        rack = c.canonical_rack(f"{m.group(1).upper()}-{int(m.group(2)):02d}")
        if rack:
            break
    job = next((j for j in c.jobs if j in ql), None)
    if LAPTOP_WORDS.search(q):
        if c.local is not None and "laptop" in c.nodes:
            return "laptop", [("Read this computer's GPU right now.", "node_status", {"node": "laptop"}),
                              ("See which programs are using it.", "gpu_processes", {"node": "laptop"})]
        return "laptop_off", [("Check whether this computer's GPU is connected.", "fleet_overview", {})]
    if server:
        return "server", [(f"Read every GPU on {server}.", "node_status", {"node": server}),
                          (f"Check fleet memory for {server}.", "incident_history", {"target": server})]
    if rack:
        return "rack", [(f"Compare the servers in rack {rack}.", "compare_rack", {"rack": rack}),
                        (f"Read the coolant loop feeding {rack}.", "facility_sensors", {"rack": rack})]
    if job:
        return "job", [(f"Look up {job}.", "job_info", {"job": job})]
    if re.search(r"\b(hot|hottest|temp|temperature|warm|cool|heat)", ql):
        return "hot", [("Get the hottest racks.", "fleet_overview", {}),
                       ("Find the hottest single GPUs.", "top_gpus", {"metric": "temp", "count": 3})]
    if re.search(r"\b(power|watt|kw|budget|energy|electric)", ql):
        return "power", [("Read power against the budget.", "fleet_overview", {}),
                         ("Find the GPUs drawing the most.", "top_gpus", {"metric": "power", "count": 3})]
    if re.search(r"\b(ecc|memory error|memory errors|errors?)\b", ql):
        return "errors", [("Find GPUs with corrected memory errors.", "top_gpus", {"metric": "sbe", "count": 3})]
    if "nvlink" in ql:
        return "nvlink", [("Find GPUs with NVLink errors.", "top_gpus", {"metric": "nvlink_crc", "count": 3})]
    if re.search(r"\b(spare|spares|free|idle|capacity)\b", ql):
        return "spares", [("List idle healthy servers.", "find_spares", {"count": 5})]
    return "health", [("Read the whole data center.", "fleet_overview", {}),
                      ("List Nightingale's incidents.", "incidents", {})]


def _ask_next(state: dict) -> dict:
    c = state["cluster"]
    kind, plan = _ask_plan(state.get("question", ""), c, state)
    steps = state.get("steps", [])
    if len(steps) < len(plan):
        thought, tool, args = plan[len(steps)]
        return {"thought": thought, "tool": tool, "args": args}
    answer = _ask_answer(kind, plan, c, state)
    refs = sorted(set(TARGET_RE.findall(answer)) | {j for j in c.jobs if j in answer})
    return {"thought": "I have what I need.", "answer": answer, "refs": refs}


def _ask_answer(kind: str, plan, c, state) -> str:
    g = c.gauges()
    if kind == "laptop":
        n = c.nodes["laptop"]
        gp = n.gpus[0]
        if gp.lost:
            return "Your GPU has stopped answering the driver: nvidia-smi can't see it right now. Save your work; a restart usually brings it back."
        reasons = [r for r in gp.reasons if r not in ("GPU_IDLE",)]
        heat = [r for r in reasons if "THERMAL" in r]
        procs = c.local.snapshot()["processes"] if c.local is not None else []
        busiest = max(procs, key=lambda p: p.get("mem_mib") or 0)["name"] if procs else None
        txt = (f"Your {n.model.replace('NVIDIA ', '')} is at {gp.temp:.0f}°C and {gp.util}% load, drawing "
               f"{gp.power:.0f} W of {gp.power_limit} W, with the clock at {gp.clock:,} of {gp.max_clock:,} MHz.")
        if heat:
            txt += " It is cutting its clock to stay cool, so you are losing speed: give it more airflow or a faster fan mode."
        elif gp.temp >= 90:
            txt += " That is very hot for a laptop GPU; check the vents and fans."
        else:
            txt += " It is not slowing itself down for heat, so it looks healthy."
        if busiest:
            txt += f" The busiest program on it is {busiest}."
        return txt
    if kind == "laptop_off":
        return "This computer's GPU isn't connected yet. Open Data sources and click Connect next to This laptop's GPU, then ask again."
    if kind == "server":
        name = plan[0][2]["node"]
        n = c.nodes[name]
        live = [x for x in n.gpus if not x.lost]
        temps = [x.temp for x in live]
        jobs = ", ".join(sorted(c.jobs_on(name))) or "no job"
        issues = c.observed_issues(name)
        hist = len([h for h in c.history if h["target"] == name])
        txt = (f"{name} in rack {n.rack} is {n.status} and runs {jobs}. "
               + (f"Its working GPUs run {min(temps):.0f}-{max(temps):.0f}°C. " if temps else ""))
        txt += ("Right now: " + "; ".join(issues) + "." if issues else "No GPU on it shows memory, NVLink or heat problems.")
        if hist:
            txt += f" Fleet memory has {hist} earlier incident{'s' if hist != 1 else ''} on it."
        return txt
    if kind == "rack":
        rid = plan[0][2]["rack"]
        r = c.racks[rid]
        temps = dict((x[0], x[1]) for x in c._rack_temps())
        busy_avg = temps.get(rid)
        fleet = [t for k, t in temps.items() if k != rid]
        txt = f"Rack {rid}: "
        txt += (f"its busy GPUs average {busy_avg:.0f}°C against {sum(fleet) / len(fleet):.0f}°C for the other racks. "
                if busy_avg is not None and fleet else "none of its GPUs are busy right now. ")
        txt += (f"Its coolant loop {r.cdu} runs at {r.coolant_flow:.0f}% flow"
                + (" — that is low, so expect the whole rack to run hot." if r.coolant_flow < 90 else ", which is normal."))
        return txt
    if kind == "job":
        j = c.jobs[plan[0][2]["job"]]
        txt = (f"{j.id} ({j.owner}) is {j.status} on {len(j.nodes)} servers ({len(j.nodes) * 8:,} GPUs) at "
               f"{round(j.rate * 100)}% of its normal speed, step {int(j.step):,} with the last checkpoint at {j.checkpoint:,}.")
        if j.last_error:
            txt += f" Its last error: {j.last_error}."
        return txt
    if kind == "hot":
        racks = c._rack_temps()
        hot = g["hottest"]
        if not racks:
            return f"Nothing is under load right now. The warmest GPU is {hot['node']} GPU {hot['gpu']} at {hot['temp']}°C."
        top, others = racks[0], [t for _, t, _ in racks[1:]]
        avg = sum(others) / len(others) if others else top[1]
        if top[1] - avg >= 3:
            return (f"Rack {top[0]} runs hottest: its busy GPUs average {top[1]:.0f}°C, against {avg:.0f}°C for the other "
                    f"racks. The hottest single GPU is {hot['node']} GPU {hot['gpu']} at {hot['temp']}°C.")
        return (f"No rack stands out: busy GPUs average about {avg:.0f}°C everywhere (warmest is rack {top[0]} at "
                f"{top[1]:.0f}°C). The hottest single GPU is {hot['node']} GPU {hot['gpu']} at {hot['temp']}°C, well "
                f"below the 87°C slowdown point.")
    if kind == "power":
        pct = round(100 * g["power_kw"] / max(1, g["budget_kw"]))
        return (f"The GPUs draw {g['power_kw']:,} kW of a {g['budget_kw']:,} kW budget ({pct}%). "
                + ("That leaves comfortable headroom." if pct < 85 else "That is close to the limit; new jobs may need power caps."))
    if kind in ("errors", "nvlink"):
        attr = "sbe" if kind == "errors" else "nvlink_crc"
        rows = sorted(((getattr(x, attr), n.name, x.index) for n in c.nodes.values() for x in n.gpus if not x.lost),
                      reverse=True)
        bad = [r for r in rows if r[0] > 0][:3]
        what = "corrected memory errors" if kind == "errors" else "NVLink errors"
        if not bad:
            return f"No GPU is logging {what} right now."
        return f"{len(bad)} GPU{'s' if len(bad) != 1 else ''} logging {what}: " + ", ".join(
            f"{n} GPU {i} ({v:,})" for v, n, i in bad) + ". Rising counts usually come before a crash, so they're worth an incident."
    if kind == "spares":
        sp = c.spare_nodes()
        return (f"{len(sp)} healthy idle servers can take work right now"
                + (f", for example {', '.join(s.name for s in sp[:4])}." if sp else "."))
    # health
    agent = state.get("agent")
    open_inc = [i for i in agent.incidents if i["status"] in ("investigating", "awaiting_approval", "needs_attention",
                                                               "verifying")] if agent else []
    jobs_bad = [j for j in c.jobs.values() if j.status != "running"]
    txt = f"{g['healthy']:,} of {g['total']:,} GPUs are healthy, {g['watch']} under watch and {g['lost']} down. "
    flagged = c.watch_list(3)
    if flagged:
        txt += "Worth a look: " + "; ".join(flagged) + ". "
    if open_inc:
        txt += (f"{len(open_inc)} incident{'s are' if len(open_inc) != 1 else ' is'} open: "
                + "; ".join(f"{i['title'] or i['trigger']['signal']} ({i['target']})" for i in open_inc[:3]) + ". ")
    else:
        txt += "No incident is open. "
    if jobs_bad:
        txt += "Stopped jobs: " + ", ".join(f"{j.id} ({j.status})" for j in jobs_bad) + "."
    else:
        txt += "Every training job is running."
    return txt.strip()
