"""Real GPU servers: DCGM readings and Kubernetes state, polled in the background.

RealFleet reads; the Cluster turns each snapshot into servers in hall R (see fleet_sync.py) next to
the simulated data center and this computer. Its actions (cordon, evict, one nvidia-smi command)
run only when an approved plan step calls them, and every one has a dry-run form that changes
nothing, which is how Nightingale rehearses a plan on real infrastructure.
"""
from __future__ import annotations

import copy
import threading
import time
from collections import deque

from . import dcgm


class RealFleet:
    def __init__(self, reader, kube=None, interval: float = 5.0, rack_label: str = "nightingale.io/rack",
                 kube_note: str = "") -> None:
        self.reader = reader
        self.kube = kube
        self.kube_note = kube_note          # why Kubernetes isn't connected, when it isn't
        self.interval = interval
        self.rack_label = rack_label
        self.lock = threading.Lock()
        self.state = "connecting"           # connecting | ok | error
        self.error = ""
        self.kube_error = ""
        self.nodes: dict = {}
        self.pods: list = []
        self.events: deque = deque(maxlen=200)
        self.read_at = 0.0
        self.reads = 0
        self.seq = 0
        self._pod_nodes: dict = {}
        self._pod_nodes_at = 0.0
        self._events_since = time.time() - 600  # Warning events from the last 10 minutes come in as context
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------- reading
    def _resolve(self, kube_nodes: dict):
        def resolve(raw: str) -> str:
            if not raw or raw in kube_nodes:
                return raw
            if raw in self._pod_nodes:
                return self._pod_nodes[raw]
            short = raw.split(".")[0]
            return short if short in kube_nodes else raw
        return resolve

    def poll(self) -> None:
        kube_nodes, pods, events, kerr = {}, [], [], ""
        if self.kube is not None:
            try:
                kube_nodes = {n["name"]: n for n in self.kube.nodes()}
                pods = self.kube.gpu_pods()
                if time.time() - self._pod_nodes_at > 60:
                    self._pod_nodes, self._pod_nodes_at = self.kube.pod_nodes(), time.time()
                events = self.kube.events_since(self._events_since)
            except Exception as exc:  # Kubernetes trouble must not stop the GPU readings
                kerr = f"Kubernetes didn't answer: {str(exc)[:200]}"
        try:
            samples, errors = self.reader.read()
        except Exception as exc:
            samples, errors = [], [str(exc)[:300]]
        grouped = dcgm.group(samples, self._resolve(kube_nodes))
        nodes = {}
        for name in set(grouped) | {n for n, k in kube_nodes.items() if k["gpus"] > 0}:
            g = grouped.get(name, {"gpus": {}, "model": "", "driver": ""})
            k = kube_nodes.get(name, {})
            labels = k.get("labels", {})
            nodes[name] = {"name": name, "gpus": g["gpus"], "model": g["model"], "driver": g["driver"],
                           "reporting": name in grouped, "kube": bool(k), "ready": k.get("ready", True),
                           "ready_reason": k.get("reason", ""), "cordoned": k.get("unschedulable", False),
                           "gpu_count": k.get("gpus", len(g["gpus"])),
                           "rack": labels.get(self.rack_label) or labels.get("topology.kubernetes.io/rack") or ""}
        with self.lock:
            self.read_at = time.time()
            self.reads += 1
            self.seq += 1
            self.kube_error = kerr
            if samples:
                self.nodes, self.state, self.error = nodes, "ok", "; ".join(errors)[:300]
            else:
                # keep the last good view (its GPUs go "not reporting" after two misses), say why
                for name, n in nodes.items():
                    if name in self.nodes:
                        self.nodes[name].update({k: n[k] for k in ("ready", "ready_reason", "cordoned", "kube")})
                        self.nodes[name]["reporting"] = False
                    else:
                        self.nodes[name] = n
                for name in self.nodes:
                    if name not in nodes:
                        self.nodes[name]["reporting"] = False
                self.state = "error"
                self.error = ("; ".join(errors) or "no DCGM metrics in the answer")[:300]
            if self.kube is not None and not kerr:
                self.pods = pods
            for e in events:
                self.events.append(e)
                self._events_since = max(self._events_since, e["at"])

    # ----------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="nightingale-fleet", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception as exc:  # never let the reader thread die
                with self.lock:
                    self.state, self.error = "error", f"Reading the fleet failed: {exc}"
            self._stop.wait(self.interval)

    # ---------------------------------------------------------------- read
    def snapshot(self) -> dict:
        with self.lock:
            return {"state": self.state, "error": self.error, "seq": self.seq, "read_at": self.read_at,
                    "nodes": copy.deepcopy(self.nodes), "pods": copy.deepcopy(self.pods)}

    def take_events(self) -> list[dict]:
        with self.lock:
            out = list(self.events)
            self.events.clear()
            return out

    def status(self) -> dict:
        with self.lock:
            gpus = sum(len(n["gpus"]) for n in self.nodes.values())
            return {"state": self.state, "error": self.error, "source": self.reader.kind,
                    "where": self.reader.describe(), "read_at": self.read_at, "reads": self.reads,
                    "servers": len(self.nodes), "gpus": gpus,
                    "kube": self.kube is not None, "kube_where": self.kube.describe() if self.kube else "",
                    "kube_error": self.kube_error, "kube_note": self.kube_note,
                    "interval": self.interval}

    # ------------------------------------------------------------- actions
    def _no_kube(self, what: str) -> tuple[bool, str]:
        return False, (f"Kubernetes isn't connected, so Nightingale can't {what} on real servers. "
                       "Connect a kubeconfig (FLEET_KUBECONFIG) to let approved plans act; until then, "
                       "open a ticket for whoever runs these servers.")

    def cordon(self, node: str, on: bool, dry_run: bool) -> tuple[bool, str]:
        if self.kube is None:
            return self._no_kube("cordon or uncordon a node")
        return self.kube.set_unschedulable(node, on, dry_run)

    def evict(self, namespace: str, pod: str, dry_run: bool) -> tuple[bool, str]:
        if self.kube is None:
            return self._no_kube("move a job")
        return self.kube.evict(namespace, pod, dry_run)

    def nvidia_smi(self, node: str, args: list[str], dry_run: bool) -> tuple[bool, str]:
        if self.kube is None:
            return self._no_kube("change a GPU setting")
        return self.kube.run_on_node(node, ["nvidia-smi", *args], dry_run)
