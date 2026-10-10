"""Network-latency lab on disposable pods (fake kubectl): measurements, removal verification, cleanup paths."""
import csv
import json
import os
import subprocess
import sys

from conftest import DATA, ROOT, calls, cluster

LAB = [sys.executable, str(ROOT / "chaos" / "netem_lab.py")]


def lab(*args):
    return subprocess.run([*LAB, *args], capture_output=True, text=True, timeout=120)


def summary():
    run = sorted((DATA / "results" / "runs").glob("*network_latency_lab"))[-1]
    return run, json.loads((run / "summary.json").read_text(encoding="utf-8"))


def test_pass_with_before_during_after_and_verified_removal(shop):
    r = lab("--delay-ms", "200", "--duration", "15", "--samples", "5")
    run, s = summary()
    assert r.returncode == 0 and s["status"] == "PASS", r.stdout
    ph = s["phases"]
    assert ph["before"]["ping_avg_ms"] < 5 and ph["during"]["ping_avg_ms"] > 195 and ph["after"]["ping_avg_ms"] < 5
    assert ph["during"]["http_total_ms_median"] - ph["before"]["http_total_ms_median"] >= 0.8 * 200
    assert s["checks"]["netem_removed_verified"] is True
    rows = list(csv.DictReader((run / "measurements.csv").open(encoding="utf-8")))
    assert {r["phase"] for r in rows} == {"before", "during", "after"} and len(rows) == 15
    assert cluster()["lab"] == {"pods": [], "netem": None}                       # lab pods deleted
    targets = {c[5] for c in calls() if c[4] == "exec" and "tc" in " ".join(c)}
    assert targets == {"chaos-lab-server"}                                         # netem only on the lab server
    assert not [c for c in calls() if c[4] in ("scale",) or (c[4] == "delete" and "chaos-lab-server" not in c)]


def test_rejects_unbounded_parameters(shop):
    for args in (["--delay-ms", "5000"], ["--duration", "600"], ["--samples", "500"],
                 ["--delay-ms", "500", "--samples", "50", "--duration", "10"]):
        r = lab(*args)
        assert r.returncode == 4 and "REJECTED" in r.stdout, args
    assert calls() == []


def test_tc_add_failure_still_cleans_up(shop):
    os.environ["FAKE_LAB_TC_FAIL"] = "1"
    r = lab("--samples", "5", "--duration", "15")
    _, s = summary()
    assert r.returncode == 2 and s["status"] == "FAILED"
    assert cluster()["lab"]["pods"] == []


def test_removal_failure_is_reported_not_hidden(shop):
    os.environ["FAKE_LAB_DEL_FAIL"] = "1"
    r = lab("--samples", "5", "--duration", "15")
    _, s = summary()
    assert r.returncode == 2 and s["status"] == "CLEANUP_FAILED"
    assert any("self-removes" in c for c in s["cleanup"])


def test_kill_switch_aborts_before_injection(shop):
    (DATA / "chaos").mkdir(parents=True, exist_ok=True)
    (DATA / "chaos" / "KILL").write_text("")
    try:
        r = lab("--samples", "5", "--duration", "15")
        _, s = summary()
        assert s["status"] == "ABORTED" and not [c for c in calls() if c[4] in ("apply", "exec")]
    finally:
        (DATA / "chaos" / "KILL").unlink()


def test_refuses_when_lab_pods_already_exist(shop):
    st = cluster(); st["lab"] = {"pods": ["chaos-lab-server", "chaos-lab-client"], "netem": None}
    (DATA / "fake_k8s.json").write_text(json.dumps(st), encoding="utf-8")
    r = lab("--samples", "5", "--duration", "15")
    _, s = summary()
    assert s["status"] == "FAILED" and "already exist" in json.dumps(s) or "already exist" in r.stdout
    assert cluster()["lab"]["pods"] == ["chaos-lab-server", "chaos-lab-client"]   # not deleted by us


def test_invalid_manifest_is_caught_by_dry_run_and_nothing_is_created(shop):
    os.environ["FAKE_LAB_INVALID_MANIFEST"] = "1"
    try:
        r = lab("--samples", "5", "--duration", "15")
    finally:
        os.environ.pop("FAKE_LAB_INVALID_MANIFEST")
    _, s = summary()
    assert r.returncode == 2 and s["status"] == "FAILED"
    assert not [c for c in calls() if c[4:6] == ["apply", "-f"]]           # real apply never ran
    assert cluster().get("lab", {}).get("pods", []) == []


def test_lab_runs_appear_in_findings_report_but_not_in_score(shop):
    import analyze as A
    lab("--delay-ms", "200", "--duration", "15", "--samples", "5")
    r = A.analyze()
    assert len(r["latency_lab"]) == 1 and r["latency_lab"][0]["status"] == "PASS"
    assert all("lab" not in g["fault"] for g in r["groups"])                      # not mixed into the score
    md = (DATA / "results" / "findings-report.md").read_text(encoding="utf-8")
    assert "## Network latency: disposable lab validation" in md
    row = next(l for l in md.splitlines() if "_network_latency_lab`" in l)
    assert "**PASS**" in row and "200 ms" in row and "True" in row
    assert "1 of 1 lab runs passed" in md
