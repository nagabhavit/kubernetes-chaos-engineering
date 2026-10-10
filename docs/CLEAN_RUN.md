# Clean-Run Checklist (Windows 11, PowerShell)

Tick each box in order. Every command runs from `C:\Projects\microservices-demo` with the venv active.

## 0. Prerequisites (one time)
- [ ] Docker Desktop running; `kind`, `kubectl` on PATH
- [ ] Python 3.13 venv: `py -3.13 -m venv .venv313` then `.\.venv313\Scripts\Activate.ps1`
- [ ] `pip install -r requirements.txt`
- [ ] Ollama installed (`winget install Ollama.Ollama`), then `ollama pull qwen2.5:3b`
- [ ] Optional second local analysis model: `ollama pull llama3.2:3b` (~2 GB). Without it, `qwen2.5:3b` writes the narrative
- [ ] Optional: Msty as the model server. Set `$env:OLLAMA_URL` to Msty's Local AI Service endpoint **only if** it speaks the Ollama API; confirm with `python chaos\cli.py health`

## 1. Cluster and app
```powershell
kind create cluster --name boutique-chaos            # skip if it already exists
kubectl create namespace boutique                    # skip if it exists
kubectl --context kind-boutique-chaos apply -n boutique -f release/kubernetes-manifests.yaml
kubectl --context kind-boutique-chaos scale deployment/loadgenerator -n boutique --replicas=0
kubectl --context kind-boutique-chaos wait --for=condition=available deployment --all -n boutique --timeout=600s
```
- [ ] **Separate window, keep open:** `kubectl --context kind-boutique-chaos port-forward -n boutique service/frontend 8080:80`

## 2. Verify before any experiment
- [ ] `python -m pytest tests -q` → 62 passed, 1 skipped (Windows: no socat) (uses fakes; touches nothing)
- [ ] `python chaos\cli.py health` → `READY`
- [ ] `python chaos\cli.py map` → dependency map rebuilt from the live cluster

## 3. Plan (AI, no execution)
- [ ] `python chaos\cli.py plan` → label says `AI (Ollama)` and lists ranked proposals plus any rejections

## 4. Dry run (nothing injected)
- [ ] `python chaos\cli.py dry-run --rank 1` → `OUTCOME: DRY_RUN`; prints the exact controller command

## 5. Three fault types, one at a time (each asks for approval; type `yes`)
- [ ] `python chaos\cli.py run --target productcatalogservice --fault scale_to_zero --duration 20`
- [ ] `python chaos\cli.py health` → `READY`
- [ ] `python chaos\cli.py run --target frontend --fault traffic_spike --spike-users 80 --duration 20`
- [ ] `python chaos\cli.py health` → `READY`
- [ ] Optional: `python chaos\cli.py run --rank 1` (run the AI's top proposal)

## 5b. Network latency on disposable lab pods (no app pod touched)
- [ ] `python chaos\cli.py latency-lab --delay-ms 200 --duration 30 --samples 15` → `RESULT: PASS` with before/during/after numbers
- [ ] `kubectl --context kind-boutique-chaos get pods -n boutique` → no `chaos-lab-*` pods left

## 6. Report
- [ ] `python chaos\cli.py report` → `results\findings-report.md`, `results\findings.json`
- [ ] Open the report and check the narrative label (local Ollama model or templates)

## 7. Cleanup and save
- [ ] `python chaos\cli.py health` → `READY` (otherwise `python chaos\restore.py`)
- [ ] `git add chaos ui tests docs architecture results README.md requirements.txt` → commit → `git push portfolio main`

## Troubleshooting
| Symptom | Fix |
|---|---|
| `Frontend: UNREACHABLE` | Port-forward window closed; restart it |
| `NOT READY` with kill switch present | `Remove-Item chaos\KILL` |
| `ACTIVE FAULT RECORDED` | `python chaos\restore.py` |
| Planner says `RULE-BASED fallback` | Ollama not running (tray icon) or model missing: `ollama list` |
| Latency lab stuck waiting for pods | First use pulls `nicolaka/netshoot:v0.13` (~200 MB); `--ready-timeout` defaults to 600 s |
| Latency lab says lab pods already exist | `kubectl --context kind-boutique-chaos -n boutique delete pod chaos-lab-server chaos-lab-client` |
| Streamlit says backend offline | `python chaos\api.py` must be running; check `http://127.0.0.1:8000/ping` |
