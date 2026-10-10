"""Static checks on the chaos YAML (the lab YAML once failed to parse on the real cluster)."""
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
LAB = ROOT / "chaos" / "latency-lab.yaml"


@pytest.mark.parametrize("name", ["latency-lab.yaml", "netem-check.yaml"])
def test_yaml_parses(name):
    docs = [d for d in yaml.safe_load_all((ROOT / "chaos" / name).read_text(encoding="utf-8")) if d]
    assert docs


def test_lab_creates_only_two_disposable_pods_in_boutique():
    docs = [d for d in yaml.safe_load_all(LAB.read_text(encoding="utf-8")) if d]
    assert [(d["kind"], d["metadata"]["name"]) for d in docs] == [("Pod", "chaos-lab-server"), ("Pod", "chaos-lab-client")]
    for d in docs:
        assert d["metadata"]["namespace"] == "boutique"
        assert d["metadata"]["labels"]["app.kubernetes.io/part-of"] == "chaos-lab"
        assert d["spec"]["activeDeadlineSeconds"] <= 1800 and d["spec"]["restartPolicy"] == "Never"
    caps = {d["metadata"]["name"]: (d["spec"]["containers"][0].get("securityContext") or {}).get("capabilities")
            for d in docs}
    assert caps == {"chaos-lab-server": {"add": ["NET_ADMIN"]}, "chaos-lab-client": None}


@pytest.mark.skipif(not shutil.which("socat") or not shutil.which("sh"), reason="needs socat + sh (Linux)")
def test_lab_server_command_returns_http_200(tmp_path):
    script = yaml.safe_load_all(LAB.read_text(encoding="utf-8")).__next__()["spec"]["containers"][0]["args"][0]
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    f = tmp_path / "srv.sh"
    f.write_text(script.replace("8080", str(port)).replace("/tmp/resp", str(tmp_path / "resp")), encoding="utf-8")
    p = subprocess.Popen(["sh", str(f)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(0.8)
        o = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with o.open(f"http://127.0.0.1:{port}/", timeout=5) as r:
            assert r.status == 200 and r.read() == b"ok"
    finally:
        p.terminate()
