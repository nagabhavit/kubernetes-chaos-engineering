#!/usr/bin/env python3
"""
Semi-automatic dependency discovery for Online Boutique on kind-boutique-chaos.

1. DISCOVERED (automatic): reads live Deployments and Services from the cluster.
   An edge A -> B is recorded when A's container env contains *_ADDR / REDIS_ADDR
   pointing at "<B>:<port>". This is configuration evidence, not proof of traffic.
2. VERIFIED (manual, from experiments): architecture/observations.json records
   what actually happened to users when a dependency was removed.

Outputs architecture/dependency-map.json and architecture/dependency-map.md.
Standard library only. Read-only against the cluster.
"""
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

CONTEXT, NAMESPACE = "kind-boutique-chaos", "boutique"
HERE = Path(__file__).resolve().parent


def kubectl_json(*args):
    r = subprocess.run(["kubectl", "--context", CONTEXT, "-n", NAMESPACE, *args, "-o", "json"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise SystemExit(f"kubectl failed: {r.stderr.strip()}")
    return json.loads(r.stdout)


def main():
    deps = kubectl_json("get", "deployments")["items"]
    svcs = kubectl_json("get", "services")["items"]
    svc_names = {s["metadata"]["name"] for s in svcs}

    services, edges = {}, []
    for d in deps:
        name = d["metadata"]["name"]
        c = d["spec"]["template"]["spec"]["containers"][0]
        services[name] = {
            "replicas": d["spec"].get("replicas", 1),
            "image": c["image"].split("/")[-1],
            "ports": [p["containerPort"] for p in c.get("ports", [])],
            "has_service": name in svc_names,
            "role": "load generator (not part of app)" if name == "loadgenerator" else
                    "datastore" if name.startswith("redis") else
                    "entry point" if name == "frontend" else "backend",
        }
        for e in c.get("env", []):
            v = e.get("value") or ""
            if e["name"].endswith("_ADDR") and ":" in v:
                target = v.split(":")[0]
                edges.append({"from": name, "to": target, "port": v.split(":")[1], "env": e["name"],
                              "evidence": "configured (env var in live Deployment)",
                              "target_deployed": target in services or any(x["metadata"]["name"] == target for x in deps)})

    obs_path = HERE / "observations.json"
    observations = json.loads(obs_path.read_text(encoding="utf-8")) if obs_path.exists() else []
    for edge in edges:
        for o in observations:
            if o["edge"] == f"{edge['from']}->{edge['to']}":
                edge.update(verified=True, criticality=o["criticality"], verified_by=o["runs"], observed=o["observed"])
        edge.setdefault("verified", False)
        edge.setdefault("criticality", "unknown (not yet tested)")

    out = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "context": CONTEXT,
           "namespace": NAMESPACE, "services": services, "edges": edges,
           "external_entry": {"from": "user / Locust", "to": "frontend", "via": "kubectl port-forward localhost:8080 -> frontend:80"}}
    (HERE / "dependency-map.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    (HERE / "dependency-map.md").write_text(render_md(out), encoding="utf-8")
    print(f"{len(services)} deployments, {len(edges)} edges, "
          f"{sum(e['verified'] for e in edges)} verified -> architecture/dependency-map.json / .md")


def mid(n):
    return n.replace("-", "_")


def render_md(m):
    L = ["# Online Boutique — Dependency Map", "",
         f"Generated {m['generated']} from the live `{m['context']}` cluster, namespace `{m['namespace']}`, "
         "by `architecture/build_dependency_map.py`. Verified edges come from `architecture/observations.json`.", "",
         "```mermaid", "flowchart LR", "  user([User / Locust]) -->|HTTP :8080 port-forward| frontend"]
    for e in m["edges"]:
        if m["services"].get(e["from"], {}).get("role", "").startswith("load"):
            continue
        tgt = mid(e["to"]) if e["target_deployed"] else f"{mid(e['to'])}[/{e['to']}<br>NOT DEPLOYED/]"
        if e["verified"] and e["criticality"].startswith("critical"):
            arrow, label = "==>", "VERIFIED critical"
        elif e["verified"]:
            arrow, label = "-->", "VERIFIED non-critical"
        else:
            arrow, label = "-.->", "configured"
        L.append(f"  {mid(e['from'])} {arrow}|{label}| {tgt}")
    L += ["  redis_cart[(redis-cart)]", "```", "",
          "Legend: **thick arrow** = removal caused user-facing errors in an experiment; "
          "**solid** = removed in an experiment with no user-facing errors; "
          "**dotted** = configured in the Deployment env but not yet tested.", "",
          "## Services", "", "| Deployment | Role | Replicas | Ports | Image |", "|---|---|---|---|---|"]
    for n, s in sorted(m["services"].items()):
        L.append(f"| {n} | {s['role']} | {s['replicas']} | {', '.join(map(str, s['ports'])) or '—'} | `{s['image']}` |")
    L += ["", "## Edges", "", "| From | To | Env var | Evidence | Criticality |", "|---|---|---|---|---|"]
    for e in m["edges"]:
        ev = "configured" + (f"; verified by {', '.join(e['verified_by'])}" if e["verified"] else "")
        if not e["target_deployed"]:
            ev += "; **target not deployed**"
        L.append(f"| {e['from']} | {e['to']} | `{e['env']}` | {ev} | {e['criticality']} |")
    verified = [e for e in m["edges"] if e["verified"]]
    if verified:
        L += ["", "## Experimental evidence", ""]
        for e in verified:
            L.append(f"- **{e['from']} → {e['to']}** ({e['criticality']}): {e['observed']}")
    L += ["", "## Limits", "",
          "- Configured edges show the code *can* call a service; they don't prove which requests use it.",
          "- Only edges whose target was actually removed in an experiment are marked verified.",
          "- Data flow summary (from upstream architecture docs, not verified here): browsing uses productcatalog, "
          "currency, ads and recommendations; cart uses cartservice → redis-cart; checkout orchestrates "
          "cart, productcatalog, currency, shipping, payment and email.", ""]
    return "\n".join(L)


if __name__ == "__main__":
    main()
