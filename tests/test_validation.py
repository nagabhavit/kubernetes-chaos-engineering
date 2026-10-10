"""Proposal validation: the deterministic gate that AI output must pass."""
import planner as P
import llm as L
import pytest

GOOD = {"target": "cartservice", "fault_type": "scale_to_zero", "duration_seconds": 10,
        "hypothesis": "Cart page fails while cart is down", "reason": "unverified edge", "expected_impact": "/cart 500"}


def test_valid_proposal_gets_id_bounds_and_recovery_plan():
    ok, bad = P.validate([GOOD], stamp="T")
    assert not bad and len(ok) == 1
    p = ok[0]
    assert p["experiment_id"] == "EXP-T-1" and p["rank"] == 1
    assert p["abort_conditions"]["max_error_rate"] == 0.2 and p["abort_conditions"]["max_p95_ms"] == 1000
    assert "rollout status" in p["recovery_verification"]


def test_malformed_json_is_rejected():
    for text in ("not json at all", "{\"experiments\": [", "```json\n[1,2]\n```"):
        with pytest.raises(L.LLMError):
            L.parse_json_object(text)
    ok, bad = P.validate("not a list")
    assert not ok and bad


@pytest.mark.parametrize("target", ["frontend", "kube-system", "cartservice; rm -rf /", "", None])
def test_unknown_target_rejected(target):
    ok, bad = P.validate([{**GOOD, "target": target}])
    assert not ok and any("allowlist" in r for r in bad[0]["reasons"])


@pytest.mark.parametrize("fault", ["latency", "disk_fill", "delete_namespace", "shell"])
def test_unsupported_fault_rejected(fault):
    ok, bad = P.validate([{**GOOD, "fault_type": fault}])
    assert not ok and any("not supported" in r for r in bad[0]["reasons"])


@pytest.mark.parametrize("patch,needle", [
    ({"duration_seconds": 600}, "duration"),
    ({"duration_seconds": "10; reboot"}, "duration"),
    ({"duration_seconds": True}, "duration"),
    ({"fault_type": "network_latency", "delay_ms": 5000}, "delay_ms"),
    ({"fault_type": "traffic_spike", "target": "frontend", "spike_users": 9999}, "spike_users"),
    ({"fault_type": "traffic_spike", "target": "cartservice"}, "frontend"),
    ({"max_error_rate": 0.9}, "may only tighten"),
    ({"max_p95_ms": 60000}, "may only tighten"),
    ({"hypothesis": ""}, "hypothesis"),
])
def test_excessive_or_invalid_parameters_rejected(patch, needle):
    ok, bad = P.validate([{**GOOD, **patch}])
    assert not ok and any(needle in r for r in bad[0]["reasons"]), bad


def test_quoted_number_from_small_model_is_accepted():
    ok, _ = P.validate([{**GOOD, "duration_seconds": " 15 "}])
    assert ok and ok[0]["duration_seconds"] == 15


def test_tightened_thresholds_are_kept():
    ok, _ = P.validate([{**GOOD, "max_error_rate": 0.1, "max_p95_ms": 500}])
    assert ok[0]["abort_conditions"]["max_error_rate"] == 0.1 and ok[0]["abort_conditions"]["max_p95_ms"] == 500
