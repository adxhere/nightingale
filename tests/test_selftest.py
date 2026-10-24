"""GPU self-tests: the runner, its findings, and the safety stop, using a numpy stand-in for PyTorch."""
import asyncio
import json
import os
import sys
import time

import pytest

import importlib.util

needs_numpy = pytest.mark.skipif(importlib.util.find_spec("numpy") is None,
                                 reason="the PyTorch stand-in needs numpy: pip install -r requirements-dev.txt")

import app.llm as llm_module
from app.alerts import Alerts
from app.local_gpu import LocalGPU
from app.selftest_runner import SelfTests
from app.store import Store
from test_nightingale import make_agent

HERE = os.path.dirname(__file__)
FAKE_SMI = [sys.executable, os.path.join(HERE, "fake_smi.py")]
FAKE_TORCH = {"PYTHONPATH": os.path.join(HERE, "fake_torch"), "NIGHTINGALE_SELFTEST_CPU": "1"}


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(llm_module.time, "sleep", lambda _s: None)


@pytest.fixture
def gpu_state(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text("{}")
    monkeypatch.setenv("FAKE_SMI_STATE", str(path))
    return lambda **kv: path.write_text(json.dumps(kv))


def setup(extra=None):
    cluster, agent = make_agent(size=256, seed=3)
    src = LocalGPU(command=FAKE_SMI, events=False)
    assert src.probe()[0]
    cluster.attach_local(src)
    runner = SelfTests(cluster, Store(":memory:"), extra_env={**FAKE_TORCH, **(extra or {})})
    return cluster, agent, src, runner


def wait(runner, seconds=60):
    end = time.time() + seconds
    while runner.status()["running"] and time.time() < end:
        time.sleep(0.2)
    return runner.status()


@needs_numpy
def test_quick_check_passes_and_is_logged(gpu_state):
    cluster, agent, src, runner = setup()
    runner.start("quick")
    assert "running on purpose" in cluster.local_activity
    st = wait(runner)
    assert st["result"]["ok"] and "Computes correctly" in st["result"]["summary"]
    assert cluster.local_activity == ""
    assert any(l["source"] == "selftest" and "passed" in l["text"] for l in cluster.logs)
    assert "PASSED" in cluster.run_tool("selftest_results", {"node": "laptop"})
    assert runner.store.selftests()[0]["test"] == "quick"


@needs_numpy
def test_memory_errors_become_an_incident(gpu_state):
    cluster, agent, src, runner = setup({"FAKE_TORCH_FLIP": "2"})
    runner.start("memory")
    st = wait(runner)
    assert not st["result"]["ok"] and "2 memory errors" in st["result"]["summary"]
    assert any(l["level"] == "error" and "FAILED" in l["text"] for l in cluster.logs)

    async def go():
        await agent.triage_once()
        await asyncio.gather(*list(agent.tasks))
    asyncio.run(go())
    inc = agent.incidents[0]
    assert inc["target"] == "laptop" and inc["category"] == "memory" and inc["needs_technician"]
    assert inc["trace"][0]["tool"] == "selftest_results"


@needs_numpy
def test_stop_and_the_95_degree_safety_stop(gpu_state):
    cluster, agent, src, runner = setup()
    runner.start("stress", minutes=5)
    time.sleep(1.5)
    gpu_state(temp=96, util=100, power=95, clock=1200, reasons="0x0000000000000040", pstate="P0")
    src.poll()
    cluster.tick()
    runner.check_temperature()
    st = wait(runner)
    assert not st["running"] and "95°C" in st["error"]
    assert st["history"][0]["stopped"]


def test_missing_pytorch_says_how_to_install(gpu_state):
    cluster, agent, src, runner = setup()
    runner.extra_env = {"PYTHONPATH": ""}  # no stand-in: PyTorch isn't installed here
    runner.start("quick")
    st = wait(runner)
    assert st["result"] is None and "download.pytorch.org/whl/cu126" in st["error"]


def test_only_one_test_at_a_time_and_needs_the_gpu():
    cluster, agent, src, runner = setup()
    runner.start("stress", minutes=5)
    with pytest.raises(ValueError, match="already running"):
        runner.start("quick")
    runner.stop()
    wait(runner)
    cluster.detach_local()
    with pytest.raises(ValueError, match="Connect"):
        runner.start("quick")


def test_alerts_fire_once_per_event():
    a = Alerts()
    inc = {"id": 1, "target": "laptop", "title": "Hot GPU", "trigger": {"signal": "hot"}, "status": "awaiting_approval",
           "actions": [{"status": "pending", "label": "Cap the GPU clock"}], "timeline": [], "error": "", "verification": ""}
    a.scan([inc])
    a.scan([inc])
    out = a.take()
    assert len(out) == 1 and "Cap the GPU clock" in out[0]["message"]
    dc = dict(inc, id=2, target="dgx-0001")
    a.scan([dc])
    assert a.take() == [], "data center alerts are off by default"
    inc["status"] = "resolved"
    a.scan([inc])
    assert a.take()[0]["title"] == "Fixed"


def test_alerts_fit_windows_notifications():
    a = Alerts()
    inc = {"id": 7, "target": "laptop", "title": "Laptop GPU premature thermal throttling at low temperature",
           "trigger": {"signal": "x"}, "status": "awaiting_approval", "actions": [], "timeline": [], "error": "",
           "verification": ""}
    a.scan([inc])
    out = a.take()[0]
    assert len(out["title"]) <= 63 and out["title"].endswith("…") and len(out["message"]) <= 255
