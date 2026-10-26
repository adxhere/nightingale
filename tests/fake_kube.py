"""An in-memory stand-in for app.kube.Kube: the same methods, a tiny cluster, and a record of every call."""
from __future__ import annotations

import time


class FakeKube:
    def __init__(self, nodes=("gpu-node-1", "gpu-node-2"), gpus=2) -> None:
        self.calls: list = []
        self.namespace = "nightingale"
        self.node_state = {n: {"name": n, "labels": {"nightingale.io/rack": "row-1"}, "ready": True, "reason": "",
                               "unschedulable": False, "gpus": gpus} for n in nodes}
        self.pods: list = []
        self.events: list = []
        self.exporters: list = []
        self.pdb_block = False
        self.smi_ok = True

    # reads
    def describe(self) -> str:
        return "https://fake-k8s:6443"

    def nodes(self):
        return [dict(v, labels=dict(v["labels"])) for v in self.node_state.values()]

    def gpu_pods(self):
        return [dict(p) for p in self.pods]

    def exporter_endpoints(self):
        return list(self.exporters)

    def pod_nodes(self):
        return {}

    def events_since(self, since):
        return [e for e in self.events if e["at"] > since]

    # test helpers
    def add_pod(self, name, node, job, gpus=1, namespace="ml", controller="Job", phase="Running", waiting="",
                restarts=0, terminated=None):
        p = {"name": name, "namespace": namespace, "uid": f"uid-{name}", "node": node, "phase": phase, "gpus": gpus,
             "job": job, "controller": controller, "restarts": restarts, "waiting": waiting, "terminated": terminated,
             "deleting": False, "created": time.time(), "reason": "", "message": ""}
        self.pods.append(p)
        return p

    def pod(self, name):
        return next(p for p in self.pods if p["name"] == name)

    # actions
    def set_unschedulable(self, node, on, dry_run):
        self.calls.append(("cordon" if on else "uncordon", node, dry_run))
        if node not in self.node_state:
            return False, f"node {node} not found"
        if not dry_run:
            self.node_state[node]["unschedulable"] = on
        return True, f"{node} {'would be ' if dry_run else ''}{'cordoned' if on else 'uncordoned'}"

    def evict(self, namespace, pod, dry_run):
        self.calls.append(("evict", f"{namespace}/{pod}", dry_run))
        if self.pdb_block:
            return False, f"Evicting {namespace}/{pod} is blocked by a PodDisruptionBudget right now"
        if not dry_run:
            p = next(x for x in self.pods if x["name"] == pod and x["namespace"] == namespace)
            self.pods.remove(p)
            # the controller makes a new pod, pending until a healthy node has room
            free = [n for n, v in self.node_state.items() if not v["unschedulable"] and v["ready"]
                    and n != p["node"]]
            self.add_pod(p["name"] + "-r", free[0] if free else "", p["job"], p["gpus"], namespace,
                         p["controller"], "Running" if free else "Pending")
        return True, f"{'(dry run) ' if dry_run else ''}evicted {namespace}/{pod}"

    def run_on_node(self, node, command, dry_run, timeout=120):
        self.calls.append(("smi", node, " ".join(command), dry_run))
        if not self.smi_ok:
            return False, "nvidia-smi failed: Insufficient Permissions"
        return True, "dry run ok" if dry_run else "All done."
