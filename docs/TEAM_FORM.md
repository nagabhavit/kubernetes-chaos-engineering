# Team Submission Form

| Field | Value |
|---|---|
| Team name | **TODO** |
| Members (name, role, contact) | **TODO** |
| Repository | https://github.com/nagabhavit/kubernetes-chaos-engineering |
| Demo video link | **TODO** |

## Tools
Docker Desktop · kind v0.33.0 · kubectl v1.36.1 · Kubernetes (kind node) · Python 3.13 · PowerShell · Git/GitHub · Ollama 0.40.2 (optionally Msty as the model server) · `nicolaka/netshoot:v0.13` (latency lab pods: `tc netem`, curl, ping, socat)

## AI models
| Model | Where it runs | Used for |
|---|---|---|
| Qwen2.5-3B (`qwen2.5:3b`, Apache-2.0) | Locally via Ollama | Experiment planning (schema-constrained JSON) and analysis fallback |
| Llama 3.2 3B (`llama3.2:3b`) | Locally via Ollama, optional | Findings narrative, if pulled (else Qwen2.5-3B) |
| Claude (AI coding assistant) | claude.ai | Development assistance only (code, tests, docs); **not part of the running system** |

## Third-party libraries
LangGraph 1.2.14 (+ langchain-core) · pydantic · Locust 2.43 · Streamlit · pandas · requests · Matplotlib · pytest. Everything else is the Python standard library.

## Data used
Only data generated in our own sandbox: Locust CSVs, HTTP probe logs, Kubernetes events and controller run records in `results/`. Application: Google Online Boutique v0.10.7 (Apache-2.0). No external or personal data.
