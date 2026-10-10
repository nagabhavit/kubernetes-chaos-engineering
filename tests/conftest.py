"""Test fixtures. Everything mutable is redirected to a temp dir BEFORE chaos modules are imported:
CHAOS_DATA_DIR (kill switch, state, runs, reports) and CHAOS_KUBECTL (fake kubectl)."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(tempfile.mkdtemp(prefix="chaos-tests-"))
STATE = DATA / "fake_k8s.json"
os.environ["CHAOS_DATA_DIR"] = str(DATA)
os.environ["CHAOS_KUBECTL"] = str(ROOT / "tests" / "fakes" / "fake_kubectl.py")
os.environ["FAKE_K8S_STATE"] = str(STATE)
os.environ["OLLAMA_URL"] = "http://127.0.0.1:9"        # closed port unless a test overrides it
os.environ["CHAOS_LOCUSTFILE"] = str(ROOT / "tests" / "fakes" / "locustfile.py")
for k in ("NO_PROXY", "no_proxy"):
    os.environ[k] = "*"
sys.path.insert(0, str(ROOT / "chaos"))

SERVICES = ["adservice", "cartservice", "checkoutservice", "currencyservice", "emailservice", "frontend",
            "paymentservice", "productcatalogservice", "recommendationservice", "redis-cart", "shippingservice"]


def reset_cluster(replicas=None):
    st = {"deployments": {n: {"replicas": (replicas or {}).get(n, 1)} for n in SERVICES}, "netem": {}}
    st["deployments"]["loadgenerator"] = {"replicas": 0}
    STATE.write_text(json.dumps(st), encoding="utf-8")
    Path(str(STATE) + ".calls").write_text("", encoding="utf-8")


def cluster():
    return json.loads(STATE.read_text(encoding="utf-8"))


def calls():
    return [json.loads(l) for l in Path(str(STATE) + ".calls").read_text(encoding="utf-8").splitlines() if l]


class Shop(BaseHTTPRequestHandler):
    """Fake Online Boutique frontend: 500 while productcatalogservice is down; slow while netem is active."""
    def do_GET(self):
        st = cluster()
        pc = st["deployments"]["productcatalogservice"]
        if self.server.hang_s:                      # simulates an unreachable/slow frontend (probe timeouts)
            time.sleep(self.server.hang_s)
        down = pc["replicas"] == 0 or time.time() < pc.get("ready_at", 0) or self.server.always_500
        delay = max(st.get("netem", {}).values(), default=0)
        if delay:
            time.sleep(delay / 1000 * 2)
        self.send_response(500 if down else 200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


@pytest.fixture
def shop():
    for p in ("chaos", "results"):
        shutil.rmtree(DATA / p, ignore_errors=True)
    reset_cluster()
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Shop)
    srv.always_500 = False
    srv.hang_s = 0
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    srv.url = f"http://127.0.0.1:{port}"
    yield srv
    srv.shutdown()
    for k in ("FAKE_FAIL_INJECT", "FAKE_FAIL_RESTORE", "FAKE_NEVER_READY", "FAKE_LAB_TC_FAIL", "FAKE_LAB_DEL_FAIL"):
        os.environ.pop(k, None)


FAST = ["--baseline", "1.5", "--post", "0.5", "--recovery-timeout", "8", "--window", "2", "--min-samples", "3"]


def controller(url, *args, timeout=90, background=False):
    cmd = [sys.executable, str(ROOT / "chaos" / "controller.py"), "--url", url, *FAST, *args]
    if background:
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def last_summary():
    runs = sorted((DATA / "results" / "runs").glob("2*"))
    return json.loads((runs[-1] / "summary.json").read_text(encoding="utf-8")) if runs else None
