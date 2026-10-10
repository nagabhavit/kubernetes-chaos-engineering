#!/usr/bin/env python3
"""Emergency restore: undo a fault left behind if controller.py died mid-run.

Reads chaos/state/active_fault.json, scales the target back to its ORIGINAL
replica count, waits for rollout, and removes the state file.
"""
import json
import subprocess
import sys
from pathlib import Path

CONTEXT, NAMESPACE = "kind-boutique-chaos", "boutique"
STATE_FILE = Path(__file__).resolve().parent / "state" / "active_fault.json"


def main() -> int:
    if not STATE_FILE.exists():
        print("No active fault recorded. Nothing to restore.")
        return 0
    st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
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
    base = ["kubectl", "--context", CONTEXT, "-n", NAMESPACE]
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
