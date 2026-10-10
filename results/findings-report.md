# Findings Report: Online Boutique (local kind sandbox)

Generated 2026-10-10T18:11:46+00:00 by `chaos/analyze.py` from **12 controller runs** (6 distinct experiments; fault types: pod_delete, scale_to_zero, traffic_spike), plus 3 network-latency lab runs (separate section).

**How to read this report:** *Observed facts* are computed by code from `results/runs/*`. *Likely cause* is a hypothesis. *Recommended fix* and *verification experiment* are proposals.

**Narrative written by:** local Ollama (planner model) (`qwen2.5:3b`). Not used: llama3.2:3b: not pulled

## Resilience score (team-defined heuristic, not an industry standard)

**65.5 / 100**, with 4/10 allowlisted services tested.

Method: severity (0-100) = 40 x error rate during fault + 20 x (affected routes / 3 probed routes) + 25 x min(1, user-facing outage s / 60) + 5 x min(1, Kubernetes-ready s / 120) + 10 x min(1, extra P95 ms over baseline / 1000); outage = fault duration + user recovery. priority = severity x confidence weight (high 1.0, medium 0.85, single run 0.7). Resilience score per experiment = 100 - severity; overall = mean over tested experiments.

| Experiment | Runs | Error rate in fault | Routes affected | K8s ready (s) | Users recovered (s) | P95 baseline → fault (ms) | Score |
|---|---|---|---|---|---|---|---|
| scale_to_zero → productcatalogservice | 6 | 67% | /, /cart, /product/<id> | 2.83 | 18.87 | 238.2 → 2010.45 | 34.0 |
| scale_to_zero → adservice | 2 | 0% | none | 55.4 | no user impact | 214.4 → 245.95 | 97.4 |
| scale_to_zero → cartservice | 1 | 67% | /, /cart, /product/<id> | 32.06 | 48.16 | 101.9 → 104.1 | 31.1 |
| scale_to_zero → paymentservice | 1 | 0% | none | 11.87 | no user impact | 45.4 → 33.0 | 99.5 |
| pod_delete → productcatalogservice | 1 | 67% | /, /cart, /product/<id> | 0.18 | 21.48 | 60.2 → 2020.2 | 33.2 |
| traffic_spike → frontend | 1 | 0% | none | 0.12 | no user impact | 62.8 → 251.9 | 98.1 |

## Ranked findings

### 1. [High · severity 66.0 · priority 66.0] Users keep failing after productcatalogservice is Ready

**Affected:** services frontend, productcatalogservice; routes /, /cart, /product/<id>. **Confidence:** high.

**Observed facts**

| Evidence | Value |
|---|---|
| runs | 6 |
| k8s_ready_s_median | 2.83 |
| user_recovery_s_median | 18.87 |
| gap_s_median | 16.04 |

Runs: `20261010T090605Z_scale_to_zero_productcatalogservice`, `20261010T092008Z_scale_to_zero_productcatalogservice`, `20261010T095507Z_scale_to_zero_productcatalogservice`, `20261010T095627Z_scale_to_zero_productcatalogservice`, `20261010T104422Z_scale_to_zero_productcatalogservice`, `20261010T175308Z_scale_to_zero_productcatalogservice`

**Likely cause (hypothesis):** Users are experiencing failures after productcatalogservice is Ready, likely due to the high gap time between Ready and user recovery.

**Recommended fix:** Increase the number of replicas for productcatalogservice to reduce the gap time.

**Verification experiment:** Run a stress test with increased replicas to confirm the fix.

<sub>Narrative source: AI: local Ollama (planner model) (qwen2.5:3b)</sub>

### 2. [High · severity 66.0 · priority 66.0] productcatalogservice is a hard dependency / single point of failure (scale to zero)

**Affected:** services frontend, productcatalogservice; routes /, /cart, /product/<id>. **Confidence:** high.

**Observed facts**

| Evidence | Value |
|---|---|
| runs | 6 |
| error_rate_in_fault_median | 0.67 |
| first_error_s_median | 0.42 |
| affected_routes | 3 |
| replicas | 1 |

Runs: `20261010T090605Z_scale_to_zero_productcatalogservice`, `20261010T092008Z_scale_to_zero_productcatalogservice`, `20261010T095507Z_scale_to_zero_productcatalogservice`, `20261010T095627Z_scale_to_zero_productcatalogservice`, `20261010T104422Z_scale_to_zero_productcatalogservice`, `20261010T175308Z_scale_to_zero_productcatalogservice`

**Likely cause (hypothesis):** productcatalogservice is a single point of failure, likely due to its high error rate and short recovery time.

**Recommended fix:** Increase the number of replicas for productcatalogservice to reduce the error rate and recovery time.

**Verification experiment:** Run a stress test with increased replicas to confirm the fix.

<sub>Narrative source: AI: local Ollama (planner model) (qwen2.5:3b)</sub>

### 3. [High · severity 68.9 · priority 48.2] Users keep failing after cartservice is Ready

**Affected:** services cartservice, frontend; routes /, /cart, /product/<id>. **Confidence:** low (single run).

**Observed facts**

| Evidence | Value |
|---|---|
| runs | 1 |
| k8s_ready_s_median | 32.06 |
| user_recovery_s_median | 48.16 |
| gap_s_median | 16.1 |

Runs: `20261010T100435Z_scale_to_zero_cartservice`

**Likely cause (hypothesis):** Users are experiencing failures after cartservice is Ready, likely due to the high gap time between Ready and user recovery.

**Recommended fix:** Increase the number of replicas for cartservice to reduce the gap time.

**Verification experiment:** Run a stress test with increased replicas to confirm the fix.

<sub>Narrative source: AI: local Ollama (planner model) (qwen2.5:3b)</sub>

### 4. [High · severity 68.9 · priority 48.2] cartservice is a hard dependency / single point of failure (scale to zero)

**Affected:** services cartservice, frontend; routes /, /cart, /product/<id>. **Confidence:** low (single run).

**Observed facts**

| Evidence | Value |
|---|---|
| runs | 1 |
| error_rate_in_fault_median | 0.67 |
| first_error_s_median | 0.64 |
| affected_routes | 3 |
| replicas | 1 |

Runs: `20261010T100435Z_scale_to_zero_cartservice`

**Likely cause (hypothesis):** cartservice is a single point of failure, likely due to its high error rate and short recovery time.

**Recommended fix:** Increase the number of replicas for cartservice to reduce the error rate and recovery time.

**Verification experiment:** Run a stress test with increased replicas to confirm the fix.

<sub>Narrative source: AI: local Ollama (planner model) (qwen2.5:3b)</sub>

### 5. [High · severity 68.9 · priority 48.2] cartservice takes 32 s to become Ready after restore

**Affected:** services cartservice, frontend; routes /, /cart, /product/<id>. **Confidence:** low (single run).

**Observed facts**

| Evidence | Value |
|---|---|
| runs | 1 |
| k8s_ready_s_median | 32.06 |

Runs: `20261010T100435Z_scale_to_zero_cartservice`

**Likely cause (hypothesis):** cartservice takes a long time to become Ready, likely due to its high initial readiness time.

**Recommended fix:** Increase the number of replicas for cartservice to reduce the initial readiness time.

**Verification experiment:** Run a stress test with increased replicas to confirm the fix.

<sub>Narrative source: AI: local Ollama (planner model) (qwen2.5:3b)</sub>

### 6. [High · severity 66.8 · priority 46.8] productcatalogservice is a hard dependency / single point of failure (pod delete)

**Affected:** services frontend, productcatalogservice; routes /, /cart, /product/<id>. **Confidence:** low (single run).

**Observed facts**

| Evidence | Value |
|---|---|
| runs | 1 |
| error_rate_in_fault_median | 0.67 |
| first_error_s_median | 0.43 |
| affected_routes | 3 |
| replicas | 1 |

Runs: `20261010T175626Z_pod_delete_productcatalogservice`

**Likely cause (hypothesis):** productcatalogservice is a single point of failure, likely due to its high error rate and short recovery time.

**Recommended fix:** Increase the number of replicas for productcatalogservice to reduce the error rate and recovery time.

**Verification experiment:** Run a stress test with increased replicas to confirm the fix.

<sub>Narrative source: AI: local Ollama (planner model) (qwen2.5:3b)</sub>

### 7. [Low · severity 10.0 · priority 10.0] Auto-abort fires ~2 s after the first error (our tooling)

**Affected:** services frontend, productcatalogservice; routes /, /cart, /product/<id>. **Confidence:** high.

**Observed facts**

| Evidence | Value |
|---|---|
| runs | 6 |
| first_error_s_median | 0.42 |
| abort_after_s_median | 2.83 |

Runs: `20261010T090605Z_scale_to_zero_productcatalogservice`, `20261010T092008Z_scale_to_zero_productcatalogservice`, `20261010T095507Z_scale_to_zero_productcatalogservice`, `20261010T095627Z_scale_to_zero_productcatalogservice`, `20261010T104422Z_scale_to_zero_productcatalogservice`, `20261010T175308Z_scale_to_zero_productcatalogservice`

**Likely cause (hypothesis):** Auto-abort fires after the first error, likely due to the high error rate and short recovery time.

**Recommended fix:** Increase the number of replicas for productcatalogservice to reduce the error rate and recovery time.

**Verification experiment:** Run a stress test with increased replicas to confirm the fix.

<sub>Narrative source: AI: local Ollama (planner model) (qwen2.5:3b)</sub>

### 8. [Info · severity 5.0 · priority 3.5] paymentservice outage not visible to the probe (untested path)

**Affected:** services frontend, paymentservice; routes none observed. **Confidence:** low (single run).

**Observed facts**

| Evidence | Value |
|---|---|
| runs | 1 |
| error_rate_in_fault_median | 0.0 |

Runs: `20261010T104306Z_scale_to_zero_paymentservice`

**Likely cause (hypothesis):** paymentservice outage is not visible to the probe, likely due to the untested path.

**Recommended fix:** Test the paymentservice outage path to ensure visibility.

**Verification experiment:** Run a stress test with the paymentservice outage path to confirm visibility.

<sub>Narrative source: AI: local Ollama (planner model) (qwen2.5:3b)</sub>

### 9. [Info · severity 2.6 · priority 2.2] adservice takes 55 s to become Ready after restore

**Affected:** services adservice, frontend; routes none observed. **Confidence:** medium.

**Observed facts**

| Evidence | Value |
|---|---|
| runs | 2 |
| k8s_ready_s_median | 55.4 |

Runs: `20261010T091112Z_scale_to_zero_adservice`, `20261010T091455Z_scale_to_zero_adservice`

**Likely cause (hypothesis):** adservice takes a long time to become Ready, likely due to its high initial readiness time.

**Recommended fix:** Increase the number of replicas for adservice to reduce the initial readiness time.

**Verification experiment:** Run a stress test with increased replicas to confirm the fix.

<sub>Narrative source: AI: local Ollama (planner model) (qwen2.5:3b)</sub>

### 10. [Info · severity 1.9 · priority 1.3] P95 latency rises from 63 to 252 ms under a traffic spike (no errors)

**Affected:** services frontend; routes none observed. **Confidence:** low (single run).

**Observed facts**

| Evidence | Value |
|---|---|
| runs | 1 |
| baseline_p95_ms | 62.8 |
| p95_in_fault_ms | 251.9 |
| error_rate_in_fault_median | 0.0 |
| throughput_rps | 23.86 |

Runs: `20261010T175803Z_traffic_spike_frontend`

**Likely cause (hypothesis):** There is a traffic spike, likely due to the high P95 latency.

**Recommended fix:** Monitor and adjust the traffic to reduce the P95 latency.

**Verification experiment:** Run a stress test with reduced traffic to confirm the fix.

<sub>Narrative source: AI: local Ollama (planner model) (qwen2.5:3b)</sub>

### 11. [Info · severity 0.0 · priority 0.0] Frontend degrades gracefully without adservice

**Affected:** services adservice, frontend; routes none observed. **Confidence:** medium.

**Observed facts**

| Evidence | Value |
|---|---|
| runs | 2 |
| error_rate_in_fault_median | 0.0 |
| fault_duration_s_median | 35.09 |

Runs: `20261010T091112Z_scale_to_zero_adservice`, `20261010T091455Z_scale_to_zero_adservice`

**Likely cause (hypothesis):** Frontend can degrade gracefully without adservice, likely due to the absence of adservice.

**Recommended fix:** Ensure adservice is always available to prevent degradations.

**Verification experiment:** Monitor the frontend for degradations without adservice to confirm the fix.

<sub>Narrative source: AI: local Ollama (planner model) (qwen2.5:3b)</sub>

## Network latency: disposable lab validation (not part of the score)

`chaos/netem_lab.py` applies `tc netem` only to the disposable pod `chaos-lab-server`. A second disposable pod measures it with curl (HTTP) and ping before, during and after. No application pod is touched, so these runs validate the latency mechanism and are reported here, not in the resilience score. Latency on application pods is disabled.

| Run | Result | Delay | Ping avg before → during → after (ms) | HTTP median before → during → after (ms) | Requests OK | netem removal verified | Cleanup |
|---|---|---|---|---|---|---|---|
| `20261010T172518Z_network_latency_lab` | **FAILED** (kubectl apply -f C:\Projects\microservices-demo\chaos\latency-lab.yaml failed (rc=1): error: error parsing C:\Projects\microservices-demo\chaos\latency-lab.yaml) | 200 ms | — → — → — | — → — → — | —/—/— of 15 | — | — |
| `20261010T174253Z_network_latency_lab` | **PASS** | 200 ms | 0.119 → 200.012 → 0.135 | 1.51 → 404.62 → 4.7 | 15/15/15 of 15 | True | lab pods deleted rc=0 |
| `20261010T175151Z_network_latency_lab` | **PASS** | 200 ms | 0.075 → 200.107 → 0.051 | 0.93 → 401.83 → 1.65 | 15/15/15 of 15 | True | lab pods deleted rc=0 |

2 of 3 lab runs passed. A run passes only if, during the fault, ping or the HTTP median rises by at least 80% of the configured delay; ping returns to within max(5 ms, 50%) of before; all requests succeed before and after; and removal of netem is verified with `tc qdisc show`. HTTP rises by about twice the delay because both the TCP handshake reply and the response leave the delayed pod.

## Limitations

- Local kind cluster, 1 replica per service, laptop resources. The results apply to this configuration only.
- The probe calls `/`, a product page and `/cart` every 0.25 s. Checkout, payment and email are not exercised, so 0 errors there means *not observed*, not *resilient*.
- Error rate, user-facing outage, Kubernetes readiness and latency are separate dimensions and are reported separately.
- Findings from one run are low confidence. Causes are hypotheses unless proven by a verification experiment.
- Cancelled and precheck-failed runs are excluded because nothing was injected. Historical Locust-only runs (baseline*, chaos1-5) predate the controller and are not included in the score.
