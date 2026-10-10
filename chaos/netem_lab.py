#!/usr/bin/env python3
"""
Network-latency validation on DISPOSABLE test pods (no application pod is touched).

    python chaos\\netem_lab.py --delay-ms 200 --duration 30 --samples 15

Steps (each logged to results/runs/<ts>_network_latency_lab/events.jsonl):
  1. validate the manifest (kubectl apply --dry-run=server), then create chaos-lab-server +
     chaos-lab-client (chaos/latency-lab.yaml) and wait until Ready
  2. BEFORE  : client measures the server with curl (HTTP 200 + timings) and ping
  3. INJECT  : `tc qdisc add dev eth0 root netem delay <ms>` on the server only; verified with
               `tc qdisc show`; a server-side timer removes it after --duration + 10 s regardless
  4. DURING  : same measurements
  5. REMOVE  : `tc qdisc del`; verified that no netem qdisc remains
  6. AFTER   : same measurements
  7. verdict : PASS only if the delay is visible during, gone after, and removal is verified
  8. finally : remove netem (if still present) and delete ONLY the two lab pods
Safety: fixed context/namespace, target allowlist {chaos-lab-server}, delay 20..500 ms,
duration 5..60 s, kill switch (chaos/KILL) checked between steps, Ctrl+C runs cleanup.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import controller as C  # noqa: E402

SERVER, CLIENT = "chaos-lab-server", "chaos-lab-client"
LAB_TARGETS = frozenset({SERVER})          # the only pod netem may be applied to
MANIFEST = HERE / "latency-lab.yaml"
PING_RE = re.compile(r"(?:rtt|round-trip) min/avg/max(?:/mdev)? = ([\d.]+)/([\d.]+)/([\d.]+)")


class Abort(Exception):
    pass


def k(*args, timeout=60, check=True):
    r = C.kubectl(*args, timeout=timeout)
    if check and r.returncode != 0:
        raise Abort(f"kubectl {' '.join(args[:3])} failed (rc={r.returncode}): {(r.stderr or r.stdout).strip()[:300]}")
    return r


def kill_check():
    if C.KILL_FILE.exists():
        raise Abort("kill switch chaos/KILL present")


def qdisc(pod: str) -> str:
    if pod not in LAB_TARGETS:
        raise Abort(f"refusing: {pod} is not an allowlisted lab target")
    return k("exec", pod, "--", "tc", "qdisc", "show", "dev", "eth0").stdout.strip()


def measure(ip: str, samples: int, phase: str) -> dict:
    """curl timings and ping RTT from the client to the server. Every curl line carries its exit code."""
    script = (f'for i in $(seq {samples}); do '
              f'out=$(curl -sS -m 5 -o /dev/null -w "%{{http_code}} %{{time_connect}} %{{time_total}}" '
              f'http://{ip}:8080/ 2>&1); echo "CURL rc=$? $out"; done; '
              f'ping -c {samples} -i 0.2 -W 2 {ip} 2>&1 | tail -2')
    r = k("exec", CLIENT, "--", "sh", "-c", script, timeout=samples * 6 + 30)
    rows, errors = [], []
    for line in r.stdout.splitlines():
        if not line.startswith("CURL "):
            continue
        m = re.match(r"CURL rc=(\d+) (\d{3}) ([\d.]+) ([\d.]+)$", line.strip())
        if m and m.group(1) == "0" and m.group(2) == "200":
            rows.append({"phase": phase, "http_code": 200, "connect_ms": round(float(m.group(3)) * 1000, 2),
                         "total_ms": round(float(m.group(4)) * 1000, 2)})
        else:
            errors.append(line.strip()[:200])
    pm = PING_RE.search(r.stdout)
    tot = sorted(x["total_ms"] for x in rows)
    return {"phase": phase, "requests": samples, "ok": len(rows), "errors": errors,
            "http_total_ms_median": round(statistics.median(tot), 2) if tot else None,
            "http_total_ms_p95": tot[max(0, int(round(0.95 * len(tot))) - 1)] if tot else None,
            "http_connect_ms_median": round(statistics.median(x["connect_ms"] for x in rows), 2) if rows else None,
            "ping_avg_ms": float(pm.group(2)) if pm else None,
            "ping_min_ms": float(pm.group(1)) if pm else None,
            "ping_raw": None if pm else r.stdout.strip()[-200:],
            "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--delay-ms", type=int, default=200)
    ap.add_argument("--duration", type=int, default=30, help="max seconds the delay may stay (5..60)")
    ap.add_argument("--samples", type=int, default=15, help="curl requests and pings per phase (5..50)")
    ap.add_argument("--ready-timeout", type=int, default=600, help="seconds to wait for lab pods (image pull)")
    ap.add_argument("--keep-pods", action="store_true", help="do not delete the lab pods afterwards")
    a = ap.parse_args()
    errs = []
    if not (C.MIN_DELAY_MS <= a.delay_ms <= C.MAX_DELAY_MS):
        errs.append(f"delay-ms must be {C.MIN_DELAY_MS}..{C.MAX_DELAY_MS}")
    if not (5 <= a.duration <= 60):
        errs.append("duration must be 5..60")
    if not (5 <= a.samples <= 50):
        errs.append("samples must be 5..50")
    if a.samples * (0.2 + 2 * a.delay_ms / 1000) > a.duration:
        errs.append(f"{a.samples} samples at {a.delay_ms} ms do not fit in {a.duration} s; raise --duration or lower --samples")
    if errs:
        print("REJECTED:", *errs, sep="\n  - ")
        return 4

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_network_latency_lab"
    run_dir = C.RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log = C.EventLog(run_dir / "events.jsonl")
    summary = {"run_id": run_id, "kind": "network_latency_lab", "context": C.CONTEXT, "namespace": C.NAMESPACE,
               "target": SERVER, "mechanism": "tc qdisc netem delay on the server pod's eth0 (NET_ADMIN)",
               "request": vars(a), "phases": {}}
    created = injected = False
    removed_verified = None
    status = "FAILED"
    try:
        kill_check()
        existing = k("get", "pods", "-l", "app.kubernetes.io/part-of=chaos-lab", "-o", "name").stdout.split()
        if existing:
            raise Abort(f"lab pods already exist {existing}; delete them first: "
                        f"kubectl --context {C.CONTEXT} -n {C.NAMESPACE} delete pod {SERVER} {CLIENT}")
        k("apply", "--dry-run=server", "-f", str(MANIFEST))   # validate against the API; creates nothing
        log("manifest_validated", manifest=str(MANIFEST))
        k("apply", "-f", str(MANIFEST))
        created = True
        log("lab_created", pods=[SERVER, CLIENT])
        for pod in (SERVER, CLIENT):
            k("wait", "--for=condition=Ready", f"pod/{pod}", f"--timeout={a.ready_timeout}s", timeout=a.ready_timeout + 30)
        ip = k("get", "pod", SERVER, "-o", "jsonpath={.status.podIP}").stdout.strip()
        if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):
            raise Abort(f"unexpected server pod IP '{ip}'")
        q = qdisc(SERVER)
        if "netem" in q:
            raise Abort(f"server already has a netem qdisc before the test: {q}")
        log("lab_ready", server_ip=ip, qdisc_before=q)

        kill_check()
        before = measure(ip, a.samples, "before")
        log("measured", **{k_: v for k_, v in before.items() if k_ != "rows"})

        kill_check()
        t_inject = time.time()
        k("exec", SERVER, "--", "sh", "-c",
          f"tc qdisc add dev eth0 root netem delay {a.delay_ms}ms && "
          f"(nohup sh -c 'sleep {a.duration + 10}; tc qdisc del dev eth0 root' >/dev/null 2>&1 &)")
        injected = True
        q = qdisc(SERVER)
        if "netem" not in q or f"delay {a.delay_ms}" not in q.replace(".0ms", "ms"):
            raise Abort(f"netem not confirmed after add: {q}")
        log("fault_injected", qdisc=q, delay_ms=a.delay_ms, self_removal_after_s=a.duration + 10)

        during = measure(ip, a.samples, "during")
        log("measured", **{k_: v for k_, v in during.items() if k_ != "rows"})
        if time.time() - t_inject > a.duration:
            log("warning", detail="during-phase measurement exceeded --duration; self-removal timer may have fired")

        k("exec", SERVER, "--", "tc", "qdisc", "del", "dev", "eth0", "root")
        q = qdisc(SERVER)
        removed_verified = "netem" not in q
        log("fault_removed", qdisc_after=q, verified=removed_verified, fault_duration_s=round(time.time() - t_inject, 2))
        if not removed_verified:
            raise Abort(f"netem still present after removal: {q}")
        injected = False

        after = measure(ip, a.samples, "after")
        log("measured", **{k_: v for k_, v in after.items() if k_ != "rows"})

        # ---- verdict
        def d(x, y):
            return None if x is None or y is None else round(x - y, 2)
        checks = {
            "all_requests_ok_before_and_after": before["ok"] == a.samples and after["ok"] == a.samples,
            "ping_increase_during_ms": d(during["ping_avg_ms"], before["ping_avg_ms"]),
            "http_increase_during_ms": d(during["http_total_ms_median"], before["http_total_ms_median"]),
            "ping_after_minus_before_ms": d(after["ping_avg_ms"], before["ping_avg_ms"]),
            "netem_removed_verified": removed_verified,
        }
        need = 0.8 * a.delay_ms
        visible = ((checks["ping_increase_during_ms"] or 0) >= need) or ((checks["http_increase_during_ms"] or 0) >= need)
        gone = (checks["ping_after_minus_before_ms"] is not None
                and checks["ping_after_minus_before_ms"] <= max(5.0, 0.5 * (before["ping_avg_ms"] or 0)))
        checks.update(delay_visible_during=visible, delay_gone_after=gone)
        status = "PASS" if (visible and gone and removed_verified and checks["all_requests_ok_before_and_after"]) else "FAIL"
        summary.update(phases={p["phase"]: {k_: v for k_, v in p.items() if k_ != "rows"} for p in (before, during, after)},
                       checks=checks, pass_criteria=f"increase >= 0.8 x {a.delay_ms} ms during (ping or HTTP median); "
                                                    "ping back within max(5 ms, 50%) of before; all requests OK "
                                                    "before/after; netem removal verified")
        with (run_dir / "measurements.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["phase", "http_code", "connect_ms", "total_ms"])
            w.writeheader()
            for p in (before, during, after):
                w.writerows(p["rows"])
        log("verdict", status=status, **checks)
    except KeyboardInterrupt:
        status = "ABORTED_CTRL_C"
        log("aborted", reason="Ctrl+C")
    except Abort as e:
        status = "ABORTED" if "kill switch" in str(e) else "FAILED"
        log("aborted", reason=str(e))
    except Exception as e:  # noqa: BLE001
        status = "FAILED"
        log("error", error=repr(e))
    finally:
        cleanup = []
        if injected:
            r = C.kubectl("exec", SERVER, "--", "tc", "qdisc", "del", "dev", "eth0", "root", timeout=30)
            cleanup.append(f"netem del rc={r.returncode}")
            try:
                removed_verified = "netem" not in qdisc(SERVER)
            except Exception as e:  # noqa: BLE001
                cleanup.append(f"verify failed: {e}")
        if created and not a.keep_pods:
            r = C.kubectl("delete", "pod", SERVER, CLIENT, "--wait=false", timeout=60)
            cleanup.append(f"lab pods deleted rc={r.returncode}")
        if injected and not removed_verified:
            status = "CLEANUP_FAILED"
            cleanup.append("netem may remain on the LAB server only; it self-removes after the timer, and the pod "
                           "self-terminates after 30 min")
        log("cleanup", steps=cleanup)
        summary.update(status=status, cleanup=cleanup, finished=C.utc_now())
        (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"\nRESULT: {status}   evidence: {run_dir}")
        for p in ("before", "during", "after"):
            ph = summary["phases"].get(p)
            if ph:
                print(f"  {p:6}: HTTP median {ph['http_total_ms_median']} ms (p95 {ph['http_total_ms_p95']}), "
                      f"ping avg {ph['ping_avg_ms']} ms, ok {ph['ok']}/{ph['requests']}")
    return 0 if status == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
