#!/usr/bin/env python3
"""
AI experiment planner.

Reads the dependency map, baseline metrics and previous run results, asks an LLM to
rank chaos experiments, then VALIDATES every proposal deterministically against the
controller's allowlist. The planner never executes anything: a validated proposal is
only run when a human presses Run (UI) or passes it to controller.py.

LLM:   Anthropic Messages API, configured by environment variables
         ANTHROPIC_API_KEY      (required for AI mode; never stored in the repo)
         CHAOS_PLANNER_MODEL    (default claude-sonnet-5-5)
       Without a key, or if the model output is unusable, the planner falls back to a
       clearly labelled RULE-BASED planner. It is never presented as AI.

CLI:   python chaos/planner.py            -> prints the plan, saves results/plans/<ts>.json
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import controller as C  # noqa: E402

ARCH = C.REPO / "architecture"
PLANS_DIR = C.REPO / "results" / "plans"
API_URL = os.getenv("CHAOS_PLANNER_API_URL", "https://api.anthropic.com/v1/messages")
MODEL = os.getenv("CHAOS_PLANNER_MODEL", "claude-sonnet-5-5")
PLANNER_FAULTS = ("scale_to_zero", "pod_delete")
MIN_DUR, MAX_DUR = 5, 30          # stricter than the controller's 60 s cap
MAX_PROPOSALS = 5
TEXT_LIMIT = 400


# --------------------------------------------------------------------------- context
def load_dependency_map() -> dict:
    p = ARCH / "dependency-map.json"
    if not p.exists():
        subprocess.run([sys.executable, str(ARCH / "build_dependency_map.py")], cwd=str(C.REPO),
                       capture_output=True, text=True, timeout=60)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"services": {}, "edges": []}


def load_history(limit: int = 15) -> list[dict]:
    out = []
    if not C.RUNS_DIR.exists():
        return out
    for d in sorted((x for x in C.RUNS_DIR.iterdir() if x.is_dir()), reverse=True):
        p = d / "summary.json"
        if not p.exists():
            continue
        s = json.loads(p.read_text(encoding="utf-8"))
        if not s.get("fault_injected_at"):
            continue
        fw, rec = s.get("fault_window") or {}, s.get("recovery") or {}
        out.append({"run_id": s["run_id"], "target": s["request"]["target"], "fault": s["request"]["fault"],
                    "status": s["status"], "fault_duration_s": s.get("fault_duration_s"),
                    "errors_in_fault": fw.get("errors"), "probes_in_fault": fw.get("n"),
                    "first_error_after_inject_s": s.get("first_error_after_inject_s"),
                    "k8s_ready_s": rec.get("k8s_ready_s"), "user_recovery_s": rec.get("user_recovery_s")})
        if len(out) >= limit:
            break
    return out


def build_context() -> dict:
    dm = load_dependency_map()
    services = {n: s for n, s in dm.get("services", {}).items() if n in C.ALLOWED_TARGETS or n == "frontend"}
    edges = [{"from": e["from"], "to": e["to"], "verified": e.get("verified", False),
              "criticality": e.get("criticality"), "target_deployed": e.get("target_deployed", True)}
             for e in dm.get("edges", []) if e["from"] != "loadgenerator"]
    history = load_history()
    baseline = next((json.loads((C.RUNS_DIR / h["run_id"] / "summary.json").read_text(encoding="utf-8")).get("baseline_window")
                     for h in history), None)
    return {
        "application": "Google Online Boutique on local kind cluster (sandbox only)",
        "services": services,
        "dependency_edges": edges,
        "baseline_probe_window": baseline,
        "previous_runs": history,
        "allowed_catalog": {
            "targets": sorted(C.ALLOWED_TARGETS),
            "fault_types": {
                "scale_to_zero": "deployment scaled to 0 replicas for duration_seconds, then restored",
                "pod_delete": "target pods deleted once; Kubernetes recreates them; observed for duration_seconds",
            },
            "duration_seconds": {"min": MIN_DUR, "max": MAX_DUR},
            "auto_abort": "error rate > 20% or P95 > 1000 ms over 5 s",
        },
    }


# --------------------------------------------------------------------------- validation
def validate(raw: list) -> tuple[list, list]:
    """Deterministic gate. Returns (accepted, rejected-with-reasons)."""
    accepted, rejected, seen = [], [], set()
    if not isinstance(raw, list):
        return [], [{"proposal": raw, "reasons": ["experiments must be a list"]}]
    for p in raw[: MAX_PROPOSALS * 2]:
        reasons = []
        if not isinstance(p, dict):
            rejected.append({"proposal": p, "reasons": ["not an object"]})
            continue
        t, f, d = p.get("target"), p.get("fault_type"), p.get("duration_seconds")
        if t not in C.ALLOWED_TARGETS:
            reasons.append(f"target '{t}' not in allowlist")
        if f not in PLANNER_FAULTS:
            reasons.append(f"fault_type '{f}' not supported")
        if not isinstance(d, int) or isinstance(d, bool) or not (MIN_DUR <= d <= MAX_DUR):
            reasons.append(f"duration_seconds '{d}' must be an integer {MIN_DUR}..{MAX_DUR}")
        for k in ("hypothesis", "reason"):
            if not isinstance(p.get(k), str) or not p[k].strip():
                reasons.append(f"missing {k}")
        if (t, f) in seen:
            reasons.append("duplicate target/fault")
        if reasons:
            rejected.append({"proposal": p, "reasons": reasons})
            continue
        seen.add((t, f))
        accepted.append({
            "target": t, "fault_type": f, "duration_seconds": d,
            **{k: str(p.get(k, ""))[:TEXT_LIMIT] for k in
               ("hypothesis", "expected_impact", "safety_considerations", "reason")},
        })
    accepted = accepted[:MAX_PROPOSALS]
    for i, a in enumerate(accepted, 1):   # rank = validated order
        a["rank"] = i
    return accepted, rejected


# --------------------------------------------------------------------------- LLM planner
SYSTEM = """You plan chaos-engineering experiments for a SANDBOX microservice application.
You never execute anything. You return ONLY a JSON object, no prose, no markdown fences:
{"experiments":[{"rank":1,"target":"...","fault_type":"...","duration_seconds":10,
  "hypothesis":"...","expected_impact":"...","safety_considerations":"...","reason":"..."}]}
Rules:
- Use ONLY targets and fault_types from allowed_catalog; duration within its min/max.
- Return 3 to 5 experiments ranked most informative first.
- Prefer experiments that test unverified dependency edges, services with many dependents
  (single points of failure), or questions raised by previous_runs (e.g. slow recovery,
  readiness vs user-recovery gaps). Avoid repeating an experiment whose result is already
  known unless you state what new question it answers.
- hypothesis: a falsifiable prediction about user-visible behaviour.
- reason: one short sentence citing the evidence (edges, previous runs) you used.
- Keep every text field under 300 characters. Do not invent metrics."""


def call_llm(context: dict) -> tuple[str, dict]:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    body = json.dumps({"model": MODEL, "max_tokens": 2000, "system": SYSTEM,
                       "messages": [{"role": "user", "content": json.dumps(context)}]}).encode()
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"LLM HTTP {e.code}: {e.read()[:300]!r}") from e
    text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
    return text, {"model": resp.get("model", MODEL), "usage": resp.get("usage")}


def parse_json(text: str) -> list:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model output")
    obj = json.loads(t[start:end + 1])
    return obj.get("experiments", obj) if isinstance(obj, dict) else obj


# --------------------------------------------------------------------------- rule-based fallback
def rule_based(context: dict) -> list:
    """Deterministic, transparent heuristic. Labelled as NOT AI."""
    fan_in = {}
    for e in context["dependency_edges"]:
        fan_in.setdefault(e["to"], set()).add(e["from"])
    tested = {(h["target"], h["fault"]) for h in context["previous_runs"]}
    tested_targets = {h["target"] for h in context["previous_runs"]}
    noncritical = {e["to"] for e in context["dependency_edges"] if e.get("verified") and
                   str(e.get("criticality", "")).startswith("non-critical")}
    out = []
    for t in sorted(C.ALLOWED_TARGETS):
        fault = "scale_to_zero" if (t, "scale_to_zero") not in tested else "pod_delete"
        if (t, fault) in tested:
            continue
        callers = sorted(fan_in.get(t, []))
        score = 3 * len(callers) + (2 if t not in tested_targets else 0) - (2 if t in noncritical else 0)
        out.append((score, {
            "target": t, "fault_type": fault, "duration_seconds": 10,
            "hypothesis": f"If {t} is disrupted ({fault}), requests from {', '.join(callers) or 'no known callers'} "
                          f"will degrade; the controller restores it and users recover within 30 s.",
            "expected_impact": f"{len(callers)} dependent service(s): {', '.join(callers) or 'none'}",
            "safety_considerations": "10 s duration, auto-abort at 20% errors, kill switch available",
            "reason": f"rule-based score {score}: fan-in {len(callers)}, "
                      f"{'never tested' if t not in tested_targets else 'new fault type for this target'}"}))
    return [p for _, p in sorted(out, key=lambda x: -x[0])[:MAX_PROPOSALS]]


# --------------------------------------------------------------------------- entry point
def make_plan() -> dict:
    ctx = build_context()
    plan = {"created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "planner": None,
            "model": None, "llm_error": None, "raw_output": None, "experiments": [], "rejected": []}
    try:
        text, meta = call_llm(ctx)
        plan["raw_output"] = text
        accepted, rejected = validate(parse_json(text))
        plan.update(planner="AI (LLM)", model=meta["model"], usage=meta.get("usage"),
                    experiments=accepted, rejected=rejected)
        if not accepted:
            raise RuntimeError("LLM returned no valid proposals")
    except Exception as e:  # noqa: BLE001  -> fall back, never execute anything arbitrary
        plan["llm_error"] = str(e)[:500]
        accepted, rejected_fb = validate(rule_based(ctx))
        plan.update(planner="RULE-BASED fallback (not AI)", experiments=accepted,
                    rejected=plan.get("rejected", []) + rejected_fb)
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    path = PLANS_DIR / f"plan_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    plan["context"] = ctx
    path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    plan["saved_to"] = str(path.relative_to(C.REPO))
    return plan


if __name__ == "__main__":
    p = make_plan()
    print(f"Planner: {p['planner']}  model={p['model']}")
    if p["llm_error"]:
        print(f"  (LLM not used: {p['llm_error']})")
    for e in p["experiments"]:
        print(f"#{e['rank']} {e['fault_type']} {e['target']} {e['duration_seconds']}s - {e['reason']}")
        print(f"    hypothesis: {e['hypothesis']}")
    for r in p["rejected"]:
        print(f"REJECTED {r['proposal']}: {r['reasons']}")
    print(f"Saved: {p['saved_to']}")
