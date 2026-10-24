"""This computer's GPU as a real source, using a stand-in for nvidia-smi."""
import asyncio
import json
import os
import sys

import pytest

import app.llm as llm_module
from app import actions
from app.local_gpu import LocalGPU, decode_reasons, parse_process_table, parse_query
from test_nightingale import make_agent

FAKE = [sys.executable, os.path.join(os.path.dirname(__file__), "fake_smi.py")]
THROTTLING = {"temp": 88, "power": 112, "util": 99, "clock": 1410, "reasons": "0x0000000000000044",
              "pstate": "P0", "mem_used": 6100, "fan": 78}


@pytest.fixture(autouse=True)
def no_model_delay(monkeypatch):
    monkeypatch.setattr(llm_module.time, "sleep", lambda _s: None)


@pytest.fixture
def gpu_state(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text("{}")
    monkeypatch.setenv("FAKE_SMI_STATE", str(path))
    return lambda **kv: path.write_text(json.dumps(kv))


def connect(size=256):
    cluster, agent = make_agent(size=size, seed=3)
    src = LocalGPU(command=FAKE, events=False)
    ok, msg = src.probe()
    assert ok, msg
    cluster.attach_local(src)
    return cluster, agent, src


def run(cluster, agent, src, ticks):
    async def go():
        for t in range(1, ticks):
            src.poll()
            cluster.tick()
            agent.verify_pending()
            if t % 5 == 0:
                await agent.triage_once()
                await asyncio.gather(*list(agent.tasks))
    asyncio.run(go())


def test_parsing_handles_missing_values_and_reasons():
    fields = ["index", "name", "temperature.gpu", "power.draw", "fan.speed", "clocks_event_reasons.active"]
    row = parse_query(fields, "0, NVIDIA GeForce RTX 4070 Laptop GPU, 71, [N/A], [Not Supported], 0x0000000000000044")[0]
    assert row["name"] == "NVIDIA GeForce RTX 4070 Laptop GPU" and row["temp"] == 71
    assert row["power"] is None and row["fan"] is None
    assert row["reasons"] == ["SW_POWER_CAP", "HW_THERMAL_SLOWDOWN"]
    assert decode_reasons("[N/A]") == [] and decode_reasons("0x1") == ["GPU_IDLE"]
    table = "|    0   N/A  N/A     14820    C+G   C:\\Program Files\\Game\\game.exe          N/A      |"
    assert parse_process_table(table)[0]["name"] == "game.exe"


def test_no_nvidia_smi_is_a_clear_message():
    ok, msg = LocalGPU(command=None).probe()
    assert not ok and "nvidia-smi was not found" in msg


def test_laptop_joins_the_fleet_as_its_own_server(gpu_state):
    cluster, _agent, src = connect()
    run(cluster, _agent, src, 4)
    assert "L" in cluster.halls and cluster.canonical_node("this laptop") == "laptop"
    detail = cluster.gpu_detail("laptop", 0)
    assert detail["local"] and detail["model"].endswith("Laptop GPU") and detail["temp"] == 52
    assert cluster.snapshot()["servers"] == 32 and cluster.gauges()["total"] == 257
    assert not cluster.is_spare(cluster.nodes["laptop"]), "never a spare for data center jobs"
    # Data center actions never target the laptop; only the pc_ fixes do.
    for raw in ({"action": "drain_node", "params": {"node": "laptop"}},
                {"action": "set_power_limit", "params": {"node": "laptop", "gpu": 0, "watts": 400}},
                {"action": "reboot_node", "params": {"node": "laptop"}}):
        assert actions.normalize(raw, cluster)[0] is None
    assert actions.normalize({"action": "recommend", "params": {"node": "laptop", "message": "Raise the back."}}, cluster)[0]
    assert actions.normalize({"action": "recommend", "params": {"node": "dgx-0001", "message": "hi"}}, cluster)[0] is None


def test_healthy_laptop_raises_nothing(gpu_state):
    cluster, agent, src = connect()
    run(cluster, agent, src, 30)
    assert agent.incidents == []


def test_heat_slowdown_in_light_mode_locks_the_serious_fixes(gpu_state):
    cluster, agent, src = connect()
    gpu_state(**THROTTLING)
    run(cluster, agent, src, 16)
    assert len(agent.incidents) == 1
    inc = agent.incidents[0]
    assert inc["target"] == "laptop" and inc["status"] == "awaiting_approval"
    kinds = [a["action"] for a in inc["actions"]]
    assert kinds[0] == "recommend" and "pc_cap_gpu_clock" in kinds and "pc_performance_mode" in kinds
    assert inc["dry_run"]["checked"] and inc["dry_run"]["verdict"] in ("pass", "fail")
    assert any(s["locked"] for s in inc["dry_run"]["steps"]), "serious fixes are locked in light mode"
    assert any(t["tool"] == "gpu_history" for t in inc["trace"])
    agent.approve(inc["id"], run_all=True)  # runs the light steps, stops at the first locked one
    nxt = agent._next_pending(inc)
    assert nxt["mode"] == "serious"
    with pytest.raises(ValueError, match="serious mode"):
        agent.approve(inc["id"])
    while agent._next_pending(inc):
        agent.skip(inc["id"])
    gpu_state()  # the person raised the laptop: back to normal
    run(cluster, agent, src, 20)
    assert inc["status"] == "resolved", inc["verification"]


def test_a_vanished_gpu_is_an_incident(gpu_state):
    cluster, agent, src = connect()
    gpu_state(lost=True)
    run(cluster, agent, src, 8)
    assert cluster.nodes["laptop"].gpus[0].lost
    inc = agent.incidents[0]
    assert inc["target"] == "laptop" and "answering" in inc["title"]


def test_disconnect_removes_the_laptop(gpu_state):
    cluster, agent, src = connect()
    cluster.detach_local()
    assert "laptop" not in cluster.nodes and "L" not in cluster.halls
    cluster.tick()


def test_idle_power_policy_flag_is_not_a_problem(gpu_state):
    """What this RTX 3050 laptop really did: SW_THERMAL_SLOWDOWN at 38-49C with 0-7% load (the laptop's own
    power policy). Nothing is slowed, so no incident, no warning and no Nemotron call; under load it counts."""
    cluster, agent, src = connect()
    gpu_state(temp=46, power=9, util=3, clock=1500, reasons="0x0000000000000020", pstate="P0")
    run(cluster, agent, src, 30)
    g = cluster.nodes["laptop"].gpus[0]
    assert g.reasons == () and g.policy_flags == ("SW_THERMAL_SLOWDOWN",)
    assert not cluster.local_odd(g) and agent.incidents == []
    assert not any(l["node"] == "laptop" and l["level"] == "warn" for l in cluster.logs)
    assert "power policy" in cluster.run_tool("node_status", {"node": "laptop"})
    gpu_state(temp=71, power=60, util=96, clock=1500, reasons="0x0000000000000020", pstate="P0")
    run(cluster, agent, src, 16)
    assert g.reasons == ("SW_THERMAL_SLOWDOWN",) and cluster.local_odd(g)
    assert any("clocks reduced" in l["text"] for l in cluster.logs if l["node"] == "laptop")
