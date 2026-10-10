"""Controller safety paths, run as a real subprocess against a fake kubectl and a fake shop."""
import os
import sys
import time

from conftest import DATA, calls, cluster, controller, last_summary, reset_cluster

import executor as X


def wait_for_event(name, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        for ev in (DATA / "results" / "runs").glob("2*/events.jsonl"):
            if f'"event": "{name}"' in ev.read_text(encoding="utf-8"):
                return True
        time.sleep(0.2)
    raise AssertionError(f"event {name} not seen")


def writes():
    return [c for c in calls() if c[4] in ("scale", "delete", "debug", "exec")]


def test_unknown_target_rejected_before_any_cluster_call(shop):
    r = controller(shop.url, "--target", "frontend", "--duration", "5")
    assert r.returncode == 4 and "REJECTED" in r.stdout
    assert calls() == []


def test_invalid_baseline_blocks_injection(shop):
    shop.always_500 = True
    r = controller(shop.url, "--target", "cartservice", "--duration", "5")
    s = last_summary()
    assert s["status"] == "PRECHECK_FAILED" and "baseline unhealthy" in open(
        DATA / "results" / "runs" / s["run_id"] / "events.jsonl", encoding="utf-8").read()
    assert writes() == [] and cluster()["deployments"]["cartservice"]["replicas"] == 1


def test_threshold_abort_restores(shop):
    controller(shop.url, "--target", "productcatalogservice", "--duration", "30")
    s = last_summary()
    assert s["status"] == "ABORTED_THRESHOLD" and s["cleanup"] == "ok"
    assert s["fault_duration_s"] < 10
    assert cluster()["deployments"]["productcatalogservice"]["replicas"] == 1
    assert s["recovery"]["verified"] is True
    assert not (DATA / "chaos" / "state" / "active_fault.json").exists()


def test_kill_switch(shop):
    p = controller(shop.url, "--target", "adservice", "--duration", "30", background=True)
    wait_for_event("fault_injected")
    (DATA / "chaos").mkdir(exist_ok=True)
    (DATA / "chaos" / "KILL").write_text("")
    p.wait(timeout=60)
    (DATA / "chaos" / "KILL").unlink()
    s = last_summary()
    assert s["status"] == "ABORTED_KILL_SWITCH" and s["cleanup"] == "ok"
    assert cluster()["deployments"]["adservice"]["replicas"] == 1


def test_telemetry_loss_aborts_and_restores(shop):
    p = controller(shop.url, "--target", "adservice", "--duration", "30", background=True)
    wait_for_event("fault_injected")
    shop.shutdown(); shop.server_close()
    p.wait(timeout=90)
    s = last_summary()
    assert s["status"].startswith("ABORTED_TELEMETRY_LOST") and s["cleanup"] == "ok", s["status"]
    assert s["fault_duration_s"] < 15                         # stopped early, not at the 30 s cap
    assert cluster()["deployments"]["adservice"]["replicas"] == 1


def test_executor_failure_on_injection_still_runs_cleanup(shop):
    os.environ["FAKE_FAIL_INJECT"] = "1"
    controller(shop.url, "--target", "cartservice", "--duration", "10")
    s = last_summary()
    assert s["status"] == "ABORTED_ERROR" and s["cleanup"] == "ok"
    assert cluster()["deployments"]["cartservice"]["replicas"] == 1


def test_restores_original_replica_count_not_one(shop):
    reset_cluster({"cartservice": 2})
    controller(shop.url, "--target", "cartservice", "--duration", "3")
    assert cluster()["deployments"]["cartservice"]["replicas"] == 2
    assert ["--context", "kind-boutique-chaos", "-n", "boutique", "scale", "deployment/cartservice", "--replicas=2"] in calls()


def test_cleanup_failure_is_reported_and_restore_py_recovers(shop):
    os.environ["FAKE_FAIL_RESTORE"] = "1"
    controller(shop.url, "--target", "cartservice", "--duration", "2")
    s = last_summary()
    assert s["status"] == "CLEANUP_FAILED" and s["cleanup"] == "FAILED"
    assert (DATA / "chaos" / "state" / "active_fault.json").exists()      # evidence kept for restore.py
    os.environ.pop("FAKE_FAIL_RESTORE")
    import subprocess
    from conftest import ROOT
    r = subprocess.run([sys.executable, str(ROOT / "chaos" / "restore.py")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0
    assert cluster()["deployments"]["cartservice"]["replicas"] == 1
    assert not (DATA / "chaos" / "state" / "active_fault.json").exists()


def test_recovery_unverified_is_reported(shop):
    os.environ["FAKE_NEVER_READY"] = "1"
    controller(shop.url, "--target", "productcatalogservice", "--duration", "10", "--recovery-timeout", "4")
    s = last_summary()
    assert "RECOVERY_UNVERIFIED" in s["status"] and s["recovery"]["verified"] is False


def test_watchdog_kills_hung_controller_and_restores(shop):
    prop = {"target": "adservice", "fault_type": "scale_to_zero", "duration_seconds": 30,
            "abort_conditions": {"max_error_rate": 0.2, "max_p95_ms": 1000}, "parameters": {}, "experiment_id": "T"}
    r = X.run(prop, watchdog_s=6, url=shop.url, baseline=1.5, post=0.5, recovery_timeout=8)
    assert r["watchdog_fired"] is True
    assert "Restoring adservice" in r["watchdog_restore_output"]
    assert cluster()["deployments"]["adservice"]["replicas"] == 1
    assert not (DATA / "chaos" / "state" / "active_fault.json").exists()


def test_network_latency_on_application_pods_is_disabled(shop):
    r = controller(shop.url, "--target", "productcatalogservice", "--fault", "network_latency", "--delay-ms", "200",
                   "--duration", "10")
    assert r.returncode == 4 and "disabled" in r.stdout and "netem_lab" in r.stdout
    assert calls() == []                                              # rejected before any cluster call


def test_network_latency_rejects_excessive_delay(shop):
    r = controller(shop.url, "--target", "productcatalogservice", "--fault", "network_latency", "--delay-ms", "5000")
    assert r.returncode == 4 and "delay-ms" in r.stdout and calls() == []


def test_traffic_spike_is_bounded_and_stopped(shop):
    r = controller(shop.url, "--target", "frontend", "--fault", "traffic_spike", "--spike-users", "5",
                   "--duration", "4", timeout=120)
    s = last_summary()
    assert s["status"] == "COMPLETED" and s["cleanup"] == "ok", r.stdout[-2000:]
    ev = open(DATA / "results" / "runs" / s["run_id"] / "events.jsonl", encoding="utf-8").read()
    assert "traffic spike stopped" in ev
    assert (DATA / "results" / "runs" / s["run_id"] / "spike_stats.csv").exists()
    assert writes() == []                                    # a spike changes no Kubernetes object


def test_traffic_spike_limits_enforced(shop):
    r = controller(shop.url, "--target", "frontend", "--fault", "traffic_spike", "--spike-users", "500")
    assert r.returncode == 4 and "spike-users" in r.stdout


def test_probe_timeouts_abort_even_when_window_is_sparse(shop):
    """A frontend that accepts connections but answers slower than the probe timeout leaves few samples
    in the window; the controller must still abort, and classify it as application impact."""
    p = controller(shop.url, "--target", "adservice", "--duration", "30", background=True)
    wait_for_event("fault_injected")
    shop.hang_s = 3                                        # > 2 s probe timeout -> every probe times out
    p.wait(timeout=90)
    s = last_summary()
    # connected-but-slow is application impact, not telemetry loss
    assert s["status"].startswith("ABORTED_THRESHOLD") and s["cleanup"] == "ok", s["status"]
    ev = open(DATA / "results" / "runs" / s["run_id"] / "events.jsonl", encoding="utf-8").read()
    assert '"consecutive_failed_probes"' in ev and '"telemetry_lost"' not in ev
    assert s["fault_duration_s"] < 15
    assert cluster()["deployments"]["adservice"]["replicas"] == 1


def test_probe_classifies_connect_failure_vs_slow_response():
    """Unit test for the Windows bug: connect failure -> conn_error; connected but slow -> timeout."""
    import socket as S
    import threading
    import controller as Ctl
    s = S.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    assert Ctl.probe_once("127.0.0.1", port, "/", 1.0) == (None, "conn_error")     # nothing listening
    # Windows-like: the connect gets no answer and hangs until the timeout (full listen backlog)
    full = S.socket(); full.bind(("127.0.0.1", 0)); full.listen(0)
    fillers = []
    for _ in range(4):
        c = S.socket(); c.setblocking(False)
        try:
            c.connect(full.getsockname())
        except (BlockingIOError, OSError):
            pass
        fillers.append(c)
    time.sleep(0.3)
    t = time.time()
    assert Ctl.probe_once("127.0.0.1", full.getsockname()[1], "/", 1.0) == (None, "conn_error")
    if sys.platform.startswith("linux"):       # Linux drops the SYN (hang); Windows refuses at once. Both = conn_error
        assert time.time() - t >= 0.9
    for c in fillers:
        c.close()
    full.close()
    srv = S.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(5)                      # accepts, never replies
    accepted = []
    threading.Thread(target=lambda: accepted.append(srv.accept()), daemon=True).start()
    assert Ctl.probe_once("127.0.0.1", srv.getsockname()[1], "/", 1.0) == (None, "timeout")
    srv.close()
