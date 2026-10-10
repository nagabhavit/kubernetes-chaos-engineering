#!/usr/bin/env python3
"""
ResilienceAI backend API  ->  http://127.0.0.1:8000

Connects the Streamlit UI (ui/app.py, "API mode") to the real, safety-controlled
chaos controller. All fault injection goes through chaos/controller.py, so the
allowlist, duration cap, auto-abort, kill switch and guaranteed restore all apply.

Endpoints used by the UI:
  GET  /health                       backend + cluster reachability
  GET  /ingestion/status             is the telemetry probe running
  POST /ingestion/start | /stop      start/stop continuous HTTP probing of the frontend
  GET  /telemetry?seconds=120        live error rate, P95, per-second series, recent errors, deployments
  POST /experiments                  {target_service, fault_type, duration_seconds, objective}
  GET  /experiments/current          state + events of the current/most recent controller run
  POST /experiment/stop              kill switch (controller restores immediately)
  POST /experiments/random-errors    run ONE real experiment on a random allowlisted target
  POST /experiments/random-errors/stop   same as /experiment/stop
  POST /rollback                     {confirm: true} stop any fault, restore, verify recovery
  GET  /runs                         history of controller runs (from results/runs)

Standard library only. Binds to 127.0.0.1.
"""
import json
import random
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import controller as C  # noqa: E402  (allowlist, Prober, kubectl helpers)
import dashboard as D   # noqa: E402  (run readers, cached cluster view)

HOST, PORT = "127.0.0.1", 8000
FRONTEND_URL = "http://localhost:8080"
TELEMETRY_DIR = C.REPO / "results" / "telemetry"

# UI fault label -> controller fault. None = not implemented (rejected, never faked).
FAULT_MAP = {
    "service_unavailability": "scale_to_zero",
    "pod_failure": "pod_delete",
    "latency": None,
}
RANDOM_DURATION_S = 15


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------- telemetry ingestion
class Ingestion:
    def __init__(self):
        self.prober = None
        self.started = None
        self.lock = threading.Lock()

    def start(self):
        with self.lock:
            if self.prober and self.prober.is_alive():
                return False
            TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
            path = TELEMETRY_DIR / f"ingestion_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.csv"
            self.prober = C.Prober(FRONTEND_URL, interval=0.5, timeout=2.0, csv_path=path)
            self.prober.start()
            self.started = utc()
            return True

    def stop(self):
        with self.lock:
            if not (self.prober and self.prober.is_alive()):
                return False
            self.prober.stop_evt.set()
            self.prober.join(timeout=5)
            return True

    @property
    def running(self):
        return bool(self.prober and self.prober.is_alive())

    def telemetry(self, seconds):
        p = self.prober
        if not p:
            return {"running": False, "window": None, "series": [], "recent_errors": []}
        now = time.time()
        samples = [x for x in p.samples_since(now - seconds) if x[3] != "conn_error"]
        series = {}
        for t, ok, lat, kind, status in samples:
            b = series.setdefault(int(t), {"n": 0, "err": 0, "lat": []})
            b["n"] += 1
            b["err"] += 0 if ok else 1
            b["lat"].append(lat)
        out = []
        for sec in sorted(series):
            b = series[sec]
            lat = sorted(b["lat"])
            out.append({"timestamp": datetime.fromtimestamp(sec).strftime("%H:%M:%S"),
                        "latency_ms": round(lat[-1] if len(lat) < 3 else lat[int(0.95 * (len(lat) - 1))], 1),
                        "error_rate_pct": round(100 * b["err"] / b["n"], 1), "requests": b["n"]})
        recent = [{"timestamp": datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds"),
                   "type": "Timeout" if kind == "timeout" else f"HTTP {status}" if status else kind,
                   "latency_ms": round(lat, 1), "source": "LIVE PROBE"}
                  for t, ok, lat, kind, status in samples if not ok][-50:]
        return {"running": self.running, "started": self.started, "window": p.window(5.0),
                "series": out, "recent_errors": list(reversed(recent))}


# --------------------------------------------------------------------------- experiment runner
class Runner:
    def __init__(self):
        self.proc = None
        self.run_id = None
        self.request = None
        self.log_lines = []
        self.we_created_kill = False
        self.lock = threading.Lock()

    @property
    def active(self):
        return bool(self.proc and self.proc.poll() is None)

    def start(self, target, fault, duration, objective, origin="ui"):
        with self.lock:
            if self.active:
                return 409, {"error": "an experiment is already running", "run_id": self.run_id}
            cmd = [sys.executable, "-u", str(HERE / "controller.py"), "--target", target, "--fault", fault,
                   "--duration", str(duration), "--label", f"{origin}: {objective}"[:200]]
            self.request = {"target": target, "fault": fault, "duration": duration, "objective": objective,
                            "origin": origin, "requested": utc()}
            self.run_id, self.log_lines = None, []
            self.proc = subprocess.Popen(cmd, cwd=str(C.REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, encoding="utf-8", errors="replace")
            self.pump = threading.Thread(target=self._pump, daemon=True)
            self.pump.start()
        # wait briefly for validation result or run id
        for _ in range(40):
            if self.run_id or self.proc.poll() is not None:
                break
            time.sleep(0.1)
        if self.proc.poll() == 4:  # controller validation rejection
            self.pump.join(timeout=3)
            return 400, {"status": "REJECTED", "reasons": [l.strip(" -") for l in self.log_lines if l.startswith("  -")]}
        return 202, {"id": self.run_id, "status": "STARTING", "request": self.request}

    def _pump(self):
        for line in self.proc.stdout:
            line = line.rstrip()
            self.log_lines.append(line)
            if "run_started run_id=" in line and not self.run_id:
                self.run_id = line.split("run_id=")[1].split()[0]
        self.proc.wait()
        if self.we_created_kill:  # re-arm so the next run is not blocked
            C.KILL_FILE.unlink(missing_ok=True)
            self.we_created_kill = False

    def stop(self):
        if not self.active:
            return False
        if not C.KILL_FILE.exists():
            C.KILL_FILE.touch()
            self.we_created_kill = True
        return True

    def current(self):
        det = D.run_detail(self.run_id) if self.run_id else None
        events = (det or {}).get("events", [])
        summary = (det or {}).get("summary")
        return {"id": self.run_id, "active": self.active, "request": self.request,
                "status": summary["status"] if summary else (("RUNNING · " + events[-1]["event"]) if events and self.active
                          else ("REJECTED" if self.proc and self.proc.poll() == 4 else ("IDLE" if not self.proc else "STARTING"))),
                "events": events, "summary": summary, "console_tail": self.log_lines[-15:]}


INGEST, RUNNER = Ingestion(), Runner()


def frontend_ok():
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(FRONTEND_URL + "/", timeout=3) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def rollback():
    steps = []
    if RUNNER.active:
        RUNNER.stop()
        steps.append("kill switch set for running experiment")
        t = time.time()
        while time.time() - t < 30:  # controller restores within ~1 s of seeing the kill file
            ev = D.read_events(C.RUNS_DIR / RUNNER.run_id) if RUNNER.run_id else []
            if any(e["event"] in ("restore_ok", "cleanup_failed") for e in ev):
                steps.append(next(e["event"] for e in ev if e["event"] in ("restore_ok", "cleanup_failed")))
                break
            time.sleep(0.3)
    elif C.STATE_FILE.exists():
        r = subprocess.run([sys.executable, str(HERE / "restore.py")], capture_output=True, text=True, timeout=180)
        steps.append(f"restore.py exit={r.returncode}")
    else:
        steps.append("no active fault found")
    # verify: all deployments ready AND 5 consecutive frontend 200s
    deadline, streak, healthy_deps = time.time() + 120, 0, False
    while time.time() < deadline:
        healthy_deps = not C.unhealthy_deployments()
        streak = streak + 1 if (healthy_deps and frontend_ok()) else 0
        if streak >= 5:
            break
        time.sleep(1)
    verified = streak >= 5
    steps.append("recovery verified" if verified else "recovery NOT verified within 120 s")
    return {"status": "ROLLED_BACK" if verified else "ROLLBACK_UNVERIFIED", "recovery_verified": verified,
            "deployments_ready": healthy_deps, "steps": steps, "at": utc()}


# --------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        b = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/health":
            return self._json(200, {"ok": True, "cluster": D.cluster(), "frontend_ok": frontend_ok()})
        if u.path == "/ingestion/status":
            return self._json(200, {"running": INGEST.running, "started": INGEST.started})
        if u.path == "/telemetry":
            secs = min(600, max(10, int((q.get("seconds") or ["120"])[0])))
            t = INGEST.telemetry(secs)
            t["cluster"] = D.cluster()
            return self._json(200, t)
        if u.path == "/experiments/current":
            return self._json(200, RUNNER.current())
        if u.path == "/runs":
            return self._json(200, {"runs": D.list_runs()})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._body()
        if path == "/ingestion/start":
            started = INGEST.start()
            return self._json(200, {"status": "RUNNING", "changed": started})
        if path == "/ingestion/stop":
            stopped = INGEST.stop()
            return self._json(200, {"status": "STOPPED", "changed": stopped})
        if path == "/experiments":
            target = body.get("target_service", "")
            ui_fault = body.get("fault_type", "")
            fault = FAULT_MAP.get(ui_fault, "__unknown__")
            if fault is None:
                return self._json(400, {"status": "REJECTED", "reasons": [
                    f"fault '{ui_fault}' is not implemented yet; nothing was injected"]})
            if fault == "__unknown__":
                return self._json(400, {"status": "REJECTED", "reasons": [f"unknown fault '{ui_fault}'"]})
            try:
                duration = int(body.get("duration_seconds", 10))
            except (TypeError, ValueError):
                return self._json(400, {"status": "REJECTED", "reasons": ["duration_seconds must be an integer"]})
            if not INGEST.running:
                INGEST.start()  # make sure the UI has live telemetry for the run
            code, res = RUNNER.start(target, fault, duration, str(body.get("objective", "")))
            return self._json(code, res)
        if path in ("/experiment/stop", "/experiments/random-errors/stop"):
            return self._json(200, {"status": "STOP_REQUESTED" if RUNNER.stop() else "NO_ACTIVE_EXPERIMENT"})
        if path == "/experiments/random-errors":
            target = random.choice(sorted(C.ALLOWED_TARGETS))
            fault = random.choice(["scale_to_zero", "pod_delete"])
            if not INGEST.running:
                INGEST.start()
            code, res = RUNNER.start(target, fault, RANDOM_DURATION_S, "random real experiment", origin="random")
            res["note"] = f"real {fault} on randomly chosen allowlisted target '{target}' for {RANDOM_DURATION_S}s"
            return self._json(code, res)
        if path == "/rollback":
            if body.get("confirm") is not True:
                return self._json(400, {"error": "confirm must be true"})
            return self._json(200, rollback())
        self._json(404, {"error": "not found"})

    def log_message(self, *a):
        pass


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"ResilienceAI backend on http://{HOST}:{PORT}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        if RUNNER.active:
            print("Stopping running experiment via kill switch ...")
            RUNNER.stop()
            RUNNER.proc.wait(timeout=180)
        INGEST.stop()


if __name__ == "__main__":
    main()
