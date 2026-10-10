#!/usr/bin/env python3
"""
Findings analysis: evidence -> ranked weaknesses -> recommended fixes -> resilience score.

  python chaos/analyze.py      -> results/findings-report.md + results/findings.json

Division of labour (deliberate):
  * ALL numbers, findings, severities and scores are computed by deterministic code from
    results/runs/*/summary.json. The formulas are printed in the report.
  * The LLM (same provider as the planner; free local Ollama by default) only writes the
    plain-English explanation and recommended fix for each finding. Its text is rejected
    and replaced by a template if it references unknown findings or quotes any number that
    is not in that finding's evidence (hallucination guard). The report says which text
    came from the AI and which from templates.
"""
from __future__ import annotations

import json
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import controller as C  # noqa: E402
import planner as PL    # noqa: E402  (LLM client)

OUT_MD = C.REPO / "results" / "findings-report.md"
OUT_JSON = C.REPO / "results" / "findings.json"

# Which services the HTTP probe (/, /product/<id>, /cart) actually exercises, per upstream
# architecture docs. checkout/payment/email are only used by POST /cart/checkout.
PROBE_COVERS = {"productcatalogservice", "currencyservice", "cartservice", "redis-cart",
                "recommendationservice", "adservice", "shippingservice", "frontend"}

FIX_TEMPLATES = {
    "recovery_gap": "Find out why users keep failing after the pod is Ready. A likely cause is the frontend's gRPC "
                    "client waiting in reconnect backoff. Options: run 2+ replicas so a restart never removes every "
                    "endpoint; set gRPC keepalive and a lower maximum reconnect backoff in the frontend; retry "
                    "idempotent reads with a short timeout.",
    "hard_dependency": "Remove the single point of failure: run 2+ replicas with a PodDisruptionBudget "
                       "(minAvailable: 1) and let the frontend degrade (cached or partial page) instead of "
                       "returning HTTP 500 when this dependency is unavailable.",
    "slow_restart": "Shorten startup (image/JVM tuning, readiness probe timing) or keep 2+ replicas so a restart "
                    "is invisible to users.",
    "latency_under_load": "Set CPU/memory requests and limits on the frontend, add a HorizontalPodAutoscaler and "
                          "rate limiting, and load-test to find the real capacity.",
    "graceful": "Keep this behaviour: add this experiment to a regression suite so graceful degradation stays.",
    "coverage_gap": "Not a proven strength: the probe never calls this service. Add a checkout probe "
                    "(POST /cart/checkout) and re-run before drawing conclusions.",
    "detection_delay": "Make the auto-abort faster: run probes concurrently or use a shorter probe timeout "
                       "(currently one hung request can block the prober for 2 s).",
}


def med(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(statistics.median(xs), 2) if xs else None


def load_runs() -> list[dict]:
    runs = []
    for d in sorted(x for x in C.RUNS_DIR.iterdir() if x.is_dir()) if C.RUNS_DIR.exists() else []:
        p = d / "summary.json"
        if not p.exists():
            continue
        s = json.loads(p.read_text(encoding="utf-8"))
        if not s.get("fault_injected_at"):
            continue  # cancelled / precheck-failed: nothing was injected
        runs.append(s)
    return runs


def group_metrics(runs: list[dict]) -> list[dict]:
    groups = {}
    for s in runs:
        groups.setdefault((s["request"]["target"], s["request"].get("fault", "scale_to_zero")), []).append(s)
    out = []
    for (target, fault), rs in groups.items():
        fw = [r.get("fault_window") or {} for r in rs]
        rec = [r.get("recovery") or {} for r in rs]
        base = [((r.get("baseline_window") or {}).get("p95_ms")) for r in rs]
        err = med([w.get("error_rate") for w in fw])
        any_err = any((w.get("errors") or 0) > 0 for w in fw)
        user = med([x.get("user_recovery_s") for x in rec]) if any_err else None
        k8s = med([x.get("k8s_ready_s") for x in rec])
        gap = round(user - k8s, 2) if (user is not None and k8s is not None and fault == "scale_to_zero") else None
        aborted = [r.get("fault_duration_s") for r in rs if str(r["status"]).startswith("ABORTED_THRESHOLD")]
        out.append({
            "target": target, "fault": fault, "runs": len(rs), "run_ids": [r["run_id"] for r in rs],
            "statuses": sorted({r["status"] for r in rs}),
            "error_rate_in_fault": err, "any_user_errors": any_err,
            "p95_in_fault_ms": med([w.get("p95_ms") for w in fw]), "baseline_p95_ms": med(base),
            "fault_duration_s": med([r.get("fault_duration_s") for r in rs]),
            "first_error_s": med([r.get("first_error_after_inject_s") for r in rs]),
            "abort_after_s": med(aborted) if aborted else None,
            "k8s_ready_s": k8s, "user_recovery_s": user, "ready_vs_user_gap_s": gap,
            "probe_covers_target": target in PROBE_COVERS,
        })
    return out


def severity(g: dict) -> float:
    """0-100. Formula is printed in the report."""
    err = g["error_rate_in_fault"] or 0
    outage = (g["fault_duration_s"] or 0) + (g["user_recovery_s"] or 0) if g["any_user_errors"] else 0
    k8s = g["k8s_ready_s"] or 0
    lat = max(0.0, (g["p95_in_fault_ms"] or 0) - (g["baseline_p95_ms"] or 0)) if g["fault"] == "traffic_spike" else 0
    return round(50 * err + 30 * min(1, outage / 60) + 10 * min(1, k8s / 120) + 10 * min(1, lat / 1000), 1)


def level(score: float) -> str:
    return "High" if score >= 60 else "Medium" if score >= 30 else "Low" if score > 5 else "Info"


def derive_findings(groups: list[dict]) -> list[dict]:
    F = []

    def add(kind, g, title, evidence, sev=None):
        sev = severity(g) if sev is None else sev
        F.append({"id": f"F{len(F) + 1}", "kind": kind, "target": g["target"], "fault": g["fault"], "title": title,
                  "severity_score": sev, "severity": level(sev), "evidence": evidence, "run_ids": g["run_ids"],
                  "confidence": "high" if g["runs"] >= 3 else "medium" if g["runs"] == 2 else "low (single run)"})

    for g in groups:
        t, f = g["target"], g["fault"]
        if g["ready_vs_user_gap_s"] is not None and g["ready_vs_user_gap_s"] >= 5:
            add("recovery_gap", g, f"Users keep failing after {t} is Ready",
                {"runs": g["runs"], "k8s_ready_s_median": g["k8s_ready_s"], "user_recovery_s_median": g["user_recovery_s"],
                 "gap_s_median": g["ready_vs_user_gap_s"]})
        if (g["error_rate_in_fault"] or 0) >= 0.5 and f != "traffic_spike":
            add("hard_dependency", g, f"{t} is a hard dependency / single point of failure",
                {"runs": g["runs"], "error_rate_in_fault_median": g["error_rate_in_fault"],
                 "first_error_s_median": g["first_error_s"], "replicas": 1})
        if (g["k8s_ready_s"] or 0) >= 30 and f != "traffic_spike":
            add("slow_restart", g, f"{t} takes {g['k8s_ready_s']:.0f} s to become Ready after restore",
                {"runs": g["runs"], "k8s_ready_s_median": g["k8s_ready_s"]})
        if f == "traffic_spike" and ((g["p95_in_fault_ms"] or 0) >= 3 * max(1, g["baseline_p95_ms"] or 1)
                                     or g["any_user_errors"] or "ABORTED_THRESHOLD" in g["statuses"]):
            add("latency_under_load", g, "Frontend latency/errors degrade under a traffic spike",
                {"runs": g["runs"], "baseline_p95_ms": g["baseline_p95_ms"], "p95_in_fault_ms": g["p95_in_fault_ms"],
                 "error_rate_in_fault_median": g["error_rate_in_fault"]})
        if not g["any_user_errors"] and f != "traffic_spike":
            if g["probe_covers_target"]:
                add("graceful", g, f"Frontend degrades gracefully without {t}",
                    {"runs": g["runs"], "error_rate_in_fault_median": 0.0, "fault_duration_s_median": g["fault_duration_s"]},
                    sev=0.0)
            else:
                add("coverage_gap", g, f"{t} outage not visible to the probe (untested path)",
                    {"runs": g["runs"], "error_rate_in_fault_median": 0.0}, sev=5.0)
    slow = [g for g in groups if g["abort_after_s"] and g["first_error_s"] is not None
            and g["abort_after_s"] - g["first_error_s"] >= 1.5]
    if slow:
        g = max(slow, key=lambda x: x["runs"])
        add("detection_delay", g, "Auto-abort fires ~2 s after the first error (tooling)",
            {"runs": g["runs"], "first_error_s_median": g["first_error_s"], "abort_after_s_median": g["abort_after_s"]},
            sev=10.0)
    F.sort(key=lambda x: -x["severity_score"])
    for i, f in enumerate(F, 1):
        f["rank"] = i
    return F


# --------------------------------------------------------------------------- AI narrative (guarded)
SYSTEM = """You are an SRE writing a chaos-engineering findings report for a sandbox app.
For EACH finding you receive, write:
  explanation: 1-3 sentences on what the evidence shows and the likely cause (say "likely" for causes).
  recommended_fix: 1-3 concrete, actionable sentences.
Use ONLY numbers that appear in that finding's evidence. Do not invent metrics or new findings.
Return ONLY JSON: {"findings":[{"id":"F1","explanation":"...","recommended_fix":"..."}]}"""

NUM = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")


def numbers_ok(text: str, evidence: dict) -> bool:
    allowed = set()
    for v in evidence.values():
        if isinstance(v, (int, float)):
            for x in (v, round(v), round(v, 1), round(v * 100), round(v * 100, 1)):
                allowed.add(float(x))
    for m in NUM.findall(text):
        x = float(m)
        if x <= 5 or x in allowed:   # small counts ("2 replicas") are fine
            continue
        return False
    return True


def narrate(findings: list[dict]) -> dict:
    payload = {"findings": [{k: f[k] for k in ("id", "title", "target", "fault", "severity", "evidence")} for f in findings]}
    meta = {"provider": PL.PROVIDER, "model": None, "error": None}
    try:
        text, m = PL.call_llm(payload, SYSTEM)
        meta["model"] = m["model"]
        obj = json.loads(text[text.find("{"): text.rfind("}") + 1])
        items = {i.get("id"): i for i in obj.get("findings", []) if isinstance(i, dict)}
    except Exception as e:  # noqa: BLE001
        meta["error"] = str(e)[:300]
        items = {}
    for f in findings:
        it = items.get(f["id"]) or {}
        exp, fix = str(it.get("explanation", ""))[:600], str(it.get("recommended_fix", ""))[:600]
        if exp and fix and numbers_ok(exp + " " + fix, f["evidence"]):
            f.update(explanation=exp, recommended_fix=fix, text_source=f"AI ({meta['model']})")
        else:
            f.update(explanation=None, recommended_fix=FIX_TEMPLATES[f["kind"]],
                     text_source="template" + (" (AI text rejected: unverified number)" if exp and fix else ""))
    return meta


# --------------------------------------------------------------------------- report
def resilience(groups: list[dict]) -> dict:
    per = [{"target": g["target"], "fault": g["fault"], "score": round(100 - severity(g), 1)} for g in groups]
    tested = {g["target"] for g in groups if g["target"] in C.ALLOWED_TARGETS}
    return {"overall": round(statistics.mean(p["score"] for p in per), 1) if per else None, "per_experiment": per,
            "coverage": f"{len(tested)}/{len(C.ALLOWED_TARGETS)} allowlisted services tested"}


def render(rep: dict) -> str:
    L = ["# Findings Report — Online Boutique (kind sandbox)", "",
         f"Generated {rep['generated']} by `chaos/analyze.py` from {rep['run_count']} controller runs "
         f"({len(rep['groups'])} distinct experiments). Every number below is computed from "
         "`results/runs/*/summary.json`.", "",
         f"**Narrative text:** {rep['narrative']['provider']} / {rep['narrative']['model'] or 'not used'}"
         + (f" — LLM unavailable ({rep['narrative']['error']}), template text used" if rep['narrative']['error'] else ""), "",
         "## Resilience score (heuristic)", "",
         f"**{rep['resilience']['overall']} / 100**, with {rep['resilience']['coverage']}.", "",
         "Per-experiment score = 100 − severity. Severity (0–100) = 50 × error rate during the fault "
         "+ 30 × min(1, user-facing outage s / 60) + 10 × min(1, Kubernetes-ready s / 120) "
         "+ 10 × min(1, extra P95 latency ms / 1000) (traffic spike only). Outage = fault duration + user recovery. "
         "The weights are a team choice, not an industry standard.", "",
         "| Experiment | Runs | Error rate in fault | K8s ready (s) | Users recovered (s) | Score |",
         "|---|---|---|---|---|---|"]
    for g in rep["groups"]:
        sc = next(p["score"] for p in rep["resilience"]["per_experiment"] if p["target"] == g["target"] and p["fault"] == g["fault"])
        er = "—" if g["error_rate_in_fault"] is None else f"{100 * g['error_rate_in_fault']:.0f}%"
        L.append(f"| {g['fault']} → {g['target']} | {g['runs']} | {er} | {g['k8s_ready_s'] if g['k8s_ready_s'] is not None else '—'} | "
                 f"{g['user_recovery_s'] if g['user_recovery_s'] is not None else 'no user impact'} | {sc} |")
    L += ["", "## Ranked findings", ""]
    for f in rep["findings"]:
        L += [f"### {f['rank']}. [{f['severity']} · {f['severity_score']}] {f['title']}", "",
              "| Evidence | Value |", "|---|---|"]
        L += [f"| {k} | {v} |" for k, v in f["evidence"].items()]
        L += ["", f"Runs: {', '.join('`' + r[:16] + '`' for r in f['run_ids'])} · confidence: {f['confidence']}", ""]
        if f.get("explanation"):
            L += [f"**Explanation:** {f['explanation']}", ""]
        L += [f"**Recommended fix:** {f['recommended_fix']}", "", f"<sub>Text source: {f['text_source']}</sub>", ""]
    L += ["## Limitations", "",
          "- Local kind cluster with 1 replica per service, not production. Results apply to this configuration only.",
          "- The probe calls `/`, a product page and `/cart`. Checkout, payment and email paths are not exercised, so "
          "0 errors there means *not observed*, not *resilient*.",
          "- Error rate, user-facing outage and Kubernetes readiness are different failure dimensions and are reported separately.",
          "- Causes in explanations are hypotheses unless stated as measured.",
          "- Cancelled and precheck-failed runs are excluded (nothing was injected).", ""]
    return "\n".join(L)


def analyze() -> dict:
    runs = load_runs()
    groups = group_metrics(runs)
    findings = derive_findings(groups)
    meta = narrate(findings) if findings else {"provider": PL.PROVIDER, "model": None, "error": "no findings"}
    rep = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "run_count": len(runs),
           "groups": groups, "findings": findings, "resilience": resilience(groups), "narrative": meta}
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    OUT_MD.write_text(render(rep), encoding="utf-8")
    rep["saved_to"] = str(OUT_MD.relative_to(C.REPO))
    return rep


if __name__ == "__main__":
    r = analyze()
    print(f"Resilience score {r['resilience']['overall']}/100 ({r['resilience']['coverage']}); "
          f"narrative: {r['narrative']['model'] or 'templates'}")
    for f in r["findings"]:
        print(f"{f['rank']}. [{f['severity']} {f['severity_score']}] {f['title']}  <{f['text_source']}>")
    print(f"Saved: {r['saved_to']}")
