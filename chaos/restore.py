#!/usr/bin/env python3
"""Emergency restore: undo a fault left behind if controller.py died mid-run.

Reads chaos/state/active_fault.json, scales the target back to its ORIGINAL
replica count, waits for rollout, and removes the state file.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

CONTEXT, NAMESPACE = "kind-boutique-chaos", "boutique"
DATA = Path(os.getenv("CHAOS_DATA_DIR") or Path(__file__).resolve().parent.parent)
STATE_FILE = DATA / "chaos" / "state" / "active_fault.json"


def kubectl_base():
    k = os.getenv("CHAOS_KUBECTL", "kubectl")
    return ([sys.executable, k] if k.endswith(".py") else [k]) + ["--context", CONTEXT, "-n", NAMESPACE]


def main() -> int:
    if not STATE_FILE.exists():
        print("No active fault recorded. Nothing to restore.")
        return 0
    st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if st.get("fault") == "network_latency" and st.get("pod"):
        pod, container = st["pod"], st["container"]
        base = kubectl_base()
        r = subprocess.run(base + ["exec", pod, "-c", container, "--", "sh", "-c",
                                   "tc qdisc del dev eth0 root 2>/dev/null; tc qdisc show dev eth0"],
                           capture_output=True, text=True, timeout=30)
        err = (r.stderr or "").lower()
        if r.returncode == 0 and "netem" not in r.stdout:
            print(f"netem removed from {pod}: {r.stdout.strip()}")
        elif r.returncode != 0 and any(k in err for k in ("not found", "not running", "completed", "terminated")):
            print(f"debug container already gone ({r.stderr.strip()}); its own timer removes the delay")
        else:
            print(f"could NOT confirm netem removal (rc={r.returncode}): {r.stderr.strip() or r.stdout.strip()}")
            print(f"State kept in {STATE_FILE}. Remove manually, then delete the state file:")
            print(f"  kubectl --context {CONTEXT} -n {NAMESPACE} exec {pod} -c {container} -- tc qdisc del dev eth0 root")
            return 1
        STATE_FILE.unlink()
        return 0
    if st.get("spike_pid"):
        import os
        import signal
        try:
            os.kill(int(st["spike_pid"]), signal.SIGTERM)
            print(f"Stopped traffic spike (pid {st['spike_pid']})")
        except OSError as e:
            print(f"Spike process not running ({e}); nothing to stop")
        STATE_FILE.unlink()
        return 0
    target, replicas = st["target"], int(st["original_replicas"])
    print(f"Restoring {target} to {replicas} replica(s) (run {st['run_id']})")
    base = kubectl_base()
    r = subprocess.run(base + ["scale", f"deployment/{target}", f"--replicas={replicas}"], capture_output=True, text=True)
    print(r.stdout or r.stderr)
    if r.returncode != 0:
        return 1
    r = subprocess.run(base + ["rollout", "status", f"deployment/{target}", "--timeout=120s"], capture_output=True, text=True)
    print(r.stdout or r.stderr)
    STATE_FILE.unlink()
    return 0 if r.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
