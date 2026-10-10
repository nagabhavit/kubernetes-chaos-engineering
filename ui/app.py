import json
import os
import random
import uuid
from datetime import datetime, timezone

import pandas as pd
import requests
import streamlit as st

# =========================================================
# RESILIENCEAI — CONTROL CENTER
#   Demo mode : local simulation, touches nothing.
#   API mode  : real experiments via chaos/api.py -> chaos/controller.py
#               (allowlist, 60 s cap, auto-abort, kill switch, guaranteed restore)
# =========================================================

st.set_page_config(
    page_title="ResilienceAI | Control Center",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

API_URL = os.getenv("RESILIENCE_API_URL", "http://127.0.0.1:8000")
REFRESH_S = 2

SERVICES = [
    "frontend",
    "cartservice",
    "checkoutservice",
    "paymentservice",
    "currencyservice",
    "productcatalogservice",
    "recommendationservice",
    "shippingservice",
    "emailservice",
    "adservice",
    "redis-cart",
]

FAULT_TYPES = {
    "Service unavailability (scale to 0)": "service_unavailability",
    "Pod failure (delete pod, K8s self-heals)": "pod_failure",
    "Traffic spike (sudden load on frontend)": "traffic_spike",
    "Network latency (lab only: run python chaos\\netem_lab.py)": "latency",
}

ERROR_TYPES = [
    {"type": "Timeout", "message": "Request exceeded the response time limit"},
    {"type": "HTTP 500", "message": "Internal server error reported"},
    {"type": "Connection error", "message": "Connection attempt failed"},
    {"type": "High latency", "message": "Response latency exceeded threshold"},
    {"type": "Dependency error", "message": "Downstream dependency unavailable"},
]

# =========================================================
# STYLING
# =========================================================

st.markdown(
    """
    <style>
    .block-container { padding-top: 1.5rem; padding-bottom: 2rem; }
    .hero { padding: 1.5rem; border: 1px solid rgba(128,128,128,.3); border-radius: 16px; margin-bottom: 1rem; }
    .subtle { opacity: .7; font-size: .9rem; }
    div[data-testid="stMetric"] { border: 1px solid rgba(128,128,128,.25); padding: 14px; border-radius: 12px; }
    </style>
    """,
    unsafe_allow_html=True,
)

# =========================================================
# SESSION STATE
# =========================================================

DEFAULTS = {
    "ingestion_running": False,
    "experiment_status": "IDLE",
    "experiment_id": None,
    "experiment_history": [],
    "events": [],
    "active_errors": [],
    "error_history": [],
    "metric_history": [],
    "baseline_saved": False,
    "rollback_verified": False,
    "rollback_result": None,
    "random_injection_active": False,
    "random_injection_count": 0,
    "baseline_latency": 82.0,
    "baseline_error_rate": 0.2,
    "current_latency": 82.0,
    "current_error_rate": 0.2,
}

for key, value in DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = value


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_event(message, category="INFO"):
    st.session_state.events.insert(0, {"timestamp": now_iso(), "category": category, "event": message})


def record_metrics():
    """Record illustrative local demo metrics."""
    st.session_state.metric_history.append(
        {
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "latency_ms": round(st.session_state.current_latency, 2),
            "error_rate_pct": round(st.session_state.current_error_rate, 2),
        }
    )
    st.session_state.metric_history = st.session_state.metric_history[-50:]


def api_get(path, timeout=5):
    response = requests.get(f"{API_URL}{path}", timeout=timeout)
    response.raise_for_status()
    return response.json()


def api_post(path, payload=None, timeout=30):
    """Returns (http_status, json). Does not raise on 4xx so rejections can be shown."""
    response = requests.post(f"{API_URL}{path}", json=payload or {}, timeout=timeout)
    try:
        body = response.json() if response.content else {}
    except ValueError:
        body = {"raw": response.text}
    return response.status_code, body


def backend_up():
    """Liveness via /ping (instant); cluster details via /health (kubectl can be slow on Windows)."""
    try:
        api_get("/ping", timeout=5)
    except requests.RequestException:
        return None
    try:
        return api_get("/health", timeout=20)
    except requests.RequestException:
        return {"ok": True, "cluster": {"error": "health check timed out"}, "frontend_ok": None}


def live_fragment(fn):
    """Auto-refresh a display-only section in API mode (Streamlit >= 1.37)."""
    frag = getattr(st, "fragment", None)
    if frag is None:
        return fn
    return frag(run_every=REFRESH_S)(fn)


def show_post_result(label, code, body):
    if code in (200, 202):
        st.success(f"{label}: {body.get('status', 'accepted')}")
        if body.get("note"):
            st.info(body["note"])
    elif code == 400:
        st.error(f"{label} REJECTED by safety validation — nothing was injected.")
        for r in body.get("reasons", []) or [body.get("error")]:
            st.write(f"• {r}")
    elif code == 409:
        st.warning(f"{label}: {body.get('error')} ({body.get('run_id')})")
    else:
        st.error(f"{label} failed: HTTP {code} {body}")


# =========================================================
# SIDEBAR
# =========================================================

health = backend_up()

st.sidebar.title("🛡️ ResilienceAI")
st.sidebar.caption("Cloud Resilience Control Center")
st.sidebar.divider()

mode = st.sidebar.radio("Operating mode", ["Demo mode", "API mode"], index=1 if health else 0)

st.sidebar.markdown("### Environment")
st.sidebar.write("Google Online Boutique")
st.sidebar.code("kind-boutique-chaos · boutique")

if mode == "Demo mode":
    st.sidebar.info("Demo mode uses simulated events and metrics. It does not inject faults into Kubernetes.")
else:
    st.sidebar.write(f"Backend: `{API_URL}`")
    if health:
        st.sidebar.success("Backend connected")
        cl = health.get("cluster") or {}
        if cl.get("error"):
            st.sidebar.error("Cluster unreachable")
        st.sidebar.write("Frontend:", "✅ 200" if health.get("frontend_ok") else "❌ unreachable")
    else:
        st.sidebar.error("Backend offline — run: python chaos\\api.py")

st.sidebar.divider()
st.sidebar.markdown("### Safety (enforced by controller)")
st.sidebar.write("✓ Target allowlist (frontend refused)")
st.sidebar.write("✓ Hard 60 s fault cap")
st.sidebar.write("✓ Auto-abort: error rate > 20 % or P95 > 1 s")
st.sidebar.write("✓ Kill switch + guaranteed restore")
st.sidebar.write("✓ Recovery verification")
st.sidebar.write("✓ AI proposals validated; AI never executes")
st.sidebar.markdown("Live chart view: [127.0.0.1:8090](http://127.0.0.1:8090)")

# =========================================================
# HEADER
# =========================================================

st.markdown(
    """
    <div class="hero">
      <h1 style="margin:0">ResilienceAI</h1>
      <p style="margin:.5rem 0">Ingest telemetry. Observe faults. Measure impact. Recover.</p>
      <p class="subtle" style="margin:0">Google Online Boutique · Resilience Experiment Dashboard</p>
    </div>
    """,
    unsafe_allow_html=True,
)

if mode == "Demo mode":
    st.info("You are viewing a local simulation. Switch to API mode to run real, safety-controlled experiments.")
elif not health:
    st.error("API mode needs the backend. Start it with `python chaos\\api.py` and refresh.")
    st.stop()

# =========================================================
# 1. INGESTION
# =========================================================

st.subheader("1. Telemetry ingestion")
start_col, stop_col, status_col = st.columns([1, 1, 2])
with start_col:
    start_ingestion = st.button("▶ Start ingestion", type="primary", use_container_width=True)
with stop_col:
    stop_ingestion = st.button("■ Stop ingestion", use_container_width=True)

if start_ingestion:
    if mode == "API mode":
        code, body = api_post("/ingestion/start")
        show_post_result("Start ingestion", code, body)
    else:
        st.session_state.ingestion_running = True
        st.session_state.baseline_saved = True
        st.session_state.rollback_verified = False
        st.session_state.current_latency = st.session_state.baseline_latency
        st.session_state.current_error_rate = st.session_state.baseline_error_rate
        record_metrics()
        log_event("Demo ingestion started; initial demo baseline captured.")
        st.success("Demo ingestion started. No live telemetry is collected.")

if stop_ingestion:
    if mode == "API mode":
        code, body = api_post("/ingestion/stop")
        show_post_result("Stop ingestion", code, body)
    else:
        st.session_state.ingestion_running = False
        log_event("Demo ingestion stopped. Existing records retained.")
        st.success("Ingestion stopped; recorded history is preserved.")

with status_col:
    if mode == "API mode":
        ing = api_get("/ingestion/status")
        st.metric("Ingestion status", "RUNNING" if ing["running"] else "STOPPED",
                  help="Live HTTP probe of the frontend every 0.5 s, saved to results/telemetry/")
    else:
        st.metric("Ingestion status", "RUNNING" if st.session_state.ingestion_running else "STOPPED")

st.caption("Stopping ingestion stops collection. It does not stop a running fault or roll back an experiment.")

# =========================================================
# AI EXPERIMENT PLANNER
# =========================================================

st.divider()
st.subheader("🤖 AI experiment planner")
st.caption("The planner reads the dependency map, baseline and previous runs, and ranks experiments with a reason. "
           "Every proposal is checked by deterministic code (allowlist, fault type, 5–30 s). "
           "The AI never executes anything: a proposal runs only when you press Run.")

if mode != "API mode":
    st.info("The planner needs API mode (real backend).")
else:
    if st.button("🧠 Generate ranked plan", type="primary"):
        with st.spinner("Planning with the local model… (can take 30–120 s on a laptop CPU)"):
            code, body = api_post("/planner/plan", timeout=300)
        if code != 200:
            st.error(f"Planner failed: HTTP {code} {body}")
    try:
        plan = api_get("/planner/latest")
    except requests.RequestException:
        plan = {}
    if plan.get("experiments"):
        is_ai = str(plan.get("planner", "")).startswith("AI")
        label = f"Planner: **{plan['planner']}**" + (f" · model `{plan.get('model')}`" if is_ai else "")
        if is_ai:
            st.success(label)
        else:
            st.warning(label)
        if plan.get("llm_error"):
            st.caption(f"LLM not used: {plan['llm_error']}")
        df = pd.DataFrame([{"rank": e["rank"], "target": e["target"], "fault": e["fault_type"],
                            "duration_s": e["duration_seconds"], "reason": e["reason"],
                            "hypothesis": e["hypothesis"], "expected_impact": e.get("expected_impact", ""),
                            "grounding": e.get("grounding", "")}
                           for e in plan["experiments"]])
        st.dataframe(df, use_container_width=True, hide_index=True)
        if plan.get("rejected"):
            with st.expander(f"⚠️ {len(plan['rejected'])} proposal(s) rejected by the safety validator"):
                st.dataframe(pd.DataFrame([{"proposal": json.dumps(r["proposal"])[:200], "reasons": "; ".join(r["reasons"])}
                                           for r in plan["rejected"]]), use_container_width=True, hide_index=True)
        rc1, rc2 = st.columns([1, 2])
        with rc1:
            pick = st.selectbox("Proposal to run", [e["rank"] for e in plan["experiments"]],
                                format_func=lambda r: f"#{r} " + next(f"{e['fault_type']} → {e['target']}"
                                                                      for e in plan["experiments"] if e["rank"] == r))
        with rc2:
            st.write("")
            confirm_plan = st.checkbox("I approve running this proposal on the sandbox cluster.")
        if st.button("▶ Run selected proposal", disabled=not confirm_plan):
            code, body = api_post("/planner/run", {"rank": pick})
            show_post_result(f"Planner proposal #{pick}", code, body)
        st.caption(f"Saved plan: `{plan.get('saved_to')}` (includes inputs, raw model output and validation results)")
    else:
        st.caption("No plan yet — press Generate.")

# =========================================================
# 2. EXPERIMENT CONTROL
# =========================================================

st.divider()
st.subheader("2. Experiment control")
left, right = st.columns([1.1, 0.9], gap="large")

with left:
    with st.form("experiment_form"):
        target = st.selectbox("Target service", SERVICES, index=SERVICES.index("productcatalogservice"))
        fault_label = st.selectbox("Fault type", list(FAULT_TYPES.keys()))
        duration = st.slider("Requested fault duration (seconds)", min_value=5, max_value=30, value=10, step=5)
        st.caption("Traffic spike always targets the frontend (it adds load; no service is modified).")
        objective = st.text_input("Experiment objective", "Observe service degradation and recovery")
        acknowledged = st.checkbox("I understand this experiment may affect availability.")
        submit_experiment = st.form_submit_button("Start experiment", type="primary", use_container_width=True)

    if submit_experiment:
        if not acknowledged:
            st.warning("Confirm the acknowledgement first.")
        elif mode == "API mode":
            code, body = api_post("/experiments", {
                "target_service": target,
                "fault_type": FAULT_TYPES[fault_label],
                "duration_seconds": duration,
                "objective": objective,
            })
            show_post_result("Start experiment", code, body)
        else:
            experiment_id = f"DEMO-{uuid.uuid4().hex[:8].upper()}"
            st.session_state.experiment_history.insert(0, {
                "id": experiment_id, "timestamp": now_iso(), "target": target, "fault": fault_label,
                "duration_seconds": duration, "objective": objective, "status": "SIMULATED"})
            st.session_state.experiment_id = experiment_id
            st.session_state.experiment_status = "SIMULATED"
            st.session_state.rollback_verified = False
            log_event(f"Simulated experiment requested for {target}: {fault_label}.", "EXPERIMENT")
            st.info("Experiment recorded in demo mode. No real fault was injected.")

with right:
    st.markdown("#### Experiment status")

    @live_fragment
    def experiment_status_panel():
        if mode != "API mode":
            st.metric("Current state", st.session_state.experiment_status)
            st.write("Experiment ID:", st.session_state.experiment_id or "—")
            return
        cur = api_get("/experiments/current")
        st.metric("Current state", cur["status"])
        st.write("Experiment ID:", cur["id"] or "—")
        s = cur.get("summary")
        if s:
            fw, rec = s.get("fault_window") or {}, s.get("recovery") or {}
            c1, c2, c3 = st.columns(3)
            c1.metric("Fault duration", f"{s.get('fault_duration_s') or 0:.1f} s")
            c2.metric("Errors in fault", f"{fw.get('errors', 0)}/{fw.get('n', 0)}")
            urec = rec.get("user_recovery_s")
            c3.metric("User recovery", "no impact" if fw.get("errors") == 0 else (f"{urec:.1f} s" if urec is not None else "—"),
                      help=f"Kubernetes ready after {rec.get('k8s_ready_s')} s" if rec.get("k8s_ready_s") is not None else None)
        evs = cur.get("events") or []
        if evs:
            st.dataframe(pd.DataFrame([{"time": e["ts"][11:23], "event": e["event"]} for e in reversed(evs)][:12]),
                         use_container_width=True, hide_index=True, height=250)

    experiment_status_panel()

    st.markdown("#### Stop experiment")
    if st.button("⛔ Stop experiment (kill switch)", use_container_width=True):
        if mode == "API mode":
            code, body = api_post("/experiment/stop")
            show_post_result("Stop experiment", code, body)
        else:
            st.session_state.experiment_status = "STOPPED"
            st.session_state.random_injection_active = False
            log_event("Demo experiment marked stopped; no live fault existed.", "EXPERIMENT")
            st.success("Demo experiment stopped.")

# =========================================================
# 3. RANDOM INJECTION
# =========================================================

st.divider()
st.subheader("3. Random error injection")
if mode == "API mode":
    st.caption("API mode: runs ONE real 15 s experiment (scale-to-zero or pod delete) on a randomly chosen "
               "allowlisted service, under all controller safety limits.")
else:
    st.caption("Generate simulated errors to test the dashboard. Demo mode does not cause actual application failures.")

inject_col, stop_random_col, counter_col = st.columns(3)
with inject_col:
    inject_random = st.button(" ▶ Inject random errors", type="primary", use_container_width=True)
with stop_random_col:
    stop_random = st.button("⏹ Stop random injection", use_container_width=True)
with counter_col:
    st.metric("Generated demo errors" if mode == "Demo mode" else "Random experiments started",
              st.session_state.random_injection_count)

if inject_random:
    if mode == "API mode":
        code, body = api_post("/experiments/random-errors")
        show_post_result("Random experiment", code, body)
        if code == 202:
            st.session_state.random_injection_count += 1
    elif not st.session_state.ingestion_running:
        st.warning("Start ingestion first. Random demo events can only be generated while ingestion is running.")
    else:
        new_errors = []
        for service in random.sample(SERVICES, k=random.randint(1, 3)):
            et = random.choice(ERROR_TYPES)
            new_errors.append({"timestamp": now_iso(), "service": service, "type": et["type"], "message": et["message"],
                               "suspected_cause": "Random demo injection", "source": "SIMULATION"})
        st.session_state.active_errors.extend(new_errors)
        st.session_state.error_history = (new_errors + st.session_state.error_history)[:200]
        st.session_state.random_injection_count += len(new_errors)
        st.session_state.random_injection_active = True
        st.session_state.rollback_verified = False
        st.session_state.current_latency = round(st.session_state.baseline_latency + random.uniform(20, 300), 2)
        st.session_state.current_error_rate = round(min(100.0, st.session_state.baseline_error_rate + random.uniform(1, 15)), 2)
        record_metrics()
        for error in new_errors:
            log_event(f"{error['type']} simulated for {error['service']}.", "ERROR")
        st.success(f"Generated {len(new_errors)} simulated error(s).")
        st.dataframe(pd.DataFrame(new_errors), use_container_width=True, hide_index=True)

if stop_random:
    if mode == "API mode":
        code, body = api_post("/experiments/random-errors/stop")
        show_post_result("Stop random experiment", code, body)
    else:
        st.session_state.random_injection_active = False
        log_event("Random demo error generation stopped; event history retained.")
        st.success("Random generation stopped. Previous error events remain in the history.")

# =========================================================
# 4. SYSTEM HEALTH, CHARTS, AFFECTED SERVICES (live in API mode)
# =========================================================

st.divider()
st.subheader("4. System health and impact")


@live_fragment
def health_section():
    if mode == "API mode":
        t = api_get("/telemetry?seconds=120")
        w = t.get("window") or {}
        deps = (t.get("cluster") or {}).get("deployments", [])
        degraded = [d for d in deps if d["name"] != "loadgenerator" and d["ready"] < max(d["want"], 1)]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Failed requests (last 5 s)", w.get("errors", 0) if w else "—")
        m2.metric("Degraded deployments", len(degraded))
        m3.metric("P95 latency (last 5 s)", f"{w['p95_ms']:.0f} ms" if w and w.get("p95_ms") is not None else "—")
        m4.metric("Error rate (last 5 s)", f"{100 * w['error_rate']:.1f}%" if w and w.get("error_rate") is not None else "—")
        if not t.get("running"):
            st.warning("Ingestion is stopped — press ▶ Start ingestion for live metrics.")
        else:
            st.caption("Live measurements from HTTP probes of the Online Boutique frontend (localhost:8080).")

        series = pd.DataFrame(t.get("series") or [])
        c1, c2 = st.columns(2, gap="large")
        with c1:
            st.markdown("#### Response latency (P95 per second)")
            if not series.empty:
                st.line_chart(series.set_index("timestamp")[["latency_ms"]])
            else:
                st.info("No samples yet.")
        with c2:
            st.markdown("#### Error-rate trend")
            if not series.empty:
                st.line_chart(series.set_index("timestamp")[["error_rate_pct"]])
            else:
                st.info("No samples yet.")

        s1, s2 = st.columns(2, gap="large")
        with s1:
            st.markdown("#### Deployments")
            if deps:
                st.dataframe(pd.DataFrame([{"service": d["name"], "ready": f"{d['ready']}/{d['want']}",
                                            "status": "OK" if d["ready"] >= d["want"] else "DEGRADED"} for d in deps]),
                             use_container_width=True, hide_index=True, height=300)
            else:
                st.error((t.get("cluster") or {}).get("error", "Cluster data unavailable"))
        with s2:
            st.markdown("#### Recent failed requests")
            errs = t.get("recent_errors") or []
            if errs:
                st.dataframe(pd.DataFrame(errs), use_container_width=True, hide_index=True, height=300)
            else:
                st.success("No failed requests in the last 2 minutes.")
        return

    # ---- demo mode (unchanged behaviour)
    affected = sorted({e["service"] for e in st.session_state.active_errors})
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Active errors", len(st.session_state.active_errors))
    m2.metric("Affected services", len(affected))
    m3.metric("Latency", f"{st.session_state.current_latency:.1f} ms")
    m4.metric("Error rate", f"{st.session_state.current_error_rate:.2f}%")
    st.caption("All metrics above are simulated. They are not measurements from the live Online Boutique application.")
    c1, c2 = st.columns(2, gap="large")
    df = pd.DataFrame(st.session_state.metric_history)
    with c1:
        st.markdown("#### Response latency")
        if not df.empty:
            st.line_chart(df.set_index("timestamp")[["latency_ms"]])
        else:
            st.info("No latency samples available yet.")
    with c2:
        st.markdown("#### Error-rate trend")
        if not df.empty:
            st.line_chart(df.set_index("timestamp")[["error_rate_pct"]])
        else:
            st.info("No error-rate samples available yet.")
    s1, s2 = st.columns(2, gap="large")
    with s1:
        st.markdown("#### Affected services")
        if affected:
            st.dataframe(pd.DataFrame([{"service": s, "status": "SIMULATED ERROR",
                                        "active_errors": sum(e["service"] == s for e in st.session_state.active_errors)}
                                       for s in affected]), use_container_width=True, hide_index=True)
        else:
            st.success("No active simulated errors.")
    with s2:
        st.markdown("#### Active error details")
        if st.session_state.active_errors:
            st.dataframe(pd.DataFrame(st.session_state.active_errors), use_container_width=True, hide_index=True)
        else:
            st.success("No active simulated errors.")


health_section()

# =========================================================
# 5. HISTORY
# =========================================================

st.divider()
st.subheader("5. Event and experiment history")

if mode == "API mode":
    runs = api_get("/runs").get("runs", [])
    rows = []
    for r in runs:
        fw, rec = r.get("fault_window") or {}, r.get("recovery") or {}
        rows.append({"run": r["id"], "target": r.get("target"), "status": r.get("status"),
                     "fault_s": r.get("fault_duration_s"),
                     "errors_in_fault": f"{fw.get('errors')}/{fw.get('n')}" if fw else None,
                     "k8s_ready_s": rec.get("k8s_ready_s"),
                     "user_recovery_s": "no impact" if fw.get("errors") == 0 else rec.get("user_recovery_s")})
    st.markdown("#### Experiment history (results/runs)")
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.caption("No experiments recorded yet.")
else:
    history_tab, error_history_tab = st.tabs(["Event timeline", "All generated errors"])
    with history_tab:
        if st.session_state.events:
            st.dataframe(pd.DataFrame(st.session_state.events), use_container_width=True, hide_index=True)
        else:
            st.caption("No events recorded yet.")
    with error_history_tab:
        if st.session_state.error_history:
            st.dataframe(pd.DataFrame(st.session_state.error_history), use_container_width=True, hide_index=True)
        else:
            st.caption("No errors generated yet.")
    st.markdown("#### Experiment history")
    if st.session_state.experiment_history:
        st.dataframe(pd.DataFrame(st.session_state.experiment_history), use_container_width=True, hide_index=True)
    else:
        st.caption("No experiments recorded yet.")

# =========================================================
# FINDINGS & RESILIENCE SCORE
# =========================================================

st.divider()
st.subheader("📊 Findings, severity ranking & resilience score")
st.caption("All numbers, severities and the score are computed by code from the run evidence (formula in the report). "
           "The local AI only writes the explanation and fix text; any AI sentence quoting a number that is not in the "
           "evidence is rejected and replaced by a template.")
if mode != "API mode":
    st.info("Analysis needs API mode (real run evidence).")
else:
    if st.button("📊 Analyze all runs", type="primary"):
        with st.spinner("Analyzing runs and writing the report… (the local model can take 30–120 s)"):
            code, body = api_post("/analysis/run", timeout=300)
        if code != 200:
            st.error(f"Analysis failed: HTTP {code} {body}")
    rep = api_get("/analysis/latest", timeout=10)
    if rep.get("findings") is not None and rep.get("resilience"):
        r = rep["resilience"]
        a1, a2, a3 = st.columns(3)
        a1.metric("Resilience score (heuristic)", f"{r['overall']} / 100")
        a2.metric("Coverage", r["coverage"])
        a3.metric("Findings", len(rep["findings"]))
        nm = rep.get("narrative") or {}
        st.caption(f"Narrative: {nm.get('model') or 'templates'}" + (f" — {nm.get('error')}" if nm.get("error") else ""))
        st.dataframe(pd.DataFrame([{"rank": f["rank"], "severity": f"{f['severity']} ({f['severity_score']})",
                                    "finding": f["title"], "runs": len(f["run_ids"]), "confidence": f["confidence"],
                                    "recommended fix": f["recommended_fix"], "text": f["text_source"]}
                                   for f in rep["findings"]]), use_container_width=True, hide_index=True)
        for f in rep["findings"][:5]:
            with st.expander(f"#{f['rank']} {f['title']}"):
                st.json(f["evidence"])
                if f.get("explanation"):
                    st.write(f"**Explanation:** {f['explanation']}")
                st.write(f"**Recommended fix:** {f['recommended_fix']}")
                st.caption(f"Runs: {', '.join(f['run_ids'])}")
        st.caption("Full report: `results/findings-report.md`")
    else:
        st.caption("No analysis yet — press Analyze.")

# =========================================================
# 6. ROLLBACK
# =========================================================

st.divider()
st.subheader("6. ↩ Roll back to initial state")
st.warning("Rollback stops any active fault, restores the original replica count, and verifies recovery "
           "(all deployments ready + 5 consecutive successful frontend requests). History is retained.")

rollback_confirmed = st.checkbox("I confirm rollback of the current experiment.")
if st.button("↩ ROLLBACK TO INITIAL STATE", type="primary", use_container_width=True, disabled=not rollback_confirmed):
    if mode == "API mode":
        with st.spinner("Rolling back and verifying recovery (up to 2 minutes)…"):
            code, body = api_post("/rollback", {"confirm": True}, timeout=180)
        st.session_state.rollback_result = body
        st.session_state.rollback_verified = body.get("recovery_verified") is True
    else:
        st.session_state.active_errors = []
        st.session_state.random_injection_active = False
        st.session_state.experiment_status = "ROLLED BACK"
        st.session_state.current_latency = st.session_state.baseline_latency
        st.session_state.current_error_rate = st.session_state.baseline_error_rate
        record_metrics()
        log_event("Demo rollback completed. Simulated active errors cleared; history preserved.", "RECOVERY")
        st.session_state.rollback_verified = False
        st.success("Demo rollback simulated: active errors cleared and baseline demo metrics restored.")
        st.info("This is not a real recovery verification because Demo mode does not inspect Kubernetes.")

if mode == "API mode" and st.session_state.rollback_result:
    res = st.session_state.rollback_result
    msg = f"{res.get('status')} — " + " → ".join(res.get("steps", []))
    if res.get("recovery_verified"):
        st.success(msg)
    else:
        st.error(msg)

# =========================================================
# FOOTER
# =========================================================

st.divider()
st.caption("ResilienceAI · Bosch Hackathon Prototype · Demo mode is simulated. API mode runs real, "
           "safety-controlled experiments on the local kind cluster via chaos/controller.py.")
