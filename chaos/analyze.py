#!/usr/bin/env python3
"""
Analysis agent (Agent 2): run evidence -> ranked weaknesses -> fixes -> resilience score.

  python chaos/analyze.py   -> results/findings-report.md + results/findings.json

Division of labour (deliberate):
  * FACTS - every number, finding, severity, rank and score is computed here by plain code
    from results/runs/*/ (summary.json, probes.csv, Locust CSVs, k8s_events.json).
  * NARRATIVE - a LOCAL Ollama model writes only the likely-cause hypothesis, recommended fix and
    a verification experiment for each finding (schema-constrained JSON): CHAOS_ANALYSIS_MODEL
    (default llama3.2:3b) if pulled, else the planner model (qwen2.5:3b), else built-in templates.
    No cloud API is called. The report states which one wrote each text. Any AI text quoting a
    number not in that finding's evidence is rejected (hallucination guard).
"""
from __future__ import annotations

import csv
import json
import os
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import controller as C  # noqa: E402
import llm as L         # noqa: E402

OUT_MD = C.DATA / "results" / "findings-report.md"
OUT_JSON = C.DATA / "results" / "findings.json"
ANALYSIS_MODEL = os.getenv("CHAOS_ANALYSIS_MODEL", "llama3.2:3b")
PLANNER_MODEL = os.getenv("CHAOS_PLANNER_MODEL", "qwen2.5:3b")

# Services the HTTP probe (/, /product/<id>, /cart) exercises, per upstream architecture docs.
# checkout/payment/email are only used by POST /cart/checkout.
PROBE_COVERS = {"productcatalogservice", "currencyservice", "cartservice", "redis-cart",
                "recommendationservice", "adservice", "shippingservice", "frontend"}
CONF_WEIGHT = {"high": 1.0, "medium": 0.85, "low (single run)": 0.7}

TEMPLATES = {
    "recovery_gap": ("The frontend's gRPC client likely waits in reconnect backoff after the dependency returns, "
                     "so requests keep failing after the new pod is Ready.",
                     "Run 2+ replicas so a restart never removes every endpoint; set gRPC keepalive and a lower "
                     "maximum reconnect backoff in the frontend; retry idempotent reads with a short timeout.",
                     "Repeat the same outage with 2 replicas (or tuned backoff) and compare user recovery time."),
    "hard_dependency": ("With a single replica and no fallback, every request that needs this service fails.",
                        "Run 2+ replicas with a PodDisruptionBudget (minAvailable: 1); let the frontend degrade "
                        "(cached or partial page) instead of returning HTTP 500.",
                        "Repeat the outage with 2 replicas; expect a near-zero error rate."),
    "slow_restart": ("Container start-up and readiness checks take a long time for this service.",
                     "Shorten start-up (image/runtime tuning, readiness probe timing) or keep 2+ replicas so a restart "
                     "is invisible to users.",
                     "Measure ready time again after tuning the readiness probe."),
    "latency_sensitivity": ("Added delay propagates to user-facing pages because calls are synchronous and have no "
                            "timeout budget or cache.",
                            "Set client timeouts and retries with deadlines, cache read-mostly data, and add an HPA "
                            "and resource requests for load spikes.",
                            "Repeat with the fix and compare P95 against the baseline."),
    "graceful": ("The frontend treats this dependency as optional.",
                 "Keep this behaviour: add the experiment to a regression suite.",
                 "Re-run after each release to confirm."),
    "coverage_gap": ("The probe never calls this service, so an outage cannot be observed.",
                     "Add a checkout probe (POST /cart/checkout) before drawing conclusions.",
                     "Re-run the same outage with a checkout probe enabled."),
    "detection_delay": ("The prober sends one request at a time; a request hanging until its 2 s timeout delays the "
                        "next measurement.",
                        "Run probes concurrently or use a shorter probe timeout so the auto-abort fires within ~1 s.",
                        "Re-run the outage and compare abort time with first-error time."),
}


def med(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(statistics.median(xs), 2) if xs else None


# --------------------------------------------------------------------------- evidence extraction
def run_evidence(run_dir: Path, s: dict) -> dict:
    """Per-run facts not in summary.json: affected routes, throughput, k8s events."""
    ev = {"affected_routes": [], "throughput_rps": None, "k8s_events": []}
    t0 = s.get("fault_injected_at")
    t1 = s.get("restore_issued_at")
    p = run_dir / "probes.csv"
    if t0 and p.exists():
        a = datetime.fromisoformat(t0.replace("Z", "+00:00")).timestamp()
        b = datetime.fromisoformat(t1.replace("Z", "+00:00")).timestamp() if t1 else a + 600
        rec = (s.get("recovery") or {}).get("recovered_at")
        b = max(b, datetime.fromisoformat(rec.replace("Z", "+00:00")).timestamp()) if rec else b
        bad = set()
        with p.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                try:
                    ts = float(r["unix_ts"])
                except (KeyError, ValueError):
                    continue
                if a <= ts <= b and r.get("ok") == "0" and r.get("kind") != "conn_error":
                    bad.add("/product/<id>" if r["path"].startswith("/product/") else r["path"])
        ev["affected_routes"] = sorted(bad)
    for name in ("spike_stats.csv", "locust_stats.csv"):
        q = run_dir / name
        if q.exists():
            with q.open(encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    if r.get("Name") == "Aggregated":
                        try:
                            ev["throughput_rps"] = round(float(r["Requests/s"]), 2)
                            ev["throughput_source"] = name
                        except (KeyError, ValueError):
                            pass
            break
    k = run_dir / "k8s_events.json"
    if k.exists():
        ev["k8s_events"] = json.loads(k.read_text(encoding="utf-8"))[:20]
    return ev


def load_runs() -> list[dict]:
    runs = []
    if not C.RUNS_DIR.exists():
        return runs
    for d in sorted(x for x in C.RUNS_DIR.iterdir() if x.is_dir()):
        p = d / "summary.json"
        if not p.exists():
            continue
        s = json.loads(p.read_text(encoding="utf-8"))
        if not s.get("fault_injected_at"):
            continue  # cancelled / precheck-failed: nothing was injected
        s["_evidence"] = run_evidence(d, s)
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
        any_err = any((w.get("errors") or 0) > 0 for w in fw)
        user = med([x.get("user_recovery_s") for x in rec]) if any_err else None
        k8s = med([x.get("k8s_ready_s") for x in rec])
        routes = sorted({rt for r in rs for rt in r["_evidence"]["affected_routes"]})
        aborted = [r.get("fault_duration_s") for r in rs if str(r["status"]).startswith("ABORTED_THRESHOLD")]
        out.append({
            "target": target, "fault": fault, "runs": len(rs), "run_ids": [r["run_id"] for r in rs],
            "statuses": sorted({r["status"] for r in rs}),
            "error_rate_in_fault": med([w.get("error_rate") for w in fw]), "any_user_errors": any_err,
            "p95_in_fault_ms": med([w.get("p95_ms") for w in fw]),
            "baseline_p95_ms": med([(r.get("baseline_window") or {}).get("p95_ms") for r in rs]),
            "post_p95_ms": med([(r.get("post_window") or {}).get("p95_ms") for r in rs]),
            "fault_duration_s": med([r.get("fault_duration_s") for r in rs]),
            "first_error_s": med([r.get("first_error_after_inject_s") for r in rs]),
            "abort_after_s": med(aborted) if aborted else None,
            "k8s_ready_s": k8s, "user_recovery_s": user,
            "ready_vs_user_gap_s": round(user - k8s, 2) if (user is not None and k8s is not None
                                                             and fault == "scale_to_zero") else None,
            "affected_routes": routes, "routes_probed": 3,
            "throughput_rps": med([r["_evidence"]["throughput_rps"] for r in rs]),
            "probe_covers_target": target in PROBE_COVERS,
            "confidence": "high" if len(rs) >= 3 else "medium" if len(rs) == 2 else "low (single run)",
        })
    return out


# --------------------------------------------------------------------------- scoring (documented)
SEVERITY_FORMULA = ("severity (0-100) = 40 x error rate during fault + 20 x (affected routes / 3 probed routes) "
                    "+ 25 x min(1, user-facing outage s / 60) + 5 x min(1, Kubernetes-ready s / 120) "
                    "+ 10 x min(1, extra P95 ms over baseline / 1000); outage = fault duration + user recovery. "
                    "priority = severity x confidence weight (high 1.0, medium 0.85, single run 0.7). "
                    "Resilience score per experiment = 100 - severity; overall = mean over tested experiments.")


def severity(g: dict) -> float:
    err = g["error_rate_in_fault"] or 0
    routes = len(g["affected_routes"]) / g["routes_probed"]
    outage = (g["fault_duration_s"] or 0) + (g["user_recovery_s"] or 0) if g["any_user_errors"] else 0
    k8s = g["k8s_ready_s"] or 0
    lat = max(0.0, (g["p95_in_fault_ms"] or 0) - (g["baseline_p95_ms"] or 0))
    return round(40 * err + 20 * min(1, routes) + 25 * min(1, outage / 60) + 5 * min(1, k8s / 120)
                 + 10 * min(1, lat / 1000), 1)


def level(score: float) -> str:
    return "High" if score >= 60 else "Medium" if score >= 30 else "Low" if score > 5 else "Info"


def derive_findings(groups: list[dict]) -> list[dict]:
    F = []

    def add(kind, g, title, evidence, sev=None):
        sev = severity(g) if sev is None else sev
        F.append({"id": f"F{len(F) + 1}", "kind": kind, "target": g["target"], "fault": g["fault"], "title": title,
                  "severity_score": sev, "severity": level(sev),
                  "priority": round(sev * CONF_WEIGHT[g["confidence"]], 1), "confidence": g["confidence"],
                  "affected_services": sorted({g["target"], "frontend"}), "affected_routes": g["affected_routes"],
                  "evidence": evidence, "run_ids": g["run_ids"]})

    for g in groups:
        t, f = g["target"], g["fault"]
        if g["ready_vs_user_gap_s"] is not None and g["ready_vs_user_gap_s"] >= 5:
            add("recovery_gap", g, f"Users keep failing after {t} is Ready",
                {"runs": g["runs"], "k8s_ready_s_median": g["k8s_ready_s"],
                 "user_recovery_s_median": g["user_recovery_s"], "gap_s_median": g["ready_vs_user_gap_s"]})
        if (g["error_rate_in_fault"] or 0) >= 0.5 and f in ("scale_to_zero", "pod_delete"):
            add("hard_dependency", g, f"{t} is a hard dependency / single point of failure",
                {"runs": g["runs"], "error_rate_in_fault_median": g["error_rate_in_fault"],
                 "first_error_s_median": g["first_error_s"], "affected_routes": len(g["affected_routes"]),
                 "replicas": 1})
        if (g["k8s_ready_s"] or 0) >= 30 and f in ("scale_to_zero", "pod_delete"):
            add("slow_restart", g, f"{t} takes {g['k8s_ready_s']:.0f} s to become Ready after restore",
                {"runs": g["runs"], "k8s_ready_s_median": g["k8s_ready_s"]})
        if f in ("traffic_spike", "network_latency"):
            base, during = g["baseline_p95_ms"] or 0, g["p95_in_fault_ms"] or 0
            if during >= 2 * max(base, 1) or g["any_user_errors"] or "ABORTED_THRESHOLD" in g["statuses"]:
                what = "a traffic spike" if f == "traffic_spike" else f"network delay on {t}"
                add("latency_sensitivity", g, f"User-facing latency/errors degrade under {what}",
                    {"runs": g["runs"], "baseline_p95_ms": base, "p95_in_fault_ms": during,
                     "error_rate_in_fault_median": g["error_rate_in_fault"], "throughput_rps": g["throughput_rps"]})
            else:
                add("graceful", g, f"No significant degradation under {f.replace('_', ' ')} ({t})",
                    {"runs": g["runs"], "baseline_p95_ms": base, "p95_in_fault_ms": during}, sev=0.0)
        if not g["any_user_errors"] and f in ("scale_to_zero", "pod_delete"):
            if g["probe_covers_target"]:
                add("graceful", g, f"Frontend degrades gracefully without {t}",
                    {"runs": g["runs"], "error_rate_in_fault_median": 0.0,
                     "fault_duration_s_median": g["fault_duration_s"]}, sev=0.0)
            else:
                add("coverage_gap", g, f"{t} outage not visible to the probe (untested path)",
                    {"runs": g["runs"], "error_rate_in_fault_median": 0.0}, sev=5.0)
    slow = [g for g in groups if g["abort_after_s"] and g["first_error_s"] is not None
            and g["abort_after_s"] - g["first_error_s"] >= 1.5]
    if slow:
        g = max(slow, key=lambda x: x["runs"])
        add("detection_delay", g, "Auto-abort fires ~2 s after the first error (our tooling)",
            {"runs": g["runs"], "first_error_s_median": g["first_error_s"], "abort_after_s_median": g["abort_after_s"]},
            sev=10.0)
    F.sort(key=lambda x: (-x["priority"], -x["severity_score"]))
    for i, f in enumerate(F, 1):
        f["rank"] = i
    return F


# --------------------------------------------------------------------------- AI narrative (guarded)
NARRATIVE_SCHEMA = {"type": "object", "properties": {"findings": {"type": "array", "items": {
    "type": "object", "properties": {
        "id": {"type": "string"},
        "likely_cause": {"type": "string", "description": "hypothesis; say 'likely'"},
        "recommended_fix": {"type": "string"},
        "verification_experiment": {"type": "string"}},
    "required": ["id", "likely_cause", "recommended_fix", "verification_experiment"]}}},
    "required": ["findings"]}

SYSTEM = """You are an SRE writing a chaos-engineering findings report for a sandbox application.
For EACH finding you receive, write:
  likely_cause: 1-2 sentences; a hypothesis, clearly worded as "likely" unless the evidence proves it.
  recommended_fix: 1-3 concrete, actionable sentences.
  verification_experiment: 1 sentence describing an experiment that would confirm the fix.
Use ONLY numbers that appear in that finding's evidence. Do not invent metrics or findings.
Return ONLY JSON: {"findings":[{"id":"F1","likely_cause":"...","recommended_fix":"...","verification_experiment":"..."}]}"""

NUM = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")


def numbers_ok(text: str, evidence: dict) -> bool:
    allowed = set()
    for v in evidence.values():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            for x in (v, round(v), round(v, 1), round(v * 100), round(v * 100, 1)):
                allowed.add(float(x))
    for m in NUM.findall(text):
        x = float(m)
        if x <= 5 or x in allowed:   # small counts ("2 replicas") are fine
            continue
        return False
    return True


def ai_narrative(payload: dict) -> tuple[dict, dict]:
    """Local models only: second Ollama model, then the planner model, then templates."""
    errors = []
    try:
        installed = L.ollama_models()
    except L.LLMError as e:
        return {}, {"used": "templates (no local model reachable)", "model": None, "errors": [str(e)]}
    for m, why in ((ANALYSIS_MODEL, "local Ollama (analysis model)"), (PLANNER_MODEL, "local Ollama (planner model)")):
        if m not in installed and f"{m}:latest" not in installed:
            errors.append(f"{m}: not pulled")
            continue
        try:
            obj, meta = L.ollama_json(m, SYSTEM, payload, schema=NARRATIVE_SCHEMA)
            items = {i.get("id"): i for i in obj.get("findings", []) if isinstance(i, dict)}
            return items, {"used": why, "model": meta.get("model"), "errors": errors}
        except Exception as e:  # noqa: BLE001
            errors.append(f"{m}: {str(e)[:200]}")
    return {}, {"used": "templates (no local model produced usable output)", "model": None, "errors": errors}


def narrate(findings: list[dict]) -> dict:
    payload = {"findings": [{k: f[k] for k in ("id", "title", "target", "fault", "severity", "affected_routes", "evidence")}
                            for f in findings]}
    items, meta = ai_narrative(payload)
    for f in findings:
        it = items.get(f["id"]) or {}
        texts = [str(it.get(k, ""))[:600] for k in ("likely_cause", "recommended_fix", "verification_experiment")]
        tpl = TEMPLATES[f["kind"]]
        if all(texts) and numbers_ok(" ".join(texts), f["evidence"]):
            f.update(likely_cause=texts[0], recommended_fix=texts[1], verification_experiment=texts[2],
                     text_source=f"AI: {meta['used']} ({meta['model']})")
        else:
            f.update(likely_cause=tpl[0], recommended_fix=tpl[1], verification_experiment=tpl[2],
                     text_source="template" + (" (AI text rejected: unverified number)" if all(texts) else ""))
    return meta


# --------------------------------------------------------------------------- report
def resilience(groups: list[dict]) -> dict:
    per = [{"target": g["target"], "fault": g["fault"], "score": round(100 - severity(g), 1)} for g in groups]
    tested = {g["target"] for g in groups if g["target"] in C.ALLOWED_TARGETS}
    return {"overall": round(statistics.mean(p["score"] for p in per), 1) if per else None, "per_experiment": per,
            "coverage": f"{len(tested)}/{len(C.ALLOWED_TARGETS)} allowlisted services tested",
            "fault_types_tested": sorted({g["fault"] for g in groups}), "formula": SEVERITY_FORMULA}


def render(rep: dict) -> str:
    n = rep["narrative"]
    L_ = ["# Findings Report: Online Boutique (local kind sandbox)", "",
          f"Generated {rep['generated']} by `chaos/analyze.py` from **{rep['run_count']} controller runs** "
          f"({len(rep['groups'])} distinct experiments; fault types: {', '.join(rep['resilience']['fault_types_tested'])}).", "",
          "**How to read this report:** *Observed facts* are computed by code from `results/runs/*`. "
          "*Likely cause* is a hypothesis. *Recommended fix* and *verification experiment* are proposals.", "",
          f"**Narrative written by:** {n['used']}" + (f" (`{n['model']}`)" if n.get("model") else "")
          + (". Not used: " + "; ".join(n["errors"]) if n.get("errors") else ""), "",
          "## Resilience score (team-defined heuristic, not an industry standard)", "",
          f"**{rep['resilience']['overall']} / 100**, with {rep['resilience']['coverage']}.", "",
          f"Method: {SEVERITY_FORMULA}", "",
          "| Experiment | Runs | Error rate in fault | Routes affected | K8s ready (s) | Users recovered (s) | P95 baseline → fault (ms) | Score |",
          "|---|---|---|---|---|---|---|---|"]
    for g in rep["groups"]:
        sc = next(p["score"] for p in rep["resilience"]["per_experiment"] if p["target"] == g["target"] and p["fault"] == g["fault"])
        er = "—" if g["error_rate_in_fault"] is None else f"{100 * g['error_rate_in_fault']:.0f}%"
        L_.append(f"| {g['fault']} → {g['target']} | {g['runs']} | {er} | {', '.join(g['affected_routes']) or 'none'} | "
                  f"{g['k8s_ready_s'] if g['k8s_ready_s'] is not None else '—'} | "
                  f"{g['user_recovery_s'] if g['user_recovery_s'] is not None else 'no user impact'} | "
                  f"{g['baseline_p95_ms']} → {g['p95_in_fault_ms']} | {sc} |")
    L_ += ["", "## Ranked findings", ""]
    for f in rep["findings"]:
        L_ += [f"### {f['rank']}. [{f['severity']} · severity {f['severity_score']} · priority {f['priority']}] {f['title']}", "",
               f"**Affected:** services {', '.join(f['affected_services'])}; routes {', '.join(f['affected_routes']) or 'none observed'}. "
               f"**Confidence:** {f['confidence']}.", "",
               "**Observed facts**", "", "| Evidence | Value |", "|---|---|"]
        L_ += [f"| {k} | {v} |" for k, v in f["evidence"].items()]
        L_ += ["", f"Runs: {', '.join('`' + r + '`' for r in f['run_ids'])}", "",
               f"**Likely cause (hypothesis):** {f['likely_cause']}", "",
               f"**Recommended fix:** {f['recommended_fix']}", "",
               f"**Verification experiment:** {f['verification_experiment']}", "",
               f"<sub>Narrative source: {f['text_source']}</sub>", ""]
    L_ += ["## Limitations", "",
           "- Local kind cluster, 1 replica per service, laptop resources. The results apply to this configuration only.",
           "- The probe calls `/`, a product page and `/cart` every 0.25 s. Checkout, payment and email are not exercised, "
           "so 0 errors there means *not observed*, not *resilient*.",
           "- Error rate, user-facing outage, Kubernetes readiness and latency are separate dimensions and are reported separately.",
           "- Findings from one run are low confidence. Causes are hypotheses unless proven by a verification experiment.",
           "- Cancelled and precheck-failed runs are excluded because nothing was injected. Historical Locust-only runs "
           "(baseline*, chaos1-5) predate the controller and are not included in the score.", ""]
    return "\n".join(L_)


def analyze() -> dict:
    runs = load_runs()
    groups = group_metrics(runs)
    findings = derive_findings(groups)
    meta = narrate(findings) if findings else {"used": "none (no findings)", "model": None, "errors": []}
    rep = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "run_count": len(runs),
           "groups": groups, "findings": findings, "resilience": resilience(groups), "narrative": meta}
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    OUT_MD.write_text(render(rep), encoding="utf-8")
    rep["saved_to"] = str(OUT_MD)
    return rep


if __name__ == "__main__":
    r = analyze()
    print(f"Resilience score {r['resilience']['overall']}/100 ({r['resilience']['coverage']}); "
          f"narrative: {r['narrative']['used']}")
    for f in r["findings"]:
        print(f"{f['rank']}. [{f['severity']} sev {f['severity_score']} prio {f['priority']}] {f['title']}  <{f['text_source']}>")
    print(f"Saved: {r['saved_to']}")
