# Kubernetes Chaos Engineering — Online Boutique

A local Kubernetes resilience experiment using Kind, Locust, and Python to observe application behavior during a controlled microservice disruption.

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

## Results and Visualization

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

These commands assume the cluster, application, port-forward, Python environment, and dependencies are already configured. This README does not yet document a complete clean-machine setup.

## Limitations

- The experiment ran on a local Kind cluster, not a production Kubernetes environment.
- The baseline and disruption runs may differ in request timing and distribution.
- The exact failure-injection window has not been correlated with individual request timestamps.
- Results are specific to this test configuration and should not be generalized to production workloads.
- Repeated trials would improve confidence in the observations.

## Conclusion

The baseline test recorded no failed requests. During the disruption experiment, Locust recorded 11 failures and a maximum response time of approximately 20 seconds. Kubernetes subsequently restored the deployment. Timestamp correlation and repeated experiments are the next steps toward a stronger resilience assessment.

## Upstream Project

The application used for this experiment is Google's [Online Boutique microservices demo](https://github.com/GoogleCloudPlatform/microservices-demo). The chaos-testing procedure, experiment reports, and visualization in this repository document the work added for this project.
