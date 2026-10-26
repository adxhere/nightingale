"""The fixed catalog of actions Nightingale may take, plus validation and execution.

The language model can only *propose* actions from this catalog. Every proposal is
checked against the live data center (real server names, rack ids, GPU indexes, job
IDs, sane values), dry-run on a copy of the data center, and shown to a human. Nothing
runs until that human approves it, and each operation enforces its own preconditions,
so an unsafe step fails cleanly instead of doing damage.
"""
from __future__ import annotations

import re

from . import pc_actions
from .pc_actions import PC_CATALOG

CATALOG: dict[str, dict] = {
    "set_power_limit": {
        "params": ["node", "gpu", "watts"], "risk": "low",
        "desc": "Lower one GPU's power limit (300-700 W) so it makes less heat. Slows that GPU but keeps "
                "it safe while its cooling is degraded."},
    "set_rack_power_limit": {
        "params": ["rack", "watts"], "risk": "low",
        "desc": "Lower the power limit (300-700 W) of every GPU in one rack, e.g. while the rack's coolant "
                "loop is degraded. Slows the jobs there but prevents thermal shutdowns."},
    "drain_node": {
        "params": ["node"], "risk": "medium",
        "desc": "Stop the scheduler from placing new work on a server. Running jobs are not killed."},
    "drain_rack": {
        "params": ["rack"], "risk": "medium",
        "desc": "Drain every server in a rack (no new work there). Follow with restart_job_from_checkpoint "
                "for each job on the rack to move it to spares elsewhere."},
    "undrain_node": {
        "params": ["node"], "risk": "medium",
        "desc": "Return a healthy, repaired server to service (it becomes a spare)."},
    "restart_job_from_checkpoint": {
        "params": ["job"], "risk": "medium",
        "desc": "Stop a job and restart it from its last checkpoint. Drained servers and servers with "
                "missing GPUs are swapped for healthy spares."},
    "reset_gpu": {
        "params": ["node", "gpu"], "risk": "high",
        "desc": "Reset one GPU: clears volatile error counters and retires weak or pending memory rows. "
                "Fails if a running job uses the GPU or the GPU is not visible on the bus."},
    "reboot_node": {
        "params": ["node"], "risk": "high",
        "desc": "Reboot a server to recover GPUs that dropped off the bus. Fails if a running job uses the "
                "server. The server comes back drained."},
    "open_ticket": {
        "params": ["node", "gpu", "summary"], "risk": "low",
        "desc": "Open a hardware repair ticket for the on-site technician (inspect or replace a part). "
                "gpu may be null for server-level problems."},
    "open_facility_ticket": {
        "params": ["rack", "summary"], "risk": "low",
        "desc": "Open a ticket for the facilities team: cooling (CDU, coolant flow) or power problems that "
                "affect a whole rack."},
    "notify_owner": {
        "params": ["job", "message"], "risk": "low",
        "desc": "Message the team that owns a job, e.g. to report a bug in their code."},
    "recommend": {
        "params": ["node", "message"], "risk": "low",
        "desc": "For server 'laptop' (this computer's own GPU) only: a step the person does themselves, "
                "written as a short plain instruction (raise the laptop for airflow, lower a game setting). "
                "Nothing runs; Nightingale re-checks the GPU after they confirm."},
}
OPTIONAL = {("open_ticket", "gpu")}
INT_PARAMS = {"gpu", "watts"}

LABELS = {
    "set_power_limit": "Cap {node} GPU {gpu} at {watts} W",
    "set_rack_power_limit": "Cap every GPU in rack {rack} at {watts} W",
    "drain_node": "Drain {node}",
    "drain_rack": "Drain every server in rack {rack}",
    "undrain_node": "Return {node} to service",
    "restart_job_from_checkpoint": "Restart {job} from its last checkpoint",
    "reset_gpu": "Reset {node} GPU {gpu}",
    "reboot_node": "Reboot {node}",
    "open_ticket": "Open a repair ticket for {node}",
    "open_facility_ticket": "Open a facilities ticket for rack {rack}",
    "notify_owner": "Message the owners of {job}",
    "recommend": "{message}",
}


REAL_LABELS = {
    "drain_node": "Cordon {node}",
    "drain_rack": "Cordon every server in rack {rack}",
    "undrain_node": "Uncordon {node}",
    "restart_job_from_checkpoint": "Move {job}: evict its pods so it restarts on a healthy server",
}


def catalog_text() -> str:
    dc = "\n".join(f"- {name}({', '.join(spec['params'])}): {spec['desc']}" for name, spec in CATALOG.items()
                   if name != "recommend")
    return (f"For data center servers and racks:\n{dc}\n\nOn REAL servers (hall R, Kubernetes) the same actions act on "
            "real infrastructure: drain_node and drain_rack cordon nodes, undrain_node uncordons, "
            "restart_job_from_checkpoint evicts the job's pods so their controller starts them again on healthy "
            "nodes, set_power_limit and set_rack_power_limit run nvidia-smi -pl (watts within the GPU's own allowed "
            "range, shown by node_status), reset_gpu runs nvidia-smi -r. reboot_node is not available for real "
            "servers: drain, move the job, then open_ticket. Each plan is rehearsed with Kubernetes' server-side "
            "dry run first."
            f"\n\nFor server 'laptop' (this computer) only:\n"
            f"- recommend(node, message): {CATALOG['recommend']['desc']}\n{pc_actions.catalog_text()}")


def label(action: dict) -> str:
    p = action.get("params", {})
    try:
        text = LABELS[action["action"]].format(**{k: p.get(k, "?") for k in ("node", "gpu", "watts", "rack", "job", "message")})
    except (KeyError, IndexError):
        return action.get("action", "")
    if action["action"] == "open_ticket" and p.get("gpu") is not None:
        text += f" GPU {p['gpu']}"
    return text


def _to_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        m = re.search(r"-?\d+", value)
        return int(m.group()) if m else None
    return None


def normalize(raw, cluster=None) -> tuple[dict | None, str | None]:
    """Validate one proposed action. Returns (action, None) or (None, the reason it was rejected)."""
    if not isinstance(raw, dict):
        return None, "proposal is not an object"
    name = str(raw.get("action", "")).strip()
    given = raw.get("params") if isinstance(raw.get("params"), dict) else raw
    if name in PC_CATALOG:  # a fix on this computer
        params, why = pc_actions.normalize(name, given, cluster)
        if params is None:
            return None, f"{name}: {why}"
        pspec = PC_CATALOG[name]
        action = {"action": name, "params": params, "risk": pspec["risk"], "mode": pspec["mode"],
                  "admin": pspec["admin"], "guided": bool(pspec.get("guided")),
                  "reason": str(raw.get("reason", "")).strip()[:300], "status": "pending", "result": ""}
        action["label"] = pc_actions.label(name, params)
        return action, None
    spec = CATALOG.get(name)
    if not spec:
        return None, f"'{name}' is not in the action catalog"
    params: dict = {}
    for key in spec["params"]:
        value = given.get(key)
        if key in INT_PARAMS:
            number = _to_int(value)
            if number is None:
                if (name, key) in OPTIONAL:
                    params[key] = None
                    continue
                return None, f"{name}: missing or invalid '{key}'"
            params[key] = number
        else:
            if value is None or not str(value).strip():
                return None, f"{name}: missing '{key}'"
            params[key] = str(value).strip()
    if name == "recommend":
        params["message"] = params["message"][:220]
    if cluster is not None:
        error = cluster.validate_target(params)
        if error:
            return None, f"{name}: {error}"
    real = cluster is not None and _is_real_target(cluster, params)
    # Simulated H100s take 300-700 W; a real GPU's own limits are checked when the step is rehearsed.
    if "watts" in params and not (50 <= params["watts"] <= 1500 if real else 300 <= params["watts"] <= 700):
        return None, f"{name}: watts must be between {'50 and 1500' if real else '300 and 700'}"
    if cluster is not None:
        node = cluster.nodes.get(params.get("node"))
        local = bool(node is not None and node.local)
        if local and name != "recommend":
            return None, (f"{name}: {params['node']} is this computer; use one of the pc_ fixes or recommend "
                          "instead of data center actions")
        if name == "recommend" and not local:
            return None, "recommend: only for this computer ('laptop'); use the data center actions elsewhere"
        if name == "reboot_node" and real:
            return None, ("reboot_node: Nightingale doesn't reboot real servers; drain it, move its jobs and "
                          "open_ticket for the reboot")
    action = {"action": name, "params": params, "risk": spec["risk"],
              "reason": str(raw.get("reason", "")).strip()[:300], "status": "pending", "result": ""}
    action["label"] = label(action)
    if real and name in REAL_LABELS:  # say what it does on a real cluster
        action["label"] = REAL_LABELS[name].format(**{k: params.get(k, "?") for k in ("node", "rack", "job")})
    return action, None


def execute(action: dict, cluster) -> tuple[bool, str]:
    p = action["params"]
    name = action["action"]
    if name == "set_power_limit":
        return cluster.set_power_limit(p["node"], p["gpu"], p["watts"])
    if name == "set_rack_power_limit":
        return cluster.set_rack_power_limit(p["rack"], p["watts"])
    if name == "drain_node":
        return cluster.drain_node(p["node"])
    if name == "drain_rack":
        return cluster.drain_rack(p["rack"])
    if name == "undrain_node":
        return cluster.undrain_node(p["node"])
    if name == "restart_job_from_checkpoint":
        return cluster.restart_job(p["job"])
    if name == "reset_gpu":
        return cluster.reset_gpu(p["node"], p["gpu"])
    if name == "reboot_node":
        return cluster.reboot_node(p["node"])
    if name == "open_ticket":
        return cluster.open_ticket(p["node"], p["gpu"], p["summary"])
    if name == "open_facility_ticket":
        return cluster.open_facility_ticket(p["rack"], p["summary"])
    if name == "notify_owner":
        return cluster.notify_owner(p["job"], p["message"])
    if name == "recommend":
        return cluster.recommend(p["node"], p["message"])
    if name in PC_CATALOG:
        if getattr(cluster, "pc", None) is None:
            return False, "Fixes on this computer aren't available here."
        return cluster.pc.execute(name, p)
    return False, f"Unknown action {name}"


def _is_real_target(cluster, params: dict) -> bool:
    if params.get("node") in cluster.nodes:
        return bool(cluster.nodes[params["node"]].real)
    if params.get("rack") in cluster.racks:
        return cluster.racks[params["rack"]].hall == "R"
    job = cluster.jobs.get(params.get("job")) if params.get("job") else None
    return bool(job is not None and job.real)


def is_real(action: dict, cluster) -> bool:
    return _is_real_target(cluster, action.get("params", {}))


def is_pc(action: dict) -> bool:
    return action["action"] in PC_CATALOG or action["action"] == "recommend"


def precheck_plan(cluster, plan: list[dict], target: str, mode: str = "light") -> dict:
    """This computer can't be copied, so instead of a rehearsal each step is checked without changing anything."""
    steps = []
    for a in plan:
        if a["action"] == "recommend":
            steps.append({"label": a["label"], "ok": True, "locked": False, "result": "A step for you. Nothing runs."})
            continue
        pc = getattr(cluster, "pc", None)
        ok, msg = pc.precheck(a["action"], a["params"]) if pc is not None else (False, "Fixes aren't available here.")
        locked = a.get("mode") == "serious" and mode != "serious"
        steps.append({"label": a["label"], "ok": ok, "locked": locked,
                      "result": ("Needs serious mode. " if locked else "") + msg})
    failed = [s for s in steps if not s["ok"]]
    locked = [s for s in steps if s["locked"]]
    admin = [a for a in plan if a.get("admin")]
    lines = [("OK: " if s["ok"] else "PROBLEM: ") + f"{s['label']}. {s['result']}" for s in steps]
    summary = ("Checked each step on your computer without changing anything: "
               f"{len(steps) - len(failed)} of {len(steps)} ready"
               + (f", {len(locked)} need serious mode" if locked else "")
               + (f", {len(admin)} will ask Windows for permission" if admin else "")
               + (f". Can't run: {'; '.join(s['label'] + ' (' + s['result'] + ')' for s in failed)}" if failed else "")
               + ".")
    return {"verdict": "fail" if failed else "pass", "steps": steps, "lines": lines, "summary": summary,
            "crashed": [], "recovered": [], "issues": cluster.observed_issues(target), "seconds": 0, "checked": True}


def dry_run(cluster, plan: list[dict], target: str, settle_ticks: int = 12, mode: str = "light") -> dict:
    """Run a plan on a copy of the data center and report what would happen (or, for this computer, check it)."""
    if plan and all(is_pc(a) for a in plan):
        return precheck_plan(cluster, plan, target, mode)
    if plan and getattr(cluster, "fleet", None) is not None and (
            cluster.is_real(target) or (target in cluster.racks and cluster.racks[target].hall == "R")
            or any(is_real(a, cluster) for a in plan)):
        return cluster.rehearse_real(plan, target)  # real servers can't be copied: Kubernetes dry run instead
    twin = cluster.clone()
    jobs_here = twin.jobs_on(target)
    before = {j.id: j.status for j in twin.jobs.values()}
    steps = []
    for a in plan:
        ok, msg = execute(a, twin)
        steps.append({"label": a.get("label") or a["action"], "ok": ok, "result": msg})
        for _ in range(2):
            twin.tick()
    for _ in range(settle_ticks):
        twin.tick()
    blocked = [s for s in steps if not s["ok"]]
    crashed, recovered, untouched_changed = [], [], []
    for j in twin.jobs.values():
        was, now = before[j.id], j.status
        if now in ("crashed", "failed") and was not in ("crashed", "failed"):
            crashed.append(j.id)
        if was in ("crashed", "restarting") and now == "running":
            recovered.append(j.id)
        if j.id not in jobs_here and was != now:
            untouched_changed.append(f"{j.id} went from {was} to {now}")
    issues = twin.observed_issues(target)
    ticket = twin.has_open_ticket(target)
    if blocked or crashed:
        verdict = "fail"
    elif issues and not ticket:
        verdict = "warn"
    else:
        verdict = "pass"
    lines = []
    for s in steps:
        lines.append(("OK: " if s["ok"] else "BLOCKED: ") + f"{s['label']}. {s['result']}")
    seconds = (2 * len(plan) + settle_ticks) * 2
    jobs_line = ", ".join(f"{j.id} {j.status} at {round(j.rate * 100)}% speed" for j in twin.jobs.values() if j.id in jobs_here)
    summary = (f"Simulated {seconds} s ahead on a copy of the data center. "
               + (f"Jobs here afterwards: {jobs_line}. " if jobs_line else "")
               + (f"Would crash: {', '.join(crashed)}. " if crashed else "No job crashes. ")
               + (f"Still wrong on {target}: {'; '.join(issues)}. " if issues else f"{target} looks healthy afterwards. ")
               + ("A repair ticket stays open for the remaining hardware work. " if ticket and issues else "")
               + (f"Other jobs affected: {'; '.join(untouched_changed)}." if untouched_changed else "No other job touched."))
    return {"verdict": verdict, "steps": steps, "lines": lines, "summary": summary, "crashed": crashed,
            "recovered": recovered, "issues": issues, "seconds": seconds}
