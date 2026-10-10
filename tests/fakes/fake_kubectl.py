#!/usr/bin/env python3
"""Fake kubectl for unit tests: keeps cluster state in a JSON file; never touches a real cluster.

Env: FAKE_K8S_STATE (state file), FAKE_FAIL_INJECT, FAKE_FAIL_RESTORE, FAKE_NEVER_READY, FAKE_READY_DELAY.
Every call is appended to <state>.calls for assertions."""
import json
import os
import sys
import time

S = os.environ["FAKE_K8S_STATE"]
args = sys.argv[1:]
with open(S + ".calls", "a", encoding="utf-8") as f:
    f.write(json.dumps(args) + "\n")
assert args[:4] == ["--context", "kind-boutique-chaos", "-n", "boutique"], args
a = args[4:]
st = json.load(open(S, encoding="utf-8"))
now = time.time()


def save():
    json.dump(st, open(S, "w", encoding="utf-8"))


def ready(n):
    d = st["deployments"][n]
    if os.getenv("FAKE_NEVER_READY") and d.get("touched"):
        return 0
    return d["replicas"] if now >= d.get("ready_at", 0) else 0


def dep(n):
    return {"metadata": {"name": n},
            "spec": {"replicas": st["deployments"][n]["replicas"],
                     "template": {"spec": {"containers": [{"image": f"fake/{n}:test"}]}}},
            "status": {"readyReplicas": ready(n)}}


lab = st.setdefault("lab", {"pods": [], "netem": None})


def lab_save():
    st["lab"] = lab
    save()


if a[:2] == ["apply", "--dry-run=server"]:
    if os.getenv("FAKE_LAB_INVALID_MANIFEST"):
        sys.stderr.write("error: error parsing latency-lab.yaml: mapping values are not allowed\n"); sys.exit(1)
    print("pod/chaos-lab-server created (server dry run)\npod/chaos-lab-client created (server dry run)")
elif a[:2] == ["apply", "-f"]:
    lab["pods"] = ["chaos-lab-server", "chaos-lab-client"]
    lab_save(); print("pod/chaos-lab-server created\npod/chaos-lab-client created")
elif a[0] == "wait":
    pod = a[2].split("/")[1]
    if pod not in lab["pods"]:
        sys.stderr.write(f'pods "{pod}" not found\n'); sys.exit(1)
    print(f"pod/{pod} condition met")
elif a[:2] == ["get", "pods"] and "app.kubernetes.io/part-of=chaos-lab" in a:
    print("\n".join(f"pod/{p}" for p in lab["pods"]))
elif a[:3] == ["get", "pod", "chaos-lab-server"]:
    print("10.244.0.99")
elif a[0] == "exec" and a[1] == "chaos-lab-server":
    cmd = " ".join(a[3:])
    if "qdisc add" in cmd:
        if os.getenv("FAKE_LAB_TC_FAIL"):
            sys.stderr.write("RTNETLINK answers: Operation not permitted\n"); sys.exit(2)
        lab["netem"] = int(cmd.split("delay ")[1].split("ms")[0]); lab_save()
    elif "qdisc del" in cmd:
        if os.getenv("FAKE_LAB_DEL_FAIL"):
            sys.stderr.write("simulated del failure\n"); sys.exit(2)
        lab["netem"] = None; lab_save()
    elif "qdisc show" in cmd:
        print(f"qdisc netem 8001: root refcnt 13 limit 1000 delay {lab['netem']}ms" if lab["netem"]
              else "qdisc noqueue 0: root refcnt 2")
elif a[0] == "exec" and a[1] == "chaos-lab-client":
    script = a[-1]
    n = int(script.split("seq ")[1].split(")")[0])
    d = (lab["netem"] or 0) / 1000
    for i in range(n):
        print(f"CURL rc=0 200 {0.0004 + d:.6f} {0.0012 + 2 * d:.6f}")
    print(f"{n} packets transmitted, {n} received, 0% packet loss, time 2000ms")
    print(f"rtt min/avg/max/mdev = {0.05 + d * 1000:.3f}/{0.08 + d * 1000:.3f}/{0.2 + d * 1000:.3f}/0.010 ms")
elif a[0] == "delete" and a[1] == "pod" and a[2] == "chaos-lab-server":
    lab["pods"] = []; lab["netem"] = None; lab_save(); print("pods deleted")
elif a[:2] == ["get", "deployments"]:
    print(json.dumps({"items": [dep(n) for n in st["deployments"]]}))
elif a[:2] == ["get", "deployment"]:
    if a[2] not in st["deployments"]:
        sys.stderr.write(f'deployments.apps "{a[2]}" not found\n'); sys.exit(1)
    print(json.dumps(dep(a[2])))
elif a[0] == "scale":
    n, r = a[1].split("/")[1], int(a[2].split("=")[1])
    if r == 0 and os.getenv("FAKE_FAIL_INJECT"):
        sys.stderr.write("simulated injection failure\n"); sys.exit(1)
    if r > 0 and os.getenv("FAKE_FAIL_RESTORE"):
        sys.stderr.write("simulated restore failure\n"); sys.exit(1)
    d = st["deployments"][n]
    d.update(replicas=r, touched=True, ready_at=now + float(os.getenv("FAKE_READY_DELAY", "1")) if r else now)
    save(); print(f"deployment.apps/{n} scaled")
elif a[0] == "rollout":
    n = a[2].split("/")[1]
    end = now + 8
    while time.time() < end:
        st = json.load(open(S, encoding="utf-8"))
        now = time.time()
        if ready(n) >= st["deployments"][n]["replicas"]:
            print(f'deployment "{n}" successfully rolled out'); sys.exit(0)
        time.sleep(0.2)
    sys.stderr.write("timed out waiting for rollout\n"); sys.exit(1)
elif a[:2] == ["get", "pods"]:
    n = a[3].split("=")[1]
    print(json.dumps({"items": [{"metadata": {"name": f"{n}-abc"}, "status": {"phase": "Running"}}]}))
elif a[0] == "delete" and a[1] == "pod":
    n = a[3].split("=")[1] if a[2] == "-l" else a[2].rsplit("-", 1)[0]
    st["deployments"][n].update(touched=True, ready_at=now + float(os.getenv("FAKE_READY_DELAY", "1")))
    st.get("netem", {}).pop(a[2], None)
    save(); print(f'pod "{n}-abc" deleted')
elif a[0] == "debug":
    pod = a[1].split("/")[1]
    script = a[a.index("-c") + 1]
    ms = int(script.split("delay ")[1].split("ms")[0])
    st.setdefault("netem", {})[pod] = ms
    save(); print("Defaulting debug container name; NETEM_ADDED")
elif a[0] == "exec":
    pod = a[1]
    st.setdefault("netem", {}).pop(pod, None)
    save(); print("qdisc noqueue 0: root refcnt 2")
elif a[:2] == ["get", "events"]:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(json.dumps({"items": [{"involvedObject": {"kind": "Deployment", "name": n}, "lastTimestamp": ts,
                                 "type": "Normal", "reason": "ScalingReplicaSet", "message": "fake", "count": 1}
                                for n, d in st["deployments"].items() if d.get("touched")]}))
else:
    sys.stderr.write(f"fake kubectl: unsupported {a}\n"); sys.exit(2)
