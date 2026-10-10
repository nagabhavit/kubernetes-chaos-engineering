# Kubernetes Chaos Engineering — Online Boutique

AI-planned, safety-controlled chaos experiments on Google's Online Boutique in a local kind cluster. A local Ollama planner and a LangGraph workflow sit on top of a deterministic safety controller. Three application fault types (outage, pod crash, traffic spike) produce a ranked findings report. Network latency (`tc netem`) is validated separately on disposable lab pods. All AI runs locally, with no cloud API.


## Quick start (Day 2: AI-planned, safety-controlled chaos)

```powershell
.\.venv313\Scripts\Activate.ps1
pip install -r requirements.txt            # LangGraph, Locust, Streamlit, ...
ollama pull qwen2.5:3b                     # free local planner model
python -m pytest tests -q                  # 68 tests on fakes (Windows: 1 skipped, no socat)
python chaos\cli.py health                 # cluster, frontend, models, kill switch
python chaos\cli.py plan                   # AI ranks experiments (no execution)
python chaos\cli.py dry-run --rank 1       # full workflow, nothing injected
python chaos\cli.py run --rank 1           # asks for human approval, then injects
python chaos\cli.py report                 # results\findings-report.md
python chaos\cli.py latency-lab --delay-ms 200   # netem on disposable lab pods: before/during/after
python chaos\cli.py msty-export            # optional: review packet for MANUAL review in Msty Studio
python chaos\cli.py msty-import reply.md   # store the Msty reply with provenance + number check
```

| Document | Purpose |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | One-page architecture: agents, workflow, safety controls |
| [docs/CLEAN_RUN.md](docs/CLEAN_RUN.md) | From-scratch setup, the three experiments, cleanup, troubleshooting |
| [docs/AGENTS_AND_QA.md](docs/AGENTS_AND_QA.md) | Each agent's inputs and outputs; likely judge questions |
| [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) | 3-minute demo script and recording checklist |
| [docs/TEAM_FORM.md](docs/TEAM_FORM.md) | Team submission form (tools, models, libraries, data) |
| [results/findings-report.md](results/findings-report.md) | Ranked findings with evidence, fixes and resilience score |
| [architecture/workflow-graph.md](architecture/workflow-graph.md) | LangGraph workflow diagram (generated) |

## Overview

This project uses Google's [Online Boutique](https://github.com/GoogleCloudPlatform/microservices-demo) as the application under test. The application runs in a local Kind Kubernetes cluster while Locust generates HTTP traffic.

The experiment compares a baseline load test with a test in which the `productcatalogservice` deployment is scaled down and restored.

**My contribution:** Kubernetes experiment setup, controlled service disruption, Locust load testing, CSV result collection, and performance visualization.

## Architecture

```text
Locust Load Test
       |
       v
Frontend Service
       |
       v
Online Boutique Microservices
       |
       v
Product Catalog Service
       ^
       |
Scale deployment to zero
and restore the replica
       |
       v
Kubernetes recovery

Locust CSV results
       |
       v
Python + Matplotlib
       |
       v
Performance timeline chart
```

This is a conceptual view of the experiment, not a complete service dependency diagram.

## Technology Stack

- Kubernetes and Kind
- Docker Desktop
- kubectl
- Locust
- Python and Matplotlib

## Environment

- Cluster: `boutique-chaos`
- Namespace: `boutique`
- Load-test target: `http://localhost:8080`
- Application: Online Boutique microservices demo

## Experiments and Results

### Baseline load test

Configuration:
- Concurrent users: 10
- Spawn rate: 1 user per second
- Duration: 3 minutes

Results:
- Requests: 383
- Failed requests: 0
- Failure rate: 0%
- Average response time: approximately 17 ms
- Median response time: 13 ms
- 95th percentile response time: 37 ms
- Maximum response time: 96 ms
- Throughput: approximately 2.15 requests/second

### Product catalog disruption

During a Locust load test, `productcatalogservice` was scaled to zero replicas and then restored to one replica.

Results recorded by Locust:
- Requests: 351 in the aggregate CSV
- Failed requests: 11
- Failure rate: approximately 3.13%
- Average response time: approximately 491.82 ms
- Median response time: 13 ms
- 95th percentile response time: 60 ms
- Maximum response time: approximately 20.01 seconds
- Throughput: approximately 1.97 requests/second

The failure report records HTTP 500 responses affecting product-page requests, `/cart`, and `/setCurrency`.

The experiment coincided with the service scale-down and restoration. Individual failure timestamps have not yet been correlated with the precise outage window, so causality has not been conclusively established for every error.

### Recovery

The `productcatalogservice` deployment subsequently rolled out successfully, and the application pods were observed in the `Running` and ready state.

## Day 1: Safety-Controlled Experiments

Failures are injected by `chaos/controller.py`. It does not take shell commands; it only runs one approved fault on one allowlisted deployment.

### Safety controls

| Control | Implementation |
|---|---|
| Scope lock | Fixed context `kind-boutique-chaos`, namespace `boutique`; probe URL must be localhost |
| Target allowlist | 10 backend deployments; `frontend` (telemetry path) and `loadgenerator` are refused |
| Fault allowlist | `scale_to_zero` only (reversible) |
| Duration cap | Hard maximum 60 s, not overridable |
| Preflight gate | Refuses to run unless every deployment is ready, no previous fault is pending and the kill switch is clear |
| Baseline gate | 15 s of healthy probes required before injecting |
| Automatic abort | Restores immediately if probe error rate > 20 % or P95 > 1000 ms over a 5 s window, or if telemetry is lost |
| Kill switch | Create `chaos/KILL`, or press Ctrl+C |
| Guaranteed cleanup | `try/finally` restores the **original** replica count; Ctrl+C is ignored during cleanup; 3 retries |
| Crash recovery | State file `chaos/state/active_fault.json` + `chaos/restore.py` undo a fault if the controller dies |
| Recovery check | `kubectl rollout status` **and** 5 consecutive successful user requests |
| Evidence | `results/runs/<run_id>/` holds `events.jsonl`, `probes.csv`, `summary.json`, Locust CSVs and `timeline.png` (UTF-8, UTC) |

Rejected requests are logged to `results/runs/rejected.jsonl`.

### Day-1 runs (measured)

| Run | Target | Outcome | Fault duration | User errors during fault | K8s ready after restore | Users recovered after restore |
|---|---|---|---|---|---|---|
| `20261010T090605Z` | productcatalogservice | **Auto-abort** (error rate 23 % > 20 %) | 3.05 s | 3 of 5 probes | 4.1 s | **20.4 s** |
| `20261010T091112Z` | adservice | Completed at 60 s cap | 60.1 s | 0 of 232 probes | 63.7 s | no user impact |
| `20261010T091455Z` | adservice | **Kill switch** (`chaos/KILL`) | 10.1 s | 0 of 39 probes | 47.1 s | no user impact |
| rejected | frontend / 600 s duration | Refused before touching the cluster | — | — | — | — |

The probe sends sequential HTTP requests to `/`, `/product/OLJCESPC7Z` and `/cart` every 0.25 s, with a 2 s timeout. Locust load ran in parallel for the auto-abort run.

![Auto-abort run timeline](results/runs/20261010T090605Z_scale_to_zero_productcatalogservice/timeline.png)

### Initial findings

1. **Readiness does not equal user recovery (productcatalogservice).** Kubernetes reported the pod ready 4.1 s after restore, but users kept receiving HTTP 500s until 20.4 s after restore. A 3 s fault produced about 23 s of user-facing outage. *Hypothesis (not yet verified):* the frontend's gRPC connection to the catalog does not reconnect immediately.
2. **The product catalog is a single point of failure.** With one replica, its loss breaks `/`, product pages, `/cart`, `/setCurrency` and checkout within 1 s.
3. **The ad service degrades gracefully but restarts slowly.** It caused 0 user errors while down, but took 47–64 s to become ready again.
4. **Detection latency.** The auto-abort fired 3.05 s after injection, because a hung `/cart` probe (2 s timeout) blocked the sequential prober. The first error was seen at 0.7 s.

### Dependency map

`architecture/build_dependency_map.py` discovers services and `*_ADDR` dependencies from the live cluster and merges experiment evidence from `architecture/observations.json`. See [architecture/dependency-map.md](architecture/dependency-map.md). It also flags that the frontend is configured to call `shoppingassistantservice`, which is not deployed.

### Running the controller

```powershell
.\.venv313\Scripts\Activate.ps1
python chaos\controller.py --target productcatalogservice --duration 45 --load-users 10   # auto-abort demo
python chaos\controller.py --target adservice --duration 60                              # then: New-Item chaos\KILL
Remove-Item chaos\KILL                                                                   # re-arm
python chaos\restore.py                                                                  # emergency undo
python chaos\plot_run.py results\runs\<run_id>                                           # timeline chart
python architecture\build_dependency_map.py                                              # dependency map
```

### ResilienceAI control center (Streamlit UI)

`ui/app.py` provides buttons for starting and stopping ingestion, starting an experiment, the kill switch, random experiments and rollback. Live metrics refresh every 2 s. In **API mode** it calls `chaos/api.py`, which runs every fault through `chaos/controller.py`, so the allowlist, 60 s cap, auto-abort, kill switch and guaranteed restore all still apply. **Demo mode** is a labelled simulation that touches nothing.

| UI fault type | Real action |
|---|---|
| Service unavailability | `scale_to_zero` for the chosen duration, then restore |
| Pod failure | `kubectl delete pod -l app=<target>`; Kubernetes recreates it; user recovery is measured from injection |
| Latency injection | **Rejected for app pods** (disabled by design); nothing is injected. Network latency runs only on disposable lab pods via `cli.py latency-lab` |

```powershell
pip install -r ui\requirements.txt
python chaos\api.py                 # window 1: backend on 127.0.0.1:8000
streamlit run ui\app.py             # window 2: UI on localhost:8501
```

### AI experiment planner

`chaos/planner.py` gives an LLM the dependency map, the baseline and previous run results, along with the allowed catalog. The model returns ranked JSON proposals, each with a target, fault type, duration, hypothesis, expected impact and reason.

- **Free local model by default:** [Ollama](https://ollama.com) with `qwen2.5:3b`. There is no account, API key or cost, and no data leaves the laptop. No cloud API is used at runtime.
- **Deterministic validation:** each proposal is checked against the target allowlist, `scale_to_zero`/`pod_delete`/`traffic_spike` only (spike target must be `frontend`), integer 5–30 s duration, required fields and no duplicates. Invalid proposals are rejected and shown with their reasons.
- **The AI never executes anything.** A proposal runs only when a human approves it in the UI (`POST /planner/run`). It is re-validated at that point and then goes through `controller.py`.
- **Model unavailable or unusable output:** the planner falls back to a clearly labelled **rule-based** planner (fan-in plus untested edges). It is never presented as AI.
- **Evidence:** every plan is saved to `results/plans/plan_<ts>.json`, with the inputs, raw model output and validation result.

```powershell
winget install Ollama.Ollama        # one-time, free
ollama pull qwen2.5:3b              # one-time, ~1.9 GB
python chaos\planner.py             # CLI, or use the planner section in the UI
```

### Day 2: three fault types, findings and resilience score

| Fault type | What it does | Restore |
|---|---|---|
| `scale_to_zero` (service unavailability) | Scales the deployment to 0 replicas | Scales back to the **original** replica count |
| `pod_delete` (pod crash) | `kubectl delete pod -l app=<target>` | Kubernetes self-heals; recovery is measured from injection |
| `traffic_spike` (sudden load) | ~80 extra Locust users on the frontend; no Kubernetes object is changed | The load is stopped. `restore.py` can also stop it |

`chaos/analyze.py` (or **📊 Analyze all runs** in the UI) builds `results/findings-report.md` and `results/findings.json`:

- **Deterministic:** findings come from explicit rules (readiness-vs-user-recovery gap, hard dependency, slow restart, latency under load, graceful degradation, probe coverage gap, detection delay). Severity is 0–100 and the resilience score is 100 − severity. Both formulas are printed in the report.
- **AI (local Ollama):** writes only the explanation and fix text. Any sentence that quotes a number not found in that finding's evidence is rejected and replaced by a template. The report labels the source of each text.
- **Honesty rules:** cancelled runs are excluded; findings from a single run are marked low confidence; services the probe never calls are reported as *not observed*, not *resilient*.

### Day-2 runs (measured, from `results/findings-report.md`)

| Experiment | Runs | Error rate in fault | K8s ready (s) | Users recovered (s) | P95 baseline → fault (ms) | Score |
|---|---|---|---|---|---|---|
| `scale_to_zero` → productcatalogservice | 6 | 67% | 2.83 (median) | **18.87** (median) | 238 → 2010 | 34.0 |
| `pod_delete` → productcatalogservice | 1 | 67% | 0.18 | **21.48** (from the crash) | 60 → 2020 | 33.2 |
| `traffic_spike` → frontend | 1 | 0% | — | no user impact | 63 → **252** | 98.1 |
| `scale_to_zero` → cartservice | 1 | 67% | 32.06 | 48.16 | 102 → 104 | 31.1 |
| `scale_to_zero` → adservice | 2 | 0% | 55.4 | no user impact | 214 → 246 | 97.4 |
| `scale_to_zero` → paymentservice | 1 | 0% | 11.87 | not observed (probe never calls it) | 45 → 33 | 99.5 |

**Resilience score: 65.5 / 100** (team-defined heuristic, 4 of 10 allowlisted services tested; 11 ranked findings). The main finding is repeatable: Kubernetes reports productcatalogservice Ready about 3 s after restore, but users keep failing for about 19 s.

**Network latency lab (separate, not in the score):** 200 ms `tc netem` on the disposable `chaos-lab-server` pod. Two PASS runs showed ping +199.9 / +200.0 ms and HTTP +403 / +401 ms during the fault; removal was verified with `tc qdisc show` and both lab pods were deleted. One earlier lab run failed before injecting anything (YAML parse error in the lab manifest, since fixed) and is listed in the report.

**Limits:** single-node kind cluster; one replica per service; several experiments have a single run (marked low confidence); the local 3B model's explanations are shallow (the number guard checks figures, not reasoning).

### Live dashboard

`python chaos\dashboard.py` and then open http://127.0.0.1:8090. The dashboard is read-only. It shows the live error rate, P95 latency, the probe timeline with fault, restore, Kubernetes-ready and user-recovery markers, per-deployment readiness, the run event log and run history, and warnings for a present kill switch or an unrecovered fault.

## Early manual experiment (before the controller existed)

This section documents the first hand-run experiment. It is kept as history; current results are in the Day-1 and Day-2 sections above.

The `results/` directory contains the Locust reports and generated chart.

Key files:
- `results/baseline2_stats.csv`
- `results/baseline2_stats_history.csv`
- `results/chaos2_stats.csv`
- `results/chaos2_stats_history.csv`
- `results/chaos2_failures.csv`
- `results/chaos2_timeline.png`

![Chaos test performance timeline](results/chaos2_timeline.png)

## Reproduction

The recorded tests used a local Kind cluster, a deployed Online Boutique application, and a frontend port-forward to `localhost:8080`.

Run the baseline load test from the repository root:

```powershell
python -m locust -f src/loadgenerator/locustfile.py --host http://localhost:8080 --headless --users 10 --spawn-rate 1 --run-time 3m --csv results/baseline2 --csv-full-history
```

For the disruption experiment, start a separate Locust run with a different CSV prefix, then scale the product catalog deployment down and restore it:

```powershell
kubectl --context kind-boutique-chaos scale deployment/productcatalogservice -n boutique --replicas=0
kubectl --context kind-boutique-chaos scale deployment/productcatalogservice -n boutique --replicas=1
kubectl --context kind-boutique-chaos rollout status deployment/productcatalogservice -n boutique --timeout=120s
```

Generate the visualization from the saved chaos-test history:

```powershell
python create_chart.py
```

These commands assume the cluster, application, port-forward, Python environment, and dependencies are already configured. For a complete clean-machine setup, see [docs/CLEAN_RUN.md](docs/CLEAN_RUN.md).

### Limitations of this early experiment

- The experiment ran on a local Kind cluster, not a production Kubernetes environment.
- The baseline and disruption runs may differ in request timing and distribution.
- The exact failure-injection window has not been correlated with individual request timestamps.
- Results are specific to this test configuration and should not be generalized to production workloads.
- Repeated trials would improve confidence in the observations.

### Conclusion of this early experiment

The baseline test recorded no failed requests. During the disruption experiment, Locust recorded 11 failures and a maximum response time of approximately 20 seconds. Kubernetes subsequently restored the deployment. Timestamp correlation and repeated experiments are the next steps toward a stronger resilience assessment.

## Upstream Project

The application used for this experiment is Google's [Online Boutique microservices demo](https://github.com/GoogleCloudPlatform/microservices-demo). The chaos-testing procedure, experiment reports, and visualization in this repository document the work added for this project.
