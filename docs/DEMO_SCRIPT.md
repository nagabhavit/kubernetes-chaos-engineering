# 3-Minute Demo Script

**Before recording:** cluster `READY` (`python chaos\cli.py health`), port-forward open, Ollama running, a run already done today (for the report). Two windows: **PowerShell** (large font, ~18 pt) and **browser** with the shop (`localhost:8080`) and the dashboard (`127.0.0.1:8090`, from `python chaos\dashboard.py`). Record with **Win + Alt + R** (Xbox Game Bar) or OBS. Rehearse once.

| Time | Screen | Say (roughly) |
|---|---|---|
| 0:00–0:15 | Shop in browser | "Online Boutique: 11 microservices on a local kind cluster. We break parts of it on purpose, safely, and an AI helps decide what to break and explains what we learned." |
| 0:15–0:35 | `docs/ARCHITECTURE.md` diagram | "Two local AI agents on Ollama: a planner and an analyst, with no cloud API. LangGraph runs the workflow. Plain Python enforces safety. The AI never runs commands." |
| 0:35–1:00 | `python chaos\cli.py plan` | "The planner read our dependency map and previous runs, and ranked these experiments with reasons. This one was rejected by the validator: the target isn't allowed." |
| 1:00–1:40 | `python chaos\cli.py run --target productcatalogservice --fault scale_to_zero --duration 20` → type `yes`; switch to dashboard | "It asks a human first. Fault injected… error rate crosses 20%… it aborts automatically and restores the original replica count." |
| 1:40–2:00 | Dashboard timeline | "Kubernetes said ready after about 3 seconds, but users failed for about 19. Readiness isn't recovery. We saw that in 6 separate runs." |
| 2:00–2:25 | Scroll `results\findings-report.md` | "Findings ranked by severity, with evidence, a labelled hypothesis, a fix and a verification experiment. Numbers come from code; the AI writes the explanation, and invented numbers are rejected." |
| 2:25–2:45 | Brief clips: spike run, and `cli.py latency-lab` before/during/after numbers (pre-recorded is fine) | "Three application fault types: outage, pod crash (users failed 21 s) and a bounded traffic spike (P95 63 to 252 ms, no errors). Resilience score 65.5 out of 100. Network latency is proven with netem on disposable lab pods: before, during and after: about 200 ms added, then removal verified." |
| 2:45–3:00 | `python -m pytest tests -q` passing | "Every safety path, including the kill switch, watchdog and failed cleanup, is covered by tests. Thank you." |

**Checklist:** ≤ 3:00 · audio clear · no API keys visible on screen · terminal font readable · ends on a working system.
