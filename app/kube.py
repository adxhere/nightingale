"""Kubernetes, for real GPU servers: what runs where, what went wrong, and the few actions Nightingale may take.

Reads: nodes (ready, cordoned, GPU count), pods that use GPUs (grouped into jobs), Warning events,
and where the dcgm-exporter pods are. Actions, each only after a person approves it and each
rehearsed first with Kubernetes' own server-side dry run (nothing is changed by a rehearsal):

* cordon / uncordon a node (no new work lands on it; running pods keep going),
* evict the pods of a job so their controller starts them again on healthy nodes, respecting
  PodDisruptionBudgets (the job resumes from its own last checkpoint),
* run one nvidia-smi command on a node (power limit, GPU reset) in a short-lived helper pod.

Uses the official Python client (pip install kubernetes). Works with a kubeconfig file or, inside
the cluster, with the pod's service account (see deploy/k8s/nightingale-rbac.yaml).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

JOB_LABELS = ("nightingale.io/job", "training.kubeflow.org/job-name", "jobset.sigs.k8s.io/jobset-name",
              "batch.kubernetes.io/job-name", "job-name", "app.kubernetes.io/instance", "app")
EXPORTER_SELECTORS = ("app=nvidia-dcgm-exporter", "app.kubernetes.io/name=dcgm-exporter")
GPU = "nvidia.com/gpu"


def _ts(value) -> float:
    if not value:
        return 0.0
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _gpus_requested(pod) -> int:
    total = 0
    for c in (pod.spec.containers or []):
        res = c.resources
        for d in (res.limits if res else None, res.requests if res else None):
            if d and GPU in d:
                try:
                    total += int(str(d[GPU]))
                except ValueError:
                    pass
                break
    return total


def job_of(labels: dict, owners: list, pod_name: str) -> tuple[str, str]:
    """(job name, controller kind) for a pod: training-operator and Job labels first, then its owner."""
    labels = labels or {}
    for key in JOB_LABELS:
        if labels.get(key):
            kind = next((o.kind for o in owners if getattr(o, "controller", False)), "") if owners else ""
            return labels[key], kind or "label"
    for o in owners or []:
        if getattr(o, "controller", False):
            name = o.name
            if o.kind == "ReplicaSet" and labels.get("pod-template-hash") and name.endswith(labels["pod-template-hash"]):
                name = name[: -len(labels["pod-template-hash"]) - 1]
            return name, o.kind
    return pod_name, ""


def _api_error(exc) -> str:
    body = getattr(exc, "body", "") or ""
    try:
        import json  # noqa: PLC0415
        msg = json.loads(body).get("message") if body else ""
    except ValueError:
        msg = ""
    status = getattr(exc, "status", "")
    return f"{msg or getattr(exc, 'reason', '') or exc}" + (f" (HTTP {status})" if status else "")


class Kube:
    def __init__(self, api_client, namespace: str = "nightingale", image: str = "",
                 runtime_class: str | None = "nvidia", where: str = "") -> None:
        from kubernetes import client  # noqa: PLC0415 - optional dependency

        self.client = client
        self.api = api_client
        self.core = client.CoreV1Api(api_client)
        self.authz = client.AuthorizationV1Api(api_client)
        self.namespace = namespace
        self.image = image or "nvcr.io/nvidia/cuda:12.6.3-base-ubuntu22.04"
        self.runtime_class = runtime_class or None
        self.where = where

    # ------------------------------------------------------------ connect
    @classmethod
    def connect(cls, kubeconfig: str | None = None, context: str | None = None, **kw) -> tuple["Kube | None", str]:
        try:
            from kubernetes import client, config  # noqa: PLC0415
        except ImportError:
            return None, "The Kubernetes client isn't installed: pip install -r requirements-fleet.txt"
        try:
            if kubeconfig or not _in_cluster():
                cfg = client.Configuration()
                config.load_kube_config(config_file=kubeconfig or None, context=context or None,
                                        client_configuration=cfg)
                where = cfg.host
            else:
                cfg = client.Configuration()
                config.load_incluster_config(client_configuration=cfg)
                where = "in-cluster"
            api = client.ApiClient(cfg)
            kube = cls(api, where=where, **kw)
            version = client.VersionApi(api).get_code(_request_timeout=8)
            kube.ensure_namespace()
            return kube, f"Kubernetes {version.git_version} at {where}"
        except Exception as exc:  # any config or network problem is a clear message, not a crash
            return None, f"Couldn't connect to Kubernetes: {_api_error(exc)}"

    def describe(self) -> str:
        return self.where

    def ensure_namespace(self) -> None:
        """Nightingale's own namespace, for its short-lived helper pods."""
        try:
            self.core.read_namespace(self.namespace, _request_timeout=8)
        except Exception:
            try:
                self.core.create_namespace({"metadata": {"name": self.namespace,
                                                         "labels": {"app.kubernetes.io/managed-by": "nightingale"}}},
                                           _request_timeout=8)
            except Exception:
                pass  # no permission: the rehearsal of a GPU command will say so

    # -------------------------------------------------------------- reads
    def nodes(self) -> list[dict]:
        out = []
        for n in self.core.list_node(_request_timeout=10).items:
            conds = {c.type: c for c in (n.status.conditions or [])}
            ready = conds.get("Ready")
            alloc = (n.status.allocatable or {}).get(GPU, "0")
            out.append({"name": n.metadata.name, "labels": dict(n.metadata.labels or {}),
                        "ready": bool(ready and ready.status == "True"),
                        "reason": (ready.reason or ready.message or "") if ready and ready.status != "True" else "",
                        "unschedulable": bool(n.spec.unschedulable),
                        "gpus": int(alloc) if str(alloc).isdigit() else 0})
        return out

    def gpu_pods(self) -> list[dict]:
        out = []
        for p in self.core.list_pod_for_all_namespaces(_request_timeout=15).items:
            gpus = _gpus_requested(p)
            if not gpus:
                continue
            labels = dict(p.metadata.labels or {})
            job, kind = job_of(labels, p.metadata.owner_references or [], p.metadata.name)
            restarts, waiting, term = 0, "", None
            for cs in (p.status.container_statuses or []):
                restarts += cs.restart_count or 0
                if cs.state and cs.state.waiting and cs.state.waiting.reason:
                    waiting = cs.state.waiting.reason
                t = (cs.state.terminated if cs.state else None) or (cs.last_state.terminated if cs.last_state else None)
                if t is not None and (term is None or _ts(t.finished_at) > term["at"]):
                    term = {"reason": t.reason or "", "exit_code": t.exit_code, "at": _ts(t.finished_at),
                            "message": (t.message or "")[:200]}
            out.append({"name": p.metadata.name, "namespace": p.metadata.namespace, "uid": p.metadata.uid,
                        "node": p.spec.node_name or "", "phase": p.status.phase or "", "gpus": gpus,
                        "job": job, "controller": kind, "restarts": restarts, "waiting": waiting,
                        "terminated": term, "deleting": bool(p.metadata.deletion_timestamp),
                        "created": _ts(p.metadata.creation_timestamp),
                        "reason": p.status.reason or "", "message": (p.status.message or "")[:200]})
        return out

    def exporter_endpoints(self) -> list[tuple[str, str]]:
        found = {}
        for sel in EXPORTER_SELECTORS:
            for p in self.core.list_pod_for_all_namespaces(label_selector=sel, _request_timeout=10).items:
                if p.status.phase != "Running" or not p.status.pod_ip:
                    continue
                port = 9400
                for c in p.spec.containers or []:
                    for cp in c.ports or []:
                        if cp.name in ("metrics", "gpu-metrics") or cp.container_port == 9400:
                            port = cp.container_port
                found[p.spec.node_name or p.metadata.name] = f"http://{p.status.pod_ip}:{port}/metrics"
        return sorted(found.items())

    def pod_nodes(self) -> dict:
        """Exporter pod name -> node: dcgm-exporter's Hostname label is the pod's name in Kubernetes."""
        out = {}
        for sel in EXPORTER_SELECTORS:
            for p in self.core.list_pod_for_all_namespaces(label_selector=sel, _request_timeout=10).items:
                if p.spec.node_name:
                    out[p.metadata.name] = p.spec.node_name
        return out

    def events_since(self, since: float) -> list[dict]:
        out = []
        items = self.core.list_event_for_all_namespaces(field_selector="type=Warning", limit=500,
                                                        _request_timeout=10).items
        for e in items:
            at = max(_ts(e.last_timestamp), _ts(e.event_time), _ts(e.first_timestamp),
                     _ts(e.metadata.creation_timestamp))
            if at <= since:
                continue
            obj = e.involved_object
            out.append({"at": at, "type": e.type, "reason": e.reason or "", "kind": obj.kind or "",
                        "name": obj.name or "", "namespace": obj.namespace or "", "count": e.count or 1,
                        "message": (e.message or "")[:240]})
        out.sort(key=lambda x: x["at"])
        return out

    def can_i(self, verb: str, resource: str, namespace: str | None = None, subresource: str | None = None,
              group: str = "") -> bool:
        body = {"spec": {"resourceAttributes": {"verb": verb, "resource": resource, "group": group,
                                                **({"namespace": namespace} if namespace else {}),
                                                **({"subresource": subresource} if subresource else {})}}}
        try:
            r = self.authz.create_self_subject_access_review(body, _request_timeout=8)
            return bool(r.status.allowed)
        except Exception:
            return False

    # ------------------------------------------------------------ actions
    def set_unschedulable(self, node: str, on: bool, dry_run: bool) -> tuple[bool, str]:
        try:
            self.core.patch_node(node, {"spec": {"unschedulable": on}}, _request_timeout=10,
                                 **({"dry_run": "All"} if dry_run else {}))
        except Exception as exc:
            return False, f"Kubernetes refused to {'cordon' if on else 'uncordon'} {node}: {_api_error(exc)}"
        if dry_run:
            return True, f"Kubernetes accepted the change in a dry run: {node} would be {'cordoned' if on else 'uncordoned'}."
        return True, (f"{node} is cordoned: Kubernetes places no new work on it. Running pods keep going."
                      if on else f"{node} is uncordoned and takes new work again.")

    def evict(self, namespace: str, pod: str, dry_run: bool) -> tuple[bool, str]:
        body = self.client.V1Eviction(metadata=self.client.V1ObjectMeta(name=pod, namespace=namespace))
        try:
            self.core.create_namespaced_pod_eviction(pod, namespace, body, _request_timeout=15,
                                                     **({"dry_run": "All"} if dry_run else {}))
        except Exception as exc:
            if getattr(exc, "status", 0) == 429:
                return False, (f"Evicting {namespace}/{pod} is blocked by a PodDisruptionBudget right now; "
                               "Kubernetes won't take more of that workload down.")
            return False, f"Kubernetes refused to evict {namespace}/{pod}: {_api_error(exc)}"
        return True, (f"{namespace}/{pod} can be evicted (dry run)." if dry_run else f"Evicted {namespace}/{pod}.")

    def run_on_node(self, node: str, command: list[str], dry_run: bool, timeout: float = 120) -> tuple[bool, str]:
        """One nvidia-smi command on a node, in a privileged helper pod that is deleted afterwards."""
        spec = {"nodeName": node, "restartPolicy": "Never", "tolerations": [{"operator": "Exists"}],
                "containers": [{"name": "nvidia-smi", "image": self.image, "command": command,
                                "securityContext": {"privileged": True},
                                "env": [{"name": "NVIDIA_VISIBLE_DEVICES", "value": "all"},
                                        {"name": "NVIDIA_DRIVER_CAPABILITIES", "value": "utility"}],
                                "resources": {"limits": {"cpu": "250m", "memory": "128Mi"}}}]}
        if self.runtime_class:
            spec["runtimeClassName"] = self.runtime_class
        body = {"apiVersion": "v1", "kind": "Pod",
                "metadata": {"generateName": "nightingale-gpu-", "namespace": self.namespace,
                             "labels": {"app.kubernetes.io/managed-by": "nightingale"}},
                "spec": spec}
        try:
            pod = self.core.create_namespaced_pod(self.namespace, body, _request_timeout=15,
                                                  **({"dry_run": "All"} if dry_run else {}))
        except Exception as exc:
            return False, f"Kubernetes refused the helper pod on {node}: {_api_error(exc)}"
        if dry_run:
            return True, f"Kubernetes accepted a helper pod on {node} in a dry run (would run: {' '.join(command)})."
        name = pod.metadata.name
        phase, end = "", time.time() + timeout
        try:
            while time.time() < end:
                p = self.core.read_namespaced_pod(name, self.namespace, _request_timeout=10)
                phase = p.status.phase or ""
                if phase in ("Succeeded", "Failed"):
                    break
                waiting = next((cs.state.waiting for cs in (p.status.container_statuses or [])
                                if cs.state and cs.state.waiting and cs.state.waiting.reason in
                                ("ErrImagePull", "ImagePullBackOff", "CreateContainerError", "CrashLoopBackOff")), None)
                if waiting is not None:
                    return False, f"The helper pod on {node} couldn't start: {waiting.reason} {waiting.message or ''}".strip()
                time.sleep(1)
            try:
                logs = self.core.read_namespaced_pod_log(name, self.namespace, _request_timeout=10).strip()
            except Exception:
                logs = ""
            if phase != "Succeeded":
                return False, (f"nvidia-smi on {node} {'failed' if phase == 'Failed' else 'did not finish in time'}"
                               + (f": {logs[-300:]}" if logs else "."))
            return True, logs[-400:] or "Done."
        finally:
            try:
                self.core.delete_namespaced_pod(name, self.namespace, grace_period_seconds=0, _request_timeout=10)
            except Exception:
                pass


def _in_cluster() -> bool:
    import os  # noqa: PLC0415
    return bool(os.getenv("KUBERNETES_SERVICE_HOST")) and os.path.exists(
        "/var/run/secrets/kubernetes.io/serviceaccount/token")
