"""The real Kubernetes adapter against a live API server. Skipped unless NIGHTINGALE_TEST_KUBECONFIG is set.

It needs a node called gpu-node-a reporting nvidia.com/gpu, and a Job "llm-train" in namespace "ml"
whose pod asks for one GPU (deploy/k8s/demo-training.yaml, pinned to that node, is one way).
Only dry runs plus a cordon/uncordon of gpu-node-a change anything, and the test undoes the cordon.
"""
import os

import pytest

KUBECONFIG = os.getenv("NIGHTINGALE_TEST_KUBECONFIG")
pytestmark = pytest.mark.skipif(not KUBECONFIG, reason="set NIGHTINGALE_TEST_KUBECONFIG to run against a live cluster")


def test_live_reads_and_dry_runs():
    from app.kube import Kube

    k, msg = Kube.connect(KUBECONFIG, namespace="nightingale")
    assert k is not None, msg
    nodes = {n["name"]: n for n in k.nodes()}
    assert nodes["gpu-node-a"]["gpus"] >= 1
    pod = next(p for p in k.gpu_pods() if p["job"] == "llm-train")
    assert pod["controller"] == "Job" and pod["gpus"] == 1
    assert k.set_unschedulable("gpu-node-a", True, dry_run=True)[0]
    assert not {n["name"]: n for n in k.nodes()}["gpu-node-a"]["unschedulable"], "a dry run changes nothing"
    assert k.evict(pod["namespace"], pod["name"], dry_run=True)[0]
    assert pod["name"] in {p["name"] for p in k.gpu_pods()}
    ok, out = k.run_on_node("gpu-node-a", ["nvidia-smi", "-L"], dry_run=True)
    assert ok, out
    assert not k.set_unschedulable("no-such-node", True, dry_run=True)[0]
    try:
        assert k.set_unschedulable("gpu-node-a", True, dry_run=False)[0]
        assert {n["name"]: n for n in k.nodes()}["gpu-node-a"]["unschedulable"]
    finally:
        k.set_unschedulable("gpu-node-a", False, dry_run=False)
