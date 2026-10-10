#!/usr/bin/env python3
"""
Single entry point.   python chaos\\cli.py <command>

  health     cluster, frontend, local LLM (Ollama API) models, kill switch/state
  map        rebuild architecture/dependency-map.{json,md} from the live cluster
  plan       AI planner only: ranked, validated proposals (no execution)
  dry-run    full LangGraph workflow, but the fault is NOT injected (prints the exact command)
  run        full workflow; asks for human approval before injecting
  report     rebuild results/findings-report.md from all recorded runs
  graph      write the workflow diagram to architecture/workflow-graph.md
  latency-lab  network-latency validation on DISPOSABLE pods (passes args to chaos/netem_lab.py)

  run/dry-run options: --rank N  (use planner proposal N)   or a manual proposal:
                       --target X --fault F [--duration S] [--delay-ms MS] [--spike-users N]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import controller as C  # noqa: E402
import llm as L         # noqa: E402


def cmd_health(_a) -> int:
    ok = True
    print("Cluster  :", end=" ")
    try:
        bad = C.unhealthy_deployments()
        print("all deployments ready" if not bad else f"DEGRADED {bad}")
        ok &= not bad
    except Exception as e:  # noqa: BLE001
        print(f"UNREACHABLE ({e})")
        ok = False
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open("http://localhost:8080/", timeout=5) as r:
            print("Frontend : HTTP", r.status)
    except Exception as e:  # noqa: BLE001
        print(f"Frontend : UNREACHABLE ({e}) - is the port-forward running?")
        ok = False
    try:
        print("Ollama   :", ", ".join(L.ollama_models()) or "running, no models pulled")
    except L.LLMError as e:
        print(f"Ollama   : {e}")
    print("LLM URL  :", L.OLLAMA_URL, "(planner + analyst; local only, no cloud API)")
    print("Kill sw. :", "PRESENT - remove chaos/KILL to run" if C.KILL_FILE.exists() else "clear")
    print("State    :", "ACTIVE FAULT RECORDED - run chaos/restore.py" if C.STATE_FILE.exists() else "no active fault")
    ok &= not C.KILL_FILE.exists() and not C.STATE_FILE.exists()
    print("READY" if ok else "NOT READY")
    return 0 if ok else 1


def cmd_map(_a) -> int:
    return subprocess.run([sys.executable, str(C.REPO / "architecture" / "build_dependency_map.py")]).returncode


def cmd_plan(_a) -> int:
    import planner as P
    p = P.make_plan()
    print(f"Planner: {p['planner']}  model={p['model']}")
    if p["llm_error"]:
        print(f"  (LLM not used: {p['llm_error']})")
    for e in p["experiments"]:
        print(f"  #{e['rank']} {e['experiment_id']}  {e['fault_type']} -> {e['target']}  {e['duration_seconds']}s "
              f"{e['parameters'] or ''}\n      why: {e['reason']}\n      hypothesis: {e['hypothesis']}"
              f"\n      grounding: {e['grounding']}   evidence: {e['evidence']}")
    for r in p["rejected"]:
        print(f"  REJECTED {json.dumps(r['proposal'])[:100]} -> {r['reasons']}")
    print("Saved:", p["saved_to"])
    return 0


def _manual(a):
    if not a.target:
        return None
    m = {"target": a.target, "fault_type": a.fault, "duration_seconds": a.duration,
         "hypothesis": a.hypothesis or f"Manual {a.fault} on {a.target}", "reason": "manual experiment chosen by a human",
         "expected_impact": "see results"}
    if a.delay_ms is not None:
        m["delay_ms"] = a.delay_ms
    if a.spike_users is not None:
        m["spike_users"] = a.spike_users
    return m


def _workflow(a, dry: bool) -> int:
    import workflow as W
    from langgraph.types import Command
    app = W.build_graph()
    st = W.new_state(mode="dry_run" if dry else "run", rank=a.rank, manual=_manual(a))
    cfg = {"configurable": {"thread_id": st["workflow_id"]}}
    print(f"Workflow {st['workflow_id']} ({'DRY RUN' if dry else 'LIVE'})")
    res = app.invoke(st, cfg)
    while res.get("__interrupt__"):
        p = res["__interrupt__"][0].value["proposal"]
        print("\n=== HUMAN APPROVAL REQUIRED ===")
        for k in ("experiment_id", "planner", "planner_model", "target", "fault_type", "duration_seconds",
                  "parameters", "abort_conditions", "hypothesis", "reason", "grounding", "evidence",
                  "recovery_verification"):
            print(f"  {k:22}: {p.get(k)}")
        ans = input("Inject this fault on the local kind sandbox? [yes/NO]: ")
        res = app.invoke(Command(resume=ans), cfg)
    print("\nSteps:")
    for s in res.get("steps", []):
        print(f"  {s['at'][11:19]}  {s['node']:14} {s['note'][:150]}")
    print("\nOUTCOME:", res.get("outcome"))
    print("Record :", res.get("record_path"))
    return 0 if not str(res.get("outcome", "")).startswith(("CLEANUP_FAILED", "RECOVERY_FAILED", "EXECUTION_ERROR")) else 2


def cmd_report(_a) -> int:
    import analyze as A
    r = A.analyze()
    print(f"Resilience score {r['resilience']['overall']}/100 ({r['resilience']['coverage']}); "
          f"narrative: {r['narrative']['used']} {r['narrative'].get('model') or ''}")
    for f in r["findings"]:
        print(f"  {f['rank']}. [{f['severity']}] {f['title']}  <{f['text_source']}>")
    print("Saved:", r["saved_to"])
    return 0


def cmd_graph(_a) -> int:
    import workflow as W
    out = C.REPO / "architecture" / "workflow-graph.md"
    out.write_text("# LangGraph workflow\n\nGenerated by `python chaos\\cli.py graph`.\n\n```mermaid\n" + W.mermaid() +
                   "\n```\n", encoding="utf-8")
    print("Saved:", out)
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "latency-lab":
        return subprocess.run([sys.executable, str(HERE / "netem_lab.py"), *sys.argv[2:]]).returncode
    ap = argparse.ArgumentParser(prog="python chaos\\cli.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("health", "map", "plan", "report", "graph"):
        sub.add_parser(name)
    for name in ("dry-run", "run"):
        s = sub.add_parser(name)
        s.add_argument("--rank", type=int, default=1)
        s.add_argument("--target")
        s.add_argument("--fault", default="scale_to_zero")
        s.add_argument("--duration", type=int, default=10)
        s.add_argument("--delay-ms", type=int)
        s.add_argument("--spike-users", type=int)
        s.add_argument("--hypothesis")
    a = ap.parse_args()
    return {"health": cmd_health, "map": cmd_map, "plan": cmd_plan, "report": cmd_report, "graph": cmd_graph,
            "dry-run": lambda x: _workflow(x, True), "run": lambda x: _workflow(x, False)}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
