"""
Executor: turns ONE validated proposal into a controller.py invocation and supervises it.

  * The command is built from a fixed argument list; no shell, no model-written strings.
  * Independent watchdog: if the controller exceeds its maximum possible lifetime it is killed
    and chaos/restore.py is run from the crash-safe state file.
  * Dry run: builds and returns the command without touching the cluster.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import controller as C  # noqa: E402

BASELINE_S, POST_S, RECOVERY_S = 15, 30, 120


def build_cmd(p: dict, label: str = "", baseline: float = BASELINE_S, post: float = POST_S,
              recovery_timeout: float = RECOVERY_S, url: str = "http://localhost:8080") -> list[str]:
    return [sys.executable, "-u", str(HERE / "controller.py"),
            "--target", str(p["target"]), "--fault", str(p["fault_type"]),
            "--duration", str(int(p["duration_seconds"])),
            "--baseline", str(baseline), "--post", str(post), "--recovery-timeout", str(recovery_timeout),
            "--url", url, "--label", (label or p.get("experiment_id", ""))[:200],
            *controller_extra_args(p)]


def controller_extra_args(p: dict) -> list[str]:
    """Fault-specific and threshold flags for a validated proposal, built explicitly (not sliced from
    another command). Used by the API's planner path so every supported parameter is forwarded."""
    ac = p.get("abort_conditions") or {}
    params = p.get("parameters") or {}
    out = ["--max-error-rate", str(float(ac.get("max_error_rate", 0.2))),
           "--max-p95-ms", str(int(ac.get("max_p95_ms", 1000)))]
    if p["fault_type"] == "traffic_spike":
        out += ["--spike-users", str(int(params.get("spike_users", 80)))]
    if p["fault_type"] == "network_latency":
        out += ["--delay-ms", str(int(params.get("delay_ms", 200)))]
    return out


def max_lifetime(p: dict, baseline=BASELINE_S, post=POST_S, recovery=RECOVERY_S) -> float:
    """Upper bound on a healthy controller run; the watchdog fires after this."""
    return baseline + int(p["duration_seconds"]) + recovery + post + 90


def run(p: dict, label: str = "", dry_run: bool = False, watchdog_s: float | None = None,
        on_line=None, **kw) -> dict:
    cmd = build_cmd(p, label, **kw)
    if dry_run:
        return {"dry_run": True, "command": cmd, "exit_code": None, "run_id": None, "watchdog_fired": False}
    limit = watchdog_s or max_lifetime(p, kw.get("baseline", BASELINE_S), kw.get("post", POST_S),
                                       kw.get("recovery_timeout", RECOVERY_S))
    proc = subprocess.Popen(cmd, cwd=str(C.REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", env=os.environ.copy())
    lines, run_id, start = [], None, time.time()
    import threading

    def pump():
        nonlocal run_id
        for line in proc.stdout:
            line = line.rstrip()
            lines.append(line)
            if on_line:
                on_line(line)
            if "run_started run_id=" in line and not run_id:
                run_id = line.split("run_id=")[1].split()[0]

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    fired, restore_out = False, None
    try:
        proc.wait(timeout=limit)
    except subprocess.TimeoutExpired:
        fired = True
        proc.kill()
        proc.wait(timeout=10)
        r = subprocess.run([sys.executable, str(HERE / "restore.py")], capture_output=True, text=True,
                           timeout=180, env=os.environ.copy())
        restore_out = (r.stdout + r.stderr).strip()[-1000:]
    t.join(timeout=5)
    summary = None
    if run_id and (C.RUNS_DIR / run_id / "summary.json").exists():
        summary = json.loads((C.RUNS_DIR / run_id / "summary.json").read_text(encoding="utf-8"))
    return {"dry_run": False, "command": cmd, "exit_code": proc.returncode, "run_id": run_id,
            "summary": summary, "watchdog_fired": fired, "watchdog_limit_s": limit,
            "watchdog_restore_output": restore_out, "elapsed_s": round(time.time() - start, 1),
            "console_tail": lines[-25:], "rejected": proc.returncode == 4}
