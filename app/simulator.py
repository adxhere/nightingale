"""A simulated NVIDIA GPU data center.

Halls hold racks, racks hold eight DGX-style servers, servers hold eight H100 GPUs.
The simulator produces telemetry and log lines shaped like the real thing (DCGM
health data, kernel NVRM Xid messages, nvidia-smi errors, PyTorch/NCCL trainer logs,
facility coolant sensors) and lets you inject realistic failures for demos and tests.

Nightingale never sees the injected faults directly. Its models only see what a
real operator would see: telemetry, logs, and the answers of read-only tools.

The data center can also hold one real machine: this computer's own GPU, read through
nvidia-smi (see local_gpu.py). It appears as server "laptop" in hall L. Fixes on it come from
pc_actions.py, run only after the person approves them, and depend on light or serious mode.
"""
from __future__ import annotations

import copy
import math
import random
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

GPUS_PER_NODE = 8
NODES_PER_RACK = 8
RACKS_PER_HALL = 16
HALL_LETTERS = "ABCDEFGH"
PCI_IDS = ["0000:18:00", "0000:2a:00", "0000:3a:00", "0000:5d:00",
           "0000:9a:00", "0000:ab:00", "0000:ba:00", "0000:db:00"]
MAX_CLOCK = 1980
IDLE_CLOCK = 345
DEFAULT_POWER_LIMIT = 700
WATCH_TEMP = 83
SLOWDOWN_TEMP = 87
SHUTDOWN_TEMP = 96
POWER_BUDGET_KW_PER_GPU = 0.8
SIZES = (256, 1024, 2048, 4096)
LOCAL_HALL = "L"
LOCAL_RACK = "L-01"
LOCAL_NODE = "laptop"
LOCAL_ALIASES = {"laptop", "local", "this computer", "this laptop", "this pc", "pc", "desktop", "localhost"}
LOCAL_HOT = 90  # laptop GPUs run 75-87C under load by design; what matters is whether clocks are cut
# Many laptops raise SW_THERMAL_SLOWDOWN while the GPU sits idle and cool: the notebook's own power policy
# (battery, silent mode, Dynamic Boost) sets a clock ceiling. With nothing running, that ceiling slows nothing,
# so it only counts once the GPU is busy or warm.
LOCAL_BUSY = 30   # % load
LOCAL_WARM = 75   # C

SCENARIOS = {
    "thermal": "Overheating GPU",
    "ecc": "Memory (ECC) failure",
    "bus": "GPU falls off the bus",
    "nvlink": "NVLink errors",
    "rack_cooling": "Rack cooling failure",
    "app_bug": "Bug in user code",
}


THERMAL_REASONS = {"HW_THERMAL_SLOWDOWN", "SW_THERMAL_SLOWDOWN"}
_REASON_ORDER = ["HW_THERMAL_SLOWDOWN", "SW_THERMAL_SLOWDOWN", "HW_POWER_BRAKE_SLOWDOWN", "HW_SLOWDOWN", "SW_POWER_CAP"]


def _main_reason(reasons) -> str:
    for r in _REASON_ORDER:
        if r in reasons:
            return r
    return "none"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def clamp_size(size: int) -> int:
    size = int(size or 1024)
    size = max(128, min(8192, size))
    return (size // (GPUS_PER_NODE * NODES_PER_RACK)) * GPUS_PER_NODE * NODES_PER_RACK or 128


@dataclass(slots=True)
class GPU:
    index: int
    pci: str
    temp: float = 34.0
    power: float = 72.0
    power_limit: int = DEFAULT_POWER_LIMIT
    clock: int = IDLE_CLOCK
    util: int = 0
    sbe: int = 0
    dbe: int = 0
    nvlink_crc: int = 0
    remap_pending: bool = False
    throttle: str = "none"
    lost: bool = False
    job: str | None = None
    # (tick, temp, power, clock, throttle, sbe, dbe, nvlink_crc, lost, util)
    history: deque = field(default_factory=lambda: deque(maxlen=30))
    # Only read from a real GPU (this computer):
    max_clock: int = MAX_CLOCK
    power_max: int | None = None
    mem_used: int = 0
    mem_total: int = 0
    fan: int | None = None
    reasons: tuple = ()
    policy_flags: tuple = ()   # slowdown flags the laptop raises while idle and cool (see LOCAL_BUSY)


@dataclass(slots=True)
class Node:
    name: str
    rack: str
    hall: str
    gpus: list
    status: str = "active"  # active | drained | rebooting
    reboot_left: int = 0
    local: bool = False     # True for this computer's own, real GPU
    model: str = "H100 SXM"
    driver: str = ""


@dataclass(slots=True)
class Rack:
    id: str
    hall: str
    nodes: list
    cdu: str
    coolant_supply: float = 25.0
    coolant_return: float = 31.5
    coolant_flow: float = 99.0


@dataclass(slots=True)
class Job:
    id: str
    owner: str
    nodes: list
    step: float
    checkpoint: int
    tokens_per_gpu: int
    loss: float
    checkpoint_every: int = 200
    status: str = "running"  # running | restarting | crashed | failed
    rate: float = 1.0
    restart_left: int = 0
    last_error: str = ""
    owner_notified: bool = False


@dataclass(slots=True)
class Fault:
    id: int
    kind: str
    node: str
    gpu: int | None
    job: str
    start: int
    rack: str = ""
    status: str = "active"  # active | resolved
    flags: dict = field(default_factory=dict)


class Cluster:
    def __init__(self, size: int = 1024, seed: int | None = None) -> None:
        self.size = clamp_size(size)
        self.seed = seed
        self.rng = random.Random(seed)
        self.lock = threading.RLock()
        self.local = None  # a LocalGPU reader when this computer's GPU is connected
        self.pc = None     # PcFixes: runs approved fixes on this computer (set by the app)
        self.window_signal: set = set()
        self.local_activity = ""   # e.g. "a stress test the person started is running on purpose"
        self.selftests: list = []  # recent self-test results on this computer
        self.reset()

    # ------------------------------------------------------------------ setup
    def reset(self, size: int | None = None) -> None:
        with self.lock:
            if size:
                self.size = clamp_size(size)
            self.rng = random.Random(self.seed)
            self.tick_count = 0
            self.logs: deque = deque(maxlen=4000)
            self._log_id = 0
            self.faults: list[Fault] = []
            self._fault_id = 0
            self.tickets: list[dict] = []
            self.notifications: list[dict] = []
            self.history: list[dict] = []  # fleet memory: past incidents
            self._last_counters: dict = {}
            self.nodes: dict[str, Node] = {}
            self.racks: dict[str, Rack] = {}
            n_nodes = self.size // GPUS_PER_NODE
            for i in range(n_nodes):
                r = i // NODES_PER_RACK
                hall = HALL_LETTERS[r // RACKS_PER_HALL]
                rack_id = f"{hall}-{r % RACKS_PER_HALL + 1:02d}"
                name = f"dgx-{i + 1:04d}"
                if rack_id not in self.racks:
                    self.racks[rack_id] = Rack(rack_id, hall, [], f"CDU-{hall}{r % RACKS_PER_HALL + 1:02d}")
                self.racks[rack_id].nodes.append(name)
                self.nodes[name] = Node(name, rack_id, hall, [GPU(g, PCI_IDS[g]) for g in range(GPUS_PER_NODE)])
            self.halls = sorted({r.hall for r in self.racks.values()})
            self._seed_fleet()
            if self.local is not None:
                self._add_local_node()
            for _ in range(14):  # settle temperatures before the first log line
                self._update_gpus(record=False)
            self._log("fleet", "nightingale", "info",
                      f"Monitoring {self.size:,} GPUs in {n_nodes} servers, {len(self.racks)} racks, "
                      f"{len(self.halls)} hall{'s' if len(self.halls) > 1 else ''}")

    def _seed_fleet(self) -> None:
        """Jobs, spares and one server already waiting for a repair, as in any real fleet."""
        names = list(self.nodes)
        n = len(names)
        down_idx = min(n - 1, int(n * 0.86))
        while (down_idx + 1) % 13 == 0:
            down_idx -= 1
        self.down_node = names[down_idx]
        down = self.nodes[self.down_node]
        down.status = "drained"
        down.gpus[0].lost = True
        usable = [nm for i, nm in enumerate(names) if (i + 1) % 13 and nm != self.down_node]
        n_big = max(3, int(len(usable) * 0.68))
        n_moe = max(2, int(len(usable) * 0.22))
        big, moe = usable[:n_big], usable[n_big:n_big + n_moe]
        emb = usable[n_big + n_moe:n_big + n_moe + 2]
        self.jobs: dict[str, Job] = {
            "llm-pretrain-405b": Job("llm-pretrain-405b", "team-foundation", big, step=120410.0,
                                     checkpoint=120400, tokens_per_gpu=3900, loss=1.61, checkpoint_every=50),
            "moe-finetune-1t": Job("moe-finetune-1t", "team-research", moe, step=8800.0,
                                   checkpoint=8800, tokens_per_gpu=2600, loss=0.94, checkpoint_every=50),
            "embeddings-finetune": Job("embeddings-finetune", "team-search", emb, step=1210.0,
                                       checkpoint=1200, tokens_per_gpu=11200, loss=0.61, checkpoint_every=20),
        }
        for job in self.jobs.values():
            for nm in job.nodes:
                for g in self.nodes[nm].gpus:
                    g.job = job.id
        tid = "HW-1001"
        self.tickets.append({"id": tid, "kind": "hardware", "node": self.down_node,
                             "rack": down.rack, "gpu": 0, "status": "open", "tick": 0,
                             "summary": f"{self.down_node} GPU 0 fell off the bus for the third time "
                                        "this month. Replace the GPU and check its riser."})
        self.history += [
            {"target": self.down_node, "gpu": 0, "when": "19 days ago", "title": "GPU 0 fell off the bus (Xid 79)",
             "fix": "Rebooted; GPU came back"},
            {"target": self.down_node, "gpu": 0, "when": "6 days ago", "title": "GPU 0 fell off the bus (Xid 79)",
             "fix": "Rebooted; GPU came back"},
            {"target": self.down_node, "gpu": 0, "when": "today, 09:12", "title": "GPU 0 fell off the bus (Xid 79)",
             "fix": f"Drained, replacement ticket {tid}"},
        ]

    # ---------------------------------------------------------------- helpers
    def _log(self, node: str, source: str, level: str, text: str) -> None:
        self._log_id += 1
        ts = _now()
        self.logs.append({"id": self._log_id, "ts": ts.isoformat(timespec="seconds"),
                          "t": ts.strftime("%H:%M:%S"), "node": node, "source": source,
                          "level": level, "text": text})

    def _xid(self, node: Node, g: GPU, code: int, msg: str, pid: int | None = None,
             name: str | None = None, level: str = "error") -> None:
        pid_s = str(pid) if pid else "'<unknown>'"
        self._log(node.name, "kernel", level,
                  f"NVRM: Xid (PCI:{g.pci}): {code}, pid={pid_s}, name={name or '<unknown>'}, {msg}")

    def last_log_id(self) -> int:
        return self._log_id

    def canonical_node(self, name: str) -> str | None:
        name = str(name or "").strip()
        if name in self.nodes:
            return name
        low = name.lower()
        if low in LOCAL_ALIASES or low.replace("_", " ").replace("-", " ") in LOCAL_ALIASES:
            return LOCAL_NODE if LOCAL_NODE in self.nodes else None
        # "dgx-53", "dgx0053", "DGX_0053" or "53"; never glue digits from a GPU number onto the server's.
        m = re.fullmatch(r"(?:dgx[-_ ]?)?(\d{1,5})", low)
        if m:
            guess = f"dgx-{int(m.group(1)):04d}"
            if guess in self.nodes:
                return guess
        return None

    def canonical_rack(self, name: str) -> str | None:
        name = str(name or "").strip().upper().replace("RACK", "").strip()
        if name in self.racks:
            return name
        if len(name) >= 2 and name[0] in HALL_LETTERS:
            digits = "".join(ch for ch in name[1:] if ch.isdigit())
            if digits:
                guess = f"{name[0]}-{int(digits):02d}"
                if guess in self.racks:
                    return guess
        return None

    def canonical_target(self, name: str) -> str | None:
        return self.canonical_node(name) or self.canonical_rack(name)

    def is_rack(self, target: str) -> bool:
        return target in self.racks

    def rank_of(self, job: Job, node: str, gpu: int) -> int:
        if node not in job.nodes:
            return -1
        return job.nodes.index(node) * GPUS_PER_NODE + gpu

    def jobs_on(self, target: str) -> set[str]:
        nodes = self.racks[target].nodes if target in self.racks else [target]
        return {j.id for j in self.jobs.values() if any(n in j.nodes for n in nodes)}

    def _job_gpus(self, job: Job) -> list:
        return [(self.nodes[n], g) for n in job.nodes for g in self.nodes[n].gpus if g.job == job.id]

    def _unresolved(self, node: str | None = None, kind: str | None = None,
                    job: str | None = None, rack: str | None = None) -> list[Fault]:
        return [f for f in self.faults if f.status != "resolved"
                and (node is None or f.node == node)
                and (kind is None or f.kind == kind)
                and (job is None or f.job == job)
                and (rack is None or f.rack == rack)]

    def is_spare(self, n: Node) -> bool:
        return not n.local and n.status == "active" and all(g.job is None and not g.lost for g in n.gpus)

    def sim_nodes(self) -> list[Node]:
        return [n for n in self.nodes.values() if not n.local]

    def spare_nodes(self, avoid_rack: str | None = None) -> list[Node]:
        cooling = {f.rack for f in self._unresolved(kind="rack_cooling")}
        spares = [n for n in self.nodes.values() if self.is_spare(n) and n.rack not in cooling]
        spares.sort(key=lambda n: (n.rack == avoid_rack, n.name))
        return spares

    def validate_target(self, params: dict) -> str | None:
        if "node" in params:
            canon = self.canonical_node(params["node"])
            if canon is None:
                return f"unknown server '{params['node']}'"
            params["node"] = canon
        if "rack" in params:
            canon = self.canonical_rack(params["rack"])
            if canon is None:
                return f"unknown rack '{params['rack']}'"
            params["rack"] = canon
        count = len(self.nodes[params["node"]].gpus) if params.get("node") in self.nodes else GPUS_PER_NODE
        if params.get("gpu") is not None and not 0 <= params["gpu"] < count:
            return f"GPU index {params['gpu']} is out of range (0-{count - 1})"
        if "job" in params and params["job"] not in self.jobs:
            return f"unknown job '{params['job']}'"
        return None

    # ------------------------------------------------------- this computer
    def attach_local(self, source) -> None:
        """Add this computer's real GPU as server 'laptop' in hall L."""
        with self.lock:
            self.local = source
            self._add_local_node()
            n = self.nodes[LOCAL_NODE]
            self._log(LOCAL_NODE, "nightingale", "info",
                      f"Connected to this computer's GPU: {len(n.gpus)} x {n.model}, driver {n.driver or 'unknown'}. "
                      "Nightingale reads it every 2 s; fixes run only after you approve them.")
            past = source.snapshot().get("past_events") or []
            if past:
                last = past[-1]
                self._log(LOCAL_NODE, "windows", "warn",
                          f"{len(past)} display-driver error event{'s' if len(past) > 1 else ''} in the last 24 hours; "
                          f"latest at {last['time'][11:16]}: {last['provider']} event {last['id']}: {last['message'][:160]}")
            elif getattr(source, "events_enabled", False):
                self._log(LOCAL_NODE, "windows", "info", "No display-driver errors in the last 24 hours.")

    def detach_local(self) -> None:
        with self.lock:
            self.local = None
            self.nodes.pop(LOCAL_NODE, None)
            self.racks.pop(LOCAL_RACK, None)
            self.halls = sorted({r.hall for r in self.racks.values()})
            self._log("fleet", "nightingale", "info", "Disconnected from this computer's GPU.")

    def local_status(self) -> dict | None:
        if self.local is None:
            return None
        st = self.local.status()
        st["node"] = LOCAL_NODE
        st["demo"] = bool(getattr(self.local, "demo", False))
        return st

    def _add_local_node(self) -> None:
        snap = self.local.snapshot()
        gpus = snap["gpus"] or [{"index": 0, "pci": "", "name": "NVIDIA GPU", "driver": ""}]
        node = Node(LOCAL_NODE, LOCAL_RACK, LOCAL_HALL,
                    [GPU(r["index"], r.get("pci") or f"local:{r['index']}") for r in gpus], local=True,
                    model=gpus[0].get("name") or "NVIDIA GPU", driver=gpus[0].get("driver") or "")
        for g in node.gpus:
            g.temp, g.power, g.clock = 0.0, 0.0, 0
        self.nodes[LOCAL_NODE] = node
        self.racks[LOCAL_RACK] = Rack(LOCAL_RACK, LOCAL_HALL, [LOCAL_NODE], "")
        self.halls = sorted({r.hall for r in self.racks.values()})
        self._local_prev: dict = {}
        self._sync_local(record=False)

    def mark_local_checked(self) -> None:
        """Driver errors logged before this point no longer count against this computer."""
        with self.lock:
            self._local_verified_log = self._log_id

    def local_odd(self, g: GPU) -> bool:
        """Worth a look on a laptop GPU: lost, cutting clocks for heat, very hot, or memory full."""
        return bool(g.lost or THERMAL_REASONS & set(g.reasons) or g.temp >= LOCAL_HOT
                    or (g.mem_total and g.mem_used / g.mem_total >= 0.95 and g.util > 0))

    def _sync_local(self, record: bool = True) -> None:
        """Copy the latest real readings onto server 'laptop' and log what changed."""
        if self.local is None or LOCAL_NODE not in self.nodes:
            return
        node = self.nodes[LOCAL_NODE]
        snap = self.local.snapshot()
        readings = snap["gpus"]
        prev = getattr(self, "_local_prev", {})
        if snap["state"] == "ok" and readings:
            if len(readings) != len(node.gpus):
                node.gpus = [GPU(r["index"], r.get("pci") or f"local:{r['index']}") for r in readings]
            node.model, node.driver = readings[0]["name"], readings[0]["driver"]
            for g, r in zip(node.gpus, readings):
                if g.lost:
                    self._log(LOCAL_NODE, "nvidia-smi", "info", f"GPU {g.index} is answering the driver again")
                g.lost = False
                g.temp = float(r["temp"]) if r["temp"] is not None else g.temp
                g.power = float(r["power"] or 0.0)
                g.power_limit = int(r["power_limit"] or 0)
                g.power_max = int(r["power_max"]) if r["power_max"] else None
                g.clock = int(r["clock"] or 0)
                g.max_clock = int(r["max_clock"] or 0) or g.max_clock
                g.util = int(r["util"] or 0)
                g.mem_used, g.mem_total = int(r["mem_used"] or 0), int(r["mem_total"] or 0)
                g.fan = int(r["fan"]) if r["fan"] is not None else None
                raw = tuple(x for x in r["reasons"] if x != "GPU_IDLE")
                idle = g.util < LOCAL_BUSY and g.temp < LOCAL_WARM
                g.policy_flags = tuple(x for x in raw if x == "SW_THERMAL_SLOWDOWN") if idle else ()
                g.reasons = tuple(x for x in raw if x not in g.policy_flags)
                g.throttle = _main_reason(g.reasons)
                self._local_events(g, prev.get(g.index))
                prev[g.index] = {"thermal": bool(THERMAL_REASONS & set(g.reasons)),
                                 "memfull": bool(g.mem_total and g.mem_used / g.mem_total >= 0.95),
                                 "since": prev.get(g.index, {}).get("since", 0) + 1}
        elif snap["state"] == "lost":
            for g in node.gpus:
                if not g.lost:
                    g.lost = True
                    g.util, g.clock, g.power, g.throttle = 0, 0, 0.0, "n/a"
                    self._log(LOCAL_NODE, "nvidia-smi", "error",
                              f"{snap['error'] or 'No devices were found'}: GPU {g.index} stopped answering the driver")
        self._local_prev = prev
        for e in self.local.take_events():
            self._log(LOCAL_NODE, "windows", "error",
                      f"{e['provider']} event {e['id']} at {e['time'][11:19]}: {e['message'][:200]}")
        if record:
            for g in node.gpus:
                g.history.append((self.tick_count, round(g.temp), round(g.power), g.clock, g.throttle,
                                  0, 0, 0, g.lost, g.util))
            if self.tick_count % 10 == 0 and snap["state"] == "ok":
                for g in node.gpus:
                    self._log(LOCAL_NODE, "nvidia-smi", "info", self._local_line(node, g))

    def _local_line(self, node: Node, g: GPU) -> str:
        mem = f", memory {g.mem_used / 1024:.1f} of {g.mem_total / 1024:.1f} GB" if g.mem_total else ""
        fan = f", fan {g.fan}%" if g.fan is not None else ""
        cap = f" of {g.power_limit} W" if g.power_limit else ""
        return (f"GPU {g.index} {node.model}: {g.temp:.0f}C, {g.power:.0f} W{cap}, SM {g.clock:,} of {g.max_clock:,} MHz, "
                f"load {g.util}%{mem}{fan}")

    def _local_events(self, g: GPU, before: dict | None) -> None:
        thermal = [r for r in g.reasons if r in THERMAL_REASONS]
        was_thermal = bool(before and before["thermal"])
        if thermal and (not was_thermal or self.tick_count % 5 == 0):
            self._log(LOCAL_NODE, "nvidia-smi", "warn",
                      f"GPU {g.index} clocks reduced to {g.clock:,} of {g.max_clock:,} MHz by {'+'.join(thermal)} "
                      f"at {g.temp:.0f}C, load {g.util}%")
        elif was_thermal and not thermal:
            self._log(LOCAL_NODE, "nvidia-smi", "info",
                      f"GPU {g.index} no longer slowed by heat: {g.temp:.0f}C, {g.clock:,} MHz")
        full = bool(g.mem_total and g.mem_used / g.mem_total >= 0.95)
        if full and not (before and before["memfull"]):
            self._log(LOCAL_NODE, "nvidia-smi", "warn",
                      f"GPU {g.index} memory {g.mem_used:,} of {g.mem_total:,} MiB in use "
                      f"({round(100 * g.mem_used / g.mem_total)}%)")

    def _local_row(self, node: Node, g: GPU) -> str:
        if g.lost:
            return f"  {node.name} [this computer] g{g.index} LOST (not answering the NVIDIA driver)"
        return (f"  {node.name} [this computer] g{g.index} {node.model} temp={g.temp:.0f}C "
                f"power={g.power:.0f}W/{g.power_limit}W clock={g.clock}/{g.max_clock}MHz util={g.util}% "
                f"vram={g.mem_used}/{g.mem_total}MiB throttle={'+'.join(g.reasons) or 'none'}")

    def _local_status(self, n: Node) -> str:
        lines = [f"{n.name}: this computer, {len(n.gpus)} x {n.model}, NVIDIA driver {n.driver or 'unknown'}"
                 + (f". Right now {self.local_activity}" if self.local_activity else "")]
        for g in n.gpus:
            if g.lost:
                lines.append(f"  g{g.index} LOST: not answering the NVIDIA driver")
                continue
            lines.append(f"  g{g.index} temp={g.temp:.0f}C power={g.power:.0f}/{g.power_limit}W"
                         + (f" (max {g.power_max}W)" if g.power_max else "")
                         + f" clock={g.clock}/{g.max_clock}MHz load={g.util}% memory={g.mem_used}/{g.mem_total}MiB "
                         f"fan={'n/a' if g.fan is None else str(g.fan) + '%'} slowdown_reasons={'+'.join(g.reasons) or 'none'}"
                         + (f" (also {'+'.join(g.policy_flags)}, raised by the laptop's power policy while the GPU is "
                            f"idle and cool: it slows nothing)" if g.policy_flags else ""))
        return "\n".join(lines)

    def _local_issues(self, node: Node) -> list[str]:
        issues = []
        for g in node.gpus:
            if g.lost:
                issues.append(f"GPU {g.index} is not answering the NVIDIA driver")
                continue
            thermal = [r for r in g.reasons if r in THERMAL_REASONS]
            if thermal:
                issues.append(f"GPU {g.index} is cutting its clocks for heat ({'+'.join(thermal)}) at {g.temp:.0f}C")
            elif g.temp >= LOCAL_HOT:
                issues.append(f"GPU {g.index} is very hot ({g.temp:.0f}C)")
            if g.mem_total and g.mem_used / g.mem_total >= 0.95 and g.util > 0:
                issues.append(f"GPU {g.index} memory is {round(100 * g.mem_used / g.mem_total)}% full")
        recent = [l for l in list(self.logs)[-400:] if l["node"] == LOCAL_NODE and l["source"] == "windows"
                  and l["level"] == "error" and l["id"] > getattr(self, "_local_verified_log", 0)]
        if recent:
            whea = sum("WHEA" in l["text"] for l in recent)
            issues.append(f"Windows logged {len(recent)} driver or hardware error event{'s' if len(recent) > 1 else ''}"
                          + (f" ({whea} hardware errors from WHEA)" if whea else ""))
        return issues

    # ------------------------------------------------------------------- tick
    def tick(self) -> None:
        with self.lock:
            self.tick_count += 1
            self._advance_nodes()
            for f in list(self.faults):
                if f.status != "resolved":
                    self._advance_fault(f)
            self._sync_local()
            self._update_gpus()
            self._advance_jobs()
            self._routine_logs()

    def _heat(self) -> dict:
        """Extra degrees per (node, gpu) from failing cooling."""
        extra: dict = {}
        for f in self._unresolved(kind="thermal"):
            t = self.tick_count - f.start
            raw = 1.5 * t if t < 16 else 24.0 if t < 70 else min(45.0, 24 + 0.6 * (t - 70))
            g = self.nodes[f.node].gpus[f.gpu]
            if g.power_limit <= 550:
                raw *= 0.45
            extra[(f.node, f.gpu)] = max(extra.get((f.node, f.gpu), 0.0), raw)
        for f in self._unresolved(kind="rack_cooling"):
            t = self.tick_count - f.start
            for nm, factor in f.flags.get("hot", {}).items():
                node = self.nodes[nm]
                for g in node.gpus:
                    raw = min(21.0, 0.45 * t) * factor
                    if g.power_limit <= 550:
                        raw *= 0.55
                    extra[(nm, g.index)] = max(extra.get((nm, g.index), 0.0), raw)
        return extra

    def _update_gpus(self, record: bool = True) -> None:
        extra = self._heat()
        rnd = self.rng.uniform
        for node in self.nodes.values():
            if node.local:
                continue  # real readings, see _sync_local
            rebooting = node.status == "rebooting"
            for g in node.gpus:
                if g.lost or rebooting:
                    g.util, g.clock, g.power, g.throttle = 0, 0, 0.0, "n/a"
                else:
                    job = self.jobs.get(g.job) if g.job else None
                    busy = job is not None and job.status == "running"
                    g.util = 97 + int(rnd(0, 2.99)) if busy else 0
                    clock, throttle = (MAX_CLOCK, "none") if busy else (IDLE_CLOCK, "none")
                    if busy and g.power_limit < DEFAULT_POWER_LIMIT:
                        cap_clock = int(MAX_CLOCK * max(0.35, (g.power_limit - 120) / 568))
                        if cap_clock < clock:
                            clock, throttle = cap_clock, "SW_POWER_CAP"
                    if g.temp >= SLOWDOWN_TEMP:
                        th_clock = int(max(1000, MAX_CLOCK - (g.temp - (SLOWDOWN_TEMP - 1)) * 110))
                        if th_clock < clock:
                            clock, throttle = th_clock, "HW_THERMAL_SLOWDOWN"
                    g.clock, g.throttle = clock, throttle
                    if busy:
                        g.power = min(120 + g.util * 5.8 * (clock / MAX_CLOCK), g.power_limit) + rnd(-6, 4)
                    else:
                        g.power = 72 + rnd(-3, 3)
                    target = 32 + 0.045 * g.power + 0.05 * g.util + extra.get((node.name, g.index), 0.0)
                    g.temp += (target - g.temp) * 0.35 + rnd(-0.5, 0.5)
                if record:
                    g.history.append((self.tick_count, round(g.temp), round(g.power), g.clock, g.throttle,
                                      g.sbe, g.dbe, g.nvlink_crc, g.lost, g.util))
        for rack in self.racks.values():
            if rack.hall == LOCAL_HALL:
                continue
            hot = [f for f in self._unresolved(kind="rack_cooling") if f.rack == rack.id]
            if hot:
                t = self.tick_count - hot[0].start
                rack.coolant_flow = max(58.0, 99 - 1.1 * t) + rnd(-0.6, 0.6)
                rack.coolant_return = 31.5 + min(9.5, 0.22 * t) + rnd(-0.2, 0.2)
            else:
                rack.coolant_flow = 99 + rnd(-0.6, 0.6)
                rack.coolant_return = 31.5 + rnd(-0.3, 0.3)

    def _advance_nodes(self) -> None:
        for node in self.nodes.values():
            if node.status != "rebooting":
                continue
            node.reboot_left -= 1
            if node.reboot_left > 0:
                continue
            node.status = "drained"
            permanent = node.name == self.down_node and self.has_open_ticket(node.name)
            for g in node.gpus:
                if not permanent:
                    g.lost = False
                g.sbe = 0
                g.remap_pending = False
                g.temp = 36.0
            for f in self._unresolved(node=node.name):
                if f.kind in ("bus", "ecc"):
                    f.status = "resolved"
            visible = sum(not g.lost for g in node.gpus)
            self._log(node.name, "kernel", "info" if visible == GPUS_PER_NODE else "error",
                      f"node rebooted: {visible}/{GPUS_PER_NODE} GPUs visible, driver loaded")
            self._log(node.name, "nightingale", "info",
                      f"{node.name} stays drained until it is returned to service")

    def _advance_fault(self, f: Fault) -> None:
        t = self.tick_count - f.start
        node = self.nodes[f.node]

        if f.kind == "rack_cooling":
            rack = self.racks[f.rack]
            if t % 5 == 2:
                self._log("facility", "bms", "warn",
                          f"{rack.cdu} coolant flow {rack.coolant_flow:.0f}% (normal 95-100%), return "
                          f"{rack.coolant_return:.1f}C (normal 30-33C); feeds rack {rack.id}")
            hot_nodes = list(f.flags.get("hot", {}))
            if t % 6 == 4 and hot_nodes:
                nm = hot_nodes[(t // 6) % len(hot_nodes)]
                g = max(self.nodes[nm].gpus, key=lambda x: x.temp)
                if g.throttle == "HW_THERMAL_SLOWDOWN":
                    self._log(nm, "dcgm", "warn", f"GPU {g.index} clocks reduced to {g.clock} MHz by "
                                                 f"HW_THERMAL_SLOWDOWN at {g.temp:.0f}C")
                elif g.temp >= 78:
                    self._log(nm, "dcgm", "warn", f"GPU {g.index} temperature {g.temp:.0f}C, "
                                                 f"{g.temp - 68:.0f}C above this server's usual load temperature")
            return

        g = node.gpus[f.gpu]
        if f.kind == "thermal":
            if g.lost:
                return
            if g.temp >= WATCH_TEMP and not f.flags.get("warned"):
                f.flags["warned"] = True
                self._log(node.name, "dcgm", "warn",
                          f"GPU {g.index} temperature {g.temp:.0f}C and rising (slowdown threshold {SLOWDOWN_TEMP}C)")
            if g.throttle == "HW_THERMAL_SLOWDOWN" and t % 3 == 0:
                self._log(node.name, "dcgm", "warn",
                          f"GPU {g.index} clocks reduced to {g.clock} MHz by HW_THERMAL_SLOWDOWN at {g.temp:.0f}C")
            if g.temp >= SHUTDOWN_TEMP:
                self._log(node.name, "kernel", "error",
                          f"NVRM: GPU at PCI:{g.pci} has exceeded its thermal shutdown limit")
                self._lose_gpu(node, g)

        elif f.kind == "ecc":
            if f.flags.get("dbe"):
                return
            delta = self.rng.randint(25, 70)
            g.sbe += delta
            if t % 2 == 0:
                self._log(node.name, "dcgm", "warn",
                          f"GPU {g.index} volatile single-bit ECC errors: {g.sbe} (+{delta})")
            if t >= 6 and not f.flags.get("xid92"):
                f.flags["xid92"] = True
                self._xid(node, g, 92, "High single-bit ECC error rate", level="warn")
            if t >= 60:  # two minutes: time to move the job before it crashes
                f.flags["dbe"] = True
                g.dbe += 1
                g.remap_pending = True
                self._xid(node, g, 48, "An uncorrectable double bit error (DBE) has been detected "
                          "on GPU in the framebuffer at partition 2, subpartition 1.",
                          pid=self.rng.randint(20000, 90000), name="python")
                self._xid(node, g, 63, "Row Remapper: New row marked for remapping, reset gpu to activate.")
                if g.job:
                    self._crash_job(g.job, node.name, g.index,
                                    "RuntimeError: CUDA error: uncorrectable ECC error encountered")

        elif f.kind == "bus":
            if not f.flags.get("lost"):
                f.flags["lost"] = True
                self._lose_gpu(node, g)

        elif f.kind == "nvlink":
            job = self.jobs.get(g.job) if g.job else None
            busy = job is not None and job.status == "running" and node.status != "rebooting"
            if busy:
                g.nvlink_crc += self.rng.randint(120, 480)
                f.flags["busy"] = f.flags.get("busy", 0) + 1
            if t >= 3 and not f.flags.get("xid74"):
                f.flags["xid74"] = True
                self._xid(node, g, 74, "NVLink: fatal error detected on link 6 (0x10000, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0)")
            if busy and f.flags["busy"] in (5, 12, 20, 30):
                r0 = self.rank_of(job, node.name, 0)
                self._log(job.nodes[0], "trainer", "warn",
                          f"trainer[{job.id}] allreduce bus bandwidth {self.rng.randint(90, 140)} GB/s on "
                          f"ranks {r0}-{r0 + 7} (baseline 370 GB/s)")
            if busy and f.flags["busy"] >= 45:
                f.flags["busy"] = 0
                rank = self.rank_of(job, node.name, g.index)
                self._log(node.name, "trainer", "error",
                          f"trainer[{job.id}] [rank {rank}] NCCL watchdog timeout: collective ALLREDUCE "
                          f"(SeqNum=88213) ran for 600000 ms before timing out")
                self._crash_job(job.id, node.name, g.index,
                                "ProcessGroupNCCL: aborting communicator after watchdog timeout")

        elif f.kind == "app_bug":
            job = self.jobs.get(f.job)
            if job and not f.flags.get("crashed"):
                f.flags["crashed"] = True
                self._app_bug_crash(job, node, g)

    def _lose_gpu(self, node: Node, g: GPU) -> None:
        if g.lost:
            return
        g.lost = True
        self._xid(node, g, 79, "GPU has fallen off the bus.")
        self._log(node.name, "nvidia-smi", "error",
                  f"Unable to determine the device handle for GPU{g.pci}.0: Unknown Error")
        self._log(node.name, "dcgm", "error", f"GPU {g.index} health check failed: DCGM_ST_GPU_IS_LOST")
        if g.job:
            self._crash_job(g.job, node.name, g.index, "NCCL error: unhandled system error, "
                            "remote process exited or there was a network error")

    def _app_bug_crash(self, job: Job, node: Node, g: GPU) -> None:
        self._xid(node, g, 31, "MMU Fault: ENGINE GRAPHICS GPC2 GPCCLIENT_T1_3 faulted @ 0x7f3a_2c41e000. "
                  "Fault is of type FAULT_PDE ACCESS_TYPE_VIRT_READ",
                  pid=self.rng.randint(20000, 90000), name="python")
        self._log(node.name, "trainer", "error",
                  f"trainer[{job.id}] RuntimeError: CUDA error: an illegal memory access was encountered")
        self._log(node.name, "trainer", "error",
                  f'trainer[{job.id}]   File "train.py", line 214, in mine_hard_negatives: neg = emb[idx[:, k]]')
        job.status, job.owner_notified = "failed", False
        job.last_error = "CUDA error: an illegal memory access was encountered (train.py line 214)"
        rank = self.rank_of(job, node.name, g.index)
        self._log(job.nodes[0], "torchrun", "error",
                  f"torchrun[{job.id}]: worker group failed (rank {rank} exited with code 1) at step {int(job.step)}")

    def _crash_job(self, job_id: str, node_name: str, gpu: int, error: str) -> None:
        job = self.jobs.get(job_id)
        if not job or job.status not in ("running", "restarting"):
            return
        job.status, job.last_error, job.owner_notified = "crashed", error, False
        rank = self.rank_of(job, node_name, gpu)
        self._log(node_name, "trainer", "error", f"trainer[{job.id}] [rank {rank}] {error}")
        self._log(job.nodes[0], "torchrun", "error",
                  f"torchrun[{job.id}]: worker group failed (rank {rank} exited with code 1) at step "
                  f"{int(job.step)}; last checkpoint step {job.checkpoint}")

    def _check_job_on_start(self, job: Job) -> None:
        """A restarted job hits the same problem again if the cause is still there."""
        for node, g in self._job_gpus(job):
            if g.lost:
                self._crash_job(job.id, node.name, g.index, "CUDA error: no CUDA-capable device is detected")
                return
            if g.remap_pending:
                g.dbe += 1
                self._xid(node, g, 48, "An uncorrectable double bit error (DBE) has been detected "
                          "on GPU in the framebuffer at partition 2, subpartition 1.")
                self._crash_job(job.id, node.name, g.index, "RuntimeError: CUDA error: uncorrectable ECC error encountered")
                return
        for f in self._unresolved(kind="app_bug", job=job.id):
            node = self.nodes[job.nodes[0]]
            self._app_bug_crash(job, node, node.gpus[f.gpu])
            return

    def _advance_jobs(self) -> None:
        nvlink_jobs = {self.nodes[f.node].gpus[f.gpu].job for f in self._unresolved(kind="nvlink")}
        for job in self.jobs.values():
            if job.status == "restarting":
                job.restart_left -= 1
                if job.restart_left <= 0:
                    job.status = "running"
                    self._log(job.nodes[0], "torchrun", "info",
                              f"torchrun[{job.id}]: all {len(job.nodes) * GPUS_PER_NODE:,} ranks joined; "
                              f"resumed from checkpoint step {job.checkpoint}")
                    self._check_job_on_start(job)
                continue
            if job.status != "running":
                job.rate = 0.0
                continue
            slow_node, slow_g, min_clock = None, None, MAX_CLOCK + 1
            for nm in job.nodes:
                for g in self.nodes[nm].gpus:
                    if g.job == job.id and not g.lost and g.clock < min_clock:
                        slow_node, slow_g, min_clock = nm, g, g.clock
            rate = min_clock / MAX_CLOCK if slow_g else 0.0
            if job.id in nvlink_jobs:
                rate = min(rate, 0.55)
            job.rate = rate
            before = int(job.step)
            job.step += 5 * rate
            job.loss = max(0.25, job.loss - 0.0002 * rate + self.rng.uniform(-0.002, 0.002))
            if int(job.step) // job.checkpoint_every > before // job.checkpoint_every:
                job.checkpoint = (int(job.step) // job.checkpoint_every) * job.checkpoint_every
                self._log(job.nodes[0], "trainer", "info", f"trainer[{job.id}] checkpoint saved at step {job.checkpoint}")
            if self.tick_count % 5 == 0:
                self._log(job.nodes[0], "trainer", "info",
                          f"trainer[{job.id}] step {int(job.step)} loss {job.loss:.3f} "
                          f"throughput {int(job.tokens_per_gpu * rate):,} tok/s/gpu on {len(job.nodes) * 8:,} GPUs")
                if slow_g is not None and min_clock < MAX_CLOCK * 0.9:
                    rank = self.rank_of(job, slow_node, slow_g.index)
                    self._log(job.nodes[0], "trainer", "warn",
                              f"trainer[{job.id}] step time {1.2 / max(rate, 0.05):.2f}s (baseline 1.20s); "
                              f"rank {rank} is the slowest rank in allreduce")

    def _routine_logs(self) -> None:
        if self.tick_count % 5:
            return
        for hall in self.halls:
            if hall == LOCAL_HALL:
                continue  # this computer logs its own readings
            gpus = [(n, g) for n in self.nodes.values() if n.hall == hall and n.status != "rebooting" for g in n.gpus]
            visible = [(n, g) for n, g in gpus if not g.lost]
            known = sum(1 for n, g in gpus if g.lost and self.has_open_ticket(n.name))
            hot_n, hot_g = max(visible, key=lambda x: x[1].temp)
            level = "info" if len(visible) + known == len(gpus) else "warn"
            note = f" ({known} known, ticketed)" if known else ""
            self._log(f"hall-{hall}", "dcgm", level,
                      f"health check: {len(visible):,}/{len(gpus):,} GPUs visible{note}, max temp {hot_g.temp:.0f}C "
                      f"({hot_n.name} GPU {hot_g.index}), power {sum(g.power for _, g in visible) / 1000:,.0f} kW")

    # ---------------------------------------------------------- what AI sees
    def _anomalies(self, update_counters: bool) -> list[tuple]:
        """GPUs that differ from a healthy one, with counter changes since the last read."""
        rows = []
        for node in self.nodes.values():
            if node.status == "rebooting":
                continue
            for g in node.gpus:
                key = (node.name, g.index)
                prev = self._last_counters.get(key, (g.sbe, g.dbe, g.nvlink_crc))
                if update_counters:
                    self._last_counters[key] = (g.sbe, g.dbe, g.nvlink_crc)
                d = (g.sbe - prev[0], g.dbe - prev[1], g.nvlink_crc - prev[2])
                if node.local:
                    if self.local_odd(g):
                        rows.append((node, g, d))
                    continue
                odd = (g.lost or g.temp >= 80 or g.throttle == "HW_THERMAL_SLOWDOWN" or any(d) or g.dbe
                       or g.remap_pending)
                if odd:
                    rows.append((node, g, d))
        return rows

    def _working(self, g: GPU) -> bool:
        job = self.jobs.get(g.job) if g.job else None
        return job is not None and job.status == "running" and not g.lost and g.util > 0

    def rack_heat_outliers(self) -> list[str]:
        """Racks whose working GPUs run clearly hotter than working GPUs elsewhere."""
        temps = {}
        for rack in self.racks.values():
            busy = [g.temp for nm in rack.nodes for g in self.nodes[nm].gpus
                    if self.nodes[nm].status != "rebooting" and self._working(g)]
            if len(busy) >= 16:
                temps[rack.id] = sum(busy) / len(busy)
        if len(temps) < 2:
            return []
        out = []
        for rid, avg in temps.items():
            others = [t for r, t in temps.items() if r != rid]
            fleet = sum(others) / len(others)
            hottest = max(g.temp for nm in self.racks[rid].nodes for g in self.nodes[nm].gpus if not g.lost)
            if avg >= fleet + 4 and hottest >= 78:
                hot = sum(1 for nm in self.racks[rid].nodes
                          if any(self._working(g) and g.temp >= fleet + 6 for g in self.nodes[nm].gpus))
                out.append(f"rack {rid}: busy GPUs average {avg:.0f}C vs fleet {fleet:.0f}C; "
                           f"{hot} of {len(self.racks[rid].nodes)} servers hot")
        return out

    def digest(self, since_log_id: int, open_incidents: str = "none") -> tuple[str, int]:
        """Compact window for the triage model. Its size depends on what is wrong, not on fleet size."""
        with self.lock:
            sim = self.sim_nodes()
            visible = sum(not g.lost for n in sim for g in n.gpus)
            drained = [n.name for n in sim if n.status == "drained"]
            rebooting = [n.name for n in sim if n.status == "rebooting"]
            out = [f"Window ending tick {self.tick_count} ({_now().strftime('%H:%M:%S')} UTC)",
                   f"Fleet: {self.size:,} GPUs in {len(sim)} servers, {len(self.racks) - (LOCAL_RACK in self.racks)} racks; "
                   f"{visible:,} GPUs visible; drained: {', '.join(drained) or 'none'}; "
                   f"rebooting: {', '.join(rebooting) or 'none'}",
                   f"Known issues already handled (ignore): {self.known_issues() or 'none'}",
                   f"Open incidents (already being investigated): {open_incidents}",
                   "Jobs:"]
            if LOCAL_NODE in self.nodes:
                n = self.nodes[LOCAL_NODE]
                out.insert(2, f"Also watching this computer's own GPU: server {LOCAL_NODE} ({n.model}), real "
                              f"nvidia-smi readings. Laptop GPUs run 75-87C under load by design; "
                              f"flag it when clocks are cut for heat, it disappears, memory fills, or the driver crashes."
                              + (f" Right now {self.local_activity}." if self.local_activity else ""))
            for j in self.jobs.values():
                out.append(f"  {j.id}: {j.status}, {len(j.nodes)} servers ({len(j.nodes) * 8:,} GPUs), "
                           f"speed {round(j.rate * 100)}% of baseline" + (f", last error: {j.last_error}" if j.last_error else ""))
            rows = self._anomalies(update_counters=True)
            out.append("GPUs that differ from healthy (change since last window in brackets):")
            if not rows:
                out.append("  none: every visible GPU is within normal ranges")
            for node, g, d in rows[:40]:
                if node.local:
                    out.append(self._local_row(node, g))
                    continue
                if g.lost:
                    out.append(f"  {node.name} [{node.rack}, {node.status}] g{g.index} LOST (not visible on PCIe bus)")
                    continue
                out.append(f"  {node.name} [{node.rack}, {node.status}] g{g.index} temp={g.temp:.0f}C "
                           f"power={g.power:.0f}W/{g.power_limit}W clock={g.clock}MHz throttle={g.throttle} "
                           f"sbe={g.sbe}(+{d[0]}) dbe={g.dbe}(+{d[1]}) nvlink_crc={g.nvlink_crc}(+{d[2]}) "
                           f"remap_pending={'yes' if g.remap_pending else 'no'}")
            if len(rows) > 40:
                out.append(f"  ... {len(rows) - 40} more")
            capped: dict = {}
            for n in self.sim_nodes():
                for g in n.gpus:
                    if g.power_limit < DEFAULT_POWER_LIMIT and not g.lost:
                        capped.setdefault(n.rack, []).append(f"{n.name} g{g.index}")
            if capped:
                out.append("Power-capped on purpose (normal, ignore SW_POWER_CAP there): " + "; ".join(
                    f"rack {r}: {len(v)} GPUs" if len(v) > 4 else ", ".join(v) for r, v in capped.items()))
            racks = self.rack_heat_outliers()
            out.append("Racks running hotter than the fleet: " + ("; ".join(racks) if racks else "none"))
            events = [l for l in self.logs if l["id"] > since_log_id and l["level"] != "info"]
            out.append("New warning/error log lines:" if events else "New warning/error log lines: none")
            out.extend(f"  {l['t']} {l['node']} {l['source']}: {l['text']}" for l in events[-40:])
            # What this window points at. When all of it is already handled, the agent skips the model call.
            self.window_signal = ({node.name for node, _g, _d in rows} | {r.split(":")[0][5:] for r in racks}
                                  | {l["node"] for l in events})
            return "\n".join(out), self._log_id

    def known_issues(self) -> str:
        parts = []
        for t in self.tickets:
            if t["status"] == "open":
                where = t["node"] or f"rack {t['rack']}"
                parts.append(f"{where} ({t['id']}: {t['summary'][:70]})")
        return "; ".join(parts)

    def target_summary(self, target: str) -> str:
        """A short starting brief about a server or rack for the investigator."""
        with self.lock:
            if target == LOCAL_NODE and target in self.nodes:
                n = self.nodes[target]
                return (f"Server {LOCAL_NODE} is this computer: {len(n.gpus)} x {n.model}, NVIDIA driver {n.driver or 'unknown'}, "
                        "read through nvidia-smi every 2 s. Plan only pc_ fixes and recommend steps for it (see the "
                        "catalog); every fix runs only after the person approves it. No data center jobs run here. "
                        f"GPU limits: power {n.gpus[0].power_limit} W, max SM clock {n.gpus[0].max_clock} MHz.")
            if target in self.racks:
                rack = self.racks[target]
                jobs = sorted(self.jobs_on(target))
                return (f"Rack {rack.id} in hall {rack.hall}: servers {rack.nodes[0]} to {rack.nodes[-1]}, "
                        f"cooled by {rack.cdu}. Jobs on it: {', '.join(jobs) or 'none'}.")
            node = self.nodes[target]
            jobs = sorted(self.jobs_on(target))
            return (f"Server {node.name} in rack {node.rack} (hall {node.hall}), status {node.status}. "
                    f"Jobs on it: {', '.join(jobs) or 'none'}. PCI map: "
                    + ", ".join(f"g{g.index}={g.pci}" for g in node.gpus))

    # --------------------------------------------------- investigation tools
    TOOLS = {
        "node_status": "node_status(node): every GPU on one server right now, its rack, status and jobs",
        "gpu_history": "gpu_history(node, gpu): the last 15 telemetry samples of one GPU, oldest first",
        "search_logs": "search_logs(query, node=None): up to 20 recent log lines containing the text, optionally on one server or rack",
        "compare_rack": "compare_rack(rack): each server in a rack against the fleet (temperature, power, clocks, errors). Pass a rack id or a server name",
        "job_info": "job_info(job): a training job's servers, status, speed, checkpoint, slowest rank and last error",
        "rank_location": "rank_location(job, rank): which server and GPU run one rank of a job",
        "find_spares": "find_spares(count=1, avoid_rack=None): idle healthy servers that can take work",
        "incident_history": "incident_history(target, gpu=None): past incidents on a server or rack, or one GPU of a server (fleet memory)",
        "facility_sensors": "facility_sensors(rack): the coolant loop (CDU) feeding a rack: flow and temperatures",
        "gpu_processes": "gpu_processes(node): the programs using the GPUs of this computer (server 'laptop') right now",
        "selftest_results": "selftest_results(node): this computer's recent GPU self-tests (quick check, memory test, "
                            "stress test) and what they found",
    }
    # Extra read-only tools for "Ask your data center": whole-fleet questions.
    ASK_TOOLS = {
        "fleet_overview": "fleet_overview(): the whole data center right now: GPU health counts, power against "
                          "the budget, the hottest racks, every job, open repair tickets, and this computer's GPU",
        "top_gpus": "top_gpus(metric='temp', count=5): the GPUs highest on one metric across the fleet; "
                    "metric is temp, power, sbe (corrected memory errors), nvlink_crc or util",
    }

    def run_tool(self, name: str, args: dict) -> str:
        args = args if isinstance(args, dict) else {}
        fn = getattr(self, f"_tool_{name}", None) if name in self.TOOLS or name in self.ASK_TOOLS else None
        if fn is None:
            return f"Unknown tool '{name}'. Available: {', '.join(self.TOOLS)}."
        with self.lock:
            try:
                return fn(**{str(k): v for k, v in args.items() if v is not None})
            except TypeError as exc:
                return f"Bad arguments for {name}: {exc}. Usage: {self.TOOLS.get(name) or self.ASK_TOOLS[name]}"
            except Exception as exc:  # a tool must never take the agent down
                return f"{name} failed: {exc}"

    def _rack_temps(self) -> list[tuple[str, float, int]]:
        """(rack, average temperature of its working GPUs, how many) for racks with real load."""
        out = []
        for rack in self.racks.values():
            if rack.id == LOCAL_RACK:
                continue
            busy = [g.temp for nm in rack.nodes for g in self.nodes[nm].gpus
                    if self.nodes[nm].status != "rebooting" and self._working(g)]
            if busy:
                out.append((rack.id, sum(busy) / len(busy), len(busy)))
        return sorted(out, key=lambda r: -r[1])

    def _tool_fleet_overview(self, **_) -> str:
        g = self.gauges()
        sim = self.sim_nodes()
        drained = [n.name for n in sim if n.status == "drained"]
        racks = self._rack_temps()
        lines = [f"Data center: {self.size:,} GPUs in {len(sim)} servers, {len(self.racks) - (LOCAL_RACK in self.racks)} "
                 f"racks, halls {', '.join(h for h in self.halls if h != 'L')}.",
                 f"GPU health: {g['healthy']:,} healthy, {g['watch']} under watch, {g['lost']} down "
                 f"(not visible on the bus). Spare servers ready: {len(self.spare_nodes())}. "
                 f"Drained: {', '.join(drained) if drained else 'none'}.",
                 f"Power: {g['power_kw']:,} kW of a {g['budget_kw']:,} kW budget "
                 f"({round(100 * g['power_kw'] / max(1, g['budget_kw']))}%).",
                 f"Hottest GPU: {g['hottest']['node']} GPU {g['hottest']['gpu']} at {g['hottest']['temp']}C."]
        flagged = self.watch_list()
        lines.append("Down or under watch: " + ("; ".join(flagged) if flagged else "none") + ".")
        if racks:
            avg = sum(t * n for _, t, n in racks) / sum(n for _, _, n in racks)
            lines.append(f"Busy GPUs average {avg:.0f}C. Hottest racks by busy-GPU average: " +
                         ", ".join(f"{rid} {t:.0f}C ({n} busy GPUs)" for rid, t, n in racks[:5]) + ".")
        lines.append("Jobs: " + "; ".join(
            f"{j.id} ({j.owner}) {j.status}, {len(j.nodes)} servers, speed {round(j.rate * 100)}%"
            + (f", last error: {j.last_error}" if j.last_error else "") for j in self.jobs.values()) + ".")
        open_t = [t for t in self.tickets if t["status"] == "open"]
        lines.append("Open repair tickets: " + ("; ".join(f"{t['id']} {t['summary'].rstrip('.')}" for t in open_t) or "none") + ".")
        loc = self.local_status()
        if loc:
            n = self.nodes.get(LOCAL_NODE)
            gl = n.gpus[0] if n and n.gpus else None
            lines.append(f"This computer ('{LOCAL_NODE}'): {loc['name']}, state {loc['state']}" +
                         (f", {gl.temp:.0f}C, load {gl.util}%" if gl and not gl.lost else "") + ".")
        else:
            lines.append("This computer's GPU is not connected.")
        return "\n".join(lines)

    def _tool_top_gpus(self, metric="temp", count=5, **_) -> str:
        metric = str(metric or "temp").lower().replace(" ", "_")
        aliases = {"temperature": "temp", "ecc": "sbe", "memory_errors": "sbe", "nvlink": "nvlink_crc",
                   "load": "util", "utilization": "util", "watts": "power"}
        metric = aliases.get(metric, metric)
        if metric not in ("temp", "power", "sbe", "nvlink_crc", "util"):
            return "metric must be one of temp, power, sbe, nvlink_crc, util."
        count = max(1, min(int(count or 5), 20))
        rows = [(getattr(g, metric), n, g) for n in self.nodes.values() for g in n.gpus
                if not g.lost and n.status != "rebooting"]
        rows.sort(key=lambda r: -r[0])
        unit = {"temp": "C", "power": " W", "util": "%"}.get(metric, "")
        return f"Top {count} GPUs by {metric}:\n" + "\n".join(
            f"  {n.name} GPU {g.index} ({'this computer' if n.local else 'rack ' + n.rack}): {metric}={v:.0f}{unit}"
            + ("" if metric == "temp" else f", temp {g.temp:.0f}C") + f", load {g.util}%, job {g.job or 'none'}"
            for v, n, g in rows[:count])

    def _node_or_error(self, node) -> tuple[Node | None, str]:
        canon = self.canonical_node(node)
        if not canon:
            return None, f"No server called '{node}'. Servers are named like dgx-0053."
        return self.nodes[canon], ""

    def _tool_node_status(self, node, **_) -> str:
        n, err = self._node_or_error(node)
        if err:
            return err
        if n.local:
            return self._local_status(n)
        lines = [f"{n.name}: rack {n.rack}, hall {n.hall}, status {n.status}, jobs {', '.join(sorted(self.jobs_on(n.name))) or 'none'}"]
        for g in n.gpus:
            if g.lost or n.status == "rebooting":
                lines.append(f"  g{g.index} {'LOST, not visible on the PCIe bus' if g.lost else 'rebooting'}")
                continue
            lines.append(f"  g{g.index} temp={g.temp:.0f}C power={g.power:.0f}/{g.power_limit}W clock={g.clock}MHz "
                         f"util={g.util}% throttle={g.throttle} sbe={g.sbe} dbe={g.dbe} nvlink_crc={g.nvlink_crc} "
                         f"remap_pending={'yes' if g.remap_pending else 'no'} job={g.job or 'idle'}")
        return "\n".join(lines)

    def _tool_gpu_history(self, node, gpu=0, **_) -> str:
        n, err = self._node_or_error(node)
        if err:
            return err
        try:
            gi = int(gpu)
            g = n.gpus[gi]
        except (ValueError, IndexError):
            return f"gpu must be 0-{len(n.gpus) - 1}"
        lines = [f"{n.name} g{gi}, one sample every 2 s:"]
        for h in list(g.history)[-15:]:
            tick, temp, power, clock, thr, sbe, dbe, nvl, lost = h[:9]
            util = h[9] if len(h) > 9 else 0
            if lost:
                lines.append(f"  tick {tick}: LOST")
            elif n.local:
                lines.append(f"  tick {tick}: temp={temp}C power={power}W clock={clock}MHz load={util}% throttle={thr}")
            else:
                lines.append(f"  tick {tick}: temp={temp}C power={power}W clock={clock}MHz throttle={thr} "
                             f"sbe={sbe} dbe={dbe} nvlink_crc={nvl}")
        return "\n".join(lines)

    def _tool_search_logs(self, query="", node=None, **_) -> str:
        q = str(query).lower()
        where = None
        if node:
            where = self.canonical_node(node) or self.canonical_rack(node)
        rack_nodes = (set(self.racks[where].nodes) | {"facility", f"rack-{where}"}) if where in self.racks else None
        hits = []
        for l in reversed(self.logs):
            if where and ((rack_nodes is not None and l["node"] not in rack_nodes) or
                          (rack_nodes is None and l["node"] != where)):
                continue
            if q and q not in f"{l['node']} {l['source']} {l['text']}".lower():
                continue
            hits.append(f"  {l['t']} {l['node']} {l['source']}: {l['text']}")
            if len(hits) >= 20:
                break
        if not hits:
            return f"No log lines match '{query}'" + (f" on {where}" if where else "") + "."
        return f"{len(hits)} most recent matching lines (newest first):\n" + "\n".join(hits)

    def _tool_compare_rack(self, rack=None, node=None, **_) -> str:
        target = rack or node
        rid = self.canonical_rack(target)
        if not rid and self.canonical_node(target):
            rid = self.nodes[self.canonical_node(target)].rack
        if not rid:
            return f"No rack or server called '{target}'."
        if rid == LOCAL_RACK:
            return (f"{LOCAL_NODE} is a single computer, not part of a data center rack. Compare it with its own "
                    "recent readings instead (gpu_history), and check what is using it (gpu_processes).")
        fleet = [g.temp for n in self.nodes.values() for g in n.gpus if g.job and not g.lost and n.status == "active"]
        favg = sum(fleet) / len(fleet) if fleet else 0
        lines = [f"Rack {rid} ({self.racks[rid].cdu}); busy GPUs across the fleet average {favg:.0f}C:"]
        for nm in self.racks[rid].nodes:
            n = self.nodes[nm]
            vis = [g for g in n.gpus if not g.lost]
            if not vis or n.status == "rebooting":
                lines.append(f"  {nm} [{n.status}] no telemetry")
                continue
            errs = sum(g.sbe for g in vis), sum(g.dbe for g in vis), sum(g.nvlink_crc for g in vis)
            lost = len(n.gpus) - len(vis)
            lines.append(f"  {nm} [{n.status}, {'idle' if all(g.job is None for g in n.gpus) else 'busy'}] "
                         f"avg {sum(g.temp for g in vis) / len(vis):.0f}C max {max(g.temp for g in vis):.0f}C "
                         f"power {sum(g.power for g in vis) / 1000:.1f} kW min clock {min(g.clock for g in vis)}MHz "
                         f"sbe={errs[0]} dbe={errs[1]} nvlink_crc={errs[2]}" + (f" lost_gpus={lost}" if lost else ""))
        return "\n".join(lines)

    def _tool_job_info(self, job=None, **_) -> str:
        j = self.jobs.get(str(job))
        if not j:
            return f"No job '{job}'. Jobs: {', '.join(self.jobs)}."
        slow = None
        for nm in j.nodes:
            for g in self.nodes[nm].gpus:
                if g.job == j.id and not g.lost and (slow is None or g.clock < slow[1].clock):
                    slow = (nm, g)
        lines = [f"{j.id}: owner {j.owner}, status {j.status}, {len(j.nodes)} servers ({len(j.nodes) * 8:,} GPUs, "
                 f"ranks 0-{len(j.nodes) * 8 - 1}), first server {j.nodes[0]}, last {j.nodes[-1]}",
                 f"  step {int(j.step)}, last checkpoint {j.checkpoint} (every {j.checkpoint_every} steps), "
                 f"speed {round(j.rate * 100)}% of baseline",
                 f"  rank r runs on the job's server number r // 8, GPU r % 8"]
        if slow and j.status == "running":
            lines.append(f"  slowest rank {self.rank_of(j, slow[0], slow[1].index)} on {slow[0]} GPU {slow[1].index} "
                         f"at {slow[1].clock}MHz")
        lines.append(f"  last error: {j.last_error or 'none'}; owner notified: {'yes' if j.owner_notified else 'no'}")
        return "\n".join(lines)

    def _tool_rank_location(self, job=None, rank=0, **_) -> str:
        j = self.jobs.get(str(job))
        if not j:
            return f"No job '{job}'."
        try:
            r = int(rank)
        except ValueError:
            return "rank must be a number"
        if not 0 <= r < len(j.nodes) * 8:
            return f"{j.id} has ranks 0-{len(j.nodes) * 8 - 1}."
        nm = j.nodes[r // 8]
        return f"{j.id} rank {r} runs on {nm} (rack {self.nodes[nm].rack}) GPU {r % 8}."

    def _tool_find_spares(self, count=1, avoid_rack=None, **_) -> str:
        spares = self.spare_nodes(self.canonical_rack(avoid_rack) if avoid_rack else None)
        try:
            k = max(1, min(8, int(count)))
        except ValueError:
            k = 1
        if not spares:
            return "No idle healthy servers right now."
        return (f"{len(spares)} idle healthy servers; first {min(k, len(spares))}: "
                + ", ".join(f"{n.name} (rack {n.rack})" for n in spares[:k]))

    def _tool_incident_history(self, target=None, node=None, gpu=None, **_) -> str:
        t = str(target or node or "").strip()
        # Models often name a GPU inside the target: "dgx-0034 GPU 2", "dgx-0034:2", "dgx-0034/g2".
        m = re.match(r"^(.*?)(?:\s*[:/,]\s*|\s+)(?:gpu\s*|g)?(\d{1,2})$", t, re.I)
        if m and not self.canonical_target(t) and self.canonical_target(m.group(1)):
            t, gpu = m.group(1), int(m.group(2))
        canon = self.canonical_target(t)
        if not canon:
            return f"No server or rack called '{t}'. Pass a server (dgx-0053) or a rack (A-11), and a gpu number if you want one GPU."
        gpu = int(gpu) if str(gpu).strip().isdigit() else None
        rows = [h for h in self.history if (h["target"] == canon or
                (canon in self.racks and h["target"] in self.racks[canon].nodes))
                and (gpu is None or h.get("gpu") in (None, gpu))]
        where = f"{canon} GPU {gpu}" if gpu is not None else canon
        if not rows:
            return f"No incidents on {where} in the last 30 days."
        return f"{len(rows)} incidents on {where} in the last 30 days:\n" + "\n".join(
            f"  {h['when']}: {h['target']}"
            f"{' GPU ' + str(h['gpu']) if h.get('gpu') is not None and not str(h['title']).startswith('GPU') else ''} "
            f"{h['title']}. Fix: {h['fix']}" for h in rows[-8:])

    def _tool_facility_sensors(self, rack=None, node=None, **_) -> str:
        t = rack or node
        rid = self.canonical_rack(t) or (self.nodes[self.canonical_node(t)].rack if self.canonical_node(t) else None)
        if not rid:
            return f"No rack called '{t}'."
        if rid == LOCAL_RACK:
            fans = ", ".join(f"g{g.index} fan {g.fan}%" if g.fan is not None else f"g{g.index} fan not reported"
                             for g in self.nodes[LOCAL_NODE].gpus)
            return (f"{LOCAL_NODE} has no facility coolant loop: it is cooled by its own fans ({fans}). "
                    "Laptops share one cooling system between the CPU and the GPU.")
        r = self.racks[rid]
        return (f"{r.cdu} feeding rack {rid}: flow {r.coolant_flow:.0f}% (normal 95-100%), supply "
                f"{r.coolant_supply:.1f}C (normal 24-26C), return {r.coolant_return:.1f}C (normal 30-33C).")

    def _tool_selftest_results(self, node=None, **_) -> str:
        if not self.selftests:
            return ("No GPU self-tests have run on this computer yet. The person can run them from My computer: a quick "
                    "check, a memory test, and a stress test.")
        lines = []
        for t in self.selftests[:5]:
            age = round((time.time() - t["at"]) / 60)
            lines.append(f"  {t['label']} {age} min ago: {'PASSED' if t['ok'] else 'FAILED'}. {t['summary']}")
        return "Recent self-tests on this computer (newest first):\n" + "\n".join(lines)

    def _tool_gpu_processes(self, node=None, **_) -> str:
        n, err = self._node_or_error(node or LOCAL_NODE)
        if err:
            return err
        if not n.local:
            return (f"{n.name} is a data center server: it runs scheduled jobs, not desktop programs. "
                    "Use job_info for what runs there.")
        if self.local is not None:
            self.local.refresh_processes()
        procs = self.local.snapshot()["processes"] if self.local is not None else []
        if not procs:
            return f"No programs are using the GPU on {n.name} right now (or the driver does not report them)."
        return f"Programs using the GPU on {n.name}:\n" + "\n".join(
            f"  pid {p['pid']} {p['name']} ({'compute' if 'C' in p['type'] else 'graphics'})"
            + (f", {p['mem_mib']} MiB" if p.get("mem_mib") is not None else "") for p in procs)

    # ---------------------------------------------------------- verification
    def observed_issues(self, target: str) -> list[str]:
        """Problems visible right now, used to verify a fix and in dry runs."""
        with self.lock:
            if target in self.racks:
                rack = self.racks[target]
                issues = []
                if rack.coolant_flow < 90:
                    issues.append(f"{rack.cdu} coolant flow is {rack.coolant_flow:.0f}%")
                hot = [nm for nm in rack.nodes if any(g.throttle == "HW_THERMAL_SLOWDOWN" or g.temp >= 85
                                                     for g in self.nodes[nm].gpus if not g.lost)]
                if hot:
                    issues.append(f"{len(hot)} servers in rack {target} are running hot")
                return issues
            node = self.nodes[target]
            if node.local:
                return self._local_issues(node)
            if node.status == "rebooting":
                return ["server is still rebooting"]
            issues = []
            for g in node.gpus:
                if g.lost:
                    issues.append(f"GPU {g.index} is not visible on the bus")
                if g.remap_pending:
                    issues.append(f"GPU {g.index} still has a pending row remap")
                if g.throttle == "HW_THERMAL_SLOWDOWN" or g.temp >= 85:
                    issues.append(f"GPU {g.index} is running hot ({g.temp:.0f}C)")
                hist = list(g.history)
                if len(hist) >= 5 and g.nvlink_crc > hist[-5][7]:
                    issues.append(f"GPU {g.index} NVLink errors are still increasing")
                if len(hist) >= 3 and g.sbe > hist[-3][5] and not any(
                        f.kind == "ecc" and f.flags.get("dbe") for f in self._unresolved(node=target)):
                    issues.append(f"GPU {g.index} memory errors are still increasing")
            for j in self.jobs.values():
                if target in j.nodes and j.status in ("crashed", "failed") and not j.owner_notified:
                    issues.append(f"{j.id} is {j.status}")
            return issues

    def has_open_ticket(self, target: str) -> bool:
        return any(t["status"] == "open" and (t["node"] == target or (not t["node"] and t["rack"] == target))
                   for t in self.tickets)

    # ---------------------------------------------------------- operations
    def set_power_limit(self, node: str, gpu: int, watts: int) -> tuple[bool, str]:
        with self.lock:
            g = self.nodes[node].gpus[gpu]
            if g.lost:
                return False, f"GPU {gpu} on {node} is not visible on the bus, so its power limit can't be changed."
            g.power_limit = max(300, min(DEFAULT_POWER_LIMIT, int(watts)))
            self._log(node, "nvidia-smi", "info", f"power limit for GPU {gpu} set to {g.power_limit} W")
            return True, f"GPU {gpu} on {node} is now capped at {g.power_limit} W."

    def set_rack_power_limit(self, rack: str, watts: int) -> tuple[bool, str]:
        with self.lock:
            w = max(300, min(DEFAULT_POWER_LIMIT, int(watts)))
            count = 0
            for nm in self.racks[rack].nodes:
                for g in self.nodes[nm].gpus:
                    if not g.lost:
                        g.power_limit = w
                        count += 1
            self._log(f"rack-{rack}", "nvidia-smi", "info", f"power limit set to {w} W on {count} GPUs in rack {rack}")
            return True, f"{count} GPUs in rack {rack} are now capped at {w} W."

    def drain_node(self, node: str) -> tuple[bool, str]:
        with self.lock:
            n = self.nodes[node]
            if n.status == "rebooting":
                return False, f"{node} is rebooting."
            n.status = "drained"
            self._log(node, "scheduler", "info", f"{node} drained: no new work will be scheduled here")
            return True, f"{node} is drained. Running jobs keep going; nothing new lands here."

    def drain_rack(self, rack: str) -> tuple[bool, str]:
        with self.lock:
            names = [nm for nm in self.racks[rack].nodes if self.nodes[nm].status == "active"]
            for nm in names:
                self.nodes[nm].status = "drained"
            self._log(f"rack-{rack}", "scheduler", "info", f"rack {rack} drained: {len(names)} servers take no new work")
            return True, f"Rack {rack} is drained ({len(names)} servers). Running jobs keep going until they are moved."

    def undrain_node(self, node: str) -> tuple[bool, str]:
        with self.lock:
            n = self.nodes[node]
            if n.status == "rebooting":
                return False, f"{node} is still rebooting."
            if self.has_open_ticket(node):
                return False, f"{node} has an open repair ticket. Keep it drained until the repair is done."
            issues = [i for i in self.observed_issues(node) if " is crashed" not in i and " is failed" not in i]
            if issues:
                return False, f"{node} still shows problems: {'; '.join(issues)}."
            n.status = "active"
            self._log(node, "scheduler", "info", f"{node} returned to service")
            return True, f"{node} is back in service as a spare."

    def restart_job(self, job_id: str) -> tuple[bool, str]:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return False, f"Unknown job {job_id}."
            bad = [n for n in job.nodes if self.nodes[n].status != "active" or any(g.lost for g in self.nodes[n].gpus)]
            if job.status == "restarting" and not bad:
                return True, f"{job_id} is already restarting from checkpoint step {job.checkpoint}; nothing more to move."
            spares = self.spare_nodes(self.nodes[bad[0]].rack if bad else None)
            if len(spares) < len(bad):
                return False, (f"Not enough healthy spare servers to replace {', '.join(bad)}. "
                               "Return a repaired server to service first.")
            moves = []
            for b in bad:
                s = spares.pop(0)
                job.nodes[job.nodes.index(b)] = s.name
                for g in self.nodes[b].gpus:
                    if g.job == job.id:
                        g.job = None
                for g in s.gpus:
                    g.job = job.id
                moves.append(f"{b} to {s.name}")
            if job.status == "restarting":  # another incident already restarted it: join that restart
                self._log(job.nodes[0], "scheduler", "info",
                          f"{job.id} restart in progress; also moved {', '.join(moves)}")
                return True, f"{job.id} was already restarting; moved {', '.join(moves)} as part of that restart."
            lost = int(job.step) - job.checkpoint
            job.step = float(job.checkpoint)
            job.status, job.restart_left, job.last_error = "restarting", 4, ""
            where = f", moved {', '.join(moves)}" if moves else ""
            self._log(job.nodes[0], "scheduler", "info",
                      f"restarting {job.id} from checkpoint step {job.checkpoint}{where}; {lost} steps redone")
            return True, f"Restarting {job.id} from checkpoint step {job.checkpoint}{where}. {lost} steps redone."

    def reset_gpu(self, node: str, gpu: int) -> tuple[bool, str]:
        with self.lock:
            n = self.nodes[node]
            g = n.gpus[gpu]
            if n.status == "rebooting":
                return False, f"{node} is rebooting."
            if g.lost:
                return False, f"GPU {gpu} on {node} is not visible on the bus, so it can't be reset. Reboot the server instead."
            job = self.jobs.get(g.job) if g.job else None
            if job and job.status in ("running", "restarting"):
                return False, (f"GPU {gpu} on {node} is in use by {job.id}. Move the job first "
                               "(drain the server, then restart the job from its checkpoint).")
            remapped = g.remap_pending
            g.sbe, g.dbe, g.remap_pending = 0, 0, False
            for f in self._unresolved(node=node, kind="ecc"):
                if f.gpu == gpu:
                    f.status = "resolved"
            self._log(node, "nvidia-smi", "info", f"GPU {gpu} reset complete" + ("; pending row remap applied" if remapped else ""))
            msg = f"GPU {gpu} on {node} was reset" + ("; the faulty memory row is now remapped." if remapped else "; weak memory rows retired.")
            if any(f.gpu == gpu for f in self._unresolved(node=node, kind="nvlink")):
                self._log(node, "kernel", "error", f"NVRM: GPU at PCI:{g.pci}: NVLink link 6 failed to train after reset")
                msg += " NVLink link 6 did not come back, which points to a cable or NVSwitch fault."
            return True, msg

    def reboot_node(self, node: str) -> tuple[bool, str]:
        with self.lock:
            n = self.nodes[node]
            busy = [j.id for j in self.jobs.values() if node in j.nodes and j.status in ("running", "restarting")]
            if busy:
                return False, f"{node} is running {', '.join(busy)}. Move the job off this server before rebooting."
            n.status, n.reboot_left = "rebooting", 5
            self._log(node, "nightingale", "info", f"rebooting {node}")
            return True, f"{node} is rebooting (about 10 seconds). It comes back drained."

    def open_ticket(self, node: str, gpu: int | None, summary: str) -> tuple[bool, str]:
        with self.lock:
            tid = f"HW-{1001 + len(self.tickets)}"
            self.tickets.append({"id": tid, "kind": "hardware", "node": node, "rack": self.nodes[node].rack,
                                 "gpu": gpu, "summary": summary[:300], "status": "open", "tick": self.tick_count})
            self._log(node, "nightingale", "info", f"repair ticket {tid} opened: {summary[:120]}")
            return True, f"Ticket {tid} opened for the on-site technician."

    def open_facility_ticket(self, rack: str, summary: str) -> tuple[bool, str]:
        with self.lock:
            tid = f"FAC-{1001 + len(self.tickets)}"
            self.tickets.append({"id": tid, "kind": "facility", "node": "", "rack": rack, "gpu": None,
                                 "summary": summary[:300], "status": "open", "tick": self.tick_count})
            self._log("facility", "nightingale", "info", f"facility ticket {tid} opened for rack {rack}: {summary[:120]}")
            return True, f"Ticket {tid} opened for the facilities team."

    def repair_ticket(self, ticket_id: str) -> tuple[bool, str]:
        """Simulates the technician or facilities team finishing the physical repair."""
        with self.lock:
            t = next((x for x in self.tickets if x["id"] == ticket_id), None)
            if not t or t["status"] != "open":
                return False, "No open ticket with that ID."
            t["status"] = "repaired"
            if t["kind"] == "facility":
                for f in self._unresolved(kind="rack_cooling", rack=t["rack"]):
                    f.status = "resolved"
                for nm in self.racks[t["rack"]].nodes:
                    for g in self.nodes[nm].gpus:
                        g.power_limit = DEFAULT_POWER_LIMIT
                self._log("facility", "facilities", "info",
                          f"{ticket_id} done: {self.racks[t['rack']].cdu} repaired, rack {t['rack']} power limits restored")
                return True, f"{ticket_id} marked repaired."
            node = self.nodes[t["node"]]
            for f in self._unresolved(node=t["node"]):
                if f.kind not in ("app_bug", "rack_cooling"):
                    f.status = "resolved"
            for g in node.gpus:
                if t["gpu"] is None or g.index == t["gpu"]:
                    g.lost, g.nvlink_crc, g.sbe, g.remap_pending = False, 0, 0, False
                    g.power_limit = DEFAULT_POWER_LIMIT
            self._log(t["node"], "technician", "info",
                      f"{ticket_id} repaired on site; {t['node']} stays drained until returned to service")
            return True, f"{ticket_id} marked repaired."

    def notify_owner(self, job: str, message: str) -> tuple[bool, str]:
        with self.lock:
            j = self.jobs[job]
            j.owner_notified = True
            self.notifications.append({"job": job, "owner": j.owner, "message": message[:400], "tick": self.tick_count})
            self._log(j.nodes[0], "nightingale", "info", f"notified {j.owner} about {job}")
            return True, f"Message sent to {j.owner}."

    def recommend(self, node: str, message: str) -> tuple[bool, str]:
        """A step the person at this computer does themselves. Nothing runs."""
        with self.lock:
            self.notifications.append({"node": node, "message": message[:400], "tick": self.tick_count})
            self._log(node, "nightingale", "info", f"suggested: {message[:160]}")
            return True, "Noted. Nightingale will check the GPU afterwards to see whether it helped."

    def remember(self, target: str, gpu: int | None, title: str, fix: str) -> None:
        with self.lock:
            self.history.append({"target": target, "gpu": gpu, "when": "today, " + _now().strftime("%H:%M"),
                                 "title": title, "fix": fix})

    # ------------------------------------------------------------- dry runs
    def clone(self) -> "Cluster":
        """An independent copy of the whole data center, for dry-running a plan."""
        with self.lock:
            twin = Cluster.__new__(Cluster)
            state = {k: v for k, v in self.__dict__.items() if k not in ("lock", "rng", "logs", "local", "pc")}
            twin.__dict__.update(copy.deepcopy(state))
            twin.local = None  # the copy keeps this computer's last readings, frozen
            twin.pc = None     # and can never run a fix on the real computer
            twin.lock = threading.RLock()
            twin.rng = random.Random()
            twin.rng.setstate(self.rng.getstate())
            twin.logs = deque(list(self.logs)[-300:], maxlen=4000)
            return twin

    # ------------------------------------------------------------- demo
    def inject(self, kind: str) -> dict:
        with self.lock:
            if kind not in SCENARIOS:
                raise ValueError(f"Unknown drill '{kind}'")
            broken_nodes = {f.node for f in self._unresolved()}
            broken_racks = {f.rack for f in self._unresolved(kind="rack_cooling")}
            if kind == "rack_cooling":
                cands = []
                for rack in self.racks.values():
                    if rack.id in broken_racks or any(n in broken_nodes or n == self.down_node for n in rack.nodes):
                        continue
                    busy = [n for n in rack.nodes if any(g.job for g in self.nodes[n].gpus)
                            and self.nodes[n].status == "active"]
                    if len(busy) >= 6:
                        cands.append((rack, busy))
                if not cands:
                    raise ValueError("No healthy busy rack left to break. Reset the demo to start fresh.")
                rack, busy = self.rng.choice(cands)
                hot = {n: round(self.rng.uniform(0.85, 1.1), 2) for n in busy[:6]}
                self._fault_id += 1
                f = Fault(self._fault_id, kind, busy[0], None, self.nodes[busy[0]].gpus[0].job or "",
                          self.tick_count, rack=rack.id, flags={"hot": hot})
                self.faults.append(f)
                return {"id": f.id, "kind": kind, "label": SCENARIOS[kind], "target": rack.id,
                        "node": "", "gpu": None, "rack": rack.id}
            # A drill breaks a GPU under a running job, so the failure has something to hurt.
            # Prefer the usual job for each drill, then any other job that is still running.
            order = (["embeddings-finetune", "moe-finetune-1t", "llm-pretrain-405b"] if kind == "app_bug"
                     else ["llm-pretrain-405b", "moe-finetune-1t", "embeddings-finetune"])
            job_id, cands = None, []
            for jid in order:
                job = self.jobs.get(jid)
                if job is None or job.status != "running":
                    continue
                cands = [(n.name, g.index) for n, g in self._job_gpus(job)
                         if not g.lost and n.status == "active" and n.name not in broken_nodes
                         and n.rack not in broken_racks]
                if cands:
                    job_id = jid
                    break
            if job_id is None:
                stopped = [j.id for j in self.jobs.values() if j.status != "running"]
                if len(stopped) == len(self.jobs):
                    raise ValueError("Every training job is stopped, so a drill has nothing to break. Approve "
                                     "the open plans on Incidents (they restart the jobs), or reset the demo.")
                raise ValueError("No healthy GPU left under a running job. Approve the open plans on Incidents, "
                                 "or reset the demo.")
            node, gpu = self.rng.choice(cands)
            self._fault_id += 1
            f = Fault(self._fault_id, kind, node, gpu, job_id, self.tick_count, rack=self.nodes[node].rack)
            self.faults.append(f)
            return {"id": f.id, "kind": kind, "label": SCENARIOS[kind], "target": node, "node": node,
                    "gpu": gpu, "rack": self.nodes[node].rack}

    # ------------------------------------------------------------- views
    def watch_list(self, limit: int = 8) -> list[str]:
        """GPUs that are down or under watch, with the reason, for whole-fleet questions."""
        out = []
        with self.lock:
            for n in self.nodes.values():
                for g in n.gpus:
                    where = f"{n.name} GPU {g.index}"
                    if g.lost:
                        out.append(f"{where}: down, not visible on the bus"
                                   + (" (known: repair ticket open)" if self.has_open_ticket(n.name) else ""))
                        continue
                    if n.local:
                        if self.local_odd(g):
                            why = ("cutting clocks for heat" if THERMAL_REASONS & set(g.reasons) else
                                   f"{g.temp:.0f}C" if g.temp >= LOCAL_HOT else "memory almost full")
                            out.append(f"{where} (this computer): {why}")
                        continue
                    hist = g.history
                    why = []
                    if len(hist) >= 3 and hist[-1][5] > hist[-3][5]:
                        why.append(f"corrected memory errors rising ({g.sbe:,})")
                    if len(hist) >= 3 and hist[-1][7] > hist[-3][7]:
                        why.append(f"NVLink errors rising ({g.nvlink_crc:,})")
                    if g.dbe:
                        why.append("uncorrectable memory error")
                    if g.remap_pending:
                        why.append("memory row remap pending")
                    if g.throttle == "HW_THERMAL_SLOWDOWN":
                        why.append(f"slowing down for heat at {g.temp:.0f}C")
                    elif g.temp >= WATCH_TEMP:
                        why.append(f"hot, {g.temp:.0f}C")
                    if why:
                        out.append(f"{where}: {', '.join(why)}")
        return out[:limit]

    def gauges(self) -> dict:
        with self.lock:
            total = sum(len(n.gpus) for n in self.nodes.values())
            lost = watch = 0
            power = 0.0
            hottest = (0.0, "", 0)
            for n in self.nodes.values():
                for g in n.gpus:
                    if g.lost:
                        lost += 1
                        continue
                    power += g.power
                    if n.local:
                        watch += self.local_odd(g)
                        continue
                    hist = g.history
                    rising = len(hist) >= 3 and (hist[-1][5] > hist[-3][5] or hist[-1][7] > hist[-3][7])
                    if g.temp >= WATCH_TEMP or g.throttle == "HW_THERMAL_SLOWDOWN" or rising or g.remap_pending or g.dbe:
                        watch += 1
                    if g.temp > hottest[0]:
                        hottest = (g.temp, n.name, g.index)
            main = self.jobs["llm-pretrain-405b"]
            return {
                "total": total, "lost": lost, "watch": watch, "healthy": total - lost - watch,
                "power_kw": round(power / 1000), "budget_kw": round(self.size * POWER_BUDGET_KW_PER_GPU),
                "hottest": {"temp": round(hottest[0]), "node": hottest[1], "gpu": hottest[2]},
                "speed": {"job": main.id, "pct": round(main.rate * 100), "status": main.status},
            }

    def fleet_map(self) -> list[dict]:
        """Every rack, compact: one temperature and one state code per GPU."""
        with self.lock:
            out = []
            for hall in self.halls:
                racks = []
                for rack in self.racks.values():
                    if rack.hall != hall:
                        continue
                    temps, codes, busy = [], [], []
                    lost = watch = 0
                    for nm in rack.nodes:
                        n = self.nodes[nm]
                        for g in n.gpus:
                            if n.local:
                                code = "x" if g.lost else "w" if self.local_odd(g) else "b" if g.util >= 10 else "i"
                                lost += code == "x"
                                watch += code == "w"
                                temps.append(round(g.temp))
                                codes.append(code)
                                continue
                            hist = g.history
                            rising = len(hist) >= 3 and (hist[-1][5] > hist[-3][5] or hist[-1][7] > hist[-3][7])
                            if g.lost:
                                code = "x"
                                lost += 1
                            elif n.status == "rebooting":
                                code = "r"
                            elif rising or g.remap_pending or g.throttle == "HW_THERMAL_SLOWDOWN" or g.temp >= WATCH_TEMP:
                                code = "w"
                                watch += 1
                            elif n.status == "drained":
                                code = "d"
                            elif g.job is None:
                                code = "i"
                            else:
                                code = "b"
                            temps.append(round(g.temp))
                            codes.append(code)
                            if g.job and not g.lost:
                                busy.append(g.temp)
                    out_rack = {"id": rack.id, "temps": temps, "codes": "".join(codes), "lost": lost, "watch": watch,
                                "avg": round(sum(busy) / len(busy)) if busy else round(sum(temps) / len(temps)),
                                "busy": len(busy),
                                # what a sensor shows, not what the drill did
                                "cooling": rack.coolant_flow < 90}
                    if rack.hall == LOCAL_HALL:
                        out_rack.update({"local": True, "model": self.nodes[LOCAL_NODE].model, "cooling": False})
                    racks.append(out_rack)
                out.append({"hall": hall, "racks": racks})
            return out

    def rack_detail(self, rack_id: str) -> dict | None:
        with self.lock:
            rid = self.canonical_rack(rack_id)
            if not rid:
                return None
            rack = self.racks[rid]
            servers = []
            for nm in rack.nodes:
                n = self.nodes[nm]
                jobs = sorted({g.job for g in n.gpus if g.job})
                servers.append({"name": nm, "status": n.status, "jobs": jobs, "local": n.local, "model": n.model,
                                "ticket": self.has_open_ticket(nm),
                                "gpus": [self._gpu_public(n, g) for g in n.gpus]})
            return {"id": rid, "hall": rack.hall, "cdu": rack.cdu, "flow": round(rack.coolant_flow),
                    "return_temp": round(rack.coolant_return, 1), "servers": servers,
                    "local": rack.hall == LOCAL_HALL}

    def _gpu_public(self, n: Node, g: GPU) -> dict:
        hist = g.history
        return {"index": g.index, "pci": g.pci, "temp": round(g.temp, 1), "power": round(g.power),
                "power_limit": g.power_limit, "clock": g.clock, "util": g.util, "sbe": g.sbe, "dbe": g.dbe,
                "nvlink_crc": g.nvlink_crc, "remap_pending": g.remap_pending, "throttle": g.throttle,
                "lost": g.lost, "job": g.job,
                "sbe_rising": len(hist) >= 3 and hist[-1][5] > hist[-3][5],
                "nvlink_rising": len(hist) >= 3 and hist[-1][7] > hist[-3][7],
                **({"local": True, "watch": self.local_odd(g), "max_clock": g.max_clock, "power_max": g.power_max,
                    "mem_used": g.mem_used, "mem_total": g.mem_total, "fan": g.fan, "reasons": list(g.reasons),
                    "policy_flags": list(g.policy_flags)}
                   if n.local else {})}

    def gpu_detail(self, node: str, gpu: int) -> dict | None:
        with self.lock:
            nm = self.canonical_node(node)
            if not nm or not 0 <= gpu < len(self.nodes[nm].gpus):
                return None
            n = self.nodes[nm]
            g = n.gpus[gpu]
            extra = {}
            if n.local:
                snap = self.local.snapshot() if self.local is not None else {"processes": []}
                extra = {"model": n.model, "driver": n.driver,
                         "processes": [p for p in snap["processes"] if p.get("gpu", 0) == gpu][:8]}
            return {"node": nm, "rack": n.rack, "status": n.status, **self._gpu_public(n, g), **extra,
                    "history": [{"tick": h[0], "temp": h[1], "power": h[2], "clock": h[3], "sbe": h[5],
                                 "nvlink_crc": h[7], "lost": h[8], "util": h[9] if len(h) > 9 else 0} for h in g.history],
                    "incidents": [h for h in self.history if h["target"] == nm and h.get("gpu") in (None, gpu)]}

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "tick": self.tick_count, "size": self.size, "sizes": list(SIZES),
                "servers": len(self.sim_nodes()), "racks": len(self.racks) - (LOCAL_RACK in self.racks), "halls": self.halls,
                "local": self.local_status(),
                "gauges": self.gauges(),
                "jobs": [{"id": j.id, "owner": j.owner, "servers": len(j.nodes), "gpus": len(j.nodes) * 8,
                          "status": j.status, "step": int(j.step), "checkpoint": j.checkpoint,
                          "rate": round(j.rate, 3), "throughput": int(j.tokens_per_gpu * j.rate),
                          "last_error": j.last_error} for j in self.jobs.values()],
                "tickets": [dict(t) for t in self.tickets],
                "drills": [{"id": f.id, "kind": f.kind, "label": SCENARIOS[f.kind], "target": f.rack if f.kind == "rack_cooling" else f.node,
                            "gpu": f.gpu, "status": f.status} for f in self.faults],
                "scenarios": SCENARIOS,
            }

    def logs_since(self, since: int, limit: int = 400) -> list[dict]:
        with self.lock:
            return [l for l in self.logs if l["id"] > since][-limit:]
