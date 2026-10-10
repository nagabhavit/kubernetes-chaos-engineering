"""Backend API: a planner proposal's non-default parameters reach the controller unchanged."""
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import executor as X
from conftest import DATA, cluster


def test_controller_extra_args_forwards_every_parameter():
    spike = {"fault_type": "traffic_spike", "parameters": {"spike_users": 37},
             "abort_conditions": {"max_error_rate": 0.1, "max_p95_ms": 500}}
    assert X.controller_extra_args(spike) == ["--max-error-rate", "0.1", "--max-p95-ms", "500", "--spike-users", "37"]
    lat = {"fault_type": "network_latency", "parameters": {"delay_ms": 300}, "abort_conditions": {}}
    assert X.controller_extra_args(lat)[-2:] == ["--delay-ms", "300"]
    out = {"fault_type": "scale_to_zero", "parameters": {}, "abort_conditions": {"max_error_rate": 0.05, "max_p95_ms": 200}}
    assert X.controller_extra_args(out) == ["--max-error-rate", "0.05", "--max-p95-ms", "200"]
    cmd = X.build_cmd({**spike, "target": "frontend", "duration_seconds": 9, "experiment_id": "E"})
    for flag, val in (("--spike-users", "37"), ("--max-error-rate", "0.1"), ("--max-p95-ms", "500"), ("--duration", "9")):
        assert cmd[cmd.index(flag) + 1] == val


def test_planner_run_forwards_non_default_parameters_end_to_end(shop, monkeypatch):
    monkeypatch.setenv("CHAOS_FRONTEND_URL", shop.url)
    import importlib
    import api
    import planner as P
    importlib.reload(api)
    acc, _ = P.validate([{"target": "frontend", "fault_type": "traffic_spike", "duration_seconds": 5,
                          "spike_users": 7, "max_error_rate": 0.15, "max_p95_ms": 800,
                          "hypothesis": "h", "reason": "r", "expected_impact": "e"}], stamp="T")
    monkeypatch.setattr(api.PL, "make_plan", lambda: {"planner": "AI (Ollama)", "model": "m", "experiments": acc,
                                                      "rejected": [], "llm_error": None})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    o = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post(path, body=None):
        r = urllib.request.Request(base + path, data=json.dumps(body or {}).encode(), method="POST")
        try:
            return json.loads(o.open(r, timeout=120).read())
        except urllib.error.HTTPError as e:
            return json.loads(e.read())

    post("/planner/plan")
    assert post("/planner/run", {"rank": 1})["status"] == "STARTING"
    end = time.time() + 150
    while time.time() < end and api.RUNNER.active:
        time.sleep(1)
    srv.shutdown()
    run = sorted((DATA / "results" / "runs").glob("*traffic_spike*"))[-1]
    req = json.loads((run / "summary.json").read_text(encoding="utf-8"))["request"]
    assert (req["spike_users"], req["max_error_rate"], req["max_p95_ms"]) == (7, 0.15, 800.0)
    assert "planner(AI) EXP-T-1" in req["label"]
