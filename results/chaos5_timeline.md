# chaos5 — Reconciled Fault Timeline (derived)

Derived 2026-10-10 from raw evidence. Raw files are **unchanged**:
`chaos5_events.txt`, `chaos5_k8s_events.txt`, `chaos5_stats_history.csv`, `chaos5_stats.csv`, `chaos5_failures.csv`.

## Timeline (UTC)

| Time (UTC) | Unix | Source | Event |
|---|---|---|---|
| 08:01:19 | 1791619279 | Locust history | Load test starts (10 users, spawn 2/s, 60 s) |
| 08:01:34.66 | 1791619294.66 | `chaos5_events.txt` line 1 | **SCALE_DOWN_COMMAND (effective)** |
| 08:01:35 | 1791619295 | Locust history | First failures (cumulative 0 → 2) |
| 08:02:03.64 | 1791619323.64 | `chaos5_events.txt` line 2 | SCALE_DOWN_COMMAND re-issued (deployment already at 0, no-op) |
| ~08:02:19 | — | Locust (60 s run-time) | Load test ends; last history row 08:02:14 |
| 08:03:15.54 | 1791619395.54 | `chaos5_events.txt` line 3 | SCALE_UP_COMMAND |
| after 08:03:15 | — | kubectl | `rollout status` reported success; all 11 pods 1/1 Running |

## Why line 1 is the real scale-down

1. **Locust timing:** cumulative failures were 0 at 08:01:34 and 2 at 08:01:35, which is within 1 s of line 1. If the fault had started at 08:02:03, failures would not appear 28 s earlier.
2. **Kubernetes event age:** `chaos5_k8s_events.txt` was written at 08:03:46.9 UTC (file mtime). It shows exactly one `Scaled down ... from 1 to 0` event, aged `2m12s`, which puts it at about 08:01:35. It shows `Scaled up ... from 0 to 1` aged `31s`, about 08:03:16, which matches line 3.
3. Only one scale-down event exists, so the 08:02:03 command did not change replicas.

Conclusion: the extra line is not spurious. The fault began at 08:01:34.66Z, and the 08:02:03.64Z command was a repeat with no effect. (The later handoff note cited only the second command's console output.)

## Measured impact (from CSVs)

| Window | Requests | Failures | Error rate |
|---|---|---|---|
| Pre-fault (08:01:19–08:01:34) | 43 | 0 | 0% |
| During fault (08:01:35–end) | 92 | 83 | ~90% |
| Whole run (aggregate CSV) | 135 | 83 | 61.5% |

- Time from fault to first observed failure: ≤ 1 s (bounded by 1 s history resolution).
- All failures were HTTP 500, spread across `/`, `/product/*`, `/cart`, `/setCurrency`, `/cart/checkout`.
- Requests that still succeeded during the fault were not broken down per endpoint in this run.

## What chaos5 does NOT show

- **Recovery time:** the Locust run ended about 56 s before scale-up, so there are no user-facing measurements after restoration. Recovery time is **not measured** for chaos5.
- Fault duration (scale-down → scale-up command) was about 100.9 s, longer than the load window.
- Event ages in `chaos5_k8s_events.txt` have ±1 s resolution and rely on file mtime as the capture time.

## Note on file encoding

`chaos5_events.txt` and `chaos5_k8s_events.txt` are UTF-16 (PowerShell `Tee-Object`/`Out-File` default). Future scripts should write UTF-8 (`-Encoding utf8`) or the controller should write its own logs.
