"""LangGraph workflow paths: dry run, rejection, human decline, full run with analysis."""
import json

from langgraph.types import Command

import workflow as W
from conftest import calls, cluster

FAST = {"baseline": 1.5, "post": 0.5, "recovery_timeout": 8}


def _run(shop, **kw):
    app = W.build_graph()
    st = W.new_state(url=shop.url, timing=FAST, verify_timeout_s=20, **kw)
    cfg = {"configurable": {"thread_id": st["workflow_id"] + str(id(kw))}}
    return app, cfg, app.invoke(st, cfg)


def test_dry_run_injects_nothing(shop):
    _, _, r = _run(shop, mode="dry_run", manual={"target": "cartservice", "fault_type": "scale_to_zero",
                                                 "duration_seconds": 10, "hypothesis": "h", "reason": "r"})
    assert r["outcome"].startswith("DRY_RUN")
    assert "--target" in r["execution"]["command"]
    assert not [c for c in calls() if c[4] in ("scale", "delete", "debug")]
    assert json.load(open(r["record_path"], encoding="utf-8"))["outcome"].startswith("DRY_RUN")


def test_rejected_proposal_never_reaches_execute(shop):
    _, _, r = _run(shop, manual={"target": "frontend", "fault_type": "scale_to_zero", "duration_seconds": 10,
                                 "hypothesis": "h", "reason": "r"})
    assert r["outcome"].startswith("REJECTED")
    assert "execute" not in [s["node"] for s in r["steps"]]


def test_human_decline_stops_before_injection(shop):
    app, cfg, r = _run(shop, manual={"target": "cartservice", "fault_type": "scale_to_zero", "duration_seconds": 10,
                                     "hypothesis": "h", "reason": "r"})
    assert r.get("__interrupt__"), "workflow must pause for human approval"
    r = app.invoke(Command(resume="no"), cfg)
    assert r["outcome"] == "NOT_APPROVED"
    assert not [c for c in calls() if c[4] == "scale"]


def test_full_run_with_approval_restore_verify_analyze(shop):
    app, cfg, r = _run(shop, manual={"target": "productcatalogservice", "fault_type": "scale_to_zero",
                                     "duration_seconds": 20, "hypothesis": "catalog outage breaks pages", "reason": "r"})
    r = app.invoke(Command(resume="yes"), cfg)
    nodes = [s["node"] for s in r["steps"]]
    assert nodes == ["collect_state", "plan", "validate", "approve", "execute", "evaluate", "restore", "verify",
                     "analyze", "save"]
    assert r["outcome"] == "COMPLETED: ABORTED_THRESHOLD"
    assert r["telemetry"]["abort_reason"]["event"] == "threshold_breach"
    assert r["recovery"]["verified"] is True and r["analysis"]["top_findings"]
    assert cluster()["deployments"]["productcatalogservice"]["replicas"] == 1


def test_precheck_blocks_when_kill_switch_present(shop):
    import controller as C
    C.KILL_FILE.parent.mkdir(parents=True, exist_ok=True)
    C.KILL_FILE.write_text("")
    try:
        _, _, r = _run(shop, manual={"target": "cartservice", "fault_type": "scale_to_zero", "duration_seconds": 10,
                                     "hypothesis": "h", "reason": "r"})
        assert r["outcome"].startswith("PRECHECK_FAILED") and "kill switch" in r["outcome"]
    finally:
        C.KILL_FILE.unlink()
