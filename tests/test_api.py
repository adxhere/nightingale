"""Smoke tests for the HTTP API (demo mode)."""
import os

os.environ["MOCK_MODE"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def test_state_rack_gpu_and_drill():
    assert client.post("/api/reset", json={"size": 1024}).json()["size"] == 1024
    state = client.get("/api/state").json()
    assert state["mode"] == "demo" and state["size"] == 1024
    assert sum(len(h["racks"]) for h in state["fleet"]) == 16
    assert len(state["fleet"][0]["racks"][0]["temps"]) == 64
    rack = client.get("/api/rack/A-07").json()
    assert len(rack["servers"]) == 8 and len(rack["servers"][0]["gpus"]) == 8
    gpu = client.get(f"/api/gpu/{rack['servers'][0]['name']}/3").json()
    assert gpu["index"] == 3 and "history" in gpu
    assert client.get("/api/rack/Z-99").status_code == 404
    assert client.post("/api/inject", json={"kind": "rack_cooling"}).json()["rack"]
    assert client.post("/api/inject", json={"kind": "nope"}).status_code == 400


def test_resize():
    assert client.post("/api/reset", json={"size": 4096}).json()["size"] == 4096
    assert len(client.get("/api/state").json()["halls"]) == 4
    client.post("/api/reset", json={"size": 1024})


def test_analyze_sample_log():
    with open("samples/xid79_incident.log", encoding="utf-8") as fh:
        res = client.post("/api/analyze", json={"logs": fh.read()})
    assert res.status_code == 200
    body = res.json()
    assert body["category"] == "gpu_lost" and body["actions"]


def test_approve_unknown_incident():
    assert client.post("/api/incidents/999/approve", json={}).status_code == 404
