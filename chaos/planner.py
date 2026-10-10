#!/usr/bin/env python3
"""
Experiment planner (Agent 1: local Ollama model).

Inputs : dependency map, current cluster health, baseline probe metrics, previous run results,
         and the approved fault catalog with its limits.
Output : ranked experiment proposals as structured JSON (schema-constrained via Ollama).
Safety : every proposal is checked by validate() - plain code, no LLM. The planner never
         executes anything and can only TIGHTEN abort thresholds, never loosen them.
Fallback: if Ollama is unavailable, times out or returns unusable output, a clearly labelled
         RULE-BASED planner is used. It is never presented as AI.

  CHAOS_PLANNER_MODEL   default qwen2.5:3b
  python chaos/planner.py   -> prints the plan, saves results/plans/plan_<ts>.json
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import controller as C  # noqa: E402
import llm as L         # noqa: E402

ARCH = C.REPO / "architecture"
PLANS_DIR = C.DATA / "results" / "plans"
MODEL = os.getenv("CHAOS_PLANNER_MODEL", "qwen2.5:3b")
PROVIDER = "ollama"
PLANNER_FAULTS = ("scale_to_zero", "pod_delete", "traffic_spike")   # network_latency: lab only (netem_lab.py)
MIN_DUR, MAX_DUR = 5, 30                  # stricter than the controller's 60 s cap
DEFAULT_ERR, MIN_ERR = 0.20, 0.05         # abort thresholds: planner may only tighten these
DEFAULT_P95, MIN_P95 = 1000, 200
MAX_PROPOSALS = 5
TEXT_LIMIT = 400

RECOVERY_PLAN = {
    "scale_to_zero": "restore original replica count; kubectl rollout status; 5 consecutive successful frontend requests",
    "pod_delete": "Kubernetes recreates the pod; rollout status; 5 consecutive successful frontend requests",
    "traffic_spike": "stop the load generator; 5 consecutive successful requests under the P95 limit",
    "network_latency": "remove the netem qdisc (self-removes after duration); 5 consecutive fast successful requests",
}

PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {"experiments": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "rank": {"type": "integer"}, "target": {"type": "string"},
            "fault_type": {"type": "string", "enum": list(PLANNER_FAULTS)},
            "duration_seconds": {"type": "integer"},
            "delay_ms": {"type": "integer"}, "spike_users": {"type": "integer"},
            "max_error_rate": {"type": "number"}, "max_p95_ms": {"type": "integer"},
            "hypothesis": {"type": "string"}, "reason": {"type": "string"},
            "expected_impact": {"type": "string"},
        },
        "required": ["rank", "target", "fault_type", "duration_seconds", "hypothesis", "reason", "expected_impact"],
    }}},
    "required": ["experiments"],
}

SYSTEM = """You plan chaos-engineering experiments for a SANDBOX microservice application.
You never execute anything. Return ONLY JSON matching the given schema:
{"experiments":[{"rank":1,"target":"...","fault_type":"...","duration_seconds":10,
  "spike_users":80,"max_error_rate":0.2,"max_p95_ms":1000,
  "hypothesis":"...","reason":"...","expected_impact":"..."}]}
Rules:
- Use ONLY targets and fault_types from allowed_catalog, and parameters within its limits.
- traffic_spike target must be "frontend".
- Return 3 to 5 experiments, most informative first. Prefer different fault types.
- Prefer unverified dependency edges, services many others depend on, and open questions
  raised by previous_runs. Do not repeat a known result unless you say what new question it answers.
- hypothesis: a falsifiable prediction about user-visible behaviour.
- reason: one short sentence citing ONLY facts present in the input (dependency_edges, previous_runs).
  If previous_runs has no run for a target, do NOT say previous runs showed anything about it.
- Keep text fields under 300 characters. Never invent measurements or past results."""


# --------------------------------------------------------------------------- inputs
def load_dependency_map() -> dict:
    p = ARCH / "dependency-map.json"
    if not p.exists():
        subprocess.run([sys.executable, str(ARCH / "build_dependency_map.py")], cwd=str(C.REPO),
                       capture_output=True, text=True, timeout=60)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"services": {}, "edges": []}


def load_history(limit: int = 20) -> list[dict]:
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
                    "k8s_ready_s": rec.get("k8s_ready_s"), "user_recovery_s": rec.get("user_recovery_s"),
                    "baseline_window": s.get("baseline_window")})
        if len(out) >= limit:
            break
    return out


def cluster_health() -> dict:
    try:
        bad = C.unhealthy_deployments()
        return {"reachable": True, "degraded_deployments": bad}
    except Exception as e:  # noqa: BLE001
        return {"reachable": False, "error": str(e)[:200]}


def build_context() -> dict:
    dm = load_dependency_map()
    services = sorted(n for n in dm.get("services", {}) if n in C.ALLOWED_TARGETS or n == "frontend")
    edges = [{"from": e["from"], "to": e["to"], "verified": e.get("verified", False),
              "criticality": e.get("criticality"), "target_deployed": e.get("target_deployed", True)}
             for e in dm.get("edges", []) if e["from"] != "loadgenerator"]
    history = load_history()
    baseline = next((h["baseline_window"] for h in history if h.get("baseline_window")), None)
    for h in history:
        h.pop("baseline_window", None)
    return {
        "application": "Google Online Boutique on local kind cluster (sandbox only)",
        "services": services, "dependency_edges": edges,
        "cluster_health": cluster_health(),
        "baseline_probe_window": baseline, "previous_runs": history,
        "allowed_catalog": {
            "targets": sorted(C.ALLOWED_TARGETS),
            "fault_types": {
                "scale_to_zero": "deployment scaled to 0 replicas for duration_seconds, then restored",
                "pod_delete": "target pods deleted once; Kubernetes recreates them",
                "traffic_spike": f"target MUST be 'frontend'; spike_users 1..{C.MAX_SPIKE_USERS} extra users",
            },
            "duration_seconds": {"min": MIN_DUR, "max": MAX_DUR},
            "abort_conditions": {"max_error_rate": {"min": MIN_ERR, "max": DEFAULT_ERR},
                                 "max_p95_ms": {"min": MIN_P95, "max": DEFAULT_P95}},
        },
    }


# --------------------------------------------------------------------------- deterministic validation
def _int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():   # small local models often quote numbers
        return int(v.strip())
    return None


def validate(raw, stamp: str = None) -> tuple[list, list]:
    """Returns (accepted, rejected-with-reasons). Plain code: the safety gate for AI output."""
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    accepted, rejected, seen = [], [], set()
    if not isinstance(raw, list):
        return [], [{"proposal": raw, "reasons": ["experiments must be a list"]}]
    for p in raw[: MAX_PROPOSALS * 2]:
        if not isinstance(p, dict):
            rejected.append({"proposal": p, "reasons": ["not an object"]})
            continue
        reasons = []
        t, f = p.get("target"), p.get("fault_type")
        d = _int(p.get("duration_seconds"))
        if f not in PLANNER_FAULTS:
            reasons.append(f"fault_type '{f}' not supported")
        elif f == "traffic_spike":
            if t != C.SPIKE_TARGET:
                reasons.append(f"traffic_spike target must be '{C.SPIKE_TARGET}'")
        elif t not in C.ALLOWED_TARGETS:
            reasons.append(f"target '{t}' not in allowlist")
        if d is None or not (MIN_DUR <= d <= MAX_DUR):
            reasons.append(f"duration_seconds '{p.get('duration_seconds')}' must be an integer {MIN_DUR}..{MAX_DUR}")
        params = {}
        if f == "network_latency":
            ms = _int(p.get("delay_ms", 200))
            if ms is None or not (C.MIN_DELAY_MS <= ms <= C.MAX_DELAY_MS):
                reasons.append(f"delay_ms '{p.get('delay_ms')}' must be {C.MIN_DELAY_MS}..{C.MAX_DELAY_MS}")
            params["delay_ms"] = ms
        if f == "traffic_spike":
            u = _int(p.get("spike_users", 80))
            if u is None or not (1 <= u <= C.MAX_SPIKE_USERS):
                reasons.append(f"spike_users '{p.get('spike_users')}' must be 1..{C.MAX_SPIKE_USERS}")
            params["spike_users"] = u
        err = p.get("max_error_rate", DEFAULT_ERR)
        p95 = _int(p.get("max_p95_ms", DEFAULT_P95))
        if not isinstance(err, (int, float)) or isinstance(err, bool) or not (MIN_ERR <= err <= DEFAULT_ERR):
            reasons.append(f"max_error_rate '{err}' must be {MIN_ERR}..{DEFAULT_ERR} (may only tighten)")
        if p95 is None or not (MIN_P95 <= p95 <= DEFAULT_P95):
            reasons.append(f"max_p95_ms '{p.get('max_p95_ms')}' must be {MIN_P95}..{DEFAULT_P95} (may only tighten)")
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
            "target": t, "fault_type": f, "duration_seconds": d, "parameters": params,
            "abort_conditions": {"max_error_rate": float(err), "max_p95_ms": p95,
                                 "telemetry_loss": "3 consecutive connection errors or no probe for 5 s",
                                 "kill_switch": "chaos/KILL file or Ctrl+C"},
            "recovery_verification": RECOVERY_PLAN[f],
            **{k: str(p.get(k, ""))[:TEXT_LIMIT] for k in ("hypothesis", "expected_impact", "reason")},
        })
    accepted = accepted[:MAX_PROPOSALS]
    for i, a in enumerate(accepted, 1):   # rank = validated order; ID is deterministic
        a["rank"] = i
        a["experiment_id"] = f"EXP-{stamp}-{i}"
    return accepted, rejected


# --------------------------------------------------------------------------- grounding (truthfulness)
HISTORY_CLAIM = re.compile(r"\b(previous|prior|earlier|past|last)\b.{0,40}\b(runs?|experiments?|tests?)\b|"
                           r"\b(runs?|experiments?) (showed|revealed|indicated)", re.I)
# a claim about what past runs SHOWED: user-facing errors/outage vs. no impact
CLAIM_IMPACT = re.compile(r"\b(errors?|error[ -]rates?|outages?|fail(ed|ures?|ing)?|5\d\d|down(time)?|unavailab\w*|"
                          r"broke|break(s|ing)?)\b", re.I)
CLAIM_NO_IMPACT = re.compile(r"\b(no (user[- ])?(impact|errors?|failures?)|zero errors|0 errors|"
                             r"degrad\w* gracefully|unaffected|resilient)\b", re.I)


def ground(accepted: list, ctx: dict) -> list:
    """Attach code-computed evidence to each proposal and flag reasons that cite non-existent history.
    Validation decides what is SAFE; grounding decides whether the AI's justification is TRUE."""
    runs = ctx.get("previous_runs", [])
    edges = ctx.get("dependency_edges", [])
    for p in accepted:
        t, f = p["target"], p["fault_type"]
        on_t = [h for h in runs if h["target"] == t]
        same = [h for h in on_t if h["fault"] == f]
        callers = sorted({e["from"] for e in edges if e["to"] == t})
        issues = []
        reason = p.get("reason", "")
        basis = same or on_t                       # the runs a claim about this target/fault refers to
        errs = [h.get("errors_in_fault") for h in basis if h.get("errors_in_fault") is not None]
        if HISTORY_CLAIM.search(reason + " " + p.get("hypothesis", "")):
            if not on_t:
                issues.append(f"cites previous runs, but no run on {t} exists")
            elif not same and re.search(f.replace("_", "[ _-]?"), reason, re.I):
                issues.append(f"cites previous {f} runs on {t}, but none exist")
            # outcome check (reason only: the hypothesis is a prediction, the reason claims what was SEEN)
            elif HISTORY_CLAIM.search(reason) and errs:
                if CLAIM_IMPACT.search(reason) and not CLAIM_NO_IMPACT.search(reason) and sum(errs) == 0:
                    issues.append(f"claims past errors/outage, but recorded runs on {t} had 0 errors in the fault window")
                elif CLAIM_NO_IMPACT.search(reason) and sum(errs) > 0:
                    issues.append(f"claims no past impact, but recorded runs on {t} had {sum(errs)} errors in the fault window")
        p["evidence"] = {"previous_runs_on_target": len(on_t), "previous_runs_same_fault": len(same),
                         "previous_errors_in_fault": sum(errs) if errs else None,
                         "callers_in_dependency_map": callers}
        p["grounding"] = "ok" if not issues else "UNSUPPORTED: " + "; ".join(issues)
    ordered = [p for p in accepted if p["grounding"] == "ok"] + [p for p in accepted if p["grounding"] != "ok"]
    stamp = accepted[0]["experiment_id"].rsplit("-", 1)[0] if accepted else ""
    for i, p in enumerate(ordered, 1):
        p["rank"], p["experiment_id"] = i, f"{stamp}-{i}"
    return ordered


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
                          f"degrade; users recover within 30 s of restore.",
            "expected_impact": f"{len(callers)} dependent service(s): {', '.join(callers) or 'none'}",
            "reason": f"rule-based score {score}: fan-in {len(callers)}, "
                      f"{'never tested' if t not in tested_targets else 'new fault type for this target'}"}))
    if ("frontend", "traffic_spike") not in tested:
        out.append((4, {"target": "frontend", "fault_type": "traffic_spike", "duration_seconds": 20, "spike_users": 80,
                        "hypothesis": "A sudden spike raises frontend P95 latency; it returns to baseline within 30 s.",
                        "expected_impact": "higher latency on all pages; possible timeouts",
                        "reason": "rule-based score 4: traffic spike never tested"}))
    return [p for _, p in sorted(out, key=lambda x: -x[0])[:MAX_PROPOSALS]]


# --------------------------------------------------------------------------- entry point
def make_plan(context: dict = None) -> dict:
    ctx = context or build_context()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    plan = {"created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "planner": None,
            "model": None, "llm_error": None, "raw_output": None, "experiments": [], "rejected": []}
    try:
        obj, meta = L.ollama_json(MODEL, SYSTEM, ctx, schema=PROPOSAL_SCHEMA)
        plan["raw_output"] = meta.get("raw")
        accepted, rejected = validate(obj.get("experiments"), stamp)
        accepted = ground(accepted, ctx)
        plan.update(planner="AI (Ollama)", model=f"{meta['model']} (local)", experiments=accepted, rejected=rejected)
        if not accepted:
            raise L.LLMError("model returned no valid proposals")
    except Exception as e:  # noqa: BLE001  -> fall back; never execute anything arbitrary
        plan["llm_error"] = str(e)[:500]
        accepted, rejected_fb = validate(rule_based(ctx), stamp)
        accepted = ground(accepted, ctx)
        plan.update(planner="RULE-BASED fallback (not AI)", model=None, experiments=accepted,
                    rejected=plan.get("rejected", []) + rejected_fb)
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    path = PLANS_DIR / f"plan_{stamp}.json"
    plan["context"] = ctx
    path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    plan["saved_to"] = str(path)
    return plan


if __name__ == "__main__":
    p = make_plan()
    print(f"Planner: {p['planner']}  model={p['model']}")
    if p["llm_error"]:
        print(f"  (LLM not used: {p['llm_error']})")
    for e in p["experiments"]:
        print(f"#{e['rank']} {e['experiment_id']} {e['fault_type']} -> {e['target']} {e['duration_seconds']}s "
              f"{e['parameters'] or ''}  | {e['reason']}  [grounding: {e['grounding']}]")
    for r in p["rejected"]:
        print(f"REJECTED {json.dumps(r['proposal'])[:120]}: {r['reasons']}")
    print(f"Saved: {p['saved_to']}")
