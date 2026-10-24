"""Fixes for things seen in the first live run with real Nemotron models."""
import asyncio
import os

os.environ["MOCK_MODE"] = "1"

import pytest  # noqa: E402

import app.llm as llm_module  # noqa: E402
from app.agent import Agent, diagnosis_of  # noqa: E402
from app.config import Settings  # noqa: E402
from app.llm import LLMClient  # noqa: E402
from app.simulator import Cluster  # noqa: E402

GOOD = {"title": "Memory errors climbing on dgx-0034 GPU 2", "category": "memory", "scope": "gpu",
        "severity": "high", "confidence": 0.9, "summary": "GPU 2 is logging corrected memory errors fast.",
        "root_cause": "A failing memory row on GPU 2.", "evidence": ["sbe rising"],
        "actions": [{"action": "notify_owner", "params": {"job": "moe-finetune-1t", "message": "heads up"},
                     "reason": "test"}],
        "needs_technician": False, "technician_note": ""}


@pytest.fixture(autouse=True)
def no_model_delay(monkeypatch):
    monkeypatch.setattr(llm_module.time, "sleep", lambda _s: None)


def scripted_agent(script):
    """An agent whose investigation model replies with the given JSON objects, in order."""
    settings = Settings()
    settings.force_mock = True
    cluster = Cluster(1024, seed=3)
    llm = LLMClient(settings)
    replies = iter(script)
    seen = []
    llm.investigate_step = lambda messages, _state: (seen.append(messages[-1]["content"]), next(replies))[1]
    agent = Agent(cluster, llm, settings)
    node = cluster.jobs["moe-finetune-1t"].nodes[0]
    inc = agent._new_incident(node, {"target": node, "signal": "test signal", "severity": "incident"})
    return agent, inc, seen


def test_a_nested_final_answer_is_unwrapped():
    assert diagnosis_of({"final": {"diagnosis": GOOD}})["title"] == GOOD["title"]
    assert diagnosis_of({"final": GOOD})["title"] == GOOD["title"]
    assert diagnosis_of({"final": {"status": "done"}}) is None


def test_a_final_answer_in_the_wrong_shape_is_sent_back_once():
    agent, inc, seen = scripted_agent([
        {"thought": "look", "tool": "node_status", "args": {"node": "dgx-0001"}},
        {"thought": "done", "final": {"answer": "it is the memory"}},   # wrong shape
        {"thought": "fixed", "final": GOOD},
    ])
    asyncio.run(agent.investigate(inc))
    assert inc["title"] == GOOD["title"] and inc["summary"]
    assert inc["status"] == "awaiting_approval"
    assert any("missing the diagnosis fields" in m for m in seen)


def test_a_repeated_tool_call_is_not_counted_or_shown():
    call = {"thought": "again", "tool": "node_status", "args": {"node": "dgx-0001"}}
    agent, inc, seen = scripted_agent([call, call, call, {"thought": "ok", "final": GOOD}])
    asyncio.run(agent.investigate(inc))
    assert len(inc["trace"]) == 1
    assert inc["status"] == "awaiting_approval"


def test_a_stuck_incident_blocks_duplicates_and_can_be_retried_or_closed():
    agent, inc, _ = scripted_agent([{"thought": "?", "final": {}}, {"thought": "?", "final": {}}])
    asyncio.run(agent.investigate(inc))
    assert inc["status"] == "needs_attention" and inc["error"]
    assert agent._covered(inc["target"]), "triage must not open a second incident for the same server"

    agent.llm.investigate_step = lambda _m, _s: {"thought": "ok", "final": GOOD}

    async def again():
        agent.retry(inc["id"])
        await asyncio.gather(*list(agent.tasks))
    asyncio.run(again())
    assert inc["status"] == "awaiting_approval" and inc["error"] == ""
    agent.close(inc["id"])
    assert inc["status"] == "closed"
    assert not agent._covered(inc["target"])


def test_drills_use_another_running_job_when_the_main_one_is_down():
    c = Cluster(1024, seed=1)
    c.jobs["llm-pretrain-405b"].status = "crashed"
    info = c.inject("thermal")
    assert info["node"] in c.jobs["moe-finetune-1t"].nodes
    for job in c.jobs.values():
        job.status = "crashed"
    with pytest.raises(ValueError, match="Every training job is stopped"):
        c.inject("ecc")


def test_incident_history_understands_gpu_names():
    c = Cluster(4096, seed=1)
    for name in ("dgx-0020:4", "dgx-0020 GPU 4", "dgx-0020/g4", "dgx-0020 4"):
        assert "dgx-0020 GPU 4" in c.run_tool("incident_history", {"target": name}), name
    assert c.canonical_node("dgx-0020:4") is None, "the GPU number must not turn dgx-0020 into dgx-0204"
    assert c.canonical_node("dgx-53") == "dgx-0053"


def test_four_drills_at_once_all_end_well():
    """Two incidents that both restart the same job: the second joins the restart instead of failing."""
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from test_nightingale import drive, make_agent
    cluster, agent = make_agent(size=1024, seed=5)
    for kind in ("thermal", "ecc", "bus", "app_bug"):
        cluster.inject(kind)
    drive(cluster, agent, 140)
    assert len(agent.incidents) == 4
    assert {i["status"] for i in agent.incidents} <= {"resolved", "waiting_on_technician"}
    assert cluster.jobs["llm-pretrain-405b"].status == "running"
