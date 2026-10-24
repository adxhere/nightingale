"""This computer's history survives restarts."""
import time

from app.agent import Agent
from app.config import Settings
from app.llm import LLMClient
from app.simulator import GPU, Cluster
from app.store import Store


def test_samples_fixes_incidents_and_memory_are_kept(tmp_path):
    path = tmp_path / "n.db"
    s = Store(path)
    g = GPU(0, "x")
    g.temp, g.power, g.clock, g.max_clock, g.util, g.reasons = 88, 70, 1400, 1740, 99, ("HW_THERMAL_SLOWDOWN",)
    now = time.time()
    for i in range(30):
        s.add_sample(g, ts=now - 300 + i * 10)
    h = s.history("1h")
    assert h["points"] and h["points"][-1]["heat_slowdown"]
    assert s.summary("24h")["heat_minutes"] == 5
    s.log_fix("Cap the GPU clock at 1485 MHz", True, "Capped.", "serious", 3)
    s.remember(0, "Slowing down to stay cool", "Cap the GPU clock")
    s.save_incidents([{"id": 7, "target": "laptop", "status": "awaiting_approval", "opened_at": now, "title": "Hot",
                       "timeline": []},
                      {"id": 8, "target": "dgx-0001", "status": "resolved"}])  # only this computer's are kept
    again = Store(path)  # a restart
    assert again.fixes()[0]["label"].startswith("Cap the GPU clock")
    assert again.memories()[0]["title"] == "Slowing down to stay cool"
    assert [i["id"] for i in again.load_incidents()] == [7]


def test_agent_restores_history_and_keeps_counting(tmp_path):
    store = Store(tmp_path / "n.db")
    store.save_incidents([{"id": 41, "target": "laptop", "status": "investigating", "opened_at": time.time(),
                           "title": "Hot", "timeline": []}])
    settings = Settings()
    settings.force_mock = True
    agent = Agent(Cluster(256, seed=1), LLMClient(settings), settings)
    agent.store = store
    agent.reset()
    assert agent.load_history() == 1
    old = agent.incidents[0]
    assert old["status"] == "closed" and "restarted" in old["verification"]
    assert next(agent._ids) == 42, "new incidents never reuse a saved number"
