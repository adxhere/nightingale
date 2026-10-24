"""Nightingale command-line tools.

  python -m app.cli models            list the Nemotron models your key can use
  python -m app.cli analyze FILE      diagnose a log file (good for batch / serverless jobs)
  python -m app.cli benchmark [N] [--size 256|1024] [--out results.json]
                                      run N failure drills (default 12) and score Nightingale
"""
from __future__ import annotations

import asyncio
import json
import sys

from .agent import Agent
from .config import load_settings
from .llm import LLMClient
from .simulator import Cluster


def main(argv: list[str]) -> int:
    if argv and argv[0] == "benchmark":
        return benchmark(argv[1:])
    if not argv or argv[0] not in ("models", "analyze"):
        print(__doc__)
        return 1
    settings = load_settings()
    llm = LLMClient(settings)
    if argv[0] == "models":
        if llm.mock:
            print("Set NEBIUS_API_KEY first (see .env.example).")
            return 1
        for model_id in llm.list_nemotron_models():
            print(model_id)
        return 0
    if len(argv) < 2:
        print("Usage: python -m app.cli analyze path/to/logfile")
        return 1
    with open(argv[1], encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    if not llm.mock and settings.auto_discover_models:
        llm.discover_models()
    agent = Agent(Cluster(), llm, settings)
    result = asyncio.run(agent.analyze_external(raw))
    result["usage"] = llm.usage.summary()
    result["mode"] = "demo" if llm.mock else "live"
    print(json.dumps(result, indent=2))
    return 0


def benchmark(args: list[str]) -> int:
    from .benchmark import Benchmark

    n, size, out = 12, 1024, None
    rest = list(args)
    while rest:
        a = rest.pop(0)
        if a == "--size" and rest:
            size = int(rest.pop(0))
        elif a == "--out" and rest:
            out = rest.pop(0)
        elif a.isdigit():
            n = int(a)
    settings = load_settings()
    llm = LLMClient(settings)
    if not llm.mock and settings.auto_discover_models:
        llm.discover_models()
    mode = "demo (offline stand-in models)" if llm.mock else f"live: {llm.models['triage']} + {llm.models['diagnosis']}"
    print(f"Running {n} failure drills on {size:,}-GPU data centers, {mode}...")
    st = asyncio.run(Benchmark(settings, llm).run(n, size))
    if st["error"]:
        print(st["error"])
    for c in st["cases"]:
        mark = "ok " if c["found"] and c["cause"] and c["fixed"] and c["safe"] else "!! "
        print(f"  {mark}{c['n']:>2} {c['label']:<24} on {c['target']:<9} "
              f"found={c['found']!s:<5} cause={c['cause']!s:<5} fixed={c['fixed']!s:<5} safe={c['safe']!s:<5} {c['why']}")
    r = st["results"]
    if r:
        print(f"\nFound the broken part {r['found']}%, right cause {r['cause']}%, plan passed rehearsal "
              f"{r['rehearsal']}%, fixed {r['fixed']}%, safe {r['safe']}%. False alarms: {r['false_alarms']}.")
        print(f"Median time to spot {r['detect_seconds']} s (simulated), to diagnose {r['diagnose_seconds']} s. "
              f"Cost ${r['cost']:.4f} (${r['cost_per_drill']:.4f} per drill).")
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(st, fh, indent=2)
        print(f"Saved {out}")
    return 0 if r else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
