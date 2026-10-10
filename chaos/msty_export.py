#!/usr/bin/env python3
"""
Msty review workflow: MANUAL export/import, not an automatic agent integration.

  python chaos\\cli.py msty-export              -> results/msty/msty_review_<ts>.md
      A self-contained review packet: instructions, a prompt, and the code-computed facts
      from results/findings.json (score, experiments, findings, evidence, run IDs).
      Open Msty Studio, start a chat with a LOCAL model, attach or paste the file, and save the reply.

  python chaos\\cli.py msty-import <reply.md>   -> results/msty/msty_notes_<ts>.md
      Stores the reply with a provenance header and a number check: every number above 5 in the
      reply must appear in the exported evidence; anything else is listed as UNSUPPORTED.
      The original reply file is not modified. The findings report and the score are not changed.

No network calls are made here. Msty is optional; nothing in the core workflow depends on it.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import controller as C  # noqa: E402

FINDINGS_JSON = C.DATA / "results" / "findings.json"
OUT_DIR = C.DATA / "results" / "msty"
NUM = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")

PROMPT = """You are reviewing the results of chaos-engineering experiments on Google Online Boutique
(11 microservices, local kind cluster, 1 replica per service). The FACTS below were measured and
computed by code. Do not change them and do not invent new numbers, runs or experiments.

For each finding (F1, F2, ...) answer in this exact format:

### F<n>
- Agree with the observed facts? yes/no (if no, say which fact looks inconsistent and why)
- Most likely cause (hypothesis): ...
- Alternative cause (hypothesis): ...
- Recommended fix: ...
- Verification experiment (one bounded experiment that would confirm the fix): ...

Only quote numbers that appear in the FACTS. Label every cause as a hypothesis."""


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def load_findings(path: Path | None = None) -> dict:
    p = Path(path or FINDINGS_JSON)
    if not p.exists():
        raise FileNotFoundError(f"{p} not found. Run `python chaos\\cli.py report` first.")
    return json.loads(p.read_text(encoding="utf-8"))


def _numbers(obj, out: set[float]) -> set[float]:
    """Every number in the exported facts, plus common roundings and percentage forms."""
    if isinstance(obj, bool) or obj is None:
        return out
    if isinstance(obj, (int, float)):
        v = float(obj)
        for x in (v, round(v), round(v, 1), round(v, 2), round(v * 100), round(v * 100, 1)):
            out.add(float(x))
    elif isinstance(obj, str):
        for m in NUM.findall(obj):
            out.add(float(m))
    elif isinstance(obj, dict):
        for v in obj.values():
            _numbers(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _numbers(v, out)
    return out


def facts(rep: dict) -> dict:
    """Only code-computed facts go to Msty; the earlier AI narrative is left out on purpose."""
    res = rep.get("resilience") or {}
    return {
        "generated": rep.get("generated"),
        "resilience_score": res.get("overall"),
        "coverage": res.get("coverage"),
        "experiments": [{k: g.get(k) for k in ("target", "fault", "runs", "error_rate_in_fault", "affected_routes",
                                                 "k8s_ready_s", "user_recovery_s", "baseline_p95_ms", "p95_in_fault_ms",
                                                 "confidence", "run_ids")} for g in rep.get("groups", [])],
        "findings": [{"id": f"F{f.get('rank')}", "title": f.get("title"), "severity": f.get("severity"),
                      "severity_score": f.get("severity_score"), "confidence": f.get("confidence"),
                      "affected_services": f.get("affected_services"), "affected_routes": f.get("affected_routes"),
                      "evidence": f.get("evidence"), "run_ids": f.get("run_ids")} for f in rep.get("findings", [])],
        "latency_lab": [{k: x.get(k) for k in ("run_id", "status", "delay_ms")} for x in rep.get("latency_lab") or []],
    }


def export(findings_path: Path | None = None, out_dir: Path | None = None) -> Path:
    rep = load_findings(findings_path)
    f = facts(rep)
    out = Path(out_dir or OUT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"msty_review_{_ts()}.md"
    lines = [
        "# Msty review packet (manual workflow)",
        "",
        "**How to use:** open Msty Studio, start a chat with a *local* model, paste everything below the line "
        "(or attach this file), then save the model's reply as a `.md` file and run "
        "`python chaos\\cli.py msty-import <reply.md>`.",
        "",
        "This is a manual human-run review. It is not an automatic agent and it does not change the findings report or the score.",
        "",
        "---",
        "",
        PROMPT,
        "",
        "## FACTS (computed by code from results/runs/*)",
        "",
        "```json",
        json.dumps(f, indent=2),
        "```",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def check_numbers(text: str, rep: dict) -> list[str]:
    allowed = _numbers(facts(rep), set())
    bad = []
    for m in NUM.findall(text):
        x = float(m)
        if x <= 5 or x in allowed or m in bad:
            continue
        bad.append(m)
    return bad


def import_notes(reply_path: Path, findings_path: Path | None = None, out_dir: Path | None = None) -> tuple[Path, list[str]]:
    src = Path(reply_path)
    if not src.exists():
        raise FileNotFoundError(f"{src} not found")
    rep = load_findings(findings_path)
    text = src.read_text(encoding="utf-8", errors="replace")
    bad = check_numbers(text, rep)
    out = Path(out_dir or OUT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"msty_notes_{_ts()}.md"
    head = [
        "# Msty review notes (manual, human-run)",
        "",
        f"- Source file: `{src.name}`; imported {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- Reviewed facts generated: {rep.get('generated')}",
        "- Provenance: written by a local model in Msty Studio and run by a person. Not generated or verified by this tool. "
        "Causes are hypotheses.",
        "- Number check: " + ("all numbers above 5 appear in the exported evidence." if not bad else
                             f"**UNSUPPORTED numbers (not in the evidence): {', '.join(bad)}**. Treat those statements as unverified."),
        "",
        "---",
        "",
    ]
    path.write_text("\n".join(head) + text, encoding="utf-8")
    return path, bad


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("export", "import") or (argv[0] == "import" and len(argv) != 2):
        print("usage: msty_export.py export | import <reply.md>")
        return 2
    try:
        if argv[0] == "export":
            print("Saved:", export())
            print("Next: open Msty Studio, chat with a local model, paste this file, save the reply, then run msty-import.")
            return 0
        path, bad = import_notes(Path(argv[1]))
        print("Saved:", path)
        print("Number check:", "OK" if not bad else "UNSUPPORTED numbers: " + ", ".join(bad))
        return 0
    except FileNotFoundError as e:
        print("ERROR:", e)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
