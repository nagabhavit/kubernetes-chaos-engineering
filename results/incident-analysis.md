# Incident Analysis: Product Catalog Disruption

## Observed Locust Results

The `chaos2` test recorded 351 requests in its aggregate CSV, including 11 failures (approximately 3.13%).

The Locust history shows:
- Cumulative failures remained at zero before timestamp `1791616130`.
- At timestamp `1791616130`, the cumulative failure count reached 11.
- The reported P95 reached 20,000 ms in the following recorded intervals.
- By timestamp `1791616144`, the reported P95 had fallen to 18 ms and the interval failure rate was zero.

These are Locust time-series observations. The timestamps are Unix timestamps.

## Kubernetes Evidence

Kubernetes events recorded:
- A scale-down of `productcatalogservice` from one replica to zero.
- A subsequent scale-up from zero to one replica.
- Pod termination and replacement.
- Readiness-probe timeouts during pod startup.
- Successful container creation and startup events.

The event output showed relative ages rather than exact timestamps. Rollout history contained one deployment revision and no recorded change causes.

## Interpretation

The Locust data shows a concentrated failure and latency-spike window. Kubernetes events independently confirm that the product catalog service was disrupted and restored.

The available evidence does not establish the exact ordering of every failed request relative to the scale-down and recovery operations. Therefore, this experiment supports an observed association, but does not conclusively attribute all 11 failures to the injected disruption.

## Limitations and Next Experiment

For stronger causal evidence, repeat the test while recording:
1. Exact UTC timestamps immediately before and after each scale operation.
2. Locust request failures and time-series data.
3. Kubernetes pod readiness and deployment events.
4. Recovery time until the service is ready and requests succeed again.

Correlating these records will make the next experiment more reproducible and its conclusions stronger.
