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
import http.client
import json
import math
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
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
# scale_to_zero: deployment -> 0 replicas for the duration, then restored (service unavailability)
# pod_delete:    delete the target's pods once; Kubernetes recreates them (pod failure / crash)
# traffic_spike: sudden Locust load (spike_users) against the frontend for the duration, then stopped
# network_latency: netem delay on ONE target pod's interface, added by a temporary netshoot debug
#                  container that removes the delay itself after the duration (self-reverting)
FAULT_TYPES = frozenset({"scale_to_zero", "pod_delete", "traffic_spike", "network_latency"})
SPIKE_TARGET = "frontend"    # traffic_spike changes no Kubernetes object; it only adds HTTP load at the entry point
MAX_SPIKE_USERS = 150
MAX_DURATION_S = 60          # hard cap, not overridable from the CLI
MAX_LOAD_USERS = 50
MAX_CONSECUTIVE_FAILURES = 3  # abort after this many failed probes in a row, however sparse the window
MAX_SPAWN_RATE = 20          # users started per second for a spike (conservative)
MIN_DELAY_MS, MAX_DELAY_MS = 20, 500
NETEM_IMAGE = "nicolaka/netshoot:v0.13"
# network_latency on APPLICATION pods is disabled (empty allowlist) until it has been validated.
# The mechanism is validated on disposable pods with chaos/netem_lab.py instead.
NETEM_APP_TARGETS: frozenset = frozenset()

REPO = Path(__file__).resolve().parent.parent
# Tests relocate all mutable files (kill switch, state, run records) with CHAOS_DATA_DIR and
# replace kubectl with a fake via CHAOS_KUBECTL, so unit tests never touch the real cluster.
DATA = Path(os.getenv("CHAOS_DATA_DIR") or REPO)
KILL_FILE = DATA / "chaos" / "KILL"
STATE_FILE = DATA / "chaos" / "state" / "active_fault.json"
RUNS_DIR = DATA / "results" / "runs"
PROBE_PATHS = ("/", "/product/OLJCESPC7Z", "/cart")
LOCUSTFILE = os.getenv("CHAOS_LOCUSTFILE") or str(REPO / "src" / "loadgenerator" / "locustfile.py")


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
def _kubectl_base() -> list[str]:
    k = os.getenv("CHAOS_KUBECTL", "kubectl")
    return [sys.executable, k] if k.endswith(".py") else [k]


def kubectl(*args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    """Every cluster call goes through here: fixed context + namespace, timeout, no shell."""
    cmd = [*_kubectl_base(), "--context", CONTEXT, "-n", NAMESPACE, *args]
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


# --------------------------------------------------------------------------- network latency (netem)
def target_pod(target: str) -> str:
    r = kubectl("get", "pods", "-l", f"app={target}", "-o", "json")
    if r.returncode != 0:
        raise RuntimeError(f"cannot list pods for {target}: {r.stderr.strip()}")
    pods = [p["metadata"]["name"] for p in json.loads(r.stdout)["items"]
            if p.get("status", {}).get("phase") == "Running"]
    if not pods:
        raise RuntimeError(f"no running pod for {target}")
    return sorted(pods)[0]


def inject_latency(pod: str, container: str, delay_ms: int, duration_s: int) -> subprocess.CompletedProcess:
    """Ephemeral netshoot container in the pod's network namespace. Its script removes the delay
    itself after duration_s (+5 s grace), so the fault reverts even if this controller dies."""
    script = (f"tc qdisc add dev eth0 root netem delay {int(delay_ms)}ms && echo NETEM_ADDED; "
              f"sleep {int(duration_s) + 5}; tc qdisc del dev eth0 root 2>/dev/null; echo NETEM_REMOVED")
    return kubectl("debug", f"pod/{pod}", "--profile=netadmin", f"--image={NETEM_IMAGE}",
                   f"--container={container}", "--", "sh", "-c", script, timeout=60)


def remove_latency(pod: str, container: str) -> tuple[bool, str]:
    """Remove the netem qdisc now. If the debug container already exited, its script removed it."""
    r = kubectl("exec", pod, "-c", container, "--", "sh", "-c",
                "tc qdisc del dev eth0 root 2>/dev/null; tc qdisc show dev eth0", timeout=30)
    if r.returncode == 0:
        return "netem" not in r.stdout, r.stdout.strip()
    err = (r.stderr or "").lower()
    if "not found" in err or "not running" in err or "completed" in err or "terminated" in err:
        return True, "debug container already exited (its script removed the delay)"
    return False, r.stderr.strip()


# --------------------------------------------------------------------------- HTTP probe (control signal)
def probe_once(host: str, port: int, path: str, timeout: float) -> tuple[int | None, str]:
    """One GET, with the TCP connect and the HTTP exchange classified separately:
         conn_error : could not connect (frontend / port-forward unreachable)  -> telemetry loss
         timeout    : connected, but no response in time (application slow)    -> fault impact
         http_error : connected, HTTP status >= 400                             -> fault impact
    Windows does not refuse a closed localhost port instantly (the connect retries until the
    timeout), so connect failures must be detected at the connect step, not inferred from errors."""
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        try:
            conn.connect()
        except OSError:                       # refused, unreachable, or connect timed out
            return None, "conn_error"
        try:
            conn.request("GET", path, headers={"Connection": "close"})
            resp = conn.getresponse()
            resp.read()
        except (socket.timeout, TimeoutError):
            return None, "timeout"
        except (ConnectionError, http.client.HTTPException, OSError):
            return None, "conn_error"         # connection dropped mid-exchange (e.g. port-forward died)
        return resp.status, ("ok" if resp.status < 400 else "http_error")
    finally:
        conn.close()


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
        self.consecutive_failures = 0      # any non-OK probe: HTTP error, timeout or connection error
        u = urlsplit(self.base_url)
        self.host, self.port = u.hostname, u.port or 80
        self.csv_file = csv_path.open("w", newline="", encoding="utf-8")
        self.writer = csv.writer(self.csv_file)
        self.writer.writerow(["unix_ts", "utc", "path", "status", "kind", "ok", "latency_ms"])

    def run(self):
        i = 0
        while not self.stop_evt.is_set():
            path = PROBE_PATHS[i % len(PROBE_PATHS)]
            i += 1
            t0 = time.time()
            status, kind = probe_once(self.host, self.port, path, self.timeout)
            latency = (time.time() - t0) * 1000
            ok = kind == "ok" and status is not None and 200 <= status < 400
            with self.lock:
                self.samples.append((t0, ok, latency, kind, status))
                self.consecutive_conn_errors = self.consecutive_conn_errors + 1 if kind == "conn_error" else 0
                self.consecutive_failures = 0 if ok else self.consecutive_failures + 1
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
            fails = self.consecutive_failures
            last_t = self.samples[-1][0] if self.samples else None
        n = len(s)
        errors = sum(1 for x in s if not x[1])
        lat = sorted(x[2] for x in s)
        p95 = lat[max(0, math.ceil(0.95 * n) - 1)] if n else None
        return {"n": n, "errors": errors, "error_rate": (errors / n) if n else None,
                "p95_ms": round(p95, 1) if p95 is not None else None,
                "consecutive_conn_errors": conn, "consecutive_failures": fails,
                "last_sample_age_s": (now - last_t) if last_t else None}

    def samples_since(self, t: float) -> list:
        with self.lock:
            return [x for x in self.samples if x[0] >= t]


# --------------------------------------------------------------------------- validation
def validate(args) -> list[str]:
    errs = []
    if args.fault == "traffic_spike":
        if args.target != SPIKE_TARGET:
            errs.append(f"traffic_spike target must be '{SPIKE_TARGET}'")
        if not (1 <= args.spike_users <= MAX_SPIKE_USERS):
            errs.append(f"spike-users must be 1..{MAX_SPIKE_USERS}")
    elif args.target not in ALLOWED_TARGETS:
        errs.append(f"target '{args.target}' not in allowlist {sorted(ALLOWED_TARGETS)}")
    if args.fault not in FAULT_TYPES:
        errs.append(f"fault '{args.fault}' not supported {sorted(FAULT_TYPES)}")
    if args.fault == "network_latency" and args.target not in NETEM_APP_TARGETS:
        errs.append("network_latency on application pods is disabled; validate the mechanism on disposable "
                    "pods with: python chaos\\netem_lab.py")
    if args.fault == "network_latency" and not (MIN_DELAY_MS <= args.delay_ms <= MAX_DELAY_MS):
        errs.append(f"delay-ms must be {MIN_DELAY_MS}..{MAX_DELAY_MS}")
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


def capture_events(target: str, since: float, path: Path) -> int:
    """Save Kubernetes events for the target with ABSOLUTE timestamps (not relative ages)."""
    r = kubectl("get", "events", "-o", "json", timeout=30)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    out = []
    for e in json.loads(r.stdout).get("items", []):
        name = (e.get("involvedObject") or {}).get("name", "")
        ts = e.get("lastTimestamp") or e.get("eventTime") or e.get("firstTimestamp") or ""
        if not name.startswith(target) or not ts:
            continue
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if t >= since - 5:
            out.append({"time": ts, "type": e.get("type"), "reason": e.get("reason"),
                        "object": f"{(e.get('involvedObject') or {}).get('kind')}/{name}",
                        "message": (e.get("message") or "")[:300], "count": e.get("count")})
    out.sort(key=lambda x: x["time"])
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return len(out)


def stop_latency(log: EventLog, netem) -> bool:
    if netem is None:
        log("restore_ok", action="latency was never injected")
        return True
    pod, container = netem
    for i in range(1, 4):
        try:
            ok, detail = remove_latency(pod, container)
        except Exception as e:  # noqa: BLE001
            ok, detail = False, repr(e)
        if ok:
            log("restore_ok", action="netem delay removed", pod=pod, detail=detail, attempt=i)
            return True
        log("restore_failed", attempt=i, detail=detail)
        time.sleep(1.0)
    # No pod-delete fallback: never delete an application pod. The debug container's own timer
    # removes the delay; the state file is kept so restore.py / a human can verify it.
    return False


def stop_spike(log: EventLog, proc) -> bool:
    if proc is None:
        log("restore_ok", action="no spike process was started")
        return True
    try:
        proc.terminate()
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    except Exception as e:  # noqa: BLE001
        log("restore_failed", error=repr(e))
        return False
    log("restore_ok", action="traffic spike stopped", locust_exit=proc.returncode)
    return True


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
    ap.add_argument("--spike-users", type=int, default=80, help=f"traffic_spike: Locust users (max {MAX_SPIKE_USERS})")
    ap.add_argument("--delay-ms", type=int, default=200, help=f"network_latency: delay {MIN_DELAY_MS}..{MAX_DELAY_MS} ms")
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
    spike = None
    netem = None   # (pod, container) while a latency fault may be active
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
                sys.executable, "-m", "locust", "-f", LOCUSTFILE,
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
        STATE_FILE.write_text(json.dumps({"run_id": run_id, "target": args.target, "fault": args.fault,
                                          "original_replicas": original, "written": utc_now()}), encoding="utf-8")
        fault_attempted = True
        t_inject = time.time()
        if args.fault == "traffic_spike":
            spike = subprocess.Popen([
                sys.executable, "-m", "locust", "-f", LOCUSTFILE,
                "--host", args.url, "--headless", "--users", str(args.spike_users),
                "--spawn-rate", str(min(args.spike_users, MAX_SPAWN_RATE)), "--run-time", f"{args.duration + 10}s",
                "--csv", str(run_dir / "spike"), "--csv-full-history", "--only-summary"],
                cwd=str(REPO), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            STATE_FILE.write_text(json.dumps({"run_id": run_id, "target": args.target, "fault": args.fault,
                                              "original_replicas": original, "spike_pid": spike.pid,
                                              "written": utc_now()}), encoding="utf-8")
            r = subprocess.CompletedProcess([], 0 if spike.poll() is None else 1, "", "locust exited immediately")
        elif args.fault == "network_latency":
            pod = target_pod(args.target)
            container = f"chaos-netem-{run_id[9:15]}"
            netem = (pod, container)
            STATE_FILE.write_text(json.dumps({"run_id": run_id, "target": args.target, "fault": args.fault,
                                              "original_replicas": original, "pod": pod, "container": container,
                                              "written": utc_now()}), encoding="utf-8")
            r = inject_latency(pod, container, args.delay_ms, args.duration)
        elif args.fault == "pod_delete":
            r = kubectl("delete", "pod", "-l", f"app={args.target}", "--wait=false")
        else:
            r = scale(args.target, 0)
        if r.returncode != 0:
            raise RuntimeError(f"injection failed: {r.stderr.strip()}")
        log("fault_injected", target=args.target, fault=args.fault, at=iso(t_inject),
            detail=(r.stdout.strip().replace("\n", "; ") if args.fault == "pod_delete" else
                    f"spike_users={args.spike_users}" if args.fault == "traffic_spike" else
                    f"delay_ms={args.delay_ms} pod={netem[0]} container={netem[1]}" if args.fault == "network_latency"
                    else "replicas=0"))

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
            if w["consecutive_failures"] >= MAX_CONSECUTIVE_FAILURES:
                # Does not depend on the window having min_samples: on Windows an unreachable frontend makes
                # every probe hang until its timeout, so the window can stay sparse for the whole fault.
                status = "ABORTED_THRESHOLD"
                log("threshold_breach", metric="consecutive_failed_probes", value=w["consecutive_failures"],
                    limit=MAX_CONSECUTIVE_FAILURES, **w)
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
                if args.fault == "traffic_spike":
                    ok = stop_spike(log, spike)
                elif args.fault == "network_latency":
                    ok = stop_latency(log, netem)
                else:
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
            # scale_to_zero: users recover after the restore.  pod_delete: Kubernetes self-heals,
            # so measure from injection and look for the first OK streak after the first error.
            ref = t_inject if args.fault == "pod_delete" else t_restore
            recovery["measured_from"] = "fault_injected" if args.fault == "pod_delete" else "restore_issued"
            end = time.time() + args.recovery_timeout
            first_ok = None
            while time.time() < end:
                search_from = t_restore
                if args.fault == "pod_delete":
                    first_err = next((x[0] for x in prober.samples_since(t_inject) if not x[1] and x[3] != "conn_error"), None)
                    if first_err is None:
                        break  # no user-facing errors observed
                    search_from = first_err
                s = prober.samples_since(search_from)
                streak, start = 0, None
                for x in s:
                    if x[1] and (args.fault not in ("traffic_spike", "network_latency") or x[2] <= args.max_p95_ms):
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
            if not first_ok and args.fault == "pod_delete" and recovery.get("k8s_rollout_ok") and \
                    not any(not x[1] and x[3] != "conn_error" for x in prober.samples_since(t_inject)):
                recovery.update(verified=True, user_recovery_s=None, note="no user-facing errors observed")
                log("recovered", note="no user-facing errors observed")
            elif first_ok and not recovery.get("k8s_rollout_ok"):
                # users get pages again, but Kubernetes does not report the deployment ready: not recovered
                recovery.update(user_recovery_s=round(first_ok - ref, 2), recovered_at=iso(first_ok),
                                note="user requests succeed but rollout status did not report ready")
                log("recovery_unverified", reason="rollout not ready", user_recovery_s=recovery["user_recovery_s"])
            elif first_ok:
                recovery.update(verified=True, user_recovery_s=round(first_ok - ref, 2), recovered_at=iso(first_ok))
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

    if t_inject:
        try:
            summary["k8s_events_captured"] = capture_events(args.target, t_inject, run_dir / "k8s_events.json")
        except Exception as e:  # noqa: BLE001  (evidence capture must never break cleanup reporting)
            log("k8s_events_capture_failed", error=repr(e))
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
