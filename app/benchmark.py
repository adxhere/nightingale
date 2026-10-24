"""Benchmark: run failure drills on fresh copies of the data center and score Nightingale end to end.

Each drill gets its own new data center (same size, a different random seed), so drills never
interfere. Nightingale runs exactly as it does live: Nano triages every scan window, Ultra
investigates with tools, the plan is rehearsed, every step is approved (the benchmark plays the
engineer who clicks Approve), and the fix is verified. Then the result is scored:

  found       an incident opened on the part that was broken (the server, or the rack for cooling;
              for a code bug, any server of that job)
  cause       the diagnosis named the right category (memory, cooling, software, ...)
  rehearsal   the plan passed its dry run on a copy of the data center
  fixed       the incident ended resolved or handed to a technician, and every job that was
              running before the drill is running again (for a code bug: the owner was told)
  safe        nothing unsafe: no step failed when it ran, no job outside the drill crashed, and a
              code bug got no hardware actions

The benchmark keeps its own token counts, so it never mixes with the live dashboard's numbers.
"""
from __future__ import annotations

import asyncio
import copy
import random
import statistics
import time

from .agent import Agent
from .llm import LLMClient
from .simulator import SCENARIOS, Cluster

EXPECTED = {"thermal": {"thermal"}, "ecc": {"memory"}, "bus": {"gpu_lost"}, "nvlink": {"interconnect"},
            "rack_cooling": {"cooling"}, "app_bug": {"software"}}
HARDWARE_ACTIONS = {"set_power_limit", "set_rack_power_limit", "drain_node", "drain_rack", "reset_gpu", "reboot_node",
                    "restart_job_from_checkpoint", "open_ticket", "open_facility_ticket"}
FINAL = {"resolved", "waiting_on_technician", "needs_attention", "closed"}
MAX_TICKS = 160
SIZES = (256, 1024)
COUNTS = (6, 12, 30)


def _pct(cases: list[dict], key: str) -> int | None:
    vals = [c[key] for c in cases if c[key] is not None]
    return round(100 * sum(vals) / len(vals)) if vals else None


def _median(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 1) if vals else None


class Benchmark:
    def __init__(self, settings, base_llm: LLMClient) -> None:
        self.settings = settings
        self.base_llm = base_llm
        self.task: asyncio.Task | None = None
        self.state = {"running": False, "done": 0, "total": 0, "size": None, "mode": None,
                      "started_at": None, "finished_at": None, "error": "", "cases": [], "results": None}

    # ----------------------------------------------------------------- public
    def start(self, n: int, size: int) -> dict:
        if self.state["running"]:
            raise ValueError("A benchmark is already running.")
        self.task = asyncio.create_task(self.run(n, size))
        return self.status()

    def status(self) -> dict:
        return copy.deepcopy(self.state)

    async def run(self, n: int, size: int = 1024, concurrency: int = 4) -> dict:
        n = max(1, min(int(n), 60))
        llm = LLMClient(self.settings)
        llm.models = dict(self.base_llm.models)
        llm.fast = True  # demo mode: no pretend delays
        mode = "demo" if llm.mock else "live"
        kinds = [list(SCENARIOS)[i % len(SCENARIOS)] for i in range(n)]
        random.Random(n * 7919 + size).shuffle(kinds)
        self.state.update({"running": True, "done": 0, "total": n, "size": size, "mode": mode, "error": "",
                           "started_at": time.time(), "finished_at": None, "cases": [], "results": None,
                           "models": dict(llm.models)})
        sem = asyncio.Semaphore(concurrency)

        async def one(i: int, kind: str) -> None:
            async with sem:
                case = await self._case(llm, kind, seed=1000 + i, size=size)
                case["n"] = i + 1
                self.state["cases"].append(case)
                self.state["done"] += 1
                self.state["usage"] = llm.usage.summary()

        try:
            await asyncio.gather(*(one(i, k) for i, k in enumerate(kinds)))
            self.state["cases"].sort(key=lambda c: c["n"])
            self.state["results"] = self._summary(self.state["cases"], llm.usage.summary())
        except Exception as exc:  # keep what finished; say what broke
            self.state["error"] = f"The benchmark stopped: {exc}"
        finally:
            self.state["running"] = False
            self.state["finished_at"] = time.time()
        return self.status()

    # ------------------------------------------------------------------ cases
    async def _case(self, llm: LLMClient, kind: str, seed: int, size: int) -> dict:
        settings = copy.copy(self.settings)
        settings.auto_approve_low_risk = False
        cluster = Cluster(size, seed=seed)
        agent = Agent(cluster, llm, settings)
        for _ in range(6):  # a little history before anything breaks
            cluster.tick()
        before = {j.id for j in cluster.jobs.values() if j.status == "running"}
        info = cluster.inject(kind)
        fault = cluster.faults[-1]
        expected = info["target"]
        job_nodes = set(cluster.jobs[fault.job].nodes) if fault.job in cluster.jobs else set()
        start_tick, wall = cluster.tick_count, time.time()

        def matches(inc: dict) -> bool:
            if kind == "rack_cooling":
                return inc["target"] == expected or inc["rack"] == expected
            if kind == "app_bug":
                return inc["target"] == expected or inc["target"] in job_nodes
            return inc["target"] == expected

        settled = 0
        for t in range(1, MAX_TICKS + 1):
            cluster.tick()
            agent.verify_pending()
            if t % 3 == 0:
                await agent.recheck_waiting()
            if t % settings.triage_every_ticks == 0:
                await agent.triage_once()
                if agent.tasks:
                    await asyncio.gather(*list(agent.tasks))
            for inc in agent.incidents:
                if inc["status"] == "awaiting_approval":
                    agent.approve(inc["id"], run_all=True)
            hit = next((i for i in agent.incidents if matches(i)), None)
            busy = any(i["status"] in ("investigating", "verifying") for i in agent.incidents)
            if hit and hit["status"] in FINAL and not busy:
                settled += 1
                if settled >= 3:  # let a crash or a verification land before scoring
                    break
            await asyncio.sleep(0)  # let the web server breathe between ticks
        return self._score(kind, info, expected, cluster, agent, before, fault, start_tick, time.time() - wall, matches)

    def _score(self, kind, info, expected, cluster, agent, before, fault, start_tick, seconds, matches) -> dict:
        hits = [i for i in agent.incidents if matches(i)]
        inc = hits[-1] if hits else None  # the first one opened (incidents are newest first)
        others = [i for i in agent.incidents if not matches(i)]
        after = {j.id: j.status for j in cluster.jobs.values()}
        steps = inc["actions"] if inc else []
        failed_steps = [a for a in steps if a["status"] == "failed"]
        other_crashed = [j for j in before if j != fault.job and after.get(j) != "running"]
        hardware = [a for a in steps if a["action"] in HARDWARE_ACTIONS]
        found = inc is not None
        cause = (inc["category"] in EXPECTED[kind]) if inc and inc["category"] else (False if found else None)
        rehearsal = None
        if inc and steps and inc["dry_run"]:
            rehearsal = inc["dry_run"]["verdict"] in ("pass", "warn")
        if kind == "app_bug":
            fixed = bool(inc and inc["status"] in ("resolved", "waiting_on_technician")
                         and any(a["action"] == "notify_owner" and a["status"] == "done" for a in steps))
        else:
            fixed = bool(inc and inc["status"] in ("resolved", "waiting_on_technician")
                         and all(after.get(j) == "running" for j in before))
        safe = not failed_steps and not other_crashed and not (kind == "app_bug" and hardware)
        why = []
        if not found:
            why.append("no incident on the broken part")
        if found and not cause:
            why.append(f"named the cause '{inc['category'] or 'unknown'}'")
        if failed_steps:
            why.append("a step was blocked: " + failed_steps[0]["result"][:120])
        if other_crashed:
            why.append("another job crashed: " + ", ".join(other_crashed))
        if kind == "app_bug" and hardware:
            why.append("hardware steps for a code bug")
        if found and not fixed and not why:
            why.append(f"ended as {inc['status'].replace('_', ' ')}")
        return {
            "kind": kind, "label": SCENARIOS[kind], "target": expected, "gpu": info.get("gpu"),
            "incident": {"title": inc["title"] or inc["trigger"]["signal"], "target": inc["target"],
                         "status": inc["status"], "category": inc["category"], "confidence": inc["confidence"],
                         "steps": [a["label"] for a in steps], "tool_calls": len(inc["trace"]),
                         "revised": inc["revised"]} if inc else None,
            "found": found, "cause": cause, "rehearsal": rehearsal, "fixed": fixed, "safe": safe,
            "false_alarms": len(others),
            "detect_seconds": round((inc["opened_tick"] - start_tick) * self.settings.tick_seconds, 1) if inc else None,
            "diagnose_seconds": inc["investigation_seconds"] if inc else None,
            "seconds": round(seconds, 1), "why": "; ".join(why),
        }

    # ---------------------------------------------------------------- summary
    def _summary(self, cases: list[dict], usage: dict) -> dict:
        by_kind = []
        for kind, label in SCENARIOS.items():
            cs = [c for c in cases if c["kind"] == kind]
            if cs:
                by_kind.append({"kind": kind, "label": label, "n": len(cs), "found": _pct(cs, "found"),
                                "cause": _pct(cs, "cause"), "fixed": _pct(cs, "fixed"), "safe": _pct(cs, "safe")})
        n = len(cases) or 1
        return {
            "n": len(cases), "found": _pct(cases, "found"), "cause": _pct(cases, "cause"),
            "rehearsal": _pct(cases, "rehearsal"), "fixed": _pct(cases, "fixed"), "safe": _pct(cases, "safe"),
            "false_alarms": sum(c["false_alarms"] for c in cases),
            "detect_seconds": _median([c["detect_seconds"] for c in cases]),
            "diagnose_seconds": _median([c["diagnose_seconds"] for c in cases]),
            "cost": usage["total_cost"], "cost_per_drill": round(usage["total_cost"] / n, 4),
            "tokens": sum(usage[r]["in"] + usage[r]["out"] for r in ("triage", "diagnosis", "ask")),
            "calls": {r: usage[r]["calls"] for r in ("triage", "diagnosis")},
            "by_kind": by_kind,
        }
