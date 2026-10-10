#!/usr/bin/env python3
"""
Safe chaos controller for Online Boutique on the local kind cluster.

Runs ONE allowlisted, reversible fault with:
  * fixed context/namespace and a hard-coded target allowlist
  * a hard fault-duration cap (primary safety control)
  * threshold auto-abort on error rate / P95 from a live HTTP probe
  * kill switch: create the file chaos/KILL, or press Ctrl+C
  * guaranteed restore to the ORIGINAL replica count (try/finally, Ctrl+C ignored during cleanup)
  * crash-safe state file so chaos/restore.py can undo a fault if this process dies
  * recovery verification (rollout status + N consecutive successful probes)
  * UTF-8 evidence under results/runs/<run_id>/  (events.jsonl, probes.csv, summary.json)

No shell is used and nothing outside kind-boutique-chaos/boutique can be targeted.
Standard library only.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------- fixed safety config
CONTEXT = "kind-boutique-chaos"
NAMESPACE = "boutique"
# frontend is excluded because it carries the telemetry path; loadgenerator is not part of the app.
ALLOWED_TARGETS = frozenset({
    "adservice", "cartservice", "checkoutservice", "currencyservice", "emailservice",
    "paymentservice", "productcatalogservice", "recommendationservice", "redis-cart",
    "shippingservice",
})
FAULT_TYPES = frozenset({"scale_to_zero"})
MAX_DURATION_S = 60          # hard cap, not overridable from the CLI
MAX_LOAD_USERS = 50

REPO = Path(__file__).resolve().parent.parent
KILL_FILE = REPO / "chaos" / "KILL"
STATE_FILE = REPO / "chaos" / "state" / "active_fault.json"
RUNS_DIR = REPO / "results" / "runs"
PROBE_PATHS = ("/", "/product/OLJCESPC7Z", "/cart")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------- logging
class EventLog:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def __call__(self, event: str, **fields):
        rec = {"ts": utc_now(), "event": event, **fields}
        line = json.dumps(rec, default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        detail = " ".join(f"{k}={v}" for k, v in fields.items())
        print(f"[{rec['ts']}] {event} {detail}", flush=True)


# --------------------------------------------------------------------------- kubectl (no shell)
def kubectl(*args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    cmd = ["kubectl", "--context", CONTEXT, "-n", NAMESPACE, *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def get_deployment(name: str) -> dict:
    r = kubectl("get", "deployment", name, "-o", "json")
    if r.returncode != 0:
        raise RuntimeError(f"cannot read deployment {name}: {r.stderr.strip()}")
    return json.loads(r.stdout)


def unhealthy_deployments() -> list[str]:
    r = kubectl("get", "deployments", "-o", "json")
    if r.returncode != 0:
        raise RuntimeError(f"cannot list deployments: {r.stderr.strip()}")
    bad = []
    for d in json.loads(r.stdout)["items"]:
        want = d["spec"].get("replicas", 1) or 0
        ready = d.get("status", {}).get("readyReplicas", 0) or 0
        if want > 0 and ready < want:
            bad.append(f"{d['metadata']['name']} {ready}/{want}")
    return bad


def scale(name: str, replicas: int) -> subprocess.CompletedProcess:
    return kubectl("scale", f"deployment/{name}", f"--replicas={replicas}")


# --------------------------------------------------------------------------- HTTP probe (control signal)
class Prober(threading.Thread):
    def __init__(self, base_url: str, interval: float, timeout: float, csv_path: Path):
        super().__init__(daemon=True)
        self.base_url = base_url.rstrip("/")
        self.interval = interval
        self.timeout = timeout
        self.samples: deque = deque(maxlen=20000)  # (t, ok, latency_ms, kind, status)
        self.lock = threading.Lock()
        self.stop_evt = threading.Event()
        self.consecutive_conn_errors = 0
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.csv_file = csv_path.open("w", newline="", encoding="utf-8")
        self.writer = csv.writer(self.csv_file)
        self.writer.writerow(["unix_ts", "utc", "path", "status", "kind", "ok", "latency_ms"])

    def run(self):
        i = 0
        while not self.stop_evt.is_set():
            path = PROBE_PATHS[i % len(PROBE_PATHS)]
            i += 1
            t0 = time.time()
            status, kind = None, "ok"
            try:
                with self.opener.open(self.base_url + path, timeout=self.timeout) as resp:
                    resp.read()
                    status = resp.status
            except urllib.error.HTTPError as e:
                status, kind = e.code, "http_error"
            except urllib.error.URLError as e:
                kind = "timeout" if isinstance(e.reason, (socket.timeout, TimeoutError)) else "conn_error"
            except (socket.timeout, TimeoutError):
                kind = "timeout"
            except (ConnectionError, OSError):
                kind = "conn_error"
            latency = (time.time() - t0) * 1000
            ok = kind == "ok" and status is not None and 200 <= status < 400
            with self.lock:
                self.samples.append((t0, ok, latency, kind, status))
                self.consecutive_conn_errors = self.consecutive_conn_errors + 1 if kind == "conn_error" else 0
            self.writer.writerow([f"{t0:.3f}", iso(t0), path, status, kind, int(ok), f"{latency:.1f}"])
            self.csv_file.flush()
            self.stop_evt.wait(max(0.0, self.interval - (time.time() - t0)))
        self.csv_file.close()

    def window(self, seconds: float, since: float | None = None) -> dict:
        """Stats over the last `seconds` (or since `since`). conn_errors are telemetry loss, not app errors."""
        now = time.time()
        lo = since if since is not None else now - seconds
        with self.lock:
            s = [x for x in self.samples if x[0] >= lo and x[3] != "conn_error"]
            conn = self.consecutive_conn_errors
            last_t = self.samples[-1][0] if self.samples else None
        n = len(s)
        errors = sum(1 for x in s if not x[1])
        lat = sorted(x[2] for x in s)
        p95 = lat[max(0, math.ceil(0.95 * n) - 1)] if n else None
        return {"n": n, "errors": errors, "error_rate": (errors / n) if n else None,
                "p95_ms": round(p95, 1) if p95 is not None else None,
                "consecutive_conn_errors": conn, "last_sample_age_s": (now - last_t) if last_t else None}

    def samples_since(self, t: float) -> list:
        with self.lock:
            return [x for x in self.samples if x[0] >= t]


# --------------------------------------------------------------------------- validation
def validate(args) -> list[str]:
    errs = []
    if args.target not in ALLOWED_TARGETS:
        errs.append(f"target '{args.target}' not in allowlist {sorted(ALLOWED_TARGETS)}")
    if args.fault not in FAULT_TYPES:
        errs.append(f"fault '{args.fault}' not supported {sorted(FAULT_TYPES)}")
    if not (1 <= args.duration <= MAX_DURATION_S):
        errs.append(f"duration {args.duration}s outside 1..{MAX_DURATION_S}s")
    if not (0.0 < args.max_error_rate <= 1.0):
        errs.append("max-error-rate must be in (0, 1]")
    if args.max_p95_ms <= 0:
        errs.append("max-p95-ms must be > 0")
    if args.load_users and not (1 <= args.load_users <= MAX_LOAD_USERS):
        errs.append(f"load-users must be 1..{MAX_LOAD_USERS}")
    if not args.url.startswith(("http://localhost", "http://127.0.0.1")):
        errs.append("probe url must be localhost (sandbox only)")
    return errs


def record_rejection(args, errs):
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    with (RUNS_DIR / "rejected.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": utc_now(), "request": vars(args), "reasons": errs}) + "\n")
    print("REJECTED:", *errs, sep="\n  - ")


# --------------------------------------------------------------------------- main run
def sleep_watching(seconds: float):
    end = time.time() + seconds
    while time.time() < end:
        if KILL_FILE.exists():
            raise KeyboardInterrupt("kill file")
        time.sleep(0.25)


def restore(log: EventLog, target: str, replicas: int, attempts: int = 3) -> bool:
    for i in range(1, attempts + 1):
        try:
            r = scale(target, replicas)
            if r.returncode == 0:
                log("restore_ok", target=target, replicas=replicas, attempt=i)
                return True
            log("restore_failed", attempt=i, stderr=r.stderr.strip())
        except Exception as e:  # noqa: BLE001
            log("restore_failed", attempt=i, error=repr(e))
        time.sleep(1.0)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Run one safe, allowlisted chaos experiment.")
    ap.add_argument("--target", required=True)
    ap.add_argument("--fault", default="scale_to_zero")
    ap.add_argument("--duration", type=int, default=30, help=f"fault duration cap in seconds (max {MAX_DURATION_S})")
    ap.add_argument("--max-error-rate", type=float, default=0.20, help="abort if probe error rate in window exceeds this")
    ap.add_argument("--max-p95-ms", type=float, default=1000.0, help="abort if probe P95 in window exceeds this")
    ap.add_argument("--window", type=float, default=5.0, help="sliding window seconds for thresholds")
    ap.add_argument("--min-samples", type=int, default=5)
    ap.add_argument("--baseline", type=float, default=15.0, help="healthy observation seconds before injecting")
    ap.add_argument("--post", type=float, default=30.0, help="observation seconds after recovery")
    ap.add_argument("--recovery-timeout", type=float, default=120.0)
    ap.add_argument("--recovery-streak", type=int, default=5, help="consecutive OK probes that count as recovered")
    ap.add_argument("--probe-interval", type=float, default=0.25)
    ap.add_argument("--probe-timeout", type=float, default=2.0)
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--load-users", type=int, default=0, help="also run Locust with N users (0 = probe only)")
    ap.add_argument("--label", default="", help="free-text note stored in summary")
    args = ap.parse_args()

    errs = validate(args)
    if errs:
        record_rejection(args, errs)
        return 4

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"_{args.fault}_{args.target}"
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log = EventLog(run_dir / "events.jsonl")
    summary: dict = {"run_id": run_id, "request": vars(args), "context": CONTEXT, "namespace": NAMESPACE,
                     "max_duration_cap_s": MAX_DURATION_S}
    log("run_started", run_id=run_id, target=args.target, fault=args.fault, duration=args.duration)

    prober = None
    locust = None
    status = "PRECHECK_FAILED"
    fault_attempted = False
    original = None
    t_inject = t_restore = None
    cleanup = "not_needed"

    try:
        # ---------------- preflight
        if STATE_FILE.exists():
            raise RuntimeError(f"previous fault still recorded in {STATE_FILE}; run chaos/restore.py first")
        if KILL_FILE.exists():
            raise RuntimeError(f"kill switch {KILL_FILE} present; delete it to run")
        bad = unhealthy_deployments()
        if bad:
            raise RuntimeError(f"cluster not healthy: {bad}")
        dep = get_deployment(args.target)
        original = int(dep["spec"].get("replicas", 1))
        if original < 1:
            raise RuntimeError(f"{args.target} already at {original} replicas")
        summary["target_image"] = dep["spec"]["template"]["spec"]["containers"][0]["image"]
        summary["original_replicas"] = original
        try:
            summary["git_revision"] = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
                                                     capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:  # noqa: BLE001
            summary["git_revision"] = None
        log("preflight_ok", original_replicas=original, image=summary["target_image"])

        # ---------------- monitoring + optional load
        prober = Prober(args.url, args.probe_interval, args.probe_timeout, run_dir / "probes.csv")
        prober.start()
        if args.load_users:
            run_time = int(args.baseline + args.duration + args.recovery_timeout + args.post + 10)
            locust = subprocess.Popen([
                sys.executable, "-m", "locust", "-f", str(REPO / "src" / "loadgenerator" / "locustfile.py"),
                "--host", args.url, "--headless", "--users", str(args.load_users), "--spawn-rate", "2",
                "--run-time", f"{run_time}s", "--csv", str(run_dir / "locust"), "--csv-full-history",
                "--only-summary"], cwd=str(REPO))
            log("load_started", users=args.load_users, max_run_time_s=run_time)

        # ---------------- baseline gate
        t_base = time.time()
        sleep_watching(args.baseline)
        base = prober.window(args.baseline, since=t_base)
        summary["baseline_window"] = base
        log("baseline", **base)
        if base["n"] < args.min_samples or base["error_rate"] > 0.05 or base["consecutive_conn_errors"]:
            raise RuntimeError(f"baseline unhealthy, not injecting: {base}")

        # ---------------- inject
        status = "RUNNING"
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({"run_id": run_id, "target": args.target,
                                          "original_replicas": original, "written": utc_now()}), encoding="utf-8")
        fault_attempted = True
        t_inject = time.time()
        r = scale(args.target, 0)
        if r.returncode != 0:
            raise RuntimeError(f"injection failed: {r.stderr.strip()}")
        log("fault_injected", target=args.target, replicas=0, at=iso(t_inject))

        # ---------------- observe with guards
        deadline = t_inject + args.duration
        while True:
            now = time.time()
            if now >= deadline:
                status = "COMPLETED"
                log("duration_cap_reached", elapsed_s=round(now - t_inject, 2))
                break
            if KILL_FILE.exists():
                status = "ABORTED_KILL_SWITCH"
                log("kill_switch", source="file", path=str(KILL_FILE))
                break
            w = prober.window(args.window)
            if w["consecutive_conn_errors"] >= 3 or (w["last_sample_age_s"] or 0) > args.probe_timeout + 3:
                status = "ABORTED_TELEMETRY_LOST"
                log("telemetry_lost", **w)
                break
            if w["n"] >= args.min_samples:
                if w["error_rate"] > args.max_error_rate:
                    status = "ABORTED_THRESHOLD"
                    log("threshold_breach", metric="error_rate", value=round(w["error_rate"], 3),
                        limit=args.max_error_rate, **w)
                    break
                if w["p95_ms"] > args.max_p95_ms:
                    status = "ABORTED_THRESHOLD"
                    log("threshold_breach", metric="p95_ms", value=w["p95_ms"], limit=args.max_p95_ms, **w)
                    break
            time.sleep(0.2)

    except KeyboardInterrupt as e:
        status = "ABORTED_KILL_SWITCH" if fault_attempted else "CANCELLED"
        log("kill_switch", source=str(e) or "ctrl_c")
    except Exception as e:  # noqa: BLE001
        if fault_attempted:
            status = "ABORTED_ERROR"
        log("error", error=str(e))
    finally:
        if fault_attempted:
            prev = signal.signal(signal.SIGINT, signal.SIG_IGN)   # cleanup must not be interrupted
            try:
                t_restore = time.time()
                ok = restore(log, args.target, original)
                cleanup = "ok" if ok else "FAILED"
                if ok:
                    STATE_FILE.unlink(missing_ok=True)
                else:
                    status = "CLEANUP_FAILED"
                    log("cleanup_failed", hint="run: python chaos/restore.py")
            finally:
                signal.signal(signal.SIGINT, prev)

    # ---------------- recovery verification
    recovery = {"verified": False}
    if fault_attempted and cleanup == "ok":
        try:
            rs = kubectl("rollout", "status", f"deployment/{args.target}", f"--timeout={int(args.recovery_timeout)}s",
                         timeout=args.recovery_timeout + 15)
            t_k8s = time.time()
            recovery["k8s_rollout_ok"] = rs.returncode == 0
            recovery["k8s_ready_s"] = round(t_k8s - t_restore, 2) if rs.returncode == 0 else None
            log("rollout_status", ok=rs.returncode == 0, out=rs.stdout.strip() or rs.stderr.strip())
            # user-facing recovery: first probe of N consecutive OKs after restore
            end = t_restore + args.recovery_timeout
            first_ok = None
            while time.time() < end:
                s = prober.samples_since(t_restore)
                streak, start = 0, None
                for x in s:
                    if x[1]:
                        start = x[0] if streak == 0 else start
                        streak += 1
                        if streak >= args.recovery_streak:
                            first_ok = start
                            break
                    else:
                        streak = 0
                if first_ok:
                    break
                time.sleep(0.5)
            if first_ok:
                recovery.update(verified=True, user_recovery_s=round(first_ok - t_restore, 2), recovered_at=iso(first_ok))
                log("recovered", user_recovery_s=recovery["user_recovery_s"])
            else:
                log("recovery_unverified", timeout_s=args.recovery_timeout)
        except Exception as e:  # noqa: BLE001
            log("recovery_check_error", error=repr(e))
        if not recovery["verified"] and status != "CLEANUP_FAILED":
            status += "+RECOVERY_UNVERIFIED"

    # ---------------- post window
    if prober and fault_attempted and recovery.get("verified"):
        t_post = time.time()
        try:
            time.sleep(args.post)   # fault already removed; kill file no longer relevant here
        except KeyboardInterrupt:
            log("post_window_cut_short")
        summary["post_window"] = prober.window(args.post, since=t_post)
        log("post_window", **summary["post_window"])

    if prober:
        if t_inject:
            # fault window = injection -> restore command (excludes recovery period)
            fw =[x for x in prober.samples_since(t_inject) if x[0] < (t_restore or time.time()) and x[3] != "conn_error"]
            n = len(fw)
            err = sum(1 for x in fw if not x[1])
            lat = sorted(x[2] for x in fw)
            summary["fault_window"] = {"n": n, "errors": err, "error_rate": (err / n) if n else None,
                                       "p95_ms": round(lat[max(0, math.ceil(0.95 * n) - 1)], 1) if n else None}
            first_err = next((x[0] for x in fw if not x[1]), None)
            summary["first_error_after_inject_s"] = round(first_err - t_inject, 2) if first_err else None
        prober.stop_evt.set()
        prober.join(timeout=5)
    if locust:
        locust.terminate()
        try:
            locust.wait(timeout=15)
        except subprocess.TimeoutExpired:
            locust.kill()
        log("load_stopped")

    summary.update(status=status, cleanup=cleanup, fault_injected_at=iso(t_inject), restore_issued_at=iso(t_restore),
                   fault_duration_s=round(t_restore - t_inject, 2) if t_inject and t_restore else None,
                   recovery=recovery, finished=utc_now())
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    log("run_finished", status=status, cleanup=cleanup, summary=str(run_dir / "summary.json"))

    if status == "COMPLETED" and recovery.get("verified"):
        return 0
    if status.startswith("ABORTED") and recovery.get("verified"):
        return 2
    return 3


if __name__ == "__main__":
    sys.exit(main())
