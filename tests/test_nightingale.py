"""End-to-end tests using the offline demo models (no API key needed)."""
import asyncio

import pytest

import app.llm as llm_module
from app import actions, mock_llm
from app.agent import Agent
from app.config import Settings
from app.llm import LLMClient, extract_json
from app.simulator import SCENARIOS, Cluster


@pytest.fixture(autouse=True)
def no_model_delay(monkeypatch):
    monkeypatch.setattr(llm_module.time, "sleep", lambda _s: None)


def make_agent(size=1024, seed=5, **overrides):
    settings = Settings()
    settings.force_mock = True
    for k, v in overrides.items():
        setattr(settings, k, v)
    cluster = Cluster(size, seed=seed)
    return cluster, Agent(cluster, LLMClient(settings), settings)


def drive(cluster, agent, ticks, approve=True):
    async def go():
        for t in range(1, ticks):
            cluster.tick()
            agent.verify_pending()
            if t % 3 == 0:
                await agent.recheck_waiting()
            if t % 5 == 0:
                await agent.triage_once()
                await asyncio.gather(*list(agent.tasks))
            if approve:
                for inc in agent.incidents:
                    if inc["status"] == "awaiting_approval":
                        agent.approve(inc["id"], run_all=True)
    asyncio.run(go())


@pytest.mark.parametrize("size,servers,racks,halls", [(256, 32, 4, 1), (1024, 128, 16, 1), (4096, 512, 64, 4)])
def test_data_center_shapes(size, servers, racks, halls):
    c = Cluster(size, seed=1)
    assert len(c.nodes) == servers and len(c.racks) == racks and len(c.halls) == halls
    assert all(len(r.nodes) == 8 for r in c.racks.values())
    assert c.spare_nodes(), "a fleet always keeps some spares"
    assert c.has_open_ticket(c.down_node)


def test_triage_window_does_not_grow_with_the_fleet():
    sizes = {}
    for size in (256, 4096):
        c = Cluster(size, seed=2)
        for _ in range(6):
            c.tick()
        sizes[size] = len(c.digest(0)[0])
    assert sizes[4096] < sizes[256] * 1.5


def test_healthy_fleet_has_no_findings():
    c = Cluster(1024, seed=3)
    c.digest(0)
    for _ in range(20):
        c.tick()
    assert mock_llm.triage(c.digest(0)[0])["findings"] == []


@pytest.mark.parametrize("kind", list(SCENARIOS))
def test_each_drill_is_found_fixed_and_verified(kind):
    cluster, agent = make_agent()
    info = cluster.inject(kind)
    drive(cluster, agent, 90)
    assert len(agent.incidents) == 1, [(i["target"], i["title"]) for i in agent.incidents]
    inc = agent.incidents[0]
    assert inc["target"] == info["target"]
    assert inc["status"] in ("resolved", "waiting_on_technician"), inc["timeline"]
    assert inc["trace"], "the investigator used tools"
    assert inc["dry_run"]["verdict"] in ("pass", "warn")
    states = {name: job.status for name, job in cluster.jobs.items()}
    if kind == "app_bug":  # a code bug is the owner's to fix; Nightingale only tells them
        assert "failed" in states.values(), states
    else:  # every hardware drill ends with all training running again
        assert set(states.values()) == {"running"}, states


def test_app_bug_gets_no_hardware_actions():
    cluster, agent = make_agent(seed=2)
    cluster.inject("app_bug")
    drive(cluster, agent, 15, approve=False)
    inc = agent.incidents[0]
    assert inc["category"] == "software"
    assert {a["action"] for a in inc["actions"]} == {"notify_owner"}


def test_failed_dry_run_makes_the_model_revise_the_plan():
    # 256 GPUs have too few spares to move a whole rack, so the first plan fails its dry run.
    cluster, agent = make_agent(size=256, seed=2)
    cluster.inject("rack_cooling")
    drive(cluster, agent, 30, approve=False)
    inc = agent.incidents[0]
    assert inc["revised"] is True
    assert inc["dry_run"]["first_attempt"]["verdict"] == "fail"
    assert inc["dry_run"]["verdict"] == "pass"
    assert inc["actions"][0]["action"] == "set_rack_power_limit"


def test_waiting_plan_is_rechecked_when_the_job_crashes():
    cluster, agent = make_agent()
    cluster.inject("ecc")
    drive(cluster, agent, 90, approve=False)
    inc = agent.incidents[0]
    assert cluster.jobs["llm-pretrain-405b"].status == "crashed"
    assert "crashed" in inc["situation"]
    assert inc["dry_run"]["verdict"] == "pass"


def test_dry_run_does_not_touch_the_real_data_center():
    c = Cluster(1024, seed=4)
    node = c.jobs["llm-pretrain-405b"].nodes[3]
    plan = [actions.normalize({"action": "drain_node", "params": {"node": node}}, c)[0],
            actions.normalize({"action": "restart_job_from_checkpoint", "params": {"job": "llm-pretrain-405b"}}, c)[0]]
    result = actions.dry_run(c, plan, node)
    assert all(s["ok"] for s in result["steps"])
    assert c.nodes[node].status == "active"
    assert c.jobs["llm-pretrain-405b"].status == "running"


def test_unsafe_steps_are_blocked():
    c = Cluster(1024, seed=1)
    busy = c.jobs["llm-pretrain-405b"].nodes[0]
    assert not c.reset_gpu(busy, 0)[0]
    assert not c.reboot_node(busy)[0]
    assert not c.undrain_node(c.down_node)[0]


def test_invalid_proposals_are_rejected():
    c = Cluster(1024, seed=1)
    assert actions.normalize({"action": "format_disk", "params": {}}, c)[0] is None
    assert actions.normalize({"action": "reset_gpu", "params": {"node": "dgx-9999", "gpu": 1}}, c)[0] is None
    assert actions.normalize({"action": "set_rack_power_limit", "params": {"rack": "Z-99", "watts": 500}}, c)[0] is None
    assert actions.normalize({"action": "set_power_limit", "params": {"node": "dgx-0001", "gpu": 0, "watts": 50}}, c)[0] is None
    ok, _ = actions.normalize({"action": "drain_node", "params": {"node": "dgx-53"}}, c)
    assert ok["params"]["node"] == "dgx-0053"


def test_tools_answer_and_fail_safely():
    c = Cluster(1024, seed=1)
    assert "rack A-01" in c.run_tool("node_status", {"node": "dgx-0001"})
    assert "Unknown tool" in c.run_tool("rm_rf", {})
    assert "No server" in c.run_tool("gpu_history", {"node": "dgx-9999", "gpu": 1})
    assert "Bad arguments" in c.run_tool("node_status", {})
    assert "No job" in c.run_tool("job_info", {"job": "nope"})
    assert "incidents on" in c.run_tool("incident_history", {"target": c.down_node})


def test_extract_json_handles_reasoning_and_fences():
    text = '<think>{"not": "this"}</think>\nSure!\n```json\n{"findings": [{"target": "dgx-0001"}]}\n```'
    assert extract_json(text) == {"findings": [{"target": "dgx-0001"}]}
