"""Real GPU servers: DCGM readings, Kubernetes state and actions, using stand-ins for dcgm-exporter and Kubernetes."""
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import app.llm as llm_module
from app import actions, dcgm
from app.dcgm_bridge import render as bridge_render
from app.fleet import RealFleet
from app.local_gpu import LocalGPU
from fake_dcgm import FakeExporter, gpu
from fake_kube import FakeKube
from test_nightingale import make_agent

HERE = os.path.dirname(__file__)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(llm_module.time, "sleep", lambda _s: None)


@pytest.fixture
def world():
    exp = FakeExporter({"gpu-node-1": [gpu(pod="trainer-0", namespace="ml"), gpu(util=4, power=60)],
                        "gpu-node-2": [gpu(util=3, power=55), gpu(util=2, power=50)]})
    kube = FakeKube()
    kube.add_pod("trainer-0", "gpu-node-1", "llm-train", gpus=1)
    cluster, agent = make_agent(size=256, seed=3)
    fleet = RealFleet(dcgm.make_reader(exp.url), kube, interval=0.1)
    fleet.poll()
    cluster.attach_fleet(fleet)
    yield exp, kube, cluster, agent, fleet
    exp.close()


def run(cluster, agent, fleet, ticks):
    async def go():
        for t in range(1, ticks):
            fleet.poll()
            cluster.tick()
            agent.verify_pending()
            if t % 5 == 0:
                await agent.triage_once()
                await asyncio.gather(*list(agent.tasks))
    asyncio.run(go())


# ------------------------------------------------------------------ reading
def test_dcgm_text_is_parsed_with_labels_aliases_and_reasons():
    text = ('# HELP x\nDCGM_FI_DEV_GPU_TEMP_CELSIUS{gpu="1",UUID="GPU-a",pci_bus_id="00000000:3B:00.0",'
            'modelName="NVIDIA H100 80GB HBM3",Hostname="dcgm-exporter-abc",kubernetes_node="node-7",'
            'DCGM_FI_DRIVER_VERSION="550.90.07",pod="train-0",namespace="ml"} 71\n'
            'DCGM_FI_DEV_CLOCKS_EVENT_REASONS{gpu="1",kubernetes_node="node-7"} 68\n'
            'DCGM_FI_DEV_FB_USED{gpu="1",kubernetes_node="node-7"} 60000\n'
            'DCGM_FI_DEV_FB_FREE{gpu="1",kubernetes_node="node-7"} 20000\n'
            'DCGM_FI_DEV_XID_ERRORS{gpu="1",kubernetes_node="node-7"} 79\nnot a metric line\n')
    nodes = dcgm.group(dcgm.parse_text(text))
    g = nodes["node-7"]["gpus"][1]
    assert g["temp"] == 71 and g["xid"] == 79 and g["mem_total"] == 80000
    assert g["reasons"] == ["SW_POWER_CAP", "HW_THERMAL_SLOWDOWN"] and g["pod"] == "train-0"
    assert g["pci"] == "0000:3b:00.0" and nodes["node-7"]["driver"] == "550.90.07"
    assert "Xid 79 (GPU has fallen off the bus)" == dcgm.xid_text(79)


def test_sources_are_chosen_from_the_url():
    assert isinstance(dcgm.make_reader("http://prometheus:9090"), dcgm.PrometheusReader)
    r = dcgm.make_reader("node-a=http://10.0.0.5:9400/metrics, http://10.0.0.6:9400/metrics")
    assert isinstance(r, dcgm.ExporterReader) and r.urls[0] == ("node-a", "http://10.0.0.5:9400/metrics")
    assert dcgm.make_reader("auto").auto
    with pytest.raises(ValueError):
        dcgm.make_reader("ftp://nope/metrics")


def test_prometheus_is_queried_for_every_dcgm_series():
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen["path"] = self.path
            body = json.dumps({"status": "success", "data": {"resultType": "vector", "result": [
                {"metric": {"__name__": "DCGM_FI_DEV_GPU_TEMP", "gpu": "0", "Hostname": "h1", "modelName": "NVIDIA L40S"},
                 "value": [1, "64"]},
                {"metric": {"__name__": "DCGM_FI_DEV_GPU_UTIL", "gpu": "0", "Hostname": "h1"}, "value": [1, "NaN"]}]}}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        samples, errors = dcgm.make_reader(f"http://127.0.0.1:{srv.server_address[1]}").read()
    finally:
        srv.shutdown()
    assert not errors and "api/v1/query" in seen["path"] and "DCGM_FI_" in seen["path"].replace("%5F", "_")
    assert dcgm.group(samples)["h1"]["gpus"][0]["temp"] == 64 and len(samples) == 1


def test_real_servers_join_as_hall_r_with_their_jobs(world):
    _exp, _kube, cluster, _agent, _fleet = world
    real = cluster.real_nodes()
    assert sorted(n.name for n in real) == ["gpu-node-1", "gpu-node-2"] and {n.rack for n in real} == {"R-01"}
    assert "R" in cluster.halls and cluster.canonical_rack("r1") == "R-01"
    assert cluster.canonical_node("NODE-2") == "gpu-node-2"
    j = cluster.jobs["llm-train"]
    assert j.real and j.status == "running" and j.nodes == ["gpu-node-1"] and j.gpus == 1
    assert cluster.nodes["gpu-node-1"].gpus[0].job == "llm-train"
    assert "REAL server" in cluster.run_tool("node_status", {"node": "gpu-node-1"})
    assert "real Kubernetes workload" in cluster.run_tool("job_info", {"job": "llm-train"})
    assert "No facility sensors" in cluster.run_tool("facility_sensors", {"rack": "R-01"})
    snap = cluster.snapshot()
    assert snap["real"]["servers"] == 2 and snap["real"]["gpus"] == 4 and snap["racks"] == 4
    assert any(r.get("real") for h in cluster.fleet_map() for r in h["racks"])
    twin = cluster.clone()
    assert twin.fleet is None, "a rehearsal copy can never touch real servers"


def test_a_healthy_real_fleet_raises_nothing(world):
    _exp, _kube, cluster, agent, fleet = world
    run(cluster, agent, fleet, 30)
    assert agent.incidents == []


def test_idle_heat_flag_is_ignored_but_heat_under_load_is_not(world):
    exp, _kube, cluster, agent, fleet = world
    exp.nodes["gpu-node-2"][0].update({"DCGM_FI_DEV_CLOCKS_EVENT_REASONS": 0x20, "DCGM_FI_DEV_GPU_TEMP": 45})
    run(cluster, agent, fleet, 14)
    assert agent.incidents == [] and not cluster.fleet_odd(cluster.nodes["gpu-node-2"], cluster.nodes["gpu-node-2"].gpus[0])
    exp.nodes["gpu-node-2"][1].update({"DCGM_FI_DEV_CLOCKS_EVENT_REASONS": 0x40, "DCGM_FI_DEV_GPU_TEMP": 91,
                                        "DCGM_FI_DEV_GPU_UTIL": 99})
    run(cluster, agent, fleet, 14)
    inc = agent.incidents[0]
    assert inc["target"] == "gpu-node-2" and inc["category"] == "thermal"
    cap = next(a for a in inc["actions"] if a["action"] == "set_power_limit")
    assert 100 <= cap["params"]["watts"] <= 350, "within the GPU's own range"


# ----------------------------------------------------------- incidents
def test_xid_79_is_found_rehearsed_with_a_dry_run_and_fixed_through_kubernetes(world):
    exp, kube, cluster, agent, fleet = world
    run(cluster, agent, fleet, 6)
    exp.nodes["gpu-node-1"][0]["DCGM_FI_DEV_XID_ERRORS"] = 79
    run(cluster, agent, fleet, 12)
    inc = agent.incidents[0]
    assert inc["target"] == "gpu-node-1" and inc["status"] == "awaiting_approval" and "Xid 79" in inc["title"]
    assert [a["action"] for a in inc["actions"]] == ["drain_node", "restart_job_from_checkpoint", "open_ticket"]
    assert inc["dry_run"]["kind"] == "kubernetes" and inc["dry_run"]["verdict"] == "pass"
    assert kube.calls and all(c[-1] is True for c in kube.calls), "before approval, only dry runs"
    assert not kube.node_state["gpu-node-1"]["unschedulable"]
    agent.approve(inc["id"], run_all=True)
    assert ("cordon", "gpu-node-1", False) in kube.calls and ("evict", "ml/trainer-0", False) in kube.calls
    assert kube.node_state["gpu-node-1"]["unschedulable"]
    run(cluster, agent, fleet, 10)
    j = cluster.jobs["llm-train"]
    assert j.nodes == ["gpu-node-2"] and j.status == "running", "Kubernetes started it on the healthy server"
    assert inc["status"] == "waiting_on_technician" and cluster.nodes["gpu-node-1"].status == "drained"


def test_memory_errors_reset_the_gpu_after_the_job_moves(world):
    exp, kube, cluster, agent, fleet = world
    run(cluster, agent, fleet, 6)
    exp.nodes["gpu-node-1"][0].update({"DCGM_FI_DEV_ECC_DBE_VOL_TOTAL": 1, "DCGM_FI_DEV_ROW_REMAP_PENDING": 1})
    run(cluster, agent, fleet, 12)
    inc = agent.incidents[0]
    kinds = [a["action"] for a in inc["actions"]]
    assert inc["category"] == "memory" and kinds[:3] == ["drain_node", "restart_job_from_checkpoint", "reset_gpu"]
    assert inc["dry_run"]["verdict"] == "pass", inc["dry_run"]["lines"]
    agent.approve(inc["id"], run_all=True)
    smi = [c for c in kube.calls if c[0] == "smi" and c[-1] is False]
    assert smi and smi[0][1] == "gpu-node-1" and smi[0][2] == "nvidia-smi -i 0 -r"


def test_without_kubernetes_the_plan_is_revised_to_a_ticket():
    exp = FakeExporter({"gpu-node-1": [gpu()]})
    try:
        cluster, agent = make_agent(size=256, seed=3)
        fleet = RealFleet(dcgm.make_reader(exp.url), None, interval=0.1, kube_note="no kubeconfig")
        fleet.poll()
        cluster.attach_fleet(fleet)
        run(cluster, agent, fleet, 6)
        exp.nodes["gpu-node-1"][0]["DCGM_FI_DEV_XID_ERRORS"] = 79
        run(cluster, agent, fleet, 12)
        inc = agent.incidents[0]
        assert inc["revised"] and inc["dry_run"]["first_attempt"]["verdict"] == "fail"
        assert [a["action"] for a in inc["actions"]] == ["open_ticket"] and inc["dry_run"]["verdict"] == "pass"
        assert "Kubernetes isn't connected" in " ".join(inc["dry_run"]["first_attempt"]["lines"])
    finally:
        exp.close()


def test_a_crashing_workload_goes_to_its_owners(world):
    _exp, kube, cluster, agent, fleet = world
    run(cluster, agent, fleet, 6)
    p = kube.pod("trainer-0")
    p.update({"waiting": "CrashLoopBackOff", "restarts": 3,
              "terminated": {"reason": "OOMKilled", "exit_code": 137, "at": 1.0, "message": ""}})
    run(cluster, agent, fleet, 12)
    inc = agent.incidents[0]
    assert inc["category"] == "software" and [a["action"] for a in inc["actions"]] == ["notify_owner"]
    assert "OOMKilled" in cluster.jobs["llm-train"].last_error


def test_a_gpu_that_stops_reporting_is_lost_after_two_readings(world):
    exp, _kube, cluster, agent, fleet = world
    run(cluster, agent, fleet, 6)
    exp.nodes["gpu-node-2"] = exp.nodes["gpu-node-2"][:1]  # GPU 1 vanishes from DCGM
    fleet.poll()
    cluster.tick()
    assert not cluster.nodes["gpu-node-2"].gpus[1].lost
    run(cluster, agent, fleet, 12)
    assert cluster.nodes["gpu-node-2"].gpus[1].lost
    assert agent.incidents[0]["target"] == "gpu-node-2" and agent.incidents[0]["category"] == "gpu_lost"


def test_real_servers_refuse_unsafe_or_impossible_steps(world):
    _exp, kube, cluster, _agent, _fleet = world
    a, why = actions.normalize({"action": "reboot_node", "params": {"node": "gpu-node-2"}}, cluster)
    assert a is None and "doesn't reboot real servers" in why
    a, _ = actions.normalize({"action": "set_power_limit", "params": {"node": "gpu-node-2", "gpu": 0, "watts": 90}}, cluster)
    assert a is not None, "real GPUs have their own range, checked in the rehearsal"
    r = actions.dry_run(cluster, [a], "gpu-node-2")
    assert r["verdict"] == "fail" and "100-350 W" in r["lines"][0]
    busy, _ = actions.normalize({"action": "reset_gpu", "params": {"node": "gpu-node-1", "gpu": 0}}, cluster)
    r = actions.dry_run(cluster, [busy], "gpu-node-1")
    assert r["verdict"] == "fail" and "in use by llm-train" in r["lines"][0]
    kube.pdb_block = True
    move, _ = actions.normalize({"action": "restart_job_from_checkpoint", "params": {"job": "llm-train"}}, cluster)
    assert "PodDisruptionBudget" in actions.dry_run(cluster, [move], "gpu-node-1")["lines"][0]
    assert all(c[-1] is True for c in kube.calls)


def test_a_node_name_that_clashes_with_the_simulator_keeps_its_kubernetes_name():
    exp = FakeExporter({"dgx-0001": [gpu()]})
    kube = FakeKube(nodes=("dgx-0001",), gpus=1)
    try:
        cluster, _agent = make_agent(size=256, seed=3)
        fleet = RealFleet(dcgm.make_reader(exp.url), kube, interval=0.1)
        fleet.poll()
        cluster.attach_fleet(fleet)
        assert cluster.nodes["dgx-0001-real"].real and not cluster.nodes["dgx-0001"].real
        ok, _msg = cluster.drain_node("dgx-0001-real")
        assert ok and ("cordon", "dgx-0001", False) in kube.calls
        cluster.reset()
        assert "dgx-0001-real" in cluster.nodes, "resetting the simulation keeps the real servers"
    finally:
        exp.close()


# ----------------------------------------------------------------- pieces
def test_the_laptop_bridge_speaks_dcgm(tmp_path, monkeypatch):
    state = tmp_path / "s.json"
    state.write_text(json.dumps({"temp": 77, "util": 98, "reasons": "0x0000000000000040"}))
    monkeypatch.setenv("FAKE_SMI_STATE", str(state))
    src = LocalGPU(command=[sys.executable, os.path.join(HERE, "fake_smi.py")], events=False)
    assert src.probe()[0]
    text = bridge_render(src.snapshot()["gpus"], "my-laptop")
    g = dcgm.group(dcgm.parse_text(text))["my-laptop"]["gpus"][0]
    assert g["temp"] == 77 and g["util"] == 98 and g["reasons"] == ["HW_THERMAL_SLOWDOWN"] and g["model"]


def test_api_connects_and_disconnects_real_servers(monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main
    exp = FakeExporter({"gpu-node-9": [gpu()]})
    try:
        with TestClient(main.app) as client:
            r = client.post("/api/fleet", json={"url": exp.url, "kube": "off"})
            assert r.status_code == 200, r.text
            assert r.json()["servers"] == 1 and "Watching only" in r.json()["message"]
            st = client.get("/api/state").json()
            assert st["real"]["rack_of"]["gpu-node-9"] == "R-01"
            assert client.get("/api/gpu/gpu-node-9/0").json()["real"]
            assert client.post("/api/fleet", json={"url": "http://127.0.0.1:1/metrics", "kube": "off"}).status_code == 400
            assert client.post("/api/fleet", json={"enabled": False}).json()["connected"] is False
            assert "gpu-node-9" not in main.cluster.nodes
            monkeypatch.setattr(main.settings, "fleet_ui_connect", False)
            assert client.post("/api/fleet", json={"url": exp.url}).status_code == 403
    finally:
        exp.close()
