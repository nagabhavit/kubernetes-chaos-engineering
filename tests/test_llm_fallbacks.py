"""Local-only LLM design: Ollama unavailable / malformed -> labelled fallbacks; no cloud API is ever called."""
import pytest

import analyze as A
import llm as L
import planner as P
from conftest import DATA, controller


def test_ollama_unavailable_planner_falls_back_and_says_so(shop):
    plan = P.make_plan(P.build_context())
    assert plan["planner"] == "RULE-BASED fallback (not AI)"
    assert "Ollama not reachable" in plan["llm_error"]
    assert plan["experiments"] and all(e["experiment_id"].startswith("EXP-") for e in plan["experiments"])
    assert all(e["fault_type"] != "network_latency" for e in plan["experiments"])     # lab only


def test_ollama_malformed_output_falls_back(monkeypatch, shop):
    monkeypatch.setattr(L, "ollama_json", lambda *a, **k: (_ for _ in ()).throw(L.LLMError("malformed JSON from model")))
    plan = P.make_plan(P.build_context())
    assert plan["planner"].startswith("RULE-BASED") and "malformed" in plan["llm_error"]


def test_ollama_schema_output_validated(monkeypatch, shop):
    good = {"rank": 1, "target": "cartservice", "fault_type": "scale_to_zero", "duration_seconds": 10,
            "hypothesis": "h", "reason": "r", "expected_impact": "e"}
    seen = {}

    def fake(model, system, user, schema=None, timeout=240):
        seen["schema"] = schema
        return {"experiments": [good, {**good, "target": "frontend"},
                                {**good, "target": "currencyservice", "fault_type": "network_latency"}]}, {"model": model, "raw": ""}
    monkeypatch.setattr(L, "ollama_json", fake)
    plan = P.make_plan(P.build_context())
    assert seen["schema"]["properties"]["experiments"]["items"]["properties"]["fault_type"]["enum"] == list(P.PLANNER_FAULTS)
    assert plan["planner"] == "AI (Ollama)" and len(plan["experiments"]) == 1 and len(plan["rejected"]) == 2


def test_no_cloud_api_even_if_key_present(monkeypatch, shop):
    controller(shop.url, "--target", "productcatalogservice", "--duration", "20")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-never-be-used")
    calls = []
    import urllib.request
    real = urllib.request.urlopen
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: calls.append(a) or real(*a, **k))
    assert not hasattr(L, "claude_json")
    r = A.analyze()
    assert calls == []                                            # urlopen (cloud path) never used
    assert r["narrative"]["used"].startswith("templates")         # Ollama unreachable in tests -> templates
    md = (DATA / "results" / "findings-report.md").read_text(encoding="utf-8")
    assert "Narrative written by:** templates" in md and "Claude" not in md


def test_local_model_narrative_with_number_guard(monkeypatch, shop):
    controller(shop.url, "--target", "productcatalogservice", "--duration", "20")   # hard-dependency finding
    controller(shop.url, "--target", "adservice", "--duration", "3")                # graceful finding
    monkeypatch.setattr(L, "ollama_models", lambda timeout=5: ["qwen2.5:3b"])        # second model not pulled

    def fake(model, system, user, schema=None, timeout=240):
        assert model == "qwen2.5:3b" and schema["required"] == ["findings"]
        items = []
        for f in user["findings"]:
            it = {"id": f["id"], "likely_cause": "Likely no fallback path.", "recommended_fix": "Run 2 replicas.",
                  "verification_experiment": "Repeat with 2 replicas."}
            if "hard dependency" in f["title"]:
                it["likely_cause"] = "Errors hit 97% of 12345 requests."              # invented numbers
            items.append(it)
        return {"findings": items}, {"model": model}
    monkeypatch.setattr(L, "ollama_json", fake)
    r = A.analyze()
    assert r["narrative"]["used"] == "local Ollama (planner model)"
    assert "llama3.2:3b: not pulled" in r["narrative"]["errors"]
    by_kind = {f["kind"]: f["text_source"] for f in r["findings"]}
    assert by_kind["hard_dependency"].startswith("template (AI text rejected")   # hallucination guard
    assert by_kind["graceful"].startswith("AI: local Ollama")                    # honest text accepted


def test_planner_reasons_citing_nonexistent_history_are_flagged(monkeypatch, shop):
    controller(shop.url, "--target", "productcatalogservice", "--duration", "20")      # one real run
    real_like = [  # what qwen2.5:3b actually produced on the user's machine (targets without history)
        {"rank": 1, "target": "redis-cart", "fault_type": "pod_delete", "duration_seconds": 15,
         "hypothesis": "h", "reason": "Previous runs showed issues with Redis scaling to zero replicas.", "expected_impact": "e"},
        {"rank": 2, "target": "productcatalogservice", "fault_type": "pod_delete", "duration_seconds": 15,
         "hypothesis": "h", "reason": "Previous runs showed a readiness gap on this service.", "expected_impact": "e"},
        {"rank": 3, "target": "cartservice", "fault_type": "scale_to_zero", "duration_seconds": 10,
         "hypothesis": "h", "reason": "Two services depend on cartservice and it is untested.", "expected_impact": "e"},
    ]
    monkeypatch.setattr(L, "ollama_json", lambda *a, **k: ({"experiments": real_like}, {"model": "qwen2.5:3b", "raw": ""}))
    plan = P.make_plan(P.build_context())
    by_t = {e["target"]: e for e in plan["experiments"]}
    assert by_t["redis-cart"]["grounding"].startswith("UNSUPPORTED")
    assert by_t["productcatalogservice"]["grounding"] == "ok"            # a run on that target exists
    assert by_t["cartservice"]["grounding"] == "ok"
    assert by_t["redis-cart"]["rank"] == 3                               # unsupported ranked last
    assert by_t["productcatalogservice"]["evidence"]["previous_runs_on_target"] == 1
    assert [e["experiment_id"][-1] for e in plan["experiments"]] == ["1", "2", "3"]


def test_planner_reason_contradicting_recorded_outcome_is_flagged():
    """qwen2.5:3b on the user's machine: 'Previous runs showed traffic spikes led to error rate increases'
    while the recorded spike run had 0 errors. Existence of a run is not enough; the claimed outcome must match."""
    ctx = {"dependency_edges": [], "previous_runs": [
        {"target": "frontend", "fault": "traffic_spike", "errors_in_fault": 0},
        {"target": "productcatalogservice", "fault": "pod_delete", "errors_in_fault": 2},
        {"target": "adservice", "fault": "scale_to_zero", "errors_in_fault": 0}]}
    props = [
        {"experiment_id": "EXP-X-1", "target": "frontend", "fault_type": "traffic_spike", "hypothesis": "errors will rise",
         "reason": "Previous runs showed traffic spikes led to error rate increases."},
        {"experiment_id": "EXP-X-2", "target": "productcatalogservice", "fault_type": "pod_delete", "hypothesis": "h",
         "reason": "Previous runs showed pods deletions resulted in immediate outages."},
        {"experiment_id": "EXP-X-3", "target": "adservice", "fault_type": "scale_to_zero", "hypothesis": "h",
         "reason": "Previous runs showed the frontend degrades gracefully without adservice."},
        {"experiment_id": "EXP-X-4", "target": "productcatalogservice", "fault_type": "pod_delete", "hypothesis": "h",
         "reason": "Previous runs showed no user impact."},
    ]
    out = {p["reason"].split(" showed ")[1][:12]: p for p in P.ground(props, ctx)}
    spike = out["traffic spik"]
    assert spike["grounding"].startswith("UNSUPPORTED") and "0 errors" in spike["grounding"]
    assert spike["evidence"]["previous_errors_in_fault"] == 0
    assert out["pods deletio"]["grounding"] == "ok"          # true: 2 errors recorded
    assert out["the frontend"]["grounding"] == "ok"          # true: 0 errors, graceful
    assert out["no user impa"]["grounding"].startswith("UNSUPPORTED")   # false: 2 errors
    assert [p["grounding"] == "ok" for p in P.ground(props, ctx)][:2] == [True, True]      # unsupported ranked last
