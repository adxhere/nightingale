"""Real GPU servers inside the Cluster: hall R, next to the simulated halls and this computer.

FleetMixin is part of Cluster. Every tick it copies the latest RealFleet snapshot onto real Node and
GPU objects, writes log lines for what changed (a new Xid, ECC errors, a pod that crashed, a node
that went NotReady), keeps the real Kubernetes workloads as jobs, and gives the investigation tools,
the triage window and the verification step what they need. It also carries out approved steps on
real servers, and rehearses them first with Kubernetes' server-side dry run.

Nothing here is simulated: a real server's readings only ever come from DCGM, and its state from
Kubernetes.
"""
from __future__ import annotations

import time

from .dcgm import XID, xid_text

FLEET_HALL = "R"
REAL_BUSY = 30        # % load: below this, an SW thermal flag on a cool GPU slows nothing
REAL_WARM = 75        # C
REAL_HOT = 85         # C, "running hot" for verification
REAL_WATCH = 83       # C, worth a look
XID_RECENT = 180      # seconds an Xid counts as a current problem
MISSES_LOST = 2       # readings in a row without a GPU before it counts as not reporting
THERMAL = {"HW_THERMAL_SLOWDOWN", "SW_THERMAL_SLOWDOWN"}
ORDER = ["HW_THERMAL_SLOWDOWN", "SW_THERMAL_SLOWDOWN", "HW_POWER_BRAKE_SLOWDOWN", "HW_SLOWDOWN", "SW_POWER_CAP"]


def _main(reasons) -> str:
    return next((r for r in ORDER if r in reasons), "none")


def _ago(ts: float) -> str:
    s = max(0, int(time.time() - ts))
    return f"{s}s" if s < 90 else f"{s // 60} min"


class FleetMixin:
    fleet = None

    # ------------------------------------------------------------ connect
    def attach_fleet(self, fleet) -> None:
        with self.lock:
            self.fleet = fleet
            self._fleet_reset_state()
            self._sync_fleet(record=False, first=True)
            st = fleet.status()
            real = self.real_nodes()
            self._log("fleet", "nightingale", "info",
                      f"Connected to real GPU servers: {len(real)} servers, {sum(len(n.gpus) for n in real)} GPUs, "
                      f"DCGM from {st['where']}. "
                      + (f"Kubernetes at {st['kube_where']}: approved plans can cordon nodes, move jobs and run "
                         "nvidia-smi there." if st["kube"] else
                         "Kubernetes isn't connected, so Nightingale only watches these servers."))

    def detach_fleet(self) -> None:
        with self.lock:
            for n in self.real_nodes():
                self.nodes.pop(n.name, None)
            for rid in [r for r, rack in self.racks.items() if rack.hall == FLEET_HALL]:
                self.racks.pop(rid, None)
            for jid in [j for j, job in self.jobs.items() if job.real]:
                self.jobs.pop(jid, None)
            if self.fleet is not None:
                self.fleet.stop()
            self.fleet = None
            self.halls = sorted({r.hall for r in self.racks.values()})
            self._log("fleet", "nightingale", "info", "Disconnected from the real GPU servers.")

    def _fleet_reset_state(self) -> None:
        self._fleet_seq = -1
        self._fleet_misses: dict = {}
        self._fleet_prev: dict = {}
        self._fleet_pods: dict = {}
        self._fleet_racks: dict = {}
        self._fleet_ready: dict = {}
        self._fleet_names: dict = {}   # Kubernetes node name -> server name shown here (they differ on a clash)
        self._fleet_moved: dict = {}   # job -> (when, servers) for jobs an approved step just evicted

    def fleet_status(self) -> dict | None:
        if self.fleet is None:
            return None
        st = self.fleet.status()
        real = self.real_nodes()
        st.update({"servers": len(real), "gpus": sum(len(n.gpus) for n in real),
                   "watch": sum(self.fleet_odd(n, g) and not g.lost for n in real for g in n.gpus),
                   "lost": sum(g.lost for n in real for g in n.gpus),
                   "cordoned": [n.name for n in real if n.status == "drained"],
                   "jobs": [j.id for j in self.jobs.values() if j.real],
                   "racks": sorted(r for r, rack in self.racks.items() if rack.hall == FLEET_HALL),
                   "rack_of": {n.name: n.rack for n in real}})
        return st

    def real_nodes(self) -> list:
        return [n for n in self.nodes.values() if n.real]

    def is_real(self, name: str) -> bool:
        n = self.nodes.get(name)
        return bool(n is not None and n.real)

    def kname(self, name: str) -> str:
        n = self.nodes[name]
        return n.kname or n.name

    # ------------------------------------------------------- what counts
    def fleet_odd(self, n, g) -> bool:
        """Worth a look on a real GPU: not reporting, hot, throttling under load, memory errors, NVLink errors, a new Xid."""
        if g.lost:
            return True
        hist = g.history
        rising = len(hist) >= 3 and (hist[-1][5] > hist[-3][5] or hist[-1][7] > hist[-3][7])
        return bool(g.temp >= REAL_WATCH or "HW_THERMAL_SLOWDOWN" in g.reasons
                    or (THERMAL & set(g.reasons) and g.util >= REAL_BUSY) or g.dbe or g.remap_pending
                    or rising or (g.xid_at and time.time() - g.xid_at < XID_RECENT) or not n.ready)

    def _fleet_why(self, n, g) -> str:
        if g.lost:
            return "not reporting to DCGM"
        why = []
        if g.xid_at and time.time() - g.xid_at < XID_RECENT:
            why.append(f"{xid_text(g.xid)} {_ago(g.xid_at)} ago")
        if g.dbe:
            why.append(f"{g.dbe} uncorrectable memory errors")
        if g.remap_pending:
            why.append("memory row remap pending")
        hist = g.history
        if len(hist) >= 3 and hist[-1][5] > hist[-3][5]:
            why.append(f"corrected memory errors rising ({g.sbe:,})")
        if len(hist) >= 3 and hist[-1][7] > hist[-3][7]:
            why.append(f"NVLink errors rising ({g.nvlink_crc:,})")
        if "HW_THERMAL_SLOWDOWN" in g.reasons or (THERMAL & set(g.reasons) and g.util >= REAL_BUSY):
            why.append(f"slowing down for heat at {g.temp:.0f}C")
        elif g.temp >= REAL_WATCH:
            why.append(f"hot, {g.temp:.0f}C")
        if not n.ready:
            why.append("node NotReady")
        return ", ".join(why)

    # ---------------------------------------------------------------- sync
    def _fleet_rack(self, label: str) -> str:
        key = label or ""
        if key not in self._fleet_racks:
            rid = f"{FLEET_HALL}-{len(self._fleet_racks) + 1:02d}"
            self._fleet_racks[key] = rid
        rid = self._fleet_racks[key]
        if rid not in self.racks:
            from .simulator import Rack  # noqa: PLC0415 - avoid a circular import
            self.racks[rid] = Rack(rid, FLEET_HALL, [], label or "real servers")
            self.racks[rid].coolant_flow = 100.0
        return rid

    def _fleet_node(self, kname: str, info: dict):
        from .simulator import GPU, Node  # noqa: PLC0415

        name = self._fleet_names.get(kname)
        if name is None:
            name = kname if kname not in self.nodes else f"{kname}-real"
            self._fleet_names[kname] = name
        n = self.nodes.get(name)
        if n is None:
            rid = self._fleet_rack(info.get("rack", ""))
            n = Node(name, rid, FLEET_HALL, [], real=True, model=info.get("model") or "NVIDIA GPU",
                     driver=info.get("driver") or "", kname=kname if kname != name else "")
            self.nodes[name] = n
            self.racks[rid].nodes.append(name)
            self.halls = sorted({r.hall for r in self.racks.values()})
        have = {g.index for g in n.gpus}
        # a Kubernetes GPU node whose exporter isn't reporting still shows its GPUs (as not reporting)
        wanted = sorted(info["gpus"]) or list(range(int(info.get("gpu_count") or 0)))
        for i in wanted:
            if i not in have:
                r = info["gpus"].get(i, {})
                g = GPU(i, r.get("pci") or f"gpu:{i}")
                g.temp, g.power, g.clock, g.max_clock, g.power_limit = 0.0, 0.0, 0, 0, 0
                n.gpus.append(g)
        n.gpus.sort(key=lambda g: g.index)
        return n

    def _sync_fleet(self, record: bool = True, first: bool = False) -> None:
        if self.fleet is None:
            return
        snap = self.fleet.snapshot()
        fresh = snap["seq"] != self._fleet_seq
        self._fleet_seq = snap["seq"]
        now = time.time()
        for kname, info in snap["nodes"].items():
            n = self._fleet_node(kname, info)
            n.model = info.get("model") or n.model
            n.driver = info.get("driver") or n.driver
            n.status = "drained" if info.get("cordoned") else "active"
            was_ready = self._fleet_ready.get(n.name, True)
            n.ready, n.ready_reason = bool(info.get("ready", True)), info.get("ready_reason", "")
            self._fleet_ready[n.name] = n.ready
            if fresh and was_ready and not n.ready and not first:
                self._log(n.name, "kubernetes", "error",
                          f"node {kname} is NotReady" + (f": {n.ready_reason}" if n.ready_reason else ""))
            elif fresh and not was_ready and n.ready:
                self._log(n.name, "kubernetes", "info", f"node {kname} is Ready again")
            for g in n.gpus:
                r = info["gpus"].get(g.index) if info.get("reporting") else None
                key = (n.name, g.index)
                if r is None:
                    if fresh:
                        self._fleet_misses[key] = self._fleet_misses.get(key, 0) + 1
                    if self._fleet_misses.get(key, 0) >= MISSES_LOST and not g.lost:
                        g.lost = True
                        g.util, g.clock, g.power, g.throttle = 0, 0, 0.0, "n/a"
                        self._log(n.name, "dcgm", "error",
                                  f"GPU {g.index} ({g.pci}) stopped reporting to DCGM"
                                  + ("" if info.get("reporting") else ": the server's exporter isn't answering"))
                    continue
                if not fresh:
                    continue  # nothing new since the last tick; the readings stand
                self._fleet_misses[key] = 0
                if g.lost:
                    g.lost = False
                    self._log(n.name, "dcgm", "info", f"GPU {g.index} is reporting to DCGM again")
                self._fleet_gpu(n, g, r, self._fleet_prev.get(key), first, now)
                self._fleet_prev[key] = dict(r)
            if record:
                for g in n.gpus:
                    g.history.append((self.tick_count, round(g.temp), round(g.power), g.clock, g.throttle,
                                      g.sbe, g.dbe, g.nvlink_crc, g.lost, g.util))
        if fresh:
            self._fleet_jobs(snap["pods"], first)
            for e in self.fleet.take_events():
                self._fleet_event(e, first)

    def _fleet_gpu(self, n, g, r: dict, prev: dict | None, quiet: bool, now: float) -> None:
        def num(key, default=0.0):
            v = r.get(key)
            return default if v is None else v

        g.temp = float(num("temp", g.temp))
        g.power = float(num("power", g.power))
        g.power_limit = int(num("power_limit", 0) or num("power_enforced", 0) or g.power_limit or 0)
        g.power_max = int(r["power_max"]) if r.get("power_max") else g.power_max
        g.power_min = int(r["power_min"]) if r.get("power_min") else g.power_min
        g.power_default = int(r["power_default"]) if r.get("power_default") else g.power_default
        g.clock = int(num("clock", g.clock))
        g.max_clock = int(r["max_clock"]) if r.get("max_clock") else max(g.max_clock, g.clock)
        g.util = int(num("util", g.util))
        g.mem_used = int(num("mem_used", g.mem_used))
        g.mem_total = int(r.get("mem_total") or g.mem_total or 0)
        g.uuid = r.get("uuid") or g.uuid
        g.pod = f"{r['namespace']}/{r['pod']}" if r.get("pod") else ""
        before = (g.sbe, g.dbe, g.nvlink_crc, g.remap_pending)
        g.sbe, g.dbe = int(num("sbe", 0)), int(num("dbe", 0))
        g.nvlink_crc = int(num("nvlink_crc", 0))
        g.remap_pending = bool(num("remap_pending", 0) or num("remap_failed", 0))
        g.extra = {k: r[k] for k in ("mem_temp", "pcie_replay", "rows_uncorrectable", "rows_correctable",
                                     "retired_pending", "thermal_violation", "power_violation") if k in r}
        if "reasons" in r:
            reasons = list(r["reasons"])
        else:  # no clock-event field exported: infer from the violation counters
            reasons = []
            if prev and r.get("thermal_violation", 0) > prev.get("thermal_violation", 0):
                reasons.append("HW_THERMAL_SLOWDOWN" if g.temp >= 87 else "SW_THERMAL_SLOWDOWN")
            if prev and r.get("power_violation", 0) > prev.get("power_violation", 0):
                reasons.append("SW_POWER_CAP")
        if g.util < REAL_BUSY and g.temp < REAL_WARM:  # an idle, cool GPU: a software heat flag slows nothing
            reasons = [x for x in reasons if x != "SW_THERMAL_SLOWDOWN"]
        was_thermal = "HW_THERMAL_SLOWDOWN" in g.reasons or (THERMAL & set(g.reasons) and g.util >= REAL_BUSY)
        g.reasons = tuple(reasons)
        g.throttle = _main(g.reasons)
        xid = int(num("xid", 0))
        xid_total = num("xid_total", None)
        if prev is None:
            if xid and not quiet:
                g.xid = xid
            elif xid:
                g.xid = xid
                self._log(n.name, "dcgm", "info",
                          f"GPU {g.index}: DCGM's last recorded {xid_text(xid)} is from before Nightingale connected")
            return
        new_xid = (xid and xid != int(prev.get("xid") or 0)) or (
            xid_total is not None and xid_total > (prev.get("xid_total") or 0) and xid)
        if new_xid and not quiet:
            g.xid, g.xid_at = xid, now
            self._log(n.name, "dcgm", "error",
                      f"NVRM: Xid (PCI:{g.pci}): {xid}, reported by DCGM for GPU {g.index}: "
                      f"{XID.get(xid, ('unlisted Xid code', ''))[0]}")
        if quiet:
            return
        if g.dbe > before[1]:
            self._log(n.name, "dcgm", "error",
                      f"GPU {g.index} uncorrectable (double-bit) ECC errors: {g.dbe} (+{g.dbe - before[1]})")
        if g.sbe - before[0] >= 10:
            self._log(n.name, "dcgm", "warn", f"GPU {g.index} single-bit ECC errors climbing: {g.sbe:,} (+{g.sbe - before[0]})")
        if g.nvlink_crc > before[2]:
            self._log(n.name, "dcgm", "warn", f"GPU {g.index} NVLink CRC errors: {g.nvlink_crc:,} (+{g.nvlink_crc - before[2]})")
        if g.remap_pending and not before[3]:
            self._log(n.name, "dcgm", "warn", f"GPU {g.index} has a pending memory row remap (needs a GPU reset)")
        thermal = "HW_THERMAL_SLOWDOWN" in g.reasons or (THERMAL & set(g.reasons) and g.util >= REAL_BUSY)
        if thermal and not was_thermal:
            self._log(n.name, "dcgm", "warn",
                      f"GPU {g.index} clocks reduced to {g.clock:,} MHz by {'+'.join(x for x in g.reasons if x in THERMAL)} "
                      f"at {g.temp:.0f}C, load {g.util}%")
        elif was_thermal and not thermal:
            self._log(n.name, "dcgm", "info", f"GPU {g.index} no longer slowed by heat: {g.temp:.0f}C")
        if g.temp >= REAL_WATCH > float(prev.get("temp") or 0):
            self._log(n.name, "dcgm", "warn", f"GPU {g.index} temperature {g.temp:.0f}C and rising")

    def _fleet_jobs(self, pods: list, first: bool) -> None:
        from .simulator import Job  # noqa: PLC0415

        names = {}
        for p in pods:
            names.setdefault(p["job"], set()).add(p["namespace"])
        groups: dict = {}
        for p in pods:
            base = p["job"] if len(names[p["job"]]) == 1 else f"{p['namespace']}/{p['job']}"
            jid = base if base not in self.jobs or self.jobs[base].real else f"k8s/{base}"
            groups.setdefault(jid, []).append(p)
        for n in self.real_nodes():
            for g in n.gpus:
                g.job = None
        by_kname = {n.kname or n.name: n for n in self.real_nodes()}
        seen = set()
        for jid, ps in groups.items():
            seen.add(jid)
            running = [p for p in ps if p["phase"] == "Running" and not p["waiting"] and not p.get("deleting")]
            crashing = [p for p in ps if p["waiting"] in ("CrashLoopBackOff", "Error", "ImagePullBackOff", "ErrImagePull")
                        or p["phase"] == "Failed"]
            status = ("crashed" if crashing and not running else "running" if len(running) == len(ps) else
                      "restarting" if running or any(p["restarts"] or p.get("deleting") for p in ps) else "pending")
            nodes = sorted({by_kname[p["node"]].name for p in ps if p["node"] in by_kname})
            j = self.jobs.get(jid)
            if j is None:
                j = Job(jid, ps[0]["namespace"], nodes, step=0.0, checkpoint=0, tokens_per_gpu=0, loss=0.0, real=True)
                self.jobs[jid] = j
                if not first:
                    self._log(nodes[0] if nodes else "fleet", "kubernetes", "info",
                              f"new GPU workload {jid} in namespace {j.owner}: {len(ps)} pod(s), "
                              f"{sum(p['gpus'] for p in ps)} GPU(s)")
            j.nodes, j.namespace, j.controller = nodes, ps[0]["namespace"], ps[0]["controller"]
            j.pods = [{"namespace": p["namespace"], "name": p["name"], "node": by_kname[p["node"]].name if p["node"] in by_kname else "",
                       "phase": p["phase"], "waiting": p["waiting"], "restarts": p["restarts"], "gpus": p["gpus"],
                       "controller": p["controller"], "deleting": bool(p.get("deleting"))} for p in ps]
            j.gpus = sum(p["gpus"] for p in ps)
            if j.status != status and not first:
                level = "error" if status == "crashed" else "info"
                self._log(nodes[0] if nodes else "fleet", "kubernetes", level, f"workload {jid} is now {status}")
            j.status, j.rate = status, (1.0 if status == "running" else 0.0)
            if crashing:
                t = next((p["terminated"] for p in crashing if p["terminated"]), None)
                j.last_error = (f"{crashing[0]['namespace']}/{crashing[0]['name']}: {crashing[0]['waiting'] or crashing[0]['phase']}"
                                + (f", exit code {t['exit_code']} ({t['reason']})" if t else ""))
            elif status == "running":
                j.last_error = ""
            # which GPU each pod uses: DCGM's pod labels when present, else the node's first free GPUs
            for p in ps:
                n = by_kname.get(p["node"])
                if n is None or p["phase"] not in ("Running", "Pending"):
                    continue
                exact = [g for g in n.gpus if g.pod == f"{p['namespace']}/{p['name']}"]
                use = exact or [g for g in n.gpus if g.job is None][: p["gpus"]]
                for g in use:
                    g.job = jid
            self._fleet_pod_logs(jid, ps, first)
        live = {p["uid"] for p in pods}
        for uid in [u for u in self._fleet_pods if u not in live]:
            del self._fleet_pods[uid]
        for jid in [j for j, job in self.jobs.items() if job.real and j not in seen]:
            gone = self.jobs.pop(jid)
            if not first:
                self._log(gone.nodes[0] if gone.nodes else "fleet", "kubernetes", "info",
                          f"GPU workload {jid} has no pods any more (finished or deleted)")

    def _fleet_pod_logs(self, jid: str, pods: list, first: bool) -> None:
        by_kname = {n.kname or n.name: n.name for n in self.real_nodes()}
        for p in pods:
            prev = self._fleet_pods.get(p["uid"])
            self._fleet_pods[p["uid"]] = p
            if first:
                continue
            where = by_kname.get(p["node"], "fleet")
            name = f"{p['namespace']}/{p['name']}"
            t = p["terminated"]
            if prev is None:
                if p["phase"] == "Pending" and p["message"]:
                    self._log(where, "kubernetes", "warn", f"pod {name} of {jid} is pending: {p['message']}")
                continue
            if p["restarts"] > prev["restarts"]:
                self._log(where, "kubernetes", "error",
                          f"pod {name} of {jid} restarted (restart {p['restarts']})"
                          + (f": exit code {t['exit_code']}, {t['reason']}" + (f", {t['message']}" if t['message'] else "")
                             if t else ""))
            if p["waiting"] and p["waiting"] != prev["waiting"] and p["waiting"] not in ("ContainerCreating", "PodInitializing"):
                self._log(where, "kubernetes", "error", f"pod {name} of {jid} is {p['waiting']}")
            if p["phase"] != prev["phase"]:
                level = "error" if p["phase"] == "Failed" else "info"
                self._log(where, "kubernetes", level, f"pod {name} of {jid} is {p['phase']}"
                          + (f" on {where}" if p["phase"] == "Running" else "")
                          + (f": {p['reason'] or ''} {p['message']}".rstrip() if p["phase"] in ("Failed", "Pending") and (p["reason"] or p["message"]) else ""))

    def _fleet_event(self, e: dict, first: bool) -> None:
        by_kname = {n.kname or n.name: n.name for n in self.real_nodes()}
        where = "fleet"
        if e["kind"] == "Node":
            where = by_kname.get(e["name"], "fleet")
        else:
            pod = next((p for p in self._fleet_pods.values() if p["name"] == e["name"] and p["namespace"] == e["namespace"]), None)
            if pod:
                where = by_kname.get(pod["node"], "fleet")
        if where == "fleet":
            return  # about something that isn't a GPU server or a GPU workload
        level = "error" if e["reason"] in ("NodeNotReady", "SystemOOM", "OOMKilling", "Evicted", "Rebooted") else "warn"
        if first:
            level = "info"
        self._log(where, "kubernetes", level,
                  f"{e['reason']}: {e['kind']} {(e['namespace'] + '/') if e['namespace'] else ''}{e['name']}: {e['message']}"
                  + (f" (x{e['count']})" if e["count"] > 1 else ""))

    # ------------------------------------------------------------- reading
    def _fleet_row(self, node, g, d) -> str:
        if g.lost:
            return f"  {node.name} [real server, rack {node.rack}, {node.status}] g{g.index} NOT REPORTING to DCGM"
        xid = f"{g.xid} ({XID.get(g.xid, ('?', ''))[0]}, {_ago(g.xid_at)} ago)" if g.xid_at else "none"
        return (f"  {node.name} [real server, rack {node.rack}, {node.status}{'' if node.ready else ', NotReady'}] "
                f"g{g.index} {node.model} temp={g.temp:.0f}C power={g.power:.0f}W/{g.power_limit}W "
                f"clock={g.clock}/{g.max_clock}MHz util={g.util}% throttle={'+'.join(g.reasons) or 'none'} "
                f"sbe={g.sbe}(+{d[0]}) dbe={g.dbe}(+{d[1]}) nvlink_crc={g.nvlink_crc}(+{d[2]}) "
                f"remap_pending={'yes' if g.remap_pending else 'no'} xid={xid} job={g.job or 'idle'}")

    def fleet_digest_line(self) -> str:
        real = self.real_nodes()
        st = self.fleet.status()
        cordoned = [n.name for n in real if n.status == "drained"]
        notready = [n.name for n in real if not n.ready]
        quiet = [n.name for n in real if n.gpus and all(g.lost for g in n.gpus)]
        names = ", ".join(n.name for n in real[:12]) + (f" and {len(real) - 12} more" if len(real) > 12 else "")
        return (f"Real servers (real DCGM readings{' and Kubernetes' if st['kube'] else ''}, hall {FLEET_HALL}): "
                f"{len(real)} servers, {sum(len(n.gpus) for n in real)} GPUs: {names or 'none yet'}; "
                f"cordoned: {', '.join(cordoned) or 'none'}; NotReady: {', '.join(notready) or 'none'}; "
                f"not reporting: {', '.join(quiet) or 'none'}. Real GPUs differ by model: flag them for Xid errors, "
                f"ECC errors, pending row remaps, rising NVLink errors, heat slowdown under load, GPUs not reporting, "
                f"NotReady nodes and crashing GPU workloads.")

    def fleet_job_line(self, j) -> str:
        return (f"  {j.id}: {j.status} (real Kubernetes workload, namespace {j.namespace}), {len(j.pods)} pod(s) on "
                f"{', '.join(j.nodes) or 'no node yet'}, {j.gpus} GPU(s)" + (f", last error: {j.last_error}" if j.last_error else ""))

    def fleet_target_summary(self, n) -> str:
        jobs = sorted(self.jobs_on(n.name))
        g0 = n.gpus[0] if n.gpus else None
        limits = ""
        if g0 is not None and g0.power_limit:
            limits = (f" GPU 0 power limit {g0.power_limit} W"
                      + (f" (allowed {g0.power_min}-{g0.power_max} W)" if g0.power_min and g0.power_max else "") + ".")
        kube = self.fleet is not None and self.fleet.kube is not None
        return (f"Server {n.name} is a REAL GPU server (Kubernetes node {n.kname or n.name}) in rack {n.rack}: "
                f"{len(n.gpus)} x {n.model}, NVIDIA driver {n.driver or 'unknown'}, status {n.status}"
                f"{'' if n.ready else ', NotReady'}. Readings come from DCGM. Jobs on it: {', '.join(jobs) or 'none'}."
                f"{limits} "
                + ("Approved steps act on real infrastructure through Kubernetes: drain_node cordons the node, "
                   "restart_job_from_checkpoint evicts the job's pods so their controller starts them again on "
                   "healthy nodes (the job resumes from its own checkpoint), set_power_limit and reset_gpu run "
                   "nvidia-smi on the node. reboot_node is not available for real servers: drain it, move the "
                   "job, and open_ticket for the reboot or repair." if kube else
                   "Kubernetes is not connected, so only open_ticket and notify_owner can run here; the other "
                   "steps will fail their rehearsal."))

    def fleet_node_text(self, n) -> str:
        jobs = sorted(self.jobs_on(n.name))
        lines = [f"{n.name}: REAL server (Kubernetes node {n.kname or n.name}), rack {n.rack}, status {n.status}"
                 f"{'' if n.ready else ', NotReady: ' + (n.ready_reason or 'unknown')}, {n.model}, driver {n.driver or '?'}, "
                 f"jobs {', '.join(jobs) or 'none'}"]
        for g in n.gpus:
            if g.lost:
                lines.append(f"  g{g.index} NOT REPORTING to DCGM")
                continue
            lim = f"/{g.power_limit}W" + (f" (allowed {g.power_min}-{g.power_max}W)" if g.power_min and g.power_max else "")
            mem = f" memory={g.mem_used}/{g.mem_total}MiB" if g.mem_total else ""
            xid = f" last_xid={xid_text(g.xid)}" + (f" {_ago(g.xid_at)} ago" if g.xid_at else " (before Nightingale connected)") if g.xid else ""
            extra = "".join(f" {k}={v:.0f}" for k, v in g.extra.items() if k in ("rows_uncorrectable", "retired_pending", "pcie_replay"))
            lines.append(f"  g{g.index} temp={g.temp:.0f}C power={g.power:.0f}{lim} clock={g.clock}/{g.max_clock}MHz "
                         f"util={g.util}%{mem} slowdown_reasons={'+'.join(g.reasons) or 'none'} sbe={g.sbe} dbe={g.dbe} "
                         f"nvlink_crc={g.nvlink_crc} remap_pending={'yes' if g.remap_pending else 'no'}{xid}{extra} "
                         f"job={g.job or 'idle'}" + (f" pod={g.pod}" if g.pod else ""))
        return "\n".join(lines)

    def fleet_job_text(self, j) -> str:
        lines = [f"{j.id}: real Kubernetes workload in namespace {j.namespace} (controller {j.controller or 'none'}), "
                 f"status {j.status}, {j.gpus} GPU(s) on {', '.join(j.nodes) or 'no node yet'}."]
        for p in j.pods[:12]:
            lines.append(f"  pod {p['namespace']}/{p['name']} on {p['node'] or '(not scheduled)'}: {p['phase']}"
                         + (f", {p['waiting']}" if p["waiting"] else "") + f", restarts {p['restarts']}, {p['gpus']} GPU(s)")
        lines.append(f"  last error: {j.last_error or 'none'}. Ranks and checkpoints are managed by the job itself; "
                     "restarting evicts its pods and the job resumes from its own last checkpoint.")
        return "\n".join(lines)

    def fleet_free_gpus(self) -> list[tuple[str, int]]:
        """Healthy, uncordoned, Ready real servers and how many of their GPUs no pod is using."""
        out = []
        for n in self.real_nodes():
            if n.status != "active" or not n.ready:
                continue
            free = sum(1 for g in n.gpus if g.job is None and not g.lost)
            if free:
                out.append((n.name, free))
        return out

    def fleet_issues(self, n) -> list[str]:
        issues = []
        if not n.ready:
            issues.append(f"node is NotReady ({n.ready_reason or 'no reason given'})")
        for g in n.gpus:
            if g.lost:
                issues.append(f"GPU {g.index} is not reporting to DCGM")
                continue
            if g.xid_at and time.time() - g.xid_at < XID_RECENT:
                issues.append(f"GPU {g.index} reported {xid_text(g.xid)} {_ago(g.xid_at)} ago")
            if g.dbe:
                issues.append(f"GPU {g.index} has {g.dbe} uncorrectable memory errors since its last reset")
            if g.remap_pending:
                issues.append(f"GPU {g.index} still has a pending row remap")
            if "HW_THERMAL_SLOWDOWN" in g.reasons or g.temp >= REAL_HOT:
                issues.append(f"GPU {g.index} is running hot ({g.temp:.0f}C)")
            hist = list(g.history)
            if len(hist) >= 5 and g.nvlink_crc > hist[-5][7]:
                issues.append(f"GPU {g.index} NVLink errors are still increasing")
            if len(hist) >= 3 and g.sbe > hist[-3][5] + 5:
                issues.append(f"GPU {g.index} memory errors are still increasing")
        for j in self.jobs.values():
            if j.real and n.name in j.nodes and j.status == "crashed" and not j.owner_notified:
                issues.append(f"{j.id} is crashing")
        return issues

    # ----------------------------------------------------------- actions
    def _fleet_cordon(self, names: list[str], on: bool, dry_run: bool) -> tuple[bool, str]:
        msgs = []
        for nm in names:
            ok, msg = self.fleet.cordon(self.kname(nm), on, dry_run)
            if not ok:
                return False, msg
            msgs.append(msg)
            if not dry_run:
                self.nodes[nm].status = "drained" if on else "active"
                self._log(nm, "kubernetes", "info", f"{nm} {'cordoned' if on else 'uncordoned'} by Nightingale (approved step)")
        return True, " ".join(msgs) if len(msgs) == 1 else f"{len(names)} servers {'cordoned' if on else 'uncordoned'}."

    def real_drain(self, node: str, dry_run: bool = False) -> tuple[bool, str]:
        return self._fleet_cordon([node], True, dry_run)

    def real_drain_rack(self, rack: str, dry_run: bool = False) -> tuple[bool, str]:
        names = [nm for nm in self.racks[rack].nodes if self.nodes[nm].status == "active"]
        if not names:
            return True, f"Every server in rack {rack} is already cordoned."
        return self._fleet_cordon(names, True, dry_run)

    def real_undrain(self, node: str, dry_run: bool = False, skip_checks: bool = False) -> tuple[bool, str]:
        if self.has_open_ticket(node):
            return False, f"{node} has an open repair ticket. Keep it cordoned until the repair is done."
        if not skip_checks:
            issues = [i for i in self.fleet_issues(self.nodes[node]) if "crashing" not in i]
            if issues:
                return False, f"{node} still shows problems: {'; '.join(issues)}."
        return self._fleet_cordon([node], False, dry_run)

    def real_restart_job(self, job_id: str, dry_run: bool = False) -> tuple[bool, str]:
        j = self.jobs[job_id]
        if not j.pods:
            return False, f"{job_id} has no pods to restart."
        bare = [p for p in j.pods if not p["controller"]]
        if bare:
            return False, (f"{bare[0]['namespace']}/{bare[0]['name']} has no controller (Job, Deployment, training "
                           "operator), so evicting it would delete it for good. Ask its owners to restart it.")
        bad = {nm for nm in j.nodes if self.nodes[nm].status != "active" or not self.nodes[nm].ready
               or any(g.lost for g in self.nodes[nm].gpus) or self.has_open_ticket(nm)}
        live = [p for p in j.pods if not p.get("deleting")]
        if not live:
            return True, f"{job_id}'s pods are already being replaced; nothing more to move."
        pods = [p for p in live if p["node"] in bad] or live
        moved = []
        for p in pods:
            ok, msg = self.fleet.evict(p["namespace"], p["name"], dry_run)
            if not ok:
                return False, msg
            moved.append(f"{p['namespace']}/{p['name']}")
        free = [(nm, k) for nm, k in self.fleet_free_gpus() if nm not in bad]
        need = sum(p["gpus"] for p in pods)
        room = (f" Free GPUs on healthy servers: {', '.join(f'{nm} ({k})' for nm, k in free)}." if free else
                " No healthy server has free GPUs right now, so the new pods will wait (Pending) until one does.")
        if dry_run:
            return True, (f"Kubernetes accepted evicting {len(moved)} pod(s) of {job_id} in a dry run "
                          f"({', '.join(moved)}); they need {need} GPU(s).{room}")
        j.status = "restarting"
        self._fleet_moved[job_id] = (time.time(), {p["node"] for p in pods})
        self._log(j.nodes[0] if j.nodes else "fleet", "kubernetes", "info",
                  f"evicted {', '.join(moved)} of {job_id} (approved step); its controller starts them again")
        return True, (f"Evicted {len(moved)} pod(s) of {job_id}" + (f" from {', '.join(sorted(bad))}" if bad else "")
                      + f". Its controller starts them again on healthy servers and the job resumes from its own "
                        f"last checkpoint.{room}")

    def _gpu_busy(self, node: str, gpu: int, moving: set) -> str:
        n = self.nodes[node]
        g = n.gpus[gpu]
        if g.job and g.job not in moving and self.jobs.get(g.job) and self.jobs[g.job].status in ("running", "restarting"):
            return g.job
        return ""

    def _wait_gone(self, job_id: str, node: str, seconds: float = 90.0) -> bool:
        """After an approved step evicted a job, wait (up to 90 s) for its pods to leave the server."""
        moved = self._fleet_moved.get(job_id)
        kube = self.fleet.kube if self.fleet is not None else None
        if not moved or time.time() - moved[0] > 300 or node not in moved[1] or kube is None:
            return False
        kname, end = self.kname(node), time.time() + seconds
        while True:
            try:
                left = [p for p in kube.gpu_pods() if p["node"] == kname and p["job"] == job_id.split("/")[-1]]
            except Exception:
                return False
            if not left:
                for g in self.nodes[node].gpus:
                    if g.job == job_id:
                        g.job = None
                return True
            if time.time() >= end:
                return False
            time.sleep(2)

    def real_power_limit(self, node: str, gpu: int, watts: int, dry_run: bool = False) -> tuple[bool, str]:
        g = self.nodes[node].gpus[gpu]
        if g.lost:
            return False, f"GPU {gpu} on {node} isn't reporting, so its power limit can't be changed."
        lo, hi = g.power_min, g.power_max or g.power_default
        if lo and hi and not lo <= watts <= hi:
            return False, f"{watts} W is outside what GPU {gpu} on {node} allows ({lo}-{hi} W)."
        ok, msg = self.fleet.nvidia_smi(self.kname(node), ["-i", str(gpu), "-pl", str(int(watts))], dry_run)
        if not ok or dry_run:
            return ok, msg
        g.power_limit = int(watts)
        self._log(node, "nvidia-smi", "info", f"power limit for GPU {gpu} set to {watts} W (approved step)")
        return True, f"GPU {gpu} on {node} is now capped at {watts} W. nvidia-smi said: {msg[-160:]}"

    def real_rack_power_limit(self, rack: str, watts: int, dry_run: bool = False) -> tuple[bool, str]:
        count = 0
        for nm in self.racks[rack].nodes:
            for g in self.nodes[nm].gpus:
                if g.lost:
                    continue
                ok, msg = self.real_power_limit(nm, g.index, watts, dry_run)
                if not ok:
                    return False, msg
                count += 1
        return True, f"{count} GPUs in rack {rack} {'would be' if dry_run else 'are now'} capped at {watts} W."

    def real_reset_gpu(self, node: str, gpu: int, dry_run: bool = False, moving: set | None = None) -> tuple[bool, str]:
        g = self.nodes[node].gpus[gpu]
        if g.lost:
            return False, (f"GPU {gpu} on {node} isn't reporting, so it can't be reset from software. "
                           "Drain the server and open a ticket for a reboot or reseat.")
        busy = self._gpu_busy(node, gpu, moving or set())
        if busy and not dry_run and self._wait_gone(busy, node):
            busy = ""  # the job an earlier step moved has left this server
        if busy:
            return False, (f"GPU {gpu} on {node} is in use by {busy}. Move the job first "
                           "(drain the server, then restart the job from its checkpoint).")
        ok, msg = self.fleet.nvidia_smi(self.kname(node), ["-i", str(gpu), "-r"], dry_run)
        if not ok or dry_run:
            return ok, msg + (" The reset itself also needs no process on the GPU when it runs." if ok else "")
        self._log(node, "nvidia-smi", "info", f"GPU {gpu} reset (approved step): {msg[-120:]}")
        g.xid_at = 0.0
        return True, f"GPU {gpu} on {node} was reset. nvidia-smi said: {msg[-160:]}"

    @staticmethod
    def real_reboot(node: str) -> tuple[bool, str]:
        return False, (f"Nightingale doesn't reboot real servers. Cordon {node}, move its jobs, then reboot it "
                       "through your server management (BMC or the cloud console); open_ticket records it for "
                       "whoever does.")

    def rehearse_real(self, plan: list[dict], target: str) -> dict:
        """Each step against the real cluster with Kubernetes' server-side dry run: nothing changes."""
        steps, moving, reset = [], set(), set()
        for a in plan:
            p, name = a["params"], a["action"]
            node = p.get("node")
            if name in ("open_ticket", "open_facility_ticket"):
                ok, msg = True, "Records a repair ticket in Nightingale; nothing changes on the servers."
            elif name == "notify_owner":
                ok, msg = True, "Records a message for the job's owners in Nightingale; nothing changes on the servers."
            elif self.fleet.kube is None:
                ok, msg = self.fleet._no_kube("run this step")
            elif name == "drain_node":
                ok, msg = self.real_drain(node, dry_run=True)
            elif name == "drain_rack":
                ok, msg = self.real_drain_rack(p["rack"], dry_run=True)
            elif name == "undrain_node":
                ok, msg = self.real_undrain(node, dry_run=True, skip_checks=node in reset)
                if ok and node in reset:
                    msg += " Its health is checked again right before it runs."
            elif name == "restart_job_from_checkpoint":
                ok, msg = self.real_restart_job(p["job"], dry_run=True)
                moving.add(p["job"])
            elif name == "reset_gpu":
                ok, msg = self.real_reset_gpu(node, p["gpu"], dry_run=True, moving=moving)
                reset.add(node)
            elif name == "set_power_limit":
                ok, msg = self.real_power_limit(node, p["gpu"], p["watts"], dry_run=True)
            elif name == "set_rack_power_limit":
                ok, msg = self.real_rack_power_limit(p["rack"], p["watts"], dry_run=True)
            elif name == "reboot_node":
                ok, msg = self.real_reboot(node)
            else:
                ok, msg = False, f"{name} isn't available for real servers."
            steps.append({"label": a.get("label") or name, "ok": ok, "locked": False, "result": msg})
        failed = [s for s in steps if not s["ok"]]
        lines = [("OK: " if s["ok"] else "BLOCKED: ") + f"{s['label']}. {s['result']}" for s in steps]
        summary = ("Rehearsed on the real cluster with Kubernetes' server-side dry run (nothing changed): "
                   f"{len(steps) - len(failed)} of {len(steps)} steps accepted"
                   + (f". Would fail: {'; '.join(s['label'] + ' (' + s['result'] + ')' for s in failed)}" if failed else "")
                   + ".")
        return {"verdict": "fail" if failed else "pass", "steps": steps, "lines": lines, "summary": summary,
                "crashed": [], "recovered": [], "issues": self.observed_issues(target), "seconds": 0,
                "checked": False, "kind": "kubernetes"}
