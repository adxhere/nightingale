"""Nightingale web app: background monitoring loop + JSON API + dashboard."""
from __future__ import annotations

import asyncio
import copy
import sys
import itertools
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import Agent
from .benchmark import COUNTS
from .benchmark import SIZES as BENCH_SIZES
from .benchmark import Benchmark
from .config import load_settings
from .llm import LLMClient
from . import actions as actions_mod
from . import autostart
from .alerts import Alerts
from .local_gpu import LocalGPU
from .modes import Modes
from .pc_actions import PC_CATALOG, make_default
from .selftest_runner import TESTS, SelfTests
from .simulator import LOCAL_NODE, Cluster
from .store import Store

log = logging.getLogger("nightingale")
STATIC = Path(__file__).resolve().parent.parent / "static"

settings = load_settings()
cluster = Cluster(size=settings.num_gpus, seed=settings.seed)
cluster.pc = make_default()  # runs approved fixes on this computer (Windows)
if settings.demo_pc:
    from .demo_pc import demo_smi_command, make_demo_fixes
    cluster.pc = make_demo_fixes()
llm = LLMClient(settings)
modes = Modes()
store = Store()
agent = Agent(cluster, llm, settings, modes)
agent.store = store
agent._ids = itertools.count(agent._id_floor())
agent.load_history()
bench = Benchmark(settings, llm)
selftests = SelfTests(cluster, store)
cluster.selftests = selftests.history[:5]
alerts = Alerts()
_cache: dict = {}
TRAY = {"running": False}  # set by the tray app when it hosts the server


def cached(key: str, seconds: float, fn):
    """Slow Windows queries (driver version, power plan) refreshed at most every few minutes."""
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < seconds:
        return hit[1]
    value = fn()
    _cache[key] = (time.time(), value)
    return value


def _ago(ts: float) -> str:
    days = int((time.time() - ts) // 86400)
    return "today" if days == 0 else "yesterday" if days == 1 else f"{days} days ago"


async def monitor_loop() -> None:
    while True:
        try:
            cluster.tick()
            agent.verify_pending()
            note = modes.check(agent.incidents)
            if note:
                cluster._log(LOCAL_NODE if cluster.local is not None else "fleet", "nightingale", "info", note)
                agent.spawn(agent.mode_changed())
            selftests.check_temperature()
            alerts.scan(agent.incidents, note)
            if cluster.local is not None and LOCAL_NODE in cluster.nodes and cluster.tick_count % 5 == 0:
                for g in cluster.nodes[LOCAL_NODE].gpus:
                    if not g.lost and g.max_clock:
                        store.add_sample(g)
            if cluster.tick_count % 10 == 0:
                store.save_incidents(agent.incidents)
            if cluster.tick_count % 3 == 0:
                agent.spawn(agent.recheck_waiting())
            if cluster.tick_count % settings.triage_every_ticks == 0:
                agent.spawn(agent.triage_once())
        except Exception:  # never let one bad tick stop monitoring
            log.exception("monitor tick failed")
        await asyncio.sleep(settings.tick_seconds)


async def connect_local() -> tuple[bool, str]:
    """Start reading this computer's GPU and add it to the data center as server 'laptop'."""
    if settings.demo_pc:
        src = LocalGPU(command=demo_smi_command(), interval=settings.local_poll_seconds, events=False)
        src.demo = True
    else:
        src = LocalGPU(interval=settings.local_poll_seconds)
    ok, msg = await asyncio.to_thread(src.probe)
    if not ok:
        return False, msg
    src.start()
    cluster.attach_local(src)
    known = {(h.get("title"), h.get("when")) for h in cluster.history if h.get("target") == LOCAL_NODE}
    for m in store.memories():  # fleet memory for this computer, from earlier runs
        row = {"target": LOCAL_NODE, "gpu": m["gpu"], "when": _ago(m["ts"]), "title": m["title"], "fix": m["fix"]}
        if (row["title"], row["when"]) not in known:
            cluster.history.append(row)
    return True, msg


def disconnect_local() -> None:
    if cluster.local is not None:
        cluster.local.stop()
        cluster.detach_local()
        agent.local_disconnected()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if not llm.mock and settings.auto_discover_models:
        await asyncio.to_thread(llm.discover_models)
    if settings.local_gpu:
        ok, msg = await connect_local()
        log.info("This computer's GPU: %s", msg)
    task = asyncio.create_task(monitor_loop())
    yield
    task.cancel()
    store.save_incidents(agent.incidents)
    if cluster.local is not None:
        cluster.local.stop()


app = FastAPI(title="Nightingale", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class InjectBody(BaseModel):
    kind: str


class ApproveBody(BaseModel):
    all: bool = False


class ResetBody(BaseModel):
    size: int | None = None


class LocalBody(BaseModel):
    enabled: bool = True


class AskBody(BaseModel):
    question: str = Field(min_length=2, max_length=500)


class BenchBody(BaseModel):
    n: int = Field(default=12, ge=1, le=60)
    size: int = 1024


class ModeBody(BaseModel):
    mode: str
    incident: int | None = None


class FixBody(BaseModel):
    action: str
    params: dict = Field(default_factory=dict)


class StepBody(BaseModel):
    index: int


class SelfTestBody(BaseModel):
    test: str
    minutes: float = 3


class ToggleBody(BaseModel):
    enabled: bool
    datacenter: bool | None = None


class AnalyzeBody(BaseModel):
    logs: str = Field(min_length=20, max_length=200_000)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "mode": "demo" if llm.mock else "live"}


@app.get("/api/state")
def state():
    return {
        **cluster.snapshot(),
        "fleet": cluster.fleet_map(),
        "incidents": copy.deepcopy(agent.incidents),
        "usage": llm.usage.summary(),
        "mode": "demo" if llm.mock else "live",
        "models": llm.models,
        "model_note": llm.model_note,
        "pc_mode": modes.status(),
        "agent": {"windows": agent.windows, "quiet_windows": agent.quiet_windows, "last_triage": agent.last_triage,
                  "watching": sorted(agent.watch), "errors": list(agent.errors),
                  "tick_seconds": settings.tick_seconds,
                  "scan_seconds": settings.tick_seconds * settings.triage_every_ticks,
                  "auto_approve_low_risk": settings.auto_approve_low_risk},
    }


@app.get("/api/rack/{rack_id}")
def rack(rack_id: str):
    detail = cluster.rack_detail(rack_id)
    if not detail:
        raise HTTPException(404, "No rack with that id")
    return detail


@app.get("/api/gpu/{node}/{gpu}")
def gpu(node: str, gpu: int):
    detail = cluster.gpu_detail(node, gpu)
    if not detail:
        raise HTTPException(404, "No GPU there")
    return detail


@app.get("/api/logs")
def logs(since: int = 0):
    return {"logs": cluster.logs_since(since)}


@app.post("/api/inject")
def inject(body: InjectBody):
    try:
        return cluster.inject(body.kind)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/incidents/{iid}/approve")
def approve(iid: int, body: ApproveBody | None = None):
    try:
        return agent.approve(iid, run_all=bool(body and body.all))
    except KeyError as exc:
        raise HTTPException(404, "Incident not found") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/incidents/{iid}/retry")
async def retry(iid: int):  # async: the new investigation is scheduled on the running event loop
    try:
        return agent.retry(iid)
    except KeyError as exc:
        raise HTTPException(404, "Incident not found") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/incidents/{iid}/retry-step")
def retry_step(iid: int, body: StepBody):
    try:
        return agent.retry_step(iid, body.index)
    except KeyError as exc:
        raise HTTPException(404, "Incident not found") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/incidents/{iid}/close")
def close(iid: int):
    try:
        return agent.close(iid)
    except KeyError as exc:
        raise HTTPException(404, "Incident not found") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/incidents/{iid}/skip")
def skip(iid: int):
    try:
        return agent.skip(iid)
    except KeyError as exc:
        raise HTTPException(404, "Incident not found") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/tickets/{tid}/repair")
def repair(tid: str):
    ok, msg = cluster.repair_ticket(tid)
    if not ok:
        raise HTTPException(400, msg)
    ticket = next(t for t in cluster.tickets if t["id"] == tid)
    agent.ticket_repaired(ticket)
    return {"ok": True, "message": msg}


@app.post("/api/local")
async def local(body: LocalBody):
    """Connect or disconnect this computer's own GPU."""
    if not body.enabled:
        disconnect_local()
        return {"ok": True, "connected": False}
    if cluster.local is None:
        ok, msg = await connect_local()
        if not ok:
            raise HTTPException(400, msg)
    st = cluster.local_status() or {}
    return {"ok": True, "connected": True, "message": f"Connected to {st.get('name') or 'this GPU'}.", **st}


@app.post("/api/reset")
def reset(body: ResetBody | None = None):
    store.save_incidents(agent.incidents)
    cluster.reset(size=body.size if body else None)  # this computer's GPU stays connected
    agent.reset()
    agent.load_history()  # resetting the simulated data center keeps this computer's history
    llm.usage.reset()
    return {"ok": True, "size": cluster.size}


# ------------------------------------------------------------- this computer
CHECKUP = ("Give my computer's GPU a full check-up. Read its live state, its recent history, the programs using "
           "it and any driver errors, then tell me plainly whether anything is wrong and what to do about it.")


def _fix_list() -> list[dict]:
    mode = modes.mode
    return [{"action": k, "params": [x for x in v["params"]], "mode": v["mode"], "risk": v["risk"],
             "admin": v["admin"], "guided": bool(v.get("guided")), "desc": v["desc"],
             "locked": v["mode"] == "serious" and mode != "serious"} for k, v in PC_CATALOG.items()]


@app.get("/api/pc")
def pc_state():
    connected = cluster.local is not None and LOCAL_NODE in cluster.nodes
    out = {"connected": connected, "status": cluster.local_status(), "mode": modes.status(), "fixes": _fix_list(),
           "windows": bool(cluster.pc and cluster.pc.windows), "recent_fixes": store.fixes(15),
           "summary": store.summary("24h"), "clock_cap": cluster.pc.clock_cap if cluster.pc else None}
    if connected:
        out["gpu"] = cluster.gpu_detail(LOCAL_NODE, 0)
        snap = cluster.local.snapshot()
        out["processes"] = snap["processes"]
        out["events"] = (snap.get("past_events") or [])[-10:] + [
            {"time": l["t"], "provider": "windows", "message": l["text"]} for l in list(cluster.logs)[-400:]
            if l["node"] == LOCAL_NODE and l["source"] == "windows" and l["level"] == "error"][-10:]
        out["issues"] = cluster.observed_issues(LOCAL_NODE)
        if cluster.pc and cluster.pc.windows:
            out["driver"] = cached("driver", 300, cluster.pc.driver_info)
            out["power_plan"] = cached("plan", 60, cluster.pc.active_plan)
    return out


@app.get("/api/pc/history")
def pc_history(span: str = "1h"):
    return store.history(span)


@app.post("/api/pc/mode")
async def pc_mode(body: ModeBody):
    try:
        st = modes.set(body.mode, incident=body.incident)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    cluster._log(LOCAL_NODE if cluster.local is not None else "fleet", "nightingale", "info", st["note"])
    await agent.mode_changed()
    return st


@app.post("/api/pc/fix")
def pc_fix(body: FixBody):
    """A fix the person starts themselves from My computer (not proposed by the model)."""
    if body.action not in PC_CATALOG:
        raise HTTPException(400, "Unknown fix.")
    action, why = actions_mod.normalize({"action": body.action, "params": body.params}, cluster)
    if action is None:
        raise HTTPException(400, why)
    if action["mode"] == "serious" and modes.mode != "serious":
        raise HTTPException(400, "This fix needs serious mode. Turn on serious mode first.")
    ok, msg = actions_mod.execute(action, cluster)
    _cache.pop("plan", None)
    _cache.pop("driver", None)
    store.log_fix(action["label"], ok, msg, modes.mode, None)
    cluster._log(LOCAL_NODE, "nightingale", "info" if ok else "warn", f"{'Done' if ok else 'Failed'}: {action['label']}. {msg}")
    return {"ok": ok, "message": msg, "label": action["label"]}


@app.get("/api/pc/selftest")
def selftest_status():
    return {**selftests.status(), "tests": TESTS}


@app.post("/api/pc/selftest")
def selftest_start(body: SelfTestBody):
    try:
        return selftests.start(body.test, body.minutes)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/pc/selftest/stop")
def selftest_stop():
    return selftests.stop()


@app.get("/api/pc/autostart")
def autostart_status():
    return {"enabled": autostart.is_enabled(), "supported": sys.platform == "win32", "alerts": alerts.enabled,
            "alerts_datacenter": alerts.datacenter, "tray": TRAY["running"], "recent_alerts": list(alerts.recent)[:10]}


@app.post("/api/pc/autostart")
def autostart_set(body: ToggleBody):
    ok, msg = autostart.set_enabled(body.enabled)
    if not ok:
        raise HTTPException(400, msg)
    return {"ok": True, "message": msg, "enabled": autostart.is_enabled()}


@app.post("/api/pc/alerts")
def alerts_set(body: ToggleBody):
    alerts.enabled = body.enabled
    if body.datacenter is not None:
        alerts.datacenter = body.datacenter
    return {"alerts": alerts.enabled, "alerts_datacenter": alerts.datacenter}


@app.post("/api/pc/checkup")
async def pc_checkup():
    if cluster.local is None:
        raise HTTPException(400, "Connect this computer's GPU first (Data sources).")
    if any(e["status"] == "thinking" for e in agent.asks):
        raise HTTPException(409, "Nemotron is still answering your last question.")
    return agent.ask(CHECKUP)


@app.get("/api/ask")
def asks():
    return {"asks": copy.deepcopy(list(agent.asks))}


@app.post("/api/ask")
async def ask(body: AskBody):  # async: the answer is worked out on the running event loop
    if any(e["status"] == "thinking" for e in agent.asks):
        raise HTTPException(409, "Nemotron is still answering your last question.")
    return agent.ask(body.question.strip())


@app.get("/api/benchmark")
def benchmark_status():
    return {**bench.status(), "counts": list(COUNTS), "sizes": list(BENCH_SIZES)}


@app.post("/api/benchmark")
async def benchmark_start(body: BenchBody):
    if body.size not in BENCH_SIZES:
        raise HTTPException(400, f"size must be one of {', '.join(map(str, BENCH_SIZES))}")
    try:
        return bench.start(body.n, body.size)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/benchmark/results.json", include_in_schema=False)
def benchmark_download():
    st = bench.status()
    if not st["results"]:
        raise HTTPException(404, "No finished benchmark yet.")
    return JSONResponse(st, headers={"Content-Disposition": 'attachment; filename="nightingale-benchmark.json"'})


@app.post("/api/analyze")
async def analyze(body: AnalyzeBody):
    try:
        return await agent.analyze_external(body.logs)
    except Exception as exc:
        raise HTTPException(502, f"Analysis failed: {exc}") from exc
