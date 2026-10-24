"""Nightingale's agent loop.

1. Triage (Nemotron Nano) reads a compact window of the whole data center and flags suspects.
2. Investigation (Nemotron Ultra) works each suspect like an engineer: it calls read-only tools
   (logs, GPU history, rack comparison, job info, spares, fleet memory, coolant sensors) until it
   can name the root cause, then proposes a fix plan.
3. Dry run: the plan runs on a copy of the data center. If it would fail, Ultra revises it once.
4. A human approves each step; the data center's own preconditions block unsafe steps.
5. After the fix, Nightingale re-checks telemetry to confirm the problem is gone.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import re
import time
from collections import deque

from . import actions, prompts
from .modes import Modes
from .simulator import Cluster

OPEN = {"investigating", "awaiting_approval", "verifying", "waiting_on_technician", "needs_attention"}
# An incident in any of these states covers its target: triage won't open a second one for it.
# "needs_attention" counts too; the person re-runs it with "Investigate again" or closes it.
DEDUPE = {"investigating", "awaiting_approval", "verifying", "waiting_on_technician", "needs_attention"}
DIAGNOSIS_KEYS = {"title", "summary", "root_cause", "actions", "category"}
VERIFY_DELAY_TICKS = 8
LOCAL_VERIFY_DELAY_TICKS = 15
MAX_TOOL_CALLS = 6
MAX_ASK_CALLS = 4
MAX_INCIDENTS = 60
STATUS_WORDS = {"investigating": "being investigated", "awaiting_approval": "plan waiting for approval",
                "needs_attention": "needs attention", "verifying": "checking the fix",
                "waiting_on_technician": "fixed for now, waiting on a technician", "resolved": "resolved",
                "closed": "closed by the operator"}


def _int_or_none(value):
    try:
        return int(value) if value is not None and str(value).strip() not in ("", "null", "None") else None
    except (TypeError, ValueError):
        return None


def _text(value, limit: int = 600) -> str:
    return str(value or "").strip()[:limit]


def diagnosis_of(step: dict) -> dict | None:
    """The diagnosis inside a final answer, unwrapped if the model nested it one level deeper
    (for example {"final": {"diagnosis": {...}}}). None if it has none of the expected fields."""
    d = step.get("final") if isinstance(step, dict) else None
    for _ in range(2):
        if not isinstance(d, dict):
            return None
        if DIAGNOSIS_KEYS & set(d):
            break
        inner = [v for v in d.values() if isinstance(v, dict)]
        d = inner[0] if len(inner) == 1 else None
    if not isinstance(d, dict) or not (d.get("summary") or d.get("root_cause") or d.get("actions")):
        return None
    return d


class Agent:
    def __init__(self, cluster: Cluster, llm, settings, modes: Modes | None = None) -> None:
        self.cluster = cluster
        self.llm = llm
        self.settings = settings
        self.modes = modes or Modes()
        self.store = None  # optional Store: keeps this computer's incidents and fixes across restarts
        self.tasks: set = set()
        self.reset()

    def reset(self) -> None:
        self.incidents: list[dict] = []
        self._ids = itertools.count(self._id_floor())
        self.windows = 0
        self.quiet_windows = 0
        self.watch: dict[str, int] = {}
        self.triage_busy = False
        self.last_triage: dict | None = None
        self.errors: deque = deque(maxlen=5)
        self.last_log_id = self.cluster.last_log_id()
        self.asks: deque = deque(maxlen=30)
        self._ask_ids = itertools.count(1)

    def _id_floor(self) -> int:
        """Incident numbers keep counting up across restarts, so saved incidents never clash with new ones."""
        if self.store is None:
            return 1
        saved = self.store.load_incidents(1)
        return (saved[0]["id"] + 1) if saved else 1

    def load_history(self) -> int:
        """Bring back this computer's incidents from earlier runs. Unfinished ones are closed with a note."""
        if self.store is None:
            return 0
        have = {i["id"] for i in self.incidents}
        old = [i for i in self.store.load_incidents() if i["id"] not in have]
        for inc in old:
            if inc.get("status") in OPEN:
                inc["status"] = "closed"
                inc["verification"] = "Closed: Nightingale restarted before this finished."
                inc.setdefault("timeline", []).append({"at": time.time(), "text": inc["verification"]})
            inc["thinking"] = False
        self.incidents = sorted(self.incidents + old, key=lambda i: -i["id"])[:MAX_INCIDENTS]
        return len(old)

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def get(self, iid: int) -> dict:
        for inc in self.incidents:
            if inc["id"] == iid:
                return inc
        raise KeyError(iid)

    def _open_for(self, target: str) -> dict | None:
        return next((i for i in self.incidents if i["target"] == target and i["status"] in DEDUPE), None)

    def _covered(self, target: str) -> bool:
        """Already handled: an open incident on it or on its rack, or a repair ticket and a drained server."""
        c = self.cluster
        if self._open_for(target):
            return True
        if target in c.nodes:
            rack = c.nodes[target].rack
            if self._open_for(rack):
                return True
            if c.has_open_ticket(target) and c.nodes[target].status == "drained":
                return True
        return c.has_open_ticket(target) and target in c.racks

    def _unexplained(self, signal: set) -> set:
        """Targets in this window that no open incident, repair ticket or drained-and-ticketed server explains."""
        c = self.cluster
        covered = set()
        for i in self.incidents:
            if i["status"] in DEDUPE:
                covered |= {i["target"], i["rack"]}
        out = set()
        for t in signal:
            if t in covered or c.has_open_ticket(t):
                continue
            node = c.nodes.get(t)
            if node is not None and (node.rack in covered or c.has_open_ticket(node.rack)):
                continue
            if t in c.nodes or t in c.racks:
                out.add(t)
        return out

    def _timeline(self, inc: dict, text: str) -> None:
        inc["timeline"].append({"at": time.time(), "text": text})

    # --------------------------------------------------------------- triage
    async def triage_once(self) -> None:
        if self.triage_busy:
            return
        self.triage_busy = True
        try:
            open_list = ", ".join(f"{i['target']} ({i['title'] or i['trigger']['signal']})"
                                  for i in self.incidents if i["status"] in DEDUPE) or "none"
            digest, upto = self.cluster.digest(self.last_log_id, open_list)
            self.last_log_id = upto
            self.windows += 1
            if not self._unexplained(self.cluster.window_signal):
                # Nemotron only when needed: nothing new to look at, so no model call this window.
                self.quiet_windows += 1
                self.last_triage = {"at": time.time(), "seconds": 0, "findings": 0, "quiet": True}
                self.watch.clear()
                return
            start = time.time()
            result = await asyncio.to_thread(self.llm.triage, digest)
            findings = result.get("findings", []) if isinstance(result, dict) else []
            findings = [f for f in findings if isinstance(f, dict)]
            self.last_triage = {"at": time.time(), "seconds": round(time.time() - start, 2), "findings": len(findings)}
            # Rack-wide and GPU-specific findings first, so job-wide symptoms can be traced to them.
            findings.sort(key=lambda f: (str(f.get("target") or f.get("node") or "")[:4] == "dgx-"
                                         and _int_or_none(f.get("gpu")) is None, f.get("severity") != "incident"))
            claimed = set().union(*(set(i["jobs"]) for i in self.incidents if i["status"] in DEDUPE))
            seen = set()
            for f in findings:
                target = self.cluster.canonical_target(str(f.get("target") or f.get("node") or ""))
                if not target or self._covered(target):
                    continue
                severity = "incident" if f.get("severity") == "incident" else "watch"
                jobs_here = self.cluster.jobs_on(target)
                if (target in self.cluster.nodes and _int_or_none(f.get("gpu")) is None
                        and jobs_here and jobs_here <= claimed):
                    continue  # a job-wide symptom of an incident already being handled
                if severity == "watch":
                    seen.add(target)
                    self.watch[target] = self.watch.get(target, 0) + 1
                    if self.watch[target] < 2:  # wait for a second window before escalating
                        continue
                self.watch.pop(target, None)
                inc = self._new_incident(target, f)
                claimed |= set(inc["jobs"])
                self.spawn(self.investigate(inc))
            for t in list(self.watch):
                if t not in seen:
                    del self.watch[t]
        except Exception as exc:  # keep the loop alive; show the error in the UI
            self.errors.append(f"Triage failed: {exc}")
        finally:
            self.triage_busy = False

    def _new_incident(self, target: str, finding: dict) -> dict:
        c = self.cluster
        signal = _text(finding.get("signal"), 200) or "anomaly"
        rack = target if target in c.racks else c.nodes[target].rack
        inc = {
            "id": next(self._ids), "target": target, "rack": rack,
            "scope": "rack" if target in c.racks else "server",
            "gpu": _int_or_none(finding.get("gpu")),
            "status": "investigating", "opened_at": time.time(), "opened_tick": c.tick_count,
            "trigger": {"signal": signal, "severity": finding.get("severity"),
                        "evidence": [_text(e, 300) for e in (finding.get("evidence") or [])][:4]},
            "jobs": sorted(c.jobs_on(target)),
            "title": "", "summary": "", "root_cause": "", "category": "", "severity": "",
            "confidence": None, "evidence": [], "actions": [], "rejected_actions": [],
            "needs_technician": False, "technician_note": "", "trace": [], "thinking": True,
            "investigation_seconds": None, "dry_run": None, "revised": False,
            "error": "", "verification": "", "situation": "", "job_states": {},
            "models": dict(self.llm.models), "timeline": [],
        }
        where = f"rack {target}" if target in c.racks else "this computer" if c.nodes[target].local else target
        self._timeline(inc, f"Nemotron triage flagged {where}: {signal}")
        self.incidents.insert(0, inc)
        del self.incidents[MAX_INCIDENTS:]
        return inc

    # -------------------------------------------------------- investigation
    def _brief(self, inc: dict) -> str:
        c = self.cluster
        ev = " | ".join(inc["trigger"]["evidence"]) or "none"
        jobs = "; ".join(f"{j.id}: {j.status}, {len(j.nodes)} servers ({len(j.nodes) * 8:,} GPUs), "
                         f"speed {round(j.rate * 100)}%" for j in c.jobs.values())
        others = ", ".join(f"{i['target']} ({i['title'] or i['trigger']['signal']})" for i in self.incidents
                           if i["id"] != inc["id"] and i["status"] in DEDUPE) or "none"
        return (f"Triage flagged: {inc['trigger']['signal']}\n"
                f"Target: {inc['target']}" + (f", GPU {inc['gpu']}" if inc["gpu"] is not None else "") + "\n"
                f"Evidence from triage: {ev}\n"
                f"{c.target_summary(inc['target'])}\n"
                f"Data center: {c.size:,} GPUs in {len(c.nodes)} servers and {len(c.racks)} racks.\n"
                f"Jobs: {jobs}\n"
                f"Known issues already handled: {c.known_issues() or 'none'}\n"
                f"Other open incidents: {others}\n"
                + (f"Fix mode on this computer: {self.modes.mode}"
                   + (" (serious-mode fixes run once approved)." if self.modes.mode == "serious" else
                      " (serious-mode fixes will show as locked until the person turns serious mode on).") + "\n"
                   if inc["target"] == "laptop" else "")
                + "Begin. Reply with one JSON object.")

    async def investigate(self, inc: dict) -> None:
        start = time.time()
        system = prompts.investigate_system(self.cluster.TOOLS)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": self._brief(inc)}]
        mock_state = {"cluster": self.cluster, "target": inc["target"], "gpu": inc["gpu"], "steps": [],
                      "signal": inc["trigger"]["signal"]}
        try:
            final = None
            calls = 0
            seen_calls = set()
            fixes_left = 1  # one chance to repair a final answer in the wrong shape
            for _ in range(MAX_TOOL_CALLS + 5):
                inc["thinking"] = True
                step = await asyncio.to_thread(self.llm.investigate_step, messages, mock_state)
                inc["thinking"] = False
                if "final" in step:
                    d = diagnosis_of(step)
                    if d is not None:
                        final = {"thought": step.get("thought", ""), "final": d}
                        break
                    if fixes_left:
                        fixes_left -= 1
                        messages += [{"role": "assistant", "content": json.dumps(step)[:2000]},
                                     {"role": "user", "content": "That final answer is missing the diagnosis fields "
                                      "(title, summary, root_cause, actions). Reply again with ONLY "
                                      '{"thought": "...", "final": <diagnosis>} in exactly this shape:\n'
                                      + prompts.DIAGNOSIS_SCHEMA}]
                        continue
                    raise ValueError("the model's final answer had no diagnosis in it")
                tool = str(step.get("tool") or "").strip()
                args = step.get("args") if isinstance(step.get("args"), dict) else {}
                if not tool or calls >= MAX_TOOL_CALLS:
                    messages += [{"role": "assistant", "content": json.dumps(step)[:2000]},
                                 {"role": "user", "content": "No more tool calls. Give your final answer now as "
                                                             '{"thought": "...", "final": <diagnosis>}.'}]
                    continue
                key = (tool, json.dumps(args, sort_keys=True))
                if key in seen_calls:  # a repeat costs nothing and isn't shown; nudge the model on
                    messages += [{"role": "assistant", "content": json.dumps(step)[:2000]},
                                 {"role": "user", "content": "You already made this exact call and have its result. "
                                  f"Use it, call a different tool, or give your final answer. "
                                  f"({MAX_TOOL_CALLS - calls} tool calls left.) Next JSON object."}]
                    continue
                result = self.cluster.run_tool(tool, args)
                seen_calls.add(key)
                calls += 1
                inc["trace"].append({"n": calls, "tool": tool, "args": args, "thought": _text(step.get("thought"), 300),
                                     "result": result[:1600], "at": time.time()})
                mock_state["steps"].append({"tool": tool, "args": args, "result": result})
                messages += [{"role": "assistant", "content": json.dumps(step)[:2000]},
                             {"role": "user", "content": f"Result of {tool}({json.dumps(args)}):\n{result[:1600]}\n"
                                                         f"({MAX_TOOL_CALLS - calls} tool calls left.) Next JSON object."}]
            if final is None:
                raise ValueError("the model did not reach a diagnosis")
            inc["investigation_seconds"] = round(time.time() - start, 1)
            self._apply_diagnosis(inc, final["final"])
            self._timeline(inc, f"Nemotron investigated with {calls} tool calls in {inc['investigation_seconds']}s: "
                                f"{inc['title']}")
            if inc["actions"]:
                await self._dry_run(inc)
                if inc["dry_run"]["verdict"] == "fail":
                    await self._revise(inc, messages, final)
            inc["status"] = "awaiting_approval" if inc["actions"] else "needs_attention"
            if self.settings.auto_approve_low_risk and inc["status"] == "awaiting_approval":
                while (nxt := self._next_pending(inc)) and nxt["risk"] == "low" and not actions.is_pc(nxt):
                    self._run(inc, nxt, auto=True)
                    if inc["status"] != "awaiting_approval":
                        break
                self._maybe_finish(inc)
        except Exception as exc:
            inc["status"] = "needs_attention"
            inc["error"] = f"Investigation failed: {exc}"
            self._timeline(inc, inc["error"])
        finally:
            inc["thinking"] = False

    # ------------------------------------------------- ask your data center
    def incidents_text(self) -> str:
        """The incidents() tool for questions: what Nightingale has open or handled recently."""
        if not self.incidents:
            return "No incidents yet: triage hasn't flagged anything since the data center started."
        rows = []
        for i in self.incidents[:12]:
            done = sum(a["status"] != "pending" for a in i["actions"])
            rows.append(f"  #{i['id']} {i['target']}: {STATUS_WORDS.get(i['status'], i['status'])}. "
                        f"{i['title'] or i['trigger']['signal']}"
                        + (f". Root cause: {i['root_cause']}" if i["root_cause"] else "")
                        + (f". {done} of {len(i['actions'])} steps done" if i["actions"] else ""))
        open_n = sum(i["status"] in OPEN for i in self.incidents)
        return f"{open_n} open, {len(self.incidents) - open_n} handled (newest first):\n" + "\n".join(rows)

    def ask(self, question: str) -> dict:
        """Start answering a question about the data center. The answer fills in as Nemotron works."""
        entry = {"id": next(self._ask_ids), "question": _text(question, 500), "status": "thinking", "answer": "",
                 "refs": [], "trace": [], "at": time.time(), "seconds": None, "error": "",
                 "model": self.llm.models["diagnosis"]}
        self.asks.append(entry)
        self.spawn(self._answer(entry))
        return entry

    def _ask_tools(self) -> dict:
        return {**Cluster.TOOLS, **Cluster.ASK_TOOLS,
                "incidents": "incidents(): Nightingale's incidents, open and recently handled, with status and root cause"}

    async def _answer(self, entry: dict) -> None:
        start = time.time()
        c = self.cluster
        messages = [{"role": "system", "content": prompts.ask_system(self._ask_tools())}]
        earlier = [e for e in self.asks if e["id"] < entry["id"] and e["status"] == "done"][-3:]
        for e in earlier:  # a little memory, so follow-up questions work
            messages += [{"role": "user", "content": e["question"]},
                         {"role": "assistant", "content": json.dumps({"thought": "answered", "answer": e["answer"]})}]
        messages.append({"role": "user", "content": (
            f"Question: {entry['question']}\n"
            f"(Data center: {c.size:,} GPUs in {len(c.sim_nodes())} servers; this computer's GPU is "
            f"{'connected as server laptop' if c.local is not None else 'not connected'}.) Reply with one JSON object.")})
        state = {"cluster": c, "agent": self, "question": entry["question"], "steps": []}
        calls, seen, nudged = 0, set(), False
        try:
            for _ in range(MAX_ASK_CALLS + 4):
                step = await asyncio.to_thread(self.llm.ask_step, messages, state)
                answer = _text(step.get("answer"), 1500) if isinstance(step, dict) else ""
                if answer:
                    if calls == 0 and not nudged:  # answers come from data, not memory
                        nudged = True
                        messages += [{"role": "assistant", "content": json.dumps(step)[:2000]},
                                     {"role": "user", "content": "Check the live data with at least one tool before "
                                                                 "answering. Next JSON object."}]
                        continue
                    entry["answer"] = answer
                    entry["refs"] = self._refs(step.get("refs"), answer)
                    break
                tool = str(step.get("tool") or "").strip()
                args = step.get("args") if isinstance(step.get("args"), dict) else {}
                if not tool or calls >= MAX_ASK_CALLS:
                    messages += [{"role": "assistant", "content": json.dumps(step)[:2000]},
                                 {"role": "user", "content": 'No more tool calls. Answer now as {"thought": "...", '
                                                             '"answer": "...", "refs": [...]}.'}]
                    continue
                key = (tool, json.dumps(args, sort_keys=True))
                if key in seen:
                    messages += [{"role": "assistant", "content": json.dumps(step)[:2000]},
                                 {"role": "user", "content": "You already have that result. Answer, or check something "
                                                             "else. Next JSON object."}]
                    continue
                seen.add(key)
                result = self.incidents_text() if tool == "incidents" else c.run_tool(tool, args)
                calls += 1
                entry["trace"].append({"n": calls, "tool": tool, "args": args,
                                       "thought": _text(step.get("thought"), 300), "result": result[:1600]})
                state["steps"].append({"tool": tool, "args": args, "result": result})
                messages += [{"role": "assistant", "content": json.dumps(step)[:2000]},
                             {"role": "user", "content": f"Result of {tool}({json.dumps(args)}):\n{result[:1600]}\n"
                                                         f"({MAX_ASK_CALLS - calls} tool calls left.) Next JSON object."}]
            if not entry["answer"]:
                raise ValueError("the model didn't reach an answer")
            entry["status"] = "done"
        except Exception as exc:
            entry["status"] = "error"
            entry["error"] = f"Couldn't answer: {exc}"
        entry["seconds"] = round(time.time() - start, 1)

    def _refs(self, refs, answer: str) -> list[str]:
        """Servers, racks and jobs named in the answer, checked against the data center, for links."""
        c = self.cluster
        names = set(str(r) for r in refs) if isinstance(refs, list) else set()
        names |= set(re.findall(r"\b(?:dgx-\d{4}|[A-H]-\d{2}|laptop)\b", answer))
        names |= {j for j in c.jobs if j in answer}
        out = []
        for n in names:
            canon = c.canonical_target(n) or (n if n in c.jobs else None)
            if canon and canon not in out:
                out.append(canon)
        return sorted(out)

    def _job_states(self, inc: dict) -> dict:
        return {j: self.cluster.jobs[j].status for j in inc["jobs"] if j in self.cluster.jobs}

    async def recheck_waiting(self) -> None:
        """If the world changes while a plan waits for approval, dry-run it again on the new state."""
        for inc in list(self.incidents):
            if inc["status"] != "awaiting_approval" or inc.get("rechecking") or not inc.get("job_states"):
                continue
            now = self._job_states(inc)
            changed = [f"{j} is now {st}" for j, st in now.items() if inc["job_states"].get(j) != st and st != "restarting"]
            if not changed:
                continue
            inc["rechecking"] = True
            try:
                await self._dry_run(inc)
                verdict = {"pass": "the plan still works", "warn": "the plan still works, with warnings",
                           "fail": "the plan no longer works; review it before approving"}[inc["dry_run"]["verdict"]]
                inc["situation"] = f"Since the diagnosis, {', '.join(changed)}. Nightingale dry-ran the plan again: {verdict}."
                self._timeline(inc, inc["situation"])
            finally:
                inc["job_states"] = now
                inc["rechecking"] = False

    async def _dry_run(self, inc: dict) -> None:
        plan = [a for a in inc["actions"] if a["status"] == "pending"]
        result = await asyncio.to_thread(actions.dry_run, self.cluster, plan, inc["target"], 12, self.modes.mode)
        inc["dry_run"] = result
        inc["job_states"] = self._job_states(inc)
        word = {"pass": "passed", "warn": "passed with warnings", "fail": "failed"}[result["verdict"]]
        what = "Checks" if result.get("checked") else "Dry run"
        self._timeline(inc, f"{what} {word}: {result['summary']}")

    async def _revise(self, inc: dict, messages: list[dict], final: dict) -> None:
        report = "\n".join(inc["dry_run"]["lines"]) + "\n" + inc["dry_run"]["summary"]
        messages = messages + [{"role": "assistant", "content": json.dumps(final)[:4000]},
                               {"role": "user", "content": prompts.REVISE_NOTE.format(report=report)}]
        mock_state = {"cluster": self.cluster, "target": inc["target"], "gpu": inc["gpu"], "revise": inc["dry_run"],
                      "signal": inc["trigger"]["signal"],
                      "steps": [{"tool": t["tool"], "args": t["args"], "result": t["result"]} for t in inc["trace"]]}
        inc["thinking"] = True
        step = await asyncio.to_thread(self.llm.investigate_step, messages, mock_state)
        inc["thinking"] = False
        if isinstance(step.get("final"), dict):
            first_try = inc["dry_run"]
            self._apply_diagnosis(inc, step["final"])
            inc["revised"] = True
            self._timeline(inc, "Nemotron revised the plan after the dry run.")
            if inc["actions"]:
                await self._dry_run(inc)
            inc["dry_run"]["first_attempt"] = {"verdict": first_try["verdict"], "lines": first_try["lines"]}

    def _apply_diagnosis(self, inc: dict, d: dict) -> None:
        proposed, rejected = [], []
        for raw in (d.get("actions") or [])[:8]:
            action, why = actions.normalize(raw, self.cluster)
            if action:
                proposed.append(action)
            else:
                rejected.append(why)
        conf = d.get("confidence")
        affected = d.get("affected") if isinstance(d.get("affected"), dict) else {}
        gpus = [g for g in (_int_or_none(x) for x in (affected.get("gpus") or [])) if g is not None]
        if inc["gpu"] is None and len(gpus) == 1 and inc["target"] in self.cluster.nodes:
            inc["gpu"] = gpus[0]
        scope = _text(d.get("scope"), 20).lower()
        inc.update({
            "title": _text(d.get("title"), 90) or inc["trigger"]["signal"],
            "category": _text(d.get("category"), 30),
            "scope": scope if scope in ("gpu", "server", "rack", "job") else inc["scope"],
            "severity": _text(d.get("severity"), 20).lower(),
            "confidence": round(float(conf), 2) if isinstance(conf, (int, float)) else None,
            "summary": _text(d.get("summary"), 900),
            "root_cause": _text(d.get("root_cause"), 400),
            "evidence": [_text(e, 300) for e in (d.get("evidence") or []) if e][:6],
            "actions": proposed, "rejected_actions": rejected,
            "needs_technician": bool(d.get("needs_technician")),
            "technician_note": _text(d.get("technician_note"), 400),
        })
        if rejected:
            self._timeline(inc, f"Blocked {len(rejected)} invalid step(s): " + "; ".join(rejected))

    # ------------------------------------------------------------ approvals
    @staticmethod
    def _next_pending(inc: dict) -> dict | None:
        return next((a for a in inc["actions"] if a["status"] == "pending"), None)

    def _run(self, inc: dict, action: dict, auto: bool = False) -> bool:
        ok, msg = actions.execute(action, self.cluster)
        action["status"] = "done" if ok else "failed"
        action["result"] = msg
        self._timeline(inc, ("Auto-ran (low risk): " if auto else "") + ("Done: " if ok else "Blocked: ") + msg)
        if self.store is not None and action["action"].startswith("pc_"):
            self.store.log_fix(action["label"], ok, msg, self.modes.mode, inc["id"])
        if not ok and not actions.is_pc(action):
            inc["status"] = "needs_attention"
        return ok

    def approve(self, iid: int, run_all: bool = False) -> dict:
        inc = self.get(iid)
        if inc["status"] != "awaiting_approval":
            raise ValueError("This incident has no steps waiting for approval.")
        nxt = self._next_pending(inc)
        if nxt is not None and nxt.get("mode") == "serious" and self.modes.mode != "serious":
            raise ValueError("This step needs serious mode. Turn on serious mode, or skip the step.")
        while (action := self._next_pending(inc)) is not None:
            if action.get("mode") == "serious" and self.modes.mode != "serious":
                break  # "approve all" stops at the first locked step
            if not self._run(inc, action) or not run_all:
                break
        self._maybe_finish(inc)
        return inc

    def retry_step(self, iid: int, index: int) -> dict:
        """A fix on this computer that failed (say, the Windows permission box was declined) can be tried again."""
        inc = self.get(iid)
        if not 0 <= index < len(inc["actions"]):
            raise ValueError("No such step.")
        a = inc["actions"][index]
        if a["status"] != "failed" or not actions.is_pc(a):
            raise ValueError("Only a failed fix on this computer can be tried again.")
        a["status"], a["result"] = "pending", ""
        if inc["status"] in ("verifying", "needs_attention"):
            inc["status"] = "awaiting_approval"
        self._timeline(inc, f"Trying again: {a['label']}")
        return inc

    async def mode_changed(self) -> None:
        """Re-check waiting plans on this computer, so locked steps unlock (or lock) at once."""
        for inc in list(self.incidents):
            if inc["target"] == "laptop" and inc["status"] == "awaiting_approval":
                await self._dry_run(inc)

    def retry(self, iid: int) -> dict:
        """Investigate a stuck incident again from scratch (after a failed investigation or a fix that didn't hold)."""
        inc = self.get(iid)
        if inc["status"] != "needs_attention":
            raise ValueError("Only an incident that needs attention can be investigated again.")
        done = [a for a in inc["actions"] if a["status"] != "pending"]
        inc.update({"status": "investigating", "title": "", "summary": "", "root_cause": "", "category": "",
                    "severity": "", "confidence": None, "evidence": [], "actions": [], "rejected_actions": [],
                    "needs_technician": False, "technician_note": "", "trace": [], "thinking": True,
                    "investigation_seconds": None, "dry_run": None, "revised": False, "error": "",
                    "verification": "", "situation": "", "job_states": {},
                    "models": dict(self.llm.models), "jobs": sorted(self.cluster.jobs_on(inc["target"]))})
        note = f" Steps already run: {'; '.join(a['label'] for a in done)}." if done else ""
        self._timeline(inc, "You asked Nemotron to investigate again." + note)
        self.spawn(self.investigate(inc))
        return inc

    def close(self, iid: int) -> dict:
        """The person closes an incident by hand: they dealt with it outside Nightingale, or it was a false alarm."""
        inc = self.get(iid)
        if inc["status"] not in ("needs_attention", "awaiting_approval"):
            raise ValueError("This incident can't be closed right now.")
        for a in inc["actions"]:
            if a["status"] == "pending":
                a["status"] = "skipped"
        inc["status"] = "closed"
        self._timeline(inc, "Closed by you without further steps.")
        return inc

    def skip(self, iid: int) -> dict:
        inc = self.get(iid)
        action = self._next_pending(inc)
        if inc["status"] != "awaiting_approval" or action is None:
            raise ValueError("This incident has no steps waiting for approval.")
        action["status"] = "skipped"
        self._timeline(inc, f"Skipped: {action['label']}")
        self._maybe_finish(inc)
        return inc

    def _maybe_finish(self, inc: dict) -> None:
        if inc["status"] == "awaiting_approval" and self._next_pending(inc) is None:
            inc["status"] = "verifying"
            local = self.cluster.nodes.get(inc["target"]) is not None and self.cluster.nodes[inc["target"]].local
            if local:  # the person did the steps; give the GPU a little longer to settle
                self.cluster.mark_local_checked()
                inc["verify_tick"] = self.cluster.tick_count + LOCAL_VERIFY_DELAY_TICKS
                self._timeline(inc, "You've done the steps. Re-checking this computer's GPU in about 30 seconds.")
            else:
                inc["verify_tick"] = self.cluster.tick_count + VERIFY_DELAY_TICKS
                self._timeline(inc, "All steps handled. Re-checking telemetry in a few seconds.")

    def verify_pending(self) -> None:
        for inc in self.incidents:
            if inc["status"] != "verifying" or self.cluster.tick_count < inc.get("verify_tick", 0):
                continue
            target = inc["target"]
            issues = self.cluster.observed_issues(target)
            ticket = self.cluster.has_open_ticket(target)
            if issues and not ticket:
                inc["status"] = "needs_attention"
                inc["verification"] = "Still seeing: " + "; ".join(issues)
            elif ticket:
                inc["status"] = "waiting_on_technician"
                inc["verification"] = ("Stable now. Waiting for the repair ticket to be closed."
                                       + (f" Remaining: {'; '.join(issues)}." if issues else ""))
            else:
                inc["status"] = "resolved"
                inc["verification"] = "Verified: no remaining problems."
            self._timeline(inc, inc["verification"])
            if inc["status"] in ("resolved", "waiting_on_technician"):
                done = [a["label"] for a in inc["actions"] if a["status"] == "done"]
                self.cluster.remember(target, inc["gpu"], inc["title"], "; ".join(done) or "none")
                if target == "laptop" and self.store is not None:
                    self.store.remember(inc["gpu"], inc["title"], "; ".join(done) or "none")

    def local_disconnected(self) -> None:
        for inc in self.incidents:
            if inc["target"] == "laptop" and inc["status"] in OPEN:
                inc["status"] = "resolved"
                inc["verification"] = "Closed: this computer's GPU was disconnected from Nightingale."
                self._timeline(inc, inc["verification"])

    def ticket_repaired(self, ticket: dict) -> None:
        target = ticket["node"] or ticket["rack"]
        for inc in self.incidents:
            if inc["target"] == target and inc["status"] == "waiting_on_technician":
                inc["status"] = "resolved"
                inc["verification"] = f"Repair finished ({ticket['id']})."
                self._timeline(inc, inc["verification"])

    # ------------------------------------------------------- pasted logs
    async def analyze_external(self, raw: str) -> dict:
        """Diagnose logs a user pastes from their own systems."""
        route = []
        text = raw
        if len(raw) > 6000:  # long logs: Nano filters, Ultra diagnoses
            chunks = [raw[i:i + 40000] for i in range(0, min(len(raw), 200000), 40000)]
            kept = []
            for chunk in chunks:
                out = await asyncio.to_thread(self.llm.extract, chunk)
                kept += [str(l) for l in (out.get("relevant_lines") or [])]
            text = "\n".join(kept[:150]) or raw[-6000:]
            who = "Demo stand-in for Nemotron Nano" if self.llm.mock else self.llm.models["triage"]
            route.append(f"{who} filtered {len(raw.splitlines())} lines down to {len(kept[:150])}")
        start = time.time()
        d = await asyncio.to_thread(self.llm.diagnose, text, True)
        who = "Demo stand-in for Nemotron Ultra" if self.llm.mock else self.llm.models["diagnosis"]
        route.append(f"{who} diagnosed this in {time.time() - start:.1f}s")
        steps = []
        for raw_action in (d.get("actions") or [])[:8]:
            action, _ = actions.normalize(raw_action)  # no data center to check against: advice only
            if not action and isinstance(raw_action, dict) and raw_action.get("action") in actions.CATALOG:
                params = raw_action.get("params") if isinstance(raw_action.get("params"), dict) else {}
                action = {"action": raw_action["action"], "params": {str(k): _text(v, 120) for k, v in params.items()},
                          "risk": actions.CATALOG[raw_action["action"]]["risk"],
                          "reason": _text(raw_action.get("reason"), 300), "status": "advice", "result": ""}
                action["label"] = actions.label(action)
            if action:
                action["status"] = "advice"
                steps.append(action)
        return {
            "title": _text(d.get("title"), 90), "category": _text(d.get("category"), 30),
            "severity": _text(d.get("severity"), 20), "confidence": d.get("confidence"),
            "summary": _text(d.get("summary"), 900), "root_cause": _text(d.get("root_cause"), 400),
            "evidence": [_text(e, 300) for e in (d.get("evidence") or [])][:6],
            "actions": steps, "needs_technician": bool(d.get("needs_technician")),
            "technician_note": _text(d.get("technician_note"), 400), "route": route,
            "models": dict(self.llm.models),
        }
