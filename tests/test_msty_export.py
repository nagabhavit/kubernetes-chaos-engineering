"""Msty is a MANUAL review workflow: export packet -> human runs it in Msty -> import with number check."""
import json
import urllib.request

import pytest

import msty_export as M
from conftest import DATA

REP = {
    "generated": "2026-10-10T18:11:46+00:00",
    "resilience": {"overall": 65.5, "coverage": "4/10 allowlisted services tested"},
    "groups": [{"target": "productcatalogservice", "fault": "scale_to_zero", "runs": 6, "error_rate_in_fault": 0.67,
                "affected_routes": ["/", "/cart"], "k8s_ready_s": 2.83, "user_recovery_s": 18.87,
                "baseline_p95_ms": 238.2, "p95_in_fault_ms": 2010.45, "confidence": "high",
                "run_ids": ["20261010T175308Z_scale_to_zero_productcatalogservice"]}],
    "findings": [{"rank": 1, "title": "Users keep failing after productcatalogservice is Ready", "severity": "High",
                  "severity_score": 66.0, "confidence": "high", "affected_services": ["frontend"],
                  "affected_routes": ["/"], "evidence": {"runs": 6, "k8s_ready_s_median": 2.83,
                                                         "user_recovery_s_median": 18.87, "gap_s_median": 16.04},
                  "run_ids": ["20261010T175308Z_scale_to_zero_productcatalogservice"],
                  "likely_cause": "AI TEXT THAT MUST NOT BE EXPORTED"}],
    "latency_lab": [{"run_id": "20261010T174253Z_network_latency_lab", "status": "PASS", "delay_ms": 200}],
}


@pytest.fixture
def findings(tmp_path, monkeypatch):
    p = tmp_path / "findings.json"
    p.write_text(json.dumps(REP), encoding="utf-8")
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("network call made"))
    return p


def test_export_contains_facts_not_ai_text(findings, tmp_path):
    out = M.export(findings, tmp_path / "msty")
    text = out.read_text(encoding="utf-8")
    assert out.name.startswith("msty_review_") and "manual" in text.lower()
    assert "20261010T175308Z_scale_to_zero_productcatalogservice" in text and "18.87" in text and "65.5" in text
    assert "AI TEXT THAT MUST NOT BE EXPORTED" not in text
    assert "Only quote numbers that appear in the FACTS" in text


def test_export_without_report_fails_clearly(tmp_path):
    with pytest.raises(FileNotFoundError, match="cli.py report"):
        M.export(tmp_path / "missing.json", tmp_path / "msty")


def test_import_flags_invented_numbers_and_keeps_original(findings, tmp_path):
    reply = tmp_path / "reply.md"
    original = "### F1\n- Users failed for 18.87 s while Ready after 2.83 s.\n- Retry storms caused 999 errors.\n"
    reply.write_text(original, encoding="utf-8")
    path, bad = M.import_notes(reply, findings, tmp_path / "msty")
    assert bad == ["999"]
    assert reply.read_text(encoding="utf-8") == original           # source untouched
    notes = path.read_text(encoding="utf-8")
    assert "UNSUPPORTED numbers" in notes and "999" in notes and "Not generated or verified by this tool" in notes
    assert notes.endswith(original)


def test_import_clean_reply_passes(findings, tmp_path):
    reply = tmp_path / "ok.md"
    reply.write_text("F1: gap 16.04 s over 6 runs; 67% errors.", encoding="utf-8")
    _, bad = M.import_notes(reply, findings, tmp_path / "msty")
    assert bad == []


def test_default_paths_follow_chaos_data_dir():
    assert str(M.FINDINGS_JSON).startswith(str(DATA)) and str(M.OUT_DIR).startswith(str(DATA))
