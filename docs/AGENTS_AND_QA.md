# The Agents in Plain English, plus Likely Judge Questions

## Who does what

| Part | Kind | Input | Output | Can it change the cluster? |
|---|---|---|---|---|
| **Planner** (`planner.py`) | AI: Ollama `qwen2.5:3b`, local | Dependency map, cluster health, baseline, previous runs, allowed fault catalog | Ranked JSON proposals: ID, target, fault, parameters, abort conditions, hypothesis, reason, recovery plan | **No** |
| **Validator** (`planner.validate`) | Plain code | One proposal | Accepted (with ID) or rejected (with reasons) | No |
| **LangGraph workflow** (`workflow.py`) | Orchestrator | Workflow state | Calls each step in order; pauses for human approval; routes failures to `restore` or `save` | Only through the executor |
| **Executor** (`executor.py`) | Plain code | A validated proposal | A fixed `controller.py` command; watchdog | Through the controller |
| **Controller** (`controller.py`) | Plain code | Target, fault, limits | Injects, monitors, aborts, restores, records evidence | **Yes**, only `kind-boutique-chaos/boutique`, allowlisted targets |
| **Analyst** (`analyze.py`) | Code + AI: local Ollama, else templates | All run records | Ranked findings, severity, score (code); cause hypothesis, fix, verification experiment (AI) | No |
| **Latency lab** (`netem_lab.py`) | Plain code | Delay, duration, samples | Before/during/after curl + ping, removal check, PASS/FAIL | Only its own two disposable pods |

## Likely judge questions and truthful answers

**Where exactly is the AI?** In two places: the planner ranks experiments, and the analyst writes explanations and fixes. Everything that touches the cluster, and every number in the report, is plain code.

**Do you trust the AI's reasons?** No, they're checked. In our first real run, `qwen2.5:3b` wrote reasons like "previous runs showed issues with Redis" when no Redis run existed. Code now attaches the real evidence to every proposal (runs on that target, runs with that fault, callers from the dependency map) and marks reasons that cite non-existent history as `UNSUPPORTED`, ranking them last. Validation checks *safety*; grounding checks *truthfulness*.

**What stops the AI from doing something dangerous?** It can't run anything. It returns JSON. The validator only accepts allowlisted targets and fault types with bounded parameters, and the AI may only tighten abort thresholds. A human approves every injection. The controller then enforces its own limits again.

**Which AI models run in the system?** Only local models through the Ollama API: `qwen2.5:3b` for planning, plus `llama3.2:3b` (if pulled) or `qwen2.5:3b` for the findings narrative. No cloud API is called at runtime. The report header names the model that wrote each text. Claude was used only as our coding assistant.

**Did you build automatic service discovery?** Partly. The dependency map is generated from the live cluster's Deployment environment variables, which is configuration, not traffic. Edges are marked *verified* only when an experiment showed the effect.

**How do you know the fault caused the errors?** Every run records the injection timestamp, and the probe log shows the first error 0.4–0.7 s later. A 15 s healthy baseline immediately before injection shows zero errors.

**What is your biggest finding?** In 5 product-catalog outage runs, Kubernetes reported the pod Ready a median of ~3 s after restore, but users kept getting HTTP 500s until ~19 s. The cart service showed the same ~16 s gap in 1 run. Our hypothesis, not yet proven, is the frontend's gRPC reconnect backoff.

**Is the resilience score an industry standard?** No. It's our documented formula (in the report) combining error rate, affected routes, outage length, restart time and extra latency. Its value is comparing runs in the same setup.

**Why didn't payment-service outages cause errors?** Our probe doesn't call checkout, so that's *not observed*, not *resilient*. The report says this explicitly.

**How is network latency injected?** With `tc qdisc ... netem delay` on a **disposable** lab pod (`chaos-lab-server`, the only allowed target). A lab client measures curl and ping before, during and after. Removal is verified with `tc qdisc show`, a timer on the server removes the delay anyway, and both lab pods are deleted afterwards. We deliberately did **not** enable latency on application pods yet.

**What happens if your controller crashes mid-fault?** The state file records what was changed. The executor's watchdog, or a human running `restore.py`, undoes it. This is tested.

**What is tested?** 68 automated tests with a fake `kubectl` and a fake shop. They cover validation, malformed AI output, kill switch, watchdog timeout, threshold abort, telemetry loss, injection failure, cleanup failure plus recovery, restoring the original replica count, recovery verification, Ollama unavailable or malformed output, no cloud API ever called, the AI number guard, the latency lab (pass, failure, cleanup failure, kill switch), the LangGraph paths (dry run, rejection, human decline, full run), and the Msty export/import (no AI text exported, invented numbers flagged, original reply untouched).

**Is Msty integrated?** Only as a manual review interface. `cli.py msty-export` produces a packet of measured facts and a prompt; a person runs it in Msty Studio with a local model; `cli.py msty-import` saves the reply with provenance and a number check. We did not find or test a programmatic Msty API, so Msty is not an automatic third agent, and nothing in the core workflow depends on it.

**Limitations?** Single-node local cluster, 1 replica per service, a laptop. Several findings come from only 1–2 runs. Causes are hypotheses until verification experiments are run.
