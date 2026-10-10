# Findings Report: Online Boutique (local kind sandbox)

Generated 2026-10-10T18:37:22+00:00 by `chaos/analyze.py` from **12 controller runs** (6 distinct experiments; fault types: pod_delete, scale_to_zero, traffic_spike), plus 3 network-latency lab runs (separate section).

**How to read this report:** *Observed facts* are computed by code from `results/runs/*`. *Likely cause* is a hypothesis. *Recommended fix* and *verification experiment* are proposals.

**Narrative written by:** local Ollama (analysis model) (`llama3.2:3b`)

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

**Likely cause (hypothesis):** The frontend's gRPC client likely waits in reconnect backoff after the dependency returns, so requests keep failing after the new pod is Ready.

**Recommended fix:** Run 2+ replicas so a restart never removes every endpoint; set gRPC keepalive and a lower maximum reconnect backoff in the frontend; retry idempotent reads with a short timeout.

**Verification experiment:** Repeat the same outage with 2 replicas (or tuned backoff) and compare user recovery time.

<sub>Narrative source: template (AI text rejected: unverified number)</sub>

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

**Likely cause (hypothesis):** The productcatalogservice is likely a single point of failure due to its hard dependency on other services. This is because the median error rate in fault is 0.67, indicating that the service is prone to errors. Additionally, the median first error time is 0.42 seconds, suggesting that the service is not recovering quickly from errors.

**Recommended fix:** Implement circuit breakers or other error handling mechanisms to detect and prevent cascading failures in the productcatalogservice. This could involve adding additional logging or monitoring to detect errors earlier.

**Verification experiment:** Verify that the median error rate in fault is reduced to 0.01 after implementing circuit breakers or other error handling mechanisms.

<sub>Narrative source: AI: local Ollama (analysis model) (llama3.2:3b)</sub>

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

**Likely cause (hypothesis):** The frontend's gRPC client likely waits in reconnect backoff after the dependency returns, so requests keep failing after the new pod is Ready.

**Recommended fix:** Run 2+ replicas so a restart never removes every endpoint; set gRPC keepalive and a lower maximum reconnect backoff in the frontend; retry idempotent reads with a short timeout.

**Verification experiment:** Repeat the same outage with 2 replicas (or tuned backoff) and compare user recovery time.

<sub>Narrative source: template (AI text rejected: unverified number)</sub>

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

**Likely cause (hypothesis):** The cartservice is likely a single point of failure due to its hard dependency on other services. This is because the median error rate in fault is 0.67, indicating that the service is prone to errors. Additionally, the median first error time is 0.64 seconds, suggesting that the service is not recovering quickly from errors.

**Recommended fix:** Implement circuit breakers or other error handling mechanisms to detect and prevent cascading failures in the cartservice. This could involve adding additional logging or monitoring to detect errors earlier.

**Verification experiment:** Verify that the median error rate in fault is reduced to 0.01 after implementing circuit breakers or other error handling mechanisms.

<sub>Narrative source: AI: local Ollama (analysis model) (llama3.2:3b)</sub>

### 5. [High · severity 68.9 · priority 48.2] cartservice takes 32 s to become Ready after restore

**Affected:** services cartservice, frontend; routes /, /cart, /product/<id>. **Confidence:** low (single run).

**Observed facts**

| Evidence | Value |
|---|---|
| runs | 1 |
| k8s_ready_s_median | 32.06 |

Runs: `20261010T100435Z_scale_to_zero_cartservice`

**Likely cause (hypothesis):** Container start-up and readiness checks take a long time for this service.

**Recommended fix:** Shorten start-up (image/runtime tuning, readiness probe timing) or keep 2+ replicas so a restart is invisible to users.

**Verification experiment:** Measure ready time again after tuning the readiness probe.

<sub>Narrative source: template (AI text rejected: unverified number)</sub>

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

**Likely cause (hypothesis):** With a single replica and no fallback, every request that needs this service fails.

**Recommended fix:** Run 2+ replicas with a PodDisruptionBudget (minAvailable: 1); let the frontend degrade (cached or partial page) instead of returning HTTP 500.

**Verification experiment:** Repeat the outage with 2 replicas; expect a near-zero error rate.

<sub>Narrative source: template (AI text rejected: unverified number)</sub>

### 7. [Low · severity 10.0 · priority 10.0] Auto-abort fires ~2 s after the first error (our tooling)

**Affected:** services frontend, productcatalogservice; routes /, /cart, /product/<id>. **Confidence:** high.

**Observed facts**

| Evidence | Value |
|---|---|
| runs | 6 |
| first_error_s_median | 0.42 |
| abort_after_s_median | 2.83 |

Runs: `20261010T090605Z_scale_to_zero_productcatalogservice`, `20261010T092008Z_scale_to_zero_productcatalogservice`, `20261010T095507Z_scale_to_zero_productcatalogservice`, `20261010T095627Z_scale_to_zero_productcatalogservice`, `20261010T104422Z_scale_to_zero_productcatalogservice`, `20261010T175308Z_scale_to_zero_productcatalogservice`

**Likely cause (hypothesis):** The productcatalogservice is likely experiencing a short delay between the first error and the auto-abort, causing the application to become unresponsive. This is because the median first error time is 0.42 seconds, which is close to the median time for the auto-abort to occur (2.83 seconds).

**Recommended fix:** Optimize the auto-abort process to reduce the delay between the first error and the auto-abort. This could involve adding additional logging or monitoring to detect issues earlier.

**Verification experiment:** Verify that the median delay between the first error and the auto-abort is reduced to 1 second after optimizing the auto-abort process.

<sub>Narrative source: AI: local Ollama (analysis model) (llama3.2:3b)</sub>

### 8. [Info · severity 5.0 · priority 3.5] paymentservice outage not visible to the probe (untested path)

**Affected:** services frontend, paymentservice; routes none observed. **Confidence:** low (single run).

**Observed facts**

| Evidence | Value |
|---|---|
| runs | 1 |
| error_rate_in_fault_median | 0.0 |

Runs: `20261010T104306Z_scale_to_zero_paymentservice`

**Likely cause (hypothesis):** The paymentservice is not visible to the probe, causing the application to become unresponsive. This is because the median error rate in fault is 0.0, indicating that the service is not failing. However, this may be due to the probe not detecting issues correctly.

**Recommended fix:** Verify that the probe is correctly detecting issues in the paymentservice. This could involve adding additional logging or monitoring to detect issues earlier.

**Verification experiment:** Verify that the median error rate in fault is 0.01 after verifying that the probe is correctly detecting issues in the paymentservice.

<sub>Narrative source: AI: local Ollama (analysis model) (llama3.2:3b)</sub>

### 9. [Info · severity 2.6 · priority 2.2] adservice takes 55 s to become Ready after restore

**Affected:** services adservice, frontend; routes none observed. **Confidence:** medium.

**Observed facts**

| Evidence | Value |
|---|---|
| runs | 2 |
| k8s_ready_s_median | 55.4 |

Runs: `20261010T091112Z_scale_to_zero_adservice`, `20261010T091455Z_scale_to_zero_adservice`

**Likely cause (hypothesis):** Container start-up and readiness checks take a long time for this service.

**Recommended fix:** Shorten start-up (image/runtime tuning, readiness probe timing) or keep 2+ replicas so a restart is invisible to users.

**Verification experiment:** Measure ready time again after tuning the readiness probe.

<sub>Narrative source: template (AI text rejected: unverified number)</sub>

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

**Likely cause (hypothesis):** Added delay propagates to user-facing pages because calls are synchronous and have no timeout budget or cache.

**Recommended fix:** Set client timeouts and retries with deadlines, cache read-mostly data, and add an HPA and resource requests for load spikes.

**Verification experiment:** Repeat with the fix and compare P95 against the baseline.

<sub>Narrative source: template (AI text rejected: unverified number)</sub>

### 11. [Info · severity 0.0 · priority 0.0] Frontend degrades gracefully without adservice

**Affected:** services adservice, frontend; routes none observed. **Confidence:** medium.

**Observed facts**

| Evidence | Value |
|---|---|
| runs | 2 |
| error_rate_in_fault_median | 0.0 |
| fault_duration_s_median | 35.09 |

Runs: `20261010T091112Z_scale_to_zero_adservice`, `20261010T091455Z_scale_to_zero_adservice`

**Likely cause (hypothesis):** The frontend is degrading gracefully without the adservice, causing the application to become unresponsive. This is because the median error rate in fault is 0.0, indicating that the service is not failing. However, this may be due to the adservice not being properly configured.

**Recommended fix:** Verify that the adservice is properly configured to detect and prevent issues in the frontend. This could involve adding additional logging or monitoring to detect issues earlier.

**Verification experiment:** Verify that the median error rate in fault is 0.01 after verifying that the adservice is properly configured to detect and prevent issues in the frontend.

<sub>Narrative source: AI: local Ollama (analysis model) (llama3.2:3b)</sub>

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
