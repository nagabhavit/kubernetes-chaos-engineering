#!/usr/bin/env python3
"""Plot one controller run: probe latency (OK vs failed) and rolling error rate,
with markers for fault injection, restore, Kubernetes ready and user recovery.

Usage:  python chaos/plot_run.py results/runs/<run_id>
Writes: results/runs/<run_id>/timeline.png
"""
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def ts(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() if iso else None


def main():
    run = Path(sys.argv[1]) if len(sys.argv) > 1 else sorted(Path("results/runs").glob("2*"))[-1]
    s = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    rows = list(csv.DictReader((run / "probes.csv").open(encoding="utf-8")))
    t_inj, t_res = ts(s.get("fault_injected_at")), ts(s.get("restore_issued_at"))
    t0 = t_inj or float(rows[0]["unix_ts"])
    rel = [float(r["unix_ts"]) - t0 for r in rows]
    lat = [float(r["latency_ms"]) for r in rows]
    ok = [r["ok"] == "1" for r in rows]

    # rolling error rate over 5 s
    err_x, err_y = [], []
    for i, x in enumerate(rel):
        win = [ok[j] for j in range(i, -1, -1) if x - rel[j] <= 5.0]
        err_x.append(x)
        err_y.append(100 * (1 - sum(win) / len(win)))

    fig, (a1, a2) = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
    a1.scatter([x for x, k in zip(rel, ok) if k], [y for y, k in zip(lat, ok) if k], s=8, c="#2a7ab0", label="OK")
    a1.scatter([x for x, k in zip(rel, ok) if not k], [y for y, k in zip(lat, ok) if not k], s=14, c="#d6336c",
               marker="x", label="Failed (HTTP 5xx / timeout)")
    a1.set_ylabel("Probe latency (ms)")
    a1.legend(loc="upper right")
    a2.plot(err_x, err_y, c="#d6336c")
    a2.axhline(100 * s["request"]["max_error_rate"], ls=":", c="grey", label="abort threshold")
    a2.set_ylabel("Error rate, 5 s window (%)")
    a2.set_xlabel("Seconds since fault injection")
    a2.set_ylim(-5, 105)

    marks = []
    if t_inj:
        marks.append((0, "fault injected", "#d6336c"))
    if t_res:
        marks.append((t_res - t0, f"restore ({s['status'].split('+')[0].lower()})", "#2b8a3e"))
        rec = s.get("recovery", {})
        if rec.get("k8s_ready_s") is not None:
            marks.append((t_res - t0 + rec["k8s_ready_s"], "k8s ready", "#868e96"))
        if rec.get("recovered_at") and s.get("fault_window", {}).get("errors"):
            marks.append((ts(rec["recovered_at"]) - t0, "users recovered", "#1971c2"))
    for ax in (a1, a2):
        for x, label, col in marks:
            ax.axvline(x, c=col, ls="--", lw=1)
    for x, label, col in marks:
        a1.annotate(label, (x, a1.get_ylim()[1]), rotation=90, va="top", ha="right", fontsize=8, color=col)
    a2.legend(loc="upper right")
    fig.suptitle(f"{s['request']['target']} — {s['request']['fault']} — {s['status']}  ({s['run_id']})", fontsize=11)
    fig.tight_layout()
    out = run / "timeline.png"
    fig.savefig(out, dpi=140)
    print(f"Chart saved: {out}")


if __name__ == "__main__":
    main()
