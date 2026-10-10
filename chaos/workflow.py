#!/usr/bin/env python3
"""
LangGraph workflow: one chaos experiment from planning to report.

    collect_state -> plan -> validate -> approve -> execute -> evaluate -> restore -> verify -> analyze -> save

Each box is a node with explicit inputs/outputs in WorkflowState. Conditional edges send
rejected, unapproved, failed or unrecovered runs straight to `save`, so every path ends
with an audit record in results/workflows/.

LangGraph only ORCHESTRATES. Safety stays in plain code:
  * planner.validate()  - allowlist, fault types, parameter bounds (AI output gate)
  * controller.py       - baseline gate, thresholds, kill switch, try/finally restore
  * executor.run()      - fixed argv (no shell) + independent watchdog
  * verify node         - independent recovery check after the controller's own check
"""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import controller as C  # noqa: E402
import executor as X    # noqa: E402
import planner as P     # noqa: E402

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph     # noqa: E402
from langgraph.types import interrupt                  # noqa: E402

WORKFLOWS_DIR = C.DATA / "results" / "workflows"


class WorkflowState(TypedDict, total=False):
    # --- inputs
    workflow_id: str
    mode: str                 # "run" | "dry_run"
    rank: int                 # which planner proposal to use (default 1)
    manual: dict              # optional human-specified proposal (skips the AI planner)
    auto_approve: bool        # tests only; the CLI asks a human
    url: str
    timing: dict              # baseline/post/recovery_timeout overrides (tests)
    verify_timeout_s: float
    # --- produced by nodes
    context: dict
    plan: dict
    proposal: dict
    approval: dict
    execution: dict
    telemetry: dict
    restore: dict
    recovery: dict
    analysis: dict
    outcome: str              # final classification
    steps: list               # audit trail: [{node, at, note}]
    record_path: str


def _step(state: WorkflowState, node: str, note: str) -> list:
    return [*state.get("steps", []), {"node": node, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                      "note": note}]


# --------------------------------------------------------------------------- 1. collect state
def collect_state(state: WorkflowState) -> dict:
    ctx = P.build_context()
    h = ctx["cluster_health"]
    problems = []
    if not h.get("reachable"):
        problems.append(f"cluster unreachable: {h.get('error')}")
    elif h.get("degraded_deployments"):
        problems.append(f"degraded deployments: {h['degraded_deployments']}")
    if C.KILL_FILE.exists():
        problems.append("kill switch chaos/KILL is present")
    if C.STATE_FILE.exists():
        problems.append("a previous fault is still recorded (run chaos/restore.py)")
    if state.get("mode") != "dry_run" and problems:
        return {"context": ctx, "outcome": "PRECHECK_FAILED: " + "; ".join(problems),
                "steps": _step(state, "collect_state", "; ".join(problems))}
    return {"context": ctx, "steps": _step(state, "collect_state",
                                           f"{len(ctx['previous_runs'])} previous runs; health {h}")}


# --------------------------------------------------------------------------- 2. plan (Ollama)
def plan(state: WorkflowState) -> dict:
    if state.get("manual"):
        p = {"planner": "manual (human-specified)", "model": None, "experiments": [], "rejected": [],
             "raw_proposals": [state["manual"]]}
        acc, rej = P.validate([state["manual"]])
        p.update(experiments=acc, rejected=rej)
        return {"plan": p, "steps": _step(state, "plan", "manual proposal supplied")}
    p = P.make_plan(state["context"])
    p.pop("context", None)
    return {"plan": p, "steps": _step(state, "plan", f"{p['planner']} -> {len(p['experiments'])} valid, "
                                                     f"{len(p['rejected'])} rejected")}


# --------------------------------------------------------------------------- 3. validate
def _flat(a: dict) -> dict:
    return {"target": a["target"], "fault_type": a["fault_type"], "duration_seconds": a["duration_seconds"],
            **(a.get("parameters") or {}), **{k: v for k, v in (a.get("abort_conditions") or {}).items()
                                              if k in ("max_error_rate", "max_p95_ms")},
            "hypothesis": a["hypothesis"], "reason": a["reason"], "expected_impact": a.get("expected_impact", "")}


def validate(state: WorkflowState) -> dict:
    exps = state["plan"].get("experiments", [])
    rank = state.get("rank") or 1
    chosen = next((e for e in exps if e["rank"] == rank), None)
    if not chosen:
        reasons = [r["reasons"] for r in state["plan"].get("rejected", [])][:3]
        return {"outcome": "REJECTED: no valid proposal" + (f" (validator: {reasons})" if reasons else ""),
                "steps": _step(state, "validate", f"no valid proposal with rank {rank}")}
    ok, bad = P.validate([_flat(chosen)])          # re-validate at execution time
    if bad:
        return {"outcome": f"REJECTED: {bad[0]['reasons']}", "steps": _step(state, "validate", str(bad[0]["reasons"]))}
    proposal = {**ok[0], "experiment_id": chosen["experiment_id"], "rank": chosen["rank"],
                "grounding": chosen.get("grounding", "n/a (manual)"), "evidence": chosen.get("evidence"),
                "planner": state["plan"]["planner"], "planner_model": state["plan"].get("model")}
    return {"proposal": proposal, "steps": _step(state, "validate", f"{proposal['experiment_id']} passed")}


# --------------------------------------------------------------------------- 4. approve (human)
def approve(state: WorkflowState) -> dict:
    p = state["proposal"]
    if state.get("mode") == "dry_run":
        return {"approval": {"approved": True, "by": "dry-run (no fault will be injected)"},
                "steps": _step(state, "approve", "dry run")}
    if state.get("auto_approve"):
        return {"approval": {"approved": True, "by": "auto (test)"}, "steps": _step(state, "approve", "auto (test)")}
    answer = interrupt({"question": "Approve this experiment on the local kind sandbox?", "proposal": p})
    approved = str(answer).strip().lower() in ("y", "yes", "approve", "true")
    return {"approval": {"approved": approved, "by": "human", "answer": str(answer)[:50]},
            **({} if approved else {"outcome": "NOT_APPROVED"}),
            "steps": _step(state, "approve", "approved" if approved else "declined")}


# --------------------------------------------------------------------------- 5. execute (controller)
def execute(state: WorkflowState) -> dict:
    p, dry = state["proposal"], state.get("mode") == "dry_run"
    label = f"workflow {state['workflow_id']} {p['experiment_id']} ({p['planner']}): {p['hypothesis']}"
    try:
        res = X.run(p, label=label, dry_run=dry, url=state.get("url", "http://localhost:8080"),
                    **(state.get("timing") or {}))
    except Exception as e:  # noqa: BLE001
        return {"execution": {"error": repr(e)}, "outcome": f"EXECUTION_ERROR: {e!r}",
                "steps": _step(state, "execute", f"error {e!r}")}
    out = {"execution": res}
    if dry:
        out["outcome"] = "DRY_RUN (no fault injected)"
        note = "dry run: " + " ".join(res["command"][2:])
    elif res["rejected"]:
        out["outcome"] = "REJECTED_BY_CONTROLLER"
        note = "controller validation rejected the run"
    elif res["watchdog_fired"]:
        out["outcome"] = "ABORTED_WATCHDOG"
        note = f"watchdog killed controller after {res['watchdog_limit_s']} s"
    elif not res.get("summary"):
        out["outcome"] = f"EXECUTION_ERROR: controller exit {res['exit_code']} without summary"
        note = out["outcome"]
    else:
        note = f"run {res['run_id']} status {res['summary']['status']}"
    out["steps"] = _step(state, "execute", note)
    return out


# --------------------------------------------------------------------------- 6. evaluate telemetry
def evaluate(state: WorkflowState) -> dict:
    s = state["execution"]["summary"]
    ev = []
    run_dir = C.RUNS_DIR / s["run_id"]
    if (run_dir / "events.jsonl").exists():
        ev = [json.loads(l) for l in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    abort = next((e for e in ev if e["event"] in ("threshold_breach", "kill_switch", "telemetry_lost", "error")), None)
    tel = {"status": s["status"], "baseline": s.get("baseline_window"), "during_fault": s.get("fault_window"),
           "post_recovery": s.get("post_window"), "first_error_after_inject_s": s.get("first_error_after_inject_s"),
           "fault_duration_s": s.get("fault_duration_s"), "abort_reason": abort,
           "controller_recovery": s.get("recovery"), "cleanup": s.get("cleanup")}
    return {"telemetry": tel, "steps": _step(state, "evaluate", f"status {s['status']}; abort {abort and abort['event']}")}


# --------------------------------------------------------------------------- 7. restore
def restore(state: WorkflowState) -> dict:
    s = (state.get("execution") or {}).get("summary") or {}
    if s.get("cleanup") == "ok" and not C.STATE_FILE.exists():
        return {"restore": {"ok": True, "by": "controller finally-block"}, "steps": _step(state, "restore", "controller restored")}
    if not C.STATE_FILE.exists() and s.get("cleanup") == "not_needed":
        return {"restore": {"ok": True, "by": "nothing injected"}, "steps": _step(state, "restore", "nothing to restore")}
    import subprocess
    r = subprocess.run([sys.executable, str(HERE / "restore.py")], capture_output=True, text=True, timeout=200)
    ok = r.returncode == 0 and not C.STATE_FILE.exists()
    return {"restore": {"ok": ok, "by": "restore.py", "output": (r.stdout + r.stderr)[-800:]},
            **({} if ok else {"outcome": "CLEANUP_FAILED: manual action required (python chaos/restore.py)"}),
            "steps": _step(state, "restore", f"restore.py rc={r.returncode}")}


# --------------------------------------------------------------------------- 8. verify recovery (independent)
def verify(state: WorkflowState) -> dict:
    url = state.get("url", "http://localhost:8080")
    deadline = time.time() + state.get("verify_timeout_s", 120)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    streak, deps_ok, t0 = 0, False, time.time()
    while time.time() < deadline:
        try:
            deps_ok = not C.unhealthy_deployments()
        except Exception:  # noqa: BLE001
            deps_ok = False
        try:
            with opener.open(url + "/", timeout=3) as r:
                ok = r.status == 200
        except Exception:  # noqa: BLE001
            ok = False
        streak = streak + 1 if (deps_ok and ok) else 0
        if streak >= 5:
            break
        time.sleep(0.5)
    verified = streak >= 5
    rec = {"verified": verified, "deployments_ready": deps_ok, "consecutive_ok": streak,
           "seconds_to_verify": round(time.time() - t0, 1)}
    out = {"recovery": rec, "steps": _step(state, "verify", f"verified={verified}")}
    if not verified:
        out["outcome"] = "RECOVERY_FAILED: system not healthy after restore"
    return out


# --------------------------------------------------------------------------- 9. analyze (local Ollama / templates)
def analyze(state: WorkflowState) -> dict:
    import analyze as A
    try:
        r = A.analyze()
        top = [{k: f[k] for k in ("rank", "severity", "title", "text_source")} for f in r["findings"][:5]]
        res = {"narrative": r["narrative"], "resilience": r["resilience"]["overall"], "top_findings": top,
               "report": r["saved_to"]}
        note = f"score {res['resilience']}; narrative {r['narrative']['used']}"
    except Exception as e:  # noqa: BLE001  (analysis failure must not hide the run outcome)
        res, note = {"error": repr(e)}, f"analysis failed {e!r}"
    s = state["execution"]["summary"]
    return {"analysis": res, "outcome": state.get("outcome") or f"COMPLETED: {s['status']}",
            "steps": _step(state, "analyze", note)}


# --------------------------------------------------------------------------- 10. save
def save(state: WorkflowState) -> dict:
    WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
    path = WORKFLOWS_DIR / f"{state['workflow_id']}.json"
    rec = {k: v for k, v in state.items() if k not in ("context",)}
    rec["outcome"] = state.get("outcome") or "UNKNOWN"
    rec["steps"] = _step(state, "save", str(path))
    path.write_text(json.dumps(rec, indent=2, default=str), encoding="utf-8")
    return {"record_path": str(path), "steps": rec["steps"], "outcome": rec["outcome"]}


# --------------------------------------------------------------------------- routing (conditional edges)
def after_collect(s):
    return "save" if s.get("outcome") else "plan"


def after_validate(s):
    return "save" if s.get("outcome") else "approve"


def after_approve(s):
    return "execute" if s["approval"]["approved"] else "save"


def after_execute(s):
    o = s.get("outcome") or ""
    if o.startswith("DRY_RUN") or o == "REJECTED_BY_CONTROLLER":
        return "save"
    if o.startswith(("EXECUTION_ERROR", "ABORTED_WATCHDOG")):
        return "restore"            # make sure nothing is left injected
    return "evaluate"


def after_restore(s):
    return "save" if (s.get("outcome") or "").startswith("CLEANUP_FAILED") else "verify"


def after_verify(s):
    if not s["recovery"]["verified"]:
        return "save"
    return "analyze" if (s.get("execution") or {}).get("summary") else "save"


def build_graph():
    g = StateGraph(WorkflowState)
    for name, fn in [("collect_state", collect_state), ("plan", plan), ("validate", validate), ("approve", approve),
                     ("execute", execute), ("evaluate", evaluate), ("restore", restore), ("verify", verify),
                     ("analyze", analyze), ("save", save)]:
        g.add_node(name, fn)
    g.add_edge(START, "collect_state")
    g.add_conditional_edges("collect_state", after_collect, {"plan": "plan", "save": "save"})
    g.add_edge("plan", "validate")
    g.add_conditional_edges("validate", after_validate, {"approve": "approve", "save": "save"})
    g.add_conditional_edges("approve", after_approve, {"execute": "execute", "save": "save"})
    g.add_conditional_edges("execute", after_execute, {"evaluate": "evaluate", "restore": "restore", "save": "save"})
    g.add_edge("evaluate", "restore")
    g.add_conditional_edges("restore", after_restore, {"verify": "verify", "save": "save"})
    g.add_conditional_edges("verify", after_verify, {"analyze": "analyze", "save": "save"})
    g.add_edge("analyze", "save")
    g.add_edge("save", END)
    return g.compile(checkpointer=InMemorySaver())


def new_state(**kw) -> WorkflowState:
    wid = "WF-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return {"workflow_id": wid, "mode": "run", "rank": 1, "steps": [], **kw}


def mermaid() -> str:
    return build_graph().get_graph().draw_mermaid()
