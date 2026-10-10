# Kubernetes Chaos Engineering — Online Boutique

## Overview
This project evaluates the resilience of Google Cloud Platform's Online Boutique microservices demo running locally on Kubernetes using Kind.

The application is deployed in a local Kubernetes cluster, and Locust generates HTTP traffic while service failures are introduced.

## Tech Stack
- Kubernetes
- Kind
- Docker Desktop
- kubectl
- Locust
- Python
- Matplotlib

## Environment
- Kubernetes cluster: `boutique-chaos`
- Namespace: `boutique`
- Load-test target: `http://localhost:8080`
- Application: Online Boutique microservices demo

## Experiments

### 1. Baseline Load Test
Configuration:
- Users: 10
- Spawn rate: 1 user/second
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

### 2. Product Catalog Failure Experiment
During a Locust load test, the `productcatalogservice` deployment was scaled down to zero replicas and then restored to one replica.

Results recorded by Locust:
- Requests: 351
- Failed requests: 11
- Failure rate: approximately 3.13%
- Average response time: approximately 491.82 ms
- Median response time: 13 ms
- 95th percentile response time: 60 ms
- Maximum response time: approximately 20.01 seconds
- Throughput: approximately 1.97 requests/second

The failure CSV records HTTP 500 responses, including product-page requests and requests to `/cart` and `/setCurrency`.

The experiment coincided with the service scale-down and restoration. Exact timestamp correlation is still needed to attribute every failure conclusively to the injection window.

## Recovery
Kubernetes successfully restored the `productcatalogservice` deployment. The application pods were subsequently observed in the `Running` and ready state.

## Results Files
All generated Locust CSV files and the chart are stored in `results/`.

Key files:
- `results/baseline2_stats.csv`
- `results/baseline2_stats_history.csv`
- `results/chaos2_stats.csv`
- `results/chaos2_stats_history.csv`
- `results/chaos2_failures.csv`
- `results/chaos2_timeline.png`

## Limitations
- This is a local Kind cluster, not a production Kubernetes environment.
- The baseline and failure experiment may differ in timing and request distribution.
- The failure-injection window has not yet been correlated with individual request timestamps.
- Results describe this test configuration and should not be generalized to production workloads.

## Reproduction

1. Deploy Online Boutique to a Kind cluster.
2. Forward the frontend service to localhost port 8080.
3. Activate the Python virtual environment.
4. Run the baseline test:

   `python -m locust -f src/loadgenerator/locustfile.py --host http://localhost:8080 --headless --users 10 --spawn-rate 1 --run-time 3m --csv results/baseline2 --csv-full-history`

5. Run the failure experiment by starting a similar Locust test and scaling `productcatalogservice` down and back up during the test.
6. Inspect the resulting CSV files and generate the timeline chart with `python create_chart.py`.

## Conclusion
The baseline test completed without recorded failures. During the service disruption experiment, Locust recorded HTTP 500 responses and substantial latency spikes. Kubernetes subsequently restored the service. More precise timestamp correlation and repeated trials would strengthen the conclusions.
