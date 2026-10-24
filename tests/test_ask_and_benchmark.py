"""Ask your data center, and the benchmark."""
import asyncio
import os
import time

os.environ["MOCK_MODE"] = "1"

import pytest  # noqa: E402

import app.llm as llm_module  # noqa: E402
from app.agent import Agent  # noqa: E402
from app.benchmark import Benchmark  # noqa: E402
from app.config import Settings  # noqa: E402
from app.llm import LLMClient  # noqa: E402
from app.simulator import Cluster  # noqa: E402


@pytest.fixture(autouse=True)
def no_model_delay(monkeypatch):
    monkeypatch.setattr(llm_module.time, "sleep", lambda _s: None)


def make(size=1024, seed=2):
    settings = Settings()
    settings.force_mock = True
    cluster = Cluster(size, seed=seed)
    for _ in range(8):
        cluster.tick()
    return cluster, Agent(cluster, LLMClient(settings), settings)


def ask(agent, question):
    async def go():
        entry = agent.ask(question)
        await asyncio.gather(*list(agent.tasks))
        return entry
    return asyncio.run(go())


def test_questions_are_answered_from_tools_with_links():
    cluster, agent = make()
    e = ask(agent, "Tell me about dgx-0111")
    assert e["status"] == "done" and "dgx-0111" in e["answer"]
    assert [t["tool"] for t in e["trace"]] == ["node_status", "incident_history"]
    assert "dgx-0111" in e["refs"] and "A-14" in e["refs"]
    e = ask(agent, "Which rack is running hottest?")
    assert e["trace"][0]["tool"] == "fleet_overview" and "°C" in e["answer"]
    assert agent.llm.usage.summary()["ask"]["calls"] >= 4


def test_the_overview_names_gpus_under_watch():
    cluster, agent = make()
    info = cluster.inject("ecc")
    for _ in range(10):
        cluster.tick()
    overview = cluster.run_tool("fleet_overview", {})
    assert f"{info['node']} GPU {info['gpu']}: corrected memory errors rising" in overview
    assert "known: repair ticket open" in overview  # the pre-seeded broken GPU
    e = ask(agent, "Is anything wrong right now?")
    assert info["node"] in e["answer"]


def test_an_answer_without_checking_data_is_sent_back():
    cluster, agent = make()
    replies = iter([{"thought": "I know this", "answer": "Everything is fine."},
                    {"thought": "check", "tool": "fleet_overview", "args": {}},
                    {"thought": "ok", "answer": "1,023 GPUs are healthy.", "refs": []}])
    agent.llm.ask_step = lambda _m, _s: next(replies)
    e = ask(agent, "Is everything fine?")
    assert e["answer"] == "1,023 GPUs are healthy." and len(e["trace"]) == 1


def test_the_incidents_tool_lists_status_and_cause():
    cluster, agent = make()
    assert "No incidents yet" in agent.incidents_text()
    inc = agent._new_incident("dgx-0005", {"target": "dgx-0005", "signal": "hot GPU", "severity": "incident"})
    inc.update({"status": "awaiting_approval", "title": "GPU 3 overheating", "root_cause": "fan failure"})
    text = agent.incidents_text()
    assert "#1 dgx-0005: plan waiting for approval. GPU 3 overheating. Root cause: fan failure" in text


def test_benchmark_runs_every_drill_type_and_scores_it():
    settings = Settings()
    settings.force_mock = True
    bench = Benchmark(settings, LLMClient(settings))
    st = asyncio.run(bench.run(6, 256))
    r = st["results"]
    assert st["error"] == "" and r["n"] == 6 and len(r["by_kind"]) == 6
    assert r["found"] == r["cause"] == r["fixed"] == r["safe"] == 100
    assert all(c["incident"] for c in st["cases"])


def test_benchmark_counts_a_miss():
    settings = Settings()
    settings.force_mock = True
    bench = Benchmark(settings, LLMClient(settings))
    original = LLMClient.triage
    LLMClient.triage = lambda self, digest: {"findings": []}  # a triage model that sees nothing
    try:
        st = asyncio.run(bench.run(2, 256))
    finally:
        LLMClient.triage = original
    assert st["results"]["found"] == 0 and st["results"]["fixed"] == 0
    assert all("no incident" in c["why"] for c in st["cases"])


def test_ask_and_benchmark_api():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        client.post("/api/reset", json={"size": 1024})
        assert client.post("/api/ask", json={"question": "How much power are we using?"}).status_code == 200
        for _ in range(40):
            asks = client.get("/api/ask").json()["asks"]
            if asks[-1]["status"] != "thinking":
                break
            time.sleep(0.2)
        assert asks[-1]["status"] == "done" and "kW" in asks[-1]["answer"]
        assert client.post("/api/benchmark", json={"n": 2, "size": 333}).status_code == 400
        assert client.get("/api/benchmark").json()["counts"] == [6, 12, 30]
