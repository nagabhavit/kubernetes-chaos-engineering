import csv
from datetime import datetime
import matplotlib.pyplot as plt

timestamps, p95, failures = [], [], []

with open("results/chaos2_stats_history.csv", newline="") as f:
    for row in csv.DictReader(f):
        if row["Type"] or row["Name"] != "Aggregated":
            continue
        if not row["Timestamp"]:
            continue

        timestamp = datetime.fromtimestamp(
            int(row["Timestamp"])
        ).strftime("%H:%M:%S")

        try:
            percentile = float(row["95%"])
        except (ValueError, TypeError):
            percentile = None

        try:
            failure_count = int(row["Total Failure Count"])
        except (ValueError, TypeError):
            failure_count = None

        if percentile is not None:
            timestamps.append(timestamp)
            p95.append(percentile)

        if failure_count is not None:
            failures.append((timestamp, failure_count))

if not timestamps:
    raise SystemExit("No valid P95 data found in the CSV.")

fig, axes = plt.subplots(2, 1, figsize=(12, 8))

axes[0].plot(timestamps, p95, marker=".")
axes[0].set_title("Chaos Test: P95 Response Time")
axes[0].set_ylabel("Milliseconds")
axes[0].grid(True)

if failures:
    axes[1].plot(
        [x[0] for x in failures],
        [x[1] for x in failures],
        marker="."
    )

axes[1].set_title("Chaos Test: Cumulative Failed Requests")
axes[1].set_ylabel("Failed requests")
axes[1].set_xlabel("Time")
axes[1].grid(True)

for ax in axes:
    ax.tick_params(axis="x", labelrotation=45)

plt.tight_layout()
plt.savefig("results/chaos2_timeline.png", dpi=150)
print("Chart saved: results/chaos2_timeline.png")
plt.show()
